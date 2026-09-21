"""Behavioral boundaries for revisable task limits and selective review."""

import json
import time

import pytest

from agent.revisable.dialogue import compaction_history, Summary
from agent.revisable.roles import prompt
from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.revisable import Plan, Stage, TaskLimits
from shared.schemas import AgentState, TaskStatus


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="test",
                        agent_architecture="plan_executor")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    orch = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                               artifacts=artifacts, driver=driver, max_steps=20,
                               max_role_invocations=30)
    yield orch, db, driver
    db.close()


def reply(name, **args):
    return GatewayResponse(content="", model="test", stop_reason="tool_calls",
                           tool_calls=[ToolCall(id="call", name=name, arguments=json.dumps(args))])


def terminal(tools):
    return next(t["function"]["name"] for t in tools
                if t["function"]["name"].startswith("submit_"))


def anchor(messages):
    return next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                if isinstance(m.get("content"), str)
                and m["content"].startswith('{"runtime_update"'))


@pytest.mark.parametrize("role", ["planner", "reviewer"])
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
async def test_late_completion_is_not_success(runtime, monkeypatch, role, stop):
    orch, db, driver = runtime
    state = AgentState(instruction="Inspect the current screen")
    state.revisable.next_role = role
    if stop == "deadline":
        state.revisable.limits.deadline_at = time.time() + 100
    task = db.create_task(state.instruction, state)
    now = time.time()

    async def complete(model, messages, *, tools, **kwargs):
        if stop == "cancel":
            orch.request_cancel(task.id)
        else:
            monkeypatch.setattr("shared.revisable.time.time", lambda: now + 200)
        return reply(terminal(tools), decision="complete", reason="Visible result")

    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.CANCELLED
    assert db.get_task(task.id).status == TaskStatus.CANCELLED
    assert driver.actions == []
    if stop == "deadline":
        assert db.get_task(task.id).failure_reason == "task_deadline_exhausted"


@pytest.mark.parametrize("requester", ["planner", "executor"])
async def test_independent_review_is_available_in_two_role_mode(runtime, monkeypatch, requester):
    orch, db, driver = runtime
    state = AgentState(instruction="Check the result")
    state.revisable.next_role = requester
    state.revisable.plan = Plan(current_stage=Stage(goal="Inspect result"))
    task = db.create_task(state.instruction, state)
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        name = terminal(tools)
        calls.append(name)
        if name == "submit_reviewer_decision":
            return reply(name, decision="complete", reason="Conflict settled by current evidence")
        if requester == "planner":
            return reply(name, decision="review", reason="Resolve target identity", source_refs=[])
        return reply(name, decision="review", summary="Resolve target identity",
                     observation_id=anchor(messages)["current_observation_id"])

    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.SUCCEEDED
    assert calls == [f"submit_{requester}_decision" if requester == "planner" else "submit_executor_step",
                     "submit_reviewer_decision"]
    assert driver.actions == []


async def test_action_budget_blocks_extra_action_but_allows_completion(runtime, monkeypatch):
    orch, db, driver = runtime
    state = AgentState(instruction="Go home once")
    state.revisable.next_role = "executor"
    state.revisable.plan = Plan(current_stage=Stage(goal="Go home"))
    state.revisable.limits = TaskLimits(device_actions=1)
    task = db.create_task(state.instruction, state)
    attempted = 0
    lifecycle = []

    async def begin(task_id):
        lifecycle.append("begin")
        return {"status": "active"}

    async def end(task_id):
        lifecycle.append("end")
        return {"status": "released"}

    monkeypatch.setattr(driver, "begin_task_session", begin, raising=False)
    monkeypatch.setattr(driver, "end_task_session", end, raising=False)

    async def complete(model, messages, *, tools, **kwargs):
        nonlocal attempted
        name = terminal(tools)
        if name == "submit_planner_decision":
            assert '"device_actions": 0' in json.dumps(messages).replace('\\"', '"')
            return reply(name, decision="complete", reason="Home reached once")
        attempted += 1
        a = anchor(messages)
        if attempted <= 2:
            return reply(name, decision="act", summary="Go home",
                         observation_id=a["current_observation_id"], action={"type": "home"})
        assert "device_action_limit_exhausted" in json.dumps(messages)
        return reply(name, decision="finish", summary="Home reached once",
                     observation_id=a["current_observation_id"])

    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.SUCCEEDED
    assert len(driver.actions) == 1
    saved = db.get_task(task.id).state
    assert saved.revisable.execution_count == 1
    assert saved.step_number == 2  # action plus handoff, independent units
    assert lifecycle == ["begin", "end"]


def test_unconfigured_deadline_stays_unlimited_and_counters_are_distinct(runtime):
    orch, _, _ = runtime
    state = AgentState(instruction="Task")
    state.step_number = 5
    state.role_invocation_count = 9
    state.revisable.execution_count = 2
    assert orch._remaining_budget(state) == {
        "device_actions": None, "executor_decisions": 15, "model_calls": 21, "seconds": None}
    assert not state.revisable.limits.expired()


def test_plan_correction_survives_compaction_filter():
    corrected = "The original instruction says daily; the earlier weekly claim is wrong."
    messages = [{"role": "user", "content": json.dumps({"runtime_update": {
        "plan_reason": corrected, "remaining_budget": {"seconds": 90}}})}]
    result = list(compaction_history(messages))
    assert corrected in result[0]["content"]
    assert "remaining_budget" not in result[0]["content"]
    with pytest.raises(ValueError):
        Summary(text="x" * 3001)


def test_shared_rules_are_injected_once_without_benchmark_specifics():
    for role in ("planner", "executor", "reviewer"):
        text = prompt(role)
        assert text.count("## Operation, evidence and budget") == 1
        assert "Reimbursable" not in text
        assert "Broccoli" not in text


@pytest.mark.parametrize("resume", [False, True])
async def test_display_name_loads_owned_core_and_workflow(runtime, monkeypatch, tmp_path, resume):
    orch, db, driver = runtime
    root = tmp_path / "skills"
    core = root / "apps/com.example.demo/core/SKILL.md"
    core.parent.mkdir(parents=True)
    core.write_text("---\nname: demo-core\napp: com.example.demo\nkind: app_core\n"
                    "description: Demo guidance\n---\nCORE_GUIDANCE_FOR_DEMO", encoding="utf-8")
    workflow = root / "apps/com.example.demo/workflows/read/SKILL.md"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("---\nname: demo-read\napp: com.example.demo\nkind: workflow\nversion: 1.0.0\ncapability: inspect\n"
                        "description: Read demo\n---\n## Procedure\n1. OWNED_WORKFLOW_GUIDANCE\n"
                        "## Verification\n- Inspect the resulting screen.", encoding="utf-8")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(root))
    state = AgentState(instruction="Inspect Demo App")
    if resume:
        # Historical states remain parseable even though target_app was a display name.
        state.revisable.plan = Plan(current_stage=Stage(goal="Inspect Demo App", target_app="Demo App"))
        state.revisable.next_role = "executor"
    task = db.create_task(state.instruction, state)
    executor_seen = False

    async def complete(model, messages, *, tools, **kwargs):
        nonlocal executor_seen
        name = terminal(tools)
        if name == "submit_planner_decision":
            if executor_seen:
                return reply(name, decision="complete", reason="Requested screen inspected")
            return reply(name, decision="execute", reason="Inspect the requested app",
                         plan={"current_stage": {"goal": "Inspect Demo App", "target_app": "Demo App",
                                                 "skill_ids": ["demo-read"]}})
        executor_seen = True
        text = json.dumps(messages)
        assert "CORE_GUIDANCE_FOR_DEMO" in text
        if not resume:
            assert "OWNED_WORKFLOW_GUIDANCE" in text
        assert anchor(messages)["current_stage"]["target_app"] == "com.example.demo"
        return reply(name, decision="finish", summary="Screen inspected",
                     observation_id=anchor(messages)["current_observation_id"])

    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.SUCCEEDED
    assert executor_seen and driver.actions == []


async def test_unresolved_name_does_not_select_an_unrelated_app_skill():
    from agent.revisable.scope import skill_app

    with pytest.raises(ValueError, match="leave it empty"):
        await skill_app("Unknown App", FixtureDriver())
    assert await skill_app("Unknown App", FixtureDriver(), strict=False) == ""
    with pytest.raises(ValueError, match="installed app"):
        await skill_app("com.some.app", FixtureDriver())
    assert await skill_app("com.some.app", None, strict=False) == "com.some.app"
    assert await skill_app("Demo App", FixtureDriver()) == "com.example.demo"
    assert await skill_app("com.example.demo", FixtureDriver()) == "com.example.demo"
