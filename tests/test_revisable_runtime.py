"""Experimental architecture invariants, without a provider or device."""

import json

import pytest

from agent.revisable.dialogue import restore_dialogue
from agent.revisable.session import ExecutionSession
from agent.revisable.store import TaskStore
from agent.revisable.tools import register_memory_tools
from agent.tool_registry import AgentRole, AgentToolRegistry, ToolExecutionContext, ToolStatus
from control_api.services import ObservabilityQueries
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.revisable import Plan, PlannerDecision, Stage
from shared.schemas import AgentState


def test_console_exposes_latest_revisable_plan_without_reconstructing_old_progress(setup):
    store, state = setup
    state.revisable.next_role = "reviewer"
    state.revisable.feedback = "Check the observed result"
    task = store.db.create_task(state.instruction, state)
    timeline = ObservabilityQueries(store.db, store.artifacts).timeline(task.id)
    runtime = timeline["revisable"]
    assert runtime["plan"]["current_stage"]["goal"] == "Collect"
    assert runtime["revision"] == 1
    assert runtime["next_role"] == "reviewer"
    assert runtime["feedback"] == "Check the observed result"
    assert "progress" not in timeline


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "test.db")
    state = AgentState(instruction="Remember values and calculate their product")
    state.revisable.plan = Plan(current_stage=Stage(goal="Collect"))
    state.revisable.revision = 1
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "task-a")
    for number in range(4):
        store.put(
            "observation",
            f"obs_{number}",
            {
                "step": number,
                "stage_id": state.revisable.stage_id,
                "stage_ids": [state.revisable.stage_id],
                "visits": [{"step": number, "stage_id": state.revisable.stage_id}],
                "app": "example",
                "text": f"value={number + 3}",
                "image_ref": None,
            },
        )
    yield store, state
    db.close()


def response(*calls):
    return GatewayResponse(
        content="",
        model="test",
        stop_reason="tool_calls",
        tool_calls=[
            ToolCall(id=f"call_{index}", name=name, arguments=json.dumps(args))
            for index, (name, args) in enumerate(calls)
        ],
    )


def context():
    return ToolExecutionContext(AgentRole.EXECUTOR, "invocation", {})


async def test_stage_completion_is_bound_and_idempotent(setup):
    store, state = setup
    state.revisable.plan.assumption_roadmap = ["Enter the result"]
    session = ExecutionSession("executor", "test")
    session.store, session.state = store, state
    registry = session._build_registry({})
    ctx = context()
    ctx.state["active_observation_id"] = "obs_3"
    first_id = state.revisable.stage_id
    args = {
        "decision": "advance",
        "completed_stage_id": first_id,
        "summary": "Collection complete",
        "observation_id": "obs_3",
    }
    accepted = await registry.execute("submit_executor_step", args, ctx)
    assert accepted.terminal_value is not None
    assert state.revisable.complete_stage(ctx.state["completed_stage_id"])
    assert not state.revisable.complete_stage(first_id)
    repeated = await registry.execute("submit_executor_step", args, ctx)
    assert repeated.terminal_value is None
    assert repeated.data["already_completed"] == first_id
    assert state.revisable.stage is None
    # A future proposal is not promoted by completion. Only Planner chooses a new goal.
    state.revisable.plan = Plan(current_stage=Stage(goal="Inspect the form"))
    state.revisable.revision += 1
    stale = await registry.execute(
        "submit_executor_step", {**args, "completed_stage_id": "plan_0_stage_1"}, ctx
    )
    assert stale.error == "stale_stage"
    assert state.revisable.stage.goal == "Inspect the form"


def test_action_catalog_rejects_parameters_dispatch_would_reject():
    from jsonschema import Draft202012Validator

    from agent.revisable.session import executor_submission_schema

    validator = Draft202012Validator(executor_submission_schema())
    base = {"decision": "act", "observation_id": "obs_1", "summary": "Enter value"}
    for kind in ("type", "replace_text"):
        validator.validate({**base, "action": {"type": kind, "text": "42"}})
        assert bool(list(
            validator.iter_errors({**base, "action": {"type": kind, "text": "42", "index": 0}})
        )) == (kind == "type")
    validator.validate({**base, "action": {"type": "tap", "index": 0}})
    assert list(validator.iter_errors({**base, "action": {"type": "tap"}}))


def test_runtime_updates_preserve_history_and_restore_after_compaction(setup):
    store, state = setup
    state.current_device_date = "2023-10-15"
    state.temporal_conventions = ["Test-only date convention"]
    first = store.execution_context(state, "obs_1")
    assert first["runtime_update"]["current_stage"]["goal"] == "Collect"
    assert first["runtime_update"]["current_device_date"] == "2023-10-15"
    assert first["runtime_update"]["temporal_conventions"] == [
        "Test-only date convention"
    ]
    assert "original_instruction" not in first["runtime_update"]
    store.save_dialogue([{"role": "user", "content": json.dumps(first)}], state)
    before = store.read_dialogue(state.revisable.dialogue_refs)
    second = store.execution_context(state, "obs_2")
    assert set(second["runtime_update"]) == {"stage_id", "current_observation_id", "current_step"}
    store.save_dialogue([{"role": "user", "content": json.dumps(second)}], state)
    assert store.read_dialogue(state.revisable.dialogue_refs)[: len(before)] == before
    state.revisable.delivered_context = {}
    assert "current_stage" in store.execution_context(state, "obs_3")["runtime_update"]


async def test_repeat_read_suppresses_whole_result_without_forcing_a_decision(setup):
    store, state = setup
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    ctx = context()
    await registry.execute("write_note", {"note_key": "value", "content": "5"}, ctx)
    first = await registry.execute("read_history", {"source": "value"}, ctx)
    assert first.data["items"][0]["text"] == "value\n5"
    await registry.execute("write_note", {"note_key": "value", "content": "9"}, ctx)
    newer = await registry.execute("read_history", {"source": "value"}, ctx)
    assert newer.data["items"][0]["text"] == "value\n9"
    duplicate = await registry.execute("read_history", {"source": "value"}, ctx)
    assert duplicate.data["items"][0]["already_supplied"]
    assert not ctx.state.get("evidence_saturated")
    old = await registry.execute("read_history", {"source": "note:value@1"}, context())
    assert old.data["items"][0]["text"].endswith("5")

async def test_protocol_stall_hands_off_once_without_device_dispatch(tmp_path, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayError
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None, data_dir=tmp_path, agent_architecture="plan_executor", default_model="test"
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    task = db.create_task("Go home", AgentState(instruction="Go home"), device_serial="fixture")
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        if any(t["function"]["name"] == "submit_planner_decision" for t in tools):
            if calls:
                assert "Executor exhausted its decision protocol" in json.dumps(messages)
            calls.append("planner")
            return response(
                (
                    "submit_planner_decision",
                    {
                        "decision": "execute",
                        "reason": "Inspect home",
                        "plan": {"current_stage": {"goal": "Go home"}},
                    },
                )
            )
        calls.append("executor")
        raise GatewayError("No valid submission", category="budget")

    monkeypatch.setattr("agent.session.complete", complete)
    assert await runtime.run_task(task.id) == TaskStatus.FAILED
    assert calls == ["planner", "executor", "planner", "executor"]
    assert driver.actions == []
    db.close()


async def test_versioned_notes_are_task_scoped_and_not_truncated(setup):
    store, state = setup
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    text = "数字12345 " * 500
    args = {
        "note_key": "values",
        "title": "Observed values",
        "content": text,
        "observation_ids": ["obs_1"],
    }
    first = await registry.execute("write_note", args, context())
    assert first.data["version"] == 1
    args["content"] = "new values"
    await registry.execute("write_note", args, context())
    read = await registry.execute("read_history", {"source": "note:values@1", "full": True}, context())
    assert read.model_metadata()["data"]["items"][0]["text"] == "Observed values\n" + text
    other = TaskStore(store.db, store.artifacts, "task-b")
    with pytest.raises(ValueError):
        other.get("note", "values")
    invalid = await registry.execute(
        "write_note", {**args, "observation_ids": ["foreign"]}, context()
    )
    assert invalid.status == ToolStatus.FAILED
    assert store.get("note", "values")["version"] == 2


async def test_history_returns_answer_directly_without_changing_action_basis(setup):
    store, state = setup
    registry = AgentToolRegistry()
    register_memory_tools(registry, "reviewer", store, state)
    ctx = ToolExecutionContext(AgentRole.REVIEWER, "read", {"active_observation_id": "current"})
    page = await registry.execute("read_history", {"query": "value=4"}, ctx)
    assert page.data["items"] == [{"source": "observation:obs_1@1", "observation_id": "obs_1", "text": "value=4"}]
    read = await registry.execute("read_history", {"source": page.data["items"][0]["source"]}, ctx)
    assert read.data["items"][0]["already_supplied"]
    assert not read.actionable_observation_id
    assert ctx.state["active_observation_id"] == "current"
    denied = await registry.execute("write_note", {"note_key": "x"}, ctx)
    assert denied.status == ToolStatus.INVALID_ARGUMENTS

async def test_history_prefix_is_task_scoped_unambiguous_and_non_actionable(setup):
    store, state = setup
    payload = store.get("observation", "obs_1")["payload"]
    store.put("observation", "obs_12345678aaaa", payload)
    other = TaskStore(store.db, store.artifacts, "other-task")
    other.put("observation", "obs_12345678bbbb", payload)
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    ctx = context()
    ctx.state["active_observation_id"] = "obs_3"
    result = await registry.execute("read_history", {"source": "obs_12345678"}, ctx)
    assert result.data["items"][0]["observation_id"] == "obs_12345678aaaa"
    assert ctx.state["active_observation_id"] == "obs_3"
    assert not result.actionable_observation_id
    store.put("observation", "obs_12345678cccc", payload)
    ambiguous = await registry.execute("read_history", {"source": "obs_12345678"}, ctx)
    assert ambiguous.status == ToolStatus.FAILED
    assert not ambiguous.data


@pytest.mark.parametrize("role", ["planner", "reviewer", "executor"])
def test_read_tools_follow_data_availability_without_blocking_executor_writes(setup, role):
    original, state = setup
    store = TaskStore(original.db, original.artifacts, "fresh-task")
    store.put("observation", "current", original.get("observation", "obs_0")["payload"])

    def tools():
        registry = AgentToolRegistry()
        register_memory_tools(registry, role, store, state)
        return {spec.name for spec in registry.specs_for_role(role)}

    reads = {"read_history"}
    assert tools() == (
        reads | {"write_note"} if role == "executor" else set()
    )
    store.put("note", "value", {"title": "Value", "content": "5", "observation_ids": ["current"]})
    store.put("observation", "later", original.get("observation", "obs_1")["payload"])
    assert reads <= tools()


async def test_planner_can_read_advertised_workflow_without_activating_it(tmp_path):
    from agent.revisable.session import terminal_registry
    from agent.session import AgentSession
    from agent.skills.library import SkillLibrary
    from agent.tool_registry import AgentToolResult

    body = (
        "## Procedure\n" + "Observe the actual state. " * 80 + "\n## Verification\nEND OF WORKFLOW"
    )
    for app in ["com.demo", "com.other"]:
        directory = tmp_path / "apps" / app / "workflows" / "inspect"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {app}-inspect\ndescription: Inspect\nversion: 0.1.0\n"
            f"app: {app}\nkind: workflow\ncapability: inspect\ntags: [inspect]\n---\n\n{body}\n"
        )
    session = AgentSession("planner", "test", library=SkillLibrary(tmp_path))
    session.freeze_allow_dirs(["generic"])
    session.set_workflow_catalog(["com.demo"])
    assert session.workflow_catalog_ids == {"com.demo-inspect"}

    async def submit(args, ctx):
        return AgentToolResult(terminal_value=Plan.model_validate(args))

    registry = terminal_registry(session, Plan, "submit_planner_decision", "Plan", submit)
    ctx = ToolExecutionContext(AgentRole.PLANNER, "plan", {})
    read = await registry.execute("load_skill", {"skill_id": "com.demo-inspect"}, ctx)
    assert read.status == ToolStatus.SUCCEEDED
    assert "END OF WORKFLOW" in read.model_metadata()["data"]["content"]
    assert not session.loaded_skill_ids
    denied = await registry.execute("load_skill", {"skill_id": "com.other-inspect"}, ctx)
    assert denied.status != ToolStatus.SUCCEEDED


@pytest.mark.parametrize("bad_source", [False, True])
async def test_note_before_submit_and_failure_blocks_terminal(setup, monkeypatch, bad_source):
    store, state = setup
    session = ExecutionSession(
        "executor", "test", settings=Settings(_env_file=None), max_total_rounds=2
    )
    session.store, session.state = store, state
    session.set_stable_system("Execute")
    rounds = []

    async def complete(model, messages, **kwargs):
        rounds.append(messages)
        if len(rounds) == 1:
            return response(
                (
                    "write_note",
                    {
                        "note_key": "values",
                        "title": "Values",
                        "content": "3,4",
                        "observation_ids": ["foreign" if bad_source else "obs_1"],
                    },
                ),
                (
                    "submit_executor_step",
                    {
                        "decision": "advance",
                        "summary": "Collected",
                        "observation_id": "current",
                        "completed_stage_id": state.revisable.stage_id,
                    },
                ),
            )
        assert "note_write_failed" in json.dumps(messages)
        return response(
            (
                "submit_executor_step",
                {
                    "decision": "replan",
                    "summary": "Need a valid source",
                    "observation_id": "current",
                },
            )
        )

    monkeypatch.setattr("agent.session.complete", complete)
    result = await session.run(
        [{"role": "user", "content": "Current values"}],
        context_state={"active_observation_id": "current", "allow_note_before_submit": True},
    )
    assert len(rounds) == (2 if bad_source else 1)
    assert result.context_state["directive"] == ("replan" if bad_source else "advance")
    assert len(result.dialogue_messages) >= 4
    if not bad_source:
        assert store.get("note", "values")["payload"]["content"] == "3,4"


async def test_stale_observation_cannot_submit_action(setup):
    store, state = setup
    session = ExecutionSession("executor", "test")
    session.store, session.state = store, state
    registry = session._build_registry({})
    ctx = context()
    ctx.state["active_observation_id"] = "current"
    result = await registry.execute(
        "submit_executor_step",
        {
            "decision": "act",
            "summary": "Tap old target",
            "observation_id": "obs_1",
            "action": {"type": "tap", "index": 1},
        },
        ctx,
    )
    assert result.error == "stale_observation"
    assert result.terminal_value is None


async def test_compression_keeps_notes_and_raw_history_and_restores_state(setup, monkeypatch):
    store, state = setup
    store.put(
        "note", "numbers", {"title": "Values", "content": "3,4,5", "observation_ids": ["obs_1"]}
    )
    for index in range(6):
        state.step_number = index
        store.save_dialogue([{"role": "user", "content": str(index) * 2000}], state)
    original_refs = list(state.revisable.dialogue_refs)

    async def complete(*args, **kwargs):
        return response(
            ("save_summary", {"results": [{"text": "Collected several values; consult numbers note.",
                                          "sources": ["R1"]}],
                              "decisions_and_attempts": [], "critical_context": []})
        )

    monkeypatch.setattr("agent.session.complete", complete)
    restored = await restore_dialogue(
        store,
        state,
        model="test",
        settings=Settings(_env_file=None, executor_context_tokens=1),
        meter=None,
        event_sink=None,
    )
    assert len(state.revisable.dialogue_refs) == 2
    assert restored[0]["content"].startswith("Earlier execution summary")
    assert store.get("note", "numbers")["payload"]["content"] == "3,4,5"
    assert len(store.read_dialogue(original_refs)) == 6
    reloaded = AgentState.model_validate_json(state.model_dump_json())
    latest = store.context(reloaded, "obs_3")
    assert latest["plan_revision"] == 1
    assert latest["current_observation_id"] == "obs_3"
    assert latest["notes"][0]["note_key"] == "numbers"


@pytest.mark.parametrize("architecture", ["plan_reviewer", "plan_executor"])
@pytest.mark.parametrize("boundary,zero_units", [("max_device_actions", False), ("max_action_attempts", False), ("max_action_attempts", True), ("prediction_rounds", True)])
@pytest.mark.parametrize("continuous", [False, True])
async def test_task_dialogue_survives_pause_resume_and_stage_change(
    tmp_path, monkeypatch, architecture, boundary, zero_units, continuous
):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        agent_architecture=architecture,
        default_model="test",
        manager_model="test",
        executor_model="test",
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    closes = []

    async def close_observation_provider():
        closes.append(True)

    driver.close_observation_provider = close_observation_provider
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    record = db.create_task("Go home", AgentState(instruction="Go home"), device_serial="fixture")
    original_act = driver.act

    async def act_after_note(action):
        assert db.get_agent_record(record.id, "note", "handoff") is not None
        result = await original_act(action)
        if zero_units:
            result.detail["device_action_units"] = 0
        return result

    driver.act = act_after_note
    calls = []
    exec_calls = 0

    async def complete(model, messages, *, tools, **kwargs):
        nonlocal exec_calls
        terminal = next(
            tool["function"]
            for tool in tools
            if tool["function"]["name"]
            in {"submit_planner_decision", "submit_reviewer_decision", "submit_executor_step"}
        )
        fields = terminal["parameters"]["properties"]
        if terminal["name"] == "submit_planner_decision":
            calls.append("planner")
            if calls.count("planner") == 3:
                assert '"handoff": "finish"' in json.dumps(messages).replace('\\"', '"')
                return response(
                    (
                        "submit_planner_decision",
                        {"decision": "complete", "reason": "Home is visible"},
                    )
                )
            if calls.count("planner") == 2:
                assert "advance" in json.dumps(messages)
                assert "Starting observation retained" in json.dumps(messages)
            return response(
                (
                    "submit_planner_decision",
                    {
                        "decision": "execute",
                        "reason": "Continue from actual results",
                        "plan": {
                            "current_stage": {
                                "goal": "Go home" if exec_calls == 0 else "Inspect home"
                            },
                            "assumption_roadmap": [
                                "This proposal must never execute automatically"
                            ],
                        },
                    },
                )
            )
        if terminal["name"] == "submit_reviewer_decision":
            calls.append("reviewer")
            return response(
                ("submit_reviewer_decision", {"decision": "complete", "reason": "Home is visible"})
            )
        calls.append("executor")
        exec_calls += 1
        anchor = next(
            json.loads(message["content"])["runtime_update"]
            for message in reversed(messages)
            if isinstance(message.get("content"), str)
            and message["content"].startswith('{"runtime_update"')
        )
        if exec_calls > 1:
            assert any(message.get("role") == "assistant" for message in messages)
            assert "action_result" in json.dumps(messages)
        directive = "act" if exec_calls == 1 else "advance" if exec_calls == 2 else "finish"
        args = {
            "decision": directive,
            "summary": directive,
            "observation_id": anchor["current_observation_id"],
        }
        if directive == "advance":
            args["completed_stage_id"] = anchor["stage_id"]
        if directive == "act":
            args["action"] = {"type": "home"}
            args["notes"] = [
                {
                    "note_key": "handoff",
                    "title": "Starting screen",
                    "content": "Starting observation retained",
                    "observation_ids": [anchor["current_observation_id"]],
                }
            ]
        return response(("submit_executor_step", args))

    monkeypatch.setattr("agent.session.complete", complete)
    async def run_with_budget(limit):
        if boundary == "prediction_rounds":
            task = db.get_task(record.id)
            task.state.revisable.limits.prediction_rounds = limit
            db.update_task(record.id, state=task.state)
            return await runtime.run_task(record.id)
        return await runtime.run_task(record.id, **{boundary: limit})

    if continuous:
        assert await run_with_budget(2) == TaskStatus.SUCCEEDED
        assert len(closes) == 1
    else:
        assert await run_with_budget(1) == TaskStatus.RUNNING
        if boundary == "prediction_rounds":
            task = db.get_task(record.id)
            assert task.state.revisable.prediction_round_count == 1
            assert runtime._remaining_budget(task.state)["prediction_rounds"] == 0
            task.state.revisable.limits.prediction_rounds = 2
            db.update_task(record.id, state=task.state)
        # A new Executor object and fresh observation are created by run_task.
        assert await runtime.run_task(record.id) == TaskStatus.SUCCEEDED
        assert len(closes) == 2
    assert calls.count("planner") == (3 if architecture == "plan_executor" else 2)
    assert calls.count("reviewer") == (architecture == "plan_reviewer")
    assert db.get_task(record.id).state.revisable.completed_stage_ids == ["plan_1_stage_1"]
    assert calls[:4] == ["planner", "executor", "executor", "planner"]
    assert len(driver.actions) == 1
    db.close()


async def test_last_review_round_is_reserved_for_a_decision(setup, monkeypatch):
    from agent.revisable.session import terminal_registry
    from agent.session import AgentSession
    from agent.tool_registry import AgentToolResult
    from shared.revisable import Review

    store, state = setup
    session = AgentSession("reviewer", "test", max_total_rounds=2)
    session.set_stable_system("Review")

    async def submit(args, ctx):
        return AgentToolResult(terminal_value=Review.model_validate(args))

    registry = terminal_registry(session, Review, "submit_reviewer_decision", "Review", submit)
    register_memory_tools(registry, "reviewer", store, state)
    choices = []

    async def complete(model, messages, **kwargs):
        choices.append(kwargs["tool_choice"])
        if len(choices) == 1:
            return response(("read_history", {"query": "missing"}))
        assert [tool["function"]["name"] for tool in kwargs["tools"]] == [
            "submit_reviewer_decision"
        ]
        assert "Evidence gathering" in messages[-1]["content"]
        return response(
            (
                "submit_reviewer_decision",
                {"decision": "inconclusive", "reason": "Cannot establish the result"},
            )
        )

    monkeypatch.setattr("agent.session.complete", complete)
    result = await session.run([], tool_registry=registry, reserve_terminal_round=True)
    assert choices == ["required", "required"]
    assert result.decision.decision == "inconclusive"


async def test_image_attachment_follows_all_parallel_tool_replies(setup, monkeypatch):
    from agent.revisable.session import terminal_registry
    from agent.session import AgentSession
    from agent.tool_registry import AgentToolResult
    from shared.revisable import Review

    store, state = setup
    observation = store.get("observation", "obs_1")["payload"]
    observation["image_ref"] = store.artifacts.save_bytes("images", b"test image")
    store.put("observation", "obs_1", observation)
    session = AgentSession("reviewer", "test", max_total_rounds=2)
    session.set_stable_system("Review")

    async def submit(args, ctx):
        return AgentToolResult(terminal_value=Review.model_validate(args))

    registry = terminal_registry(session, Review, "submit_reviewer_decision", "Review", submit)
    register_memory_tools(registry, "reviewer", store, state)
    rounds = 0

    async def complete(model, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            return response(
                ("read_history", {"source": "obs_1", "view": "image"}),
                ("read_history", {"query": "value"}),
            )
        start = next(i for i, message in enumerate(messages) if message.get("tool_calls"))
        replies = messages[start + 1 : start + 3]
        assert [message["role"] for message in replies] == ["tool", "tool"]
        assert [message["tool_call_id"] for message in replies] == ["call_0", "call_1"]
        assert messages[start + 3]["role"] == "user"
        return response(
            ("submit_reviewer_decision", {"decision": "inconclusive", "reason": "Unknown"})
        )

    monkeypatch.setattr("agent.session.complete", complete)
    await session.run([], tool_registry=registry, include_skill_context=False)


async def test_cancelled_task_does_not_call_any_role(tmp_path, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(_env_file=None, agent_architecture="plan_reviewer", data_dir=tmp_path)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    record = db.create_task("Go home", AgentState(instruction="Go home"))

    async def forbidden(*args, **kwargs):
        pytest.fail("cancelled task must not make provider requests")

    monkeypatch.setattr("agent.session.complete", forbidden)
    runtime.request_cancel(record.id)
    assert await runtime.run_task(record.id) == TaskStatus.CANCELLED
    assert not driver.actions
    db.close()


def test_optional_tool_schemas_emit_required_arrays_without_changing_defaults():
    from jsonschema import Draft202012Validator

    from agent.revisable.session import ExecutorSubmission
    from agent.revisable.tools import Arguments, ReadHistory, tool_schema
    from shared.revisable import Plan

    for model in (Arguments, ReadHistory, Plan, ExecutorSubmission):
        schema = tool_schema(model)
        Draft202012Validator.check_schema(schema)

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert isinstance(node["required"], list)
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(schema)
    assert tool_schema(ReadHistory)["required"] == []
    assert ReadHistory.model_validate({}).query == ""


async def test_append_keeps_equal_observations_and_prior_versions(setup):
    store, state = setup
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    for source in ("obs_1", "obs_2"):
        result = await registry.execute(
            "write_note",
            {
                "note_key": "sequence",
                "title": "Readings",
                "append": True,
                "content": f"{source}: 9",
                "observation_ids": [source],
            },
            context(),
        )
        assert result.status == ToolStatus.SUCCEEDED
    assert store.get("note", "sequence")["payload"] == {
        "title": "Readings",
        "content": "obs_1: 9\nobs_2: 9",
        "observation_ids": ["obs_1", "obs_2"],
        "written_step": state.step_number,
        "stage_id": state.revisable.stage_id,
    }
    assert store.get("note", "sequence", 1)["payload"]["content"] == "obs_1: 9"


async def test_active_stage_packet_supplies_full_plan_by_default(setup):
    store, state = setup
    state.revisable.plan.assumption_roadmap = ["Calculate"]
    packet = store.context(state, "obs_3")
    assert packet["current_stage"]["goal"] == "Collect"
    assert packet["original_instruction"] == state.instruction
    assert packet["latest_plan"]["assumption_roadmap"] == ["Calculate"]
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    plan = await registry.execute("read_history", {"source": "plan"}, context())
    assert plan.data["plan"]["assumption_roadmap"] == ["Calculate"]


@pytest.mark.parametrize("decision", ["act", "advance"])
async def test_no_active_stage_blocks_device_work_and_advancement(setup, decision):
    store, state = setup
    state.revisable.plan = None
    session = ExecutionSession("executor", "test")
    session.store, session.state = store, state
    args = {"decision": decision, "summary": "Continue", "observation_id": "current"}
    if decision == "advance":
        args["completed_stage_id"] = state.revisable.stage_id
    if decision == "act":
        args["action"] = {"type": "home"}
    ctx = context()
    ctx.state["active_observation_id"] = "current"
    result = await session._build_registry({}).execute("submit_executor_step", args, ctx)
    assert result.error == "no_active_stage"
    assert result.terminal_value is None


def test_action_receipt_keeps_device_coordinates_out_of_dialogue_and_review(setup):
    from types import SimpleNamespace

    from agent.revisable.roles import PlanExecutor
    from shared.schemas import ActionReceipt, ActionResult, ExecutorStep, SubmittedActionSnapshot

    store, state = setup
    result = ActionResult(
        success=True,
        message="Tapped (600,1783)",
        detail={"x": 600, "y": 1783, "dispatched_coordinates": {"x": 600, "y": 1783}},
        receipt=ActionReceipt(dispatch_succeeded=True, transaction_id="txn_internal_trace"),
    )
    step = ExecutorStep(
        summary="Open control",
        basis_observation_id="obs_1",
        submitted_action_snapshot=SubmittedActionSnapshot(type="tap", index=6),
    )
    PlanExecutor.record_result(SimpleNamespace(store=store, directive="act"), step, result, state)
    visible = store.read_dialogue(state.revisable.dialogue_refs)
    review = store.context(state, "obs_2", "reviewer")
    assert "1783" not in json.dumps([visible, review])
    assert "txn_internal_trace" not in json.dumps(visible)
    assert store.records("event")[0]["payload"]["action_result"]["receipt"]["transaction_id"] == "txn_internal_trace"
    assert review["operation_summary"]["events"][0]["submitted_action"]["index"] == 6
    assert result.detail["dispatched_coordinates"]["y"] == 1783


@pytest.mark.asyncio
async def test_compound_action_outcome_is_atomic_restorable_and_not_one_shot(setup):
    from types import SimpleNamespace

    from agent.revisable.roles import PlanExecutor
    from perception.observation import ObservationPackage
    from shared.schemas import ActionReceipt, ActionResult, CanonicalUI, ExecutorStep, ObservationMode, UIElement

    store, state = setup
    historical = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.demo",
            semantic_tree=[
                UIElement(index=-1, role="text", text="Recipe", interactable=False),
                UIElement(
                    index=-1,
                    role="text",
                    text="Description: distinct filling",
                    depth=1,
                    interactable=False,
                ),
            ],
        ),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="detail",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        clean_png=None,
        observation_id="obs_detail",
    )
    result = ActionResult(
        success=True,
        message="inspection complete",
        receipt=ActionReceipt(
            dispatch_succeeded=True,
            observation_accepted=True,
            observation_id="obs_list",
        ),
    )
    step = ExecutorStep(
        decision="act",
        summary="Inspected one candidate",
        basis_observation_id="obs_before",
    )
    owner = SimpleNamespace(
        store=store,
        directive="act",
        completed_stage_id=None,
    )
    PlanExecutor.record_result(
        owner,
        step,
        result,
        state,
        compound_evidence=historical,
    )

    blocks = store.read_dialogue(state.revisable.dialogue_refs)
    assert len(blocks) == 1
    assert blocks[0]["role"] == "user"
    assert "ACTION OUTCOME" in str(blocks[0]["content"])
    assert '"action_result"' in str(blocks[0]["content"])
    assert "HISTORICAL INTERMEDIATE" in str(blocks[0]["content"])
    assert "obs_detail" in str(blocks[0]["content"])

    kwargs = dict(
        model="test",
        settings=Settings(_env_file=None),
        meter=None,
        event_sink=None,
    )
    restored_n1 = await restore_dialogue(store, state, **kwargs)
    restored_n2 = await restore_dialogue(store, state, **kwargs)
    assert restored_n1 == restored_n2
    assert sum("HISTORICAL INTERMEDIATE" in str(item) for item in restored_n2) == 1


def test_failed_compound_result_does_not_publish_partial_intermediate(setup):
    from types import SimpleNamespace

    from agent.revisable.roles import PlanExecutor
    from perception.observation import ObservationPackage
    from shared.schemas import ActionResult, CanonicalUI, ExecutorStep, ObservationMode, UIElement

    store, state = setup
    partial = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.demo",
            semantic_tree=[
                UIElement(
                    index=-1,
                    role="text",
                    text="partial detail",
                    interactable=False,
                ),
            ],
        ),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="partial",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        observation_id="obs_partial",
    )
    PlanExecutor.record_result(
        SimpleNamespace(store=store, directive="act", completed_stage_id=None),
        ExecutorStep(decision="act", summary="Back failed"),
        ActionResult(success=False, message="failed", detail={}),
        state,
        compound_evidence=partial,
    )
    restored = store.read_dialogue(state.revisable.dialogue_refs)
    assert '"success": false' in str(restored)
    assert "HISTORICAL INTERMEDIATE" not in str(restored)
    assert "obs_partial" not in str(restored)


def test_decision_packet_supplies_whole_notes_without_truncating_sources(setup):
    store, state = setup
    store.put(
        "note",
        "reading",
        {
            "title": "Reading",
            "content": "obs_1: 9; obs_2: 9",
            "observation_ids": ["obs_1", "obs_2"],
        },
    )
    packet = store.context(state, "obs_3", "reviewer")
    assert packet["note_contents"][0]["content"] == "obs_1: 9; obs_2: 9"
    assert store.recent_notes(character_budget=1) == []
    assert store.note_index()[0]["note_key"] == "reading"


@pytest.mark.parametrize("role", ["planner", "reviewer"])
def test_decision_roles_receive_ordered_operations_beyond_the_last_four(setup, role):
    store, state = setup
    for step in range(1, 7):
        store.put(
            "event",
            str(step),
            {
                "step": step,
                "stage_id": state.revisable.stage_id,
                "executor_report": "Read 9 and continue",
                "observation_id": f"source_{step}",
                "submitted_action": {"type": "tap", "index": 6, "x": None},
                "action_result": {"success": False, "receipt": None},
            },
        )
    state.revisable.summary = "Earlier attempt failed; the next route is tentative."

    packet = store.context(state, "obs_3", role)
    summary = packet["operation_summary"]
    assert summary["earlier_executor_summary"] == state.revisable.summary
    assert summary["omitted_event_count"] == 0
    assert [event["step"] for event in summary["events"]] == list(range(1, 7))
    assert [event["observation_id"] for event in summary["events"]] == [
        f"source_{step}" for step in range(1, 7)
    ]
    for event in summary["events"]:
        assert event["executor_report"] == "Read 9 and continue"
        assert event["action_result"]["success"] is False
        assert event["submitted_action"] == {"type": "tap", "index": 6}
    assert store.get("event", "1")["payload"]["submitted_action"]["x"] is None
    assert "operation_summary" not in store.context(state, "obs_3", "executor")


def test_operation_summary_keeps_whole_recent_events_and_exposes_omissions(setup):
    store, state = setup
    events = [
        {"step": step, "executor_report": f"Exact observation {step}: 0.00123456789"}
        for step in range(1, 5)
    ]
    for event in events:
        store.put("event", str(event["step"]), event)
    state.revisable.summary = "Earlier model account."
    budget = len(state.revisable.summary) + sum(
        len(json.dumps({**event, "source": f"event:{event['step']}@1"}, ensure_ascii=False)) for event in events[-2:]
    )
    summary = store.operation_summary(state, character_budget=budget)
    assert summary["earlier_executor_summary"] == state.revisable.summary
    assert summary["events"] == [{**event, "source": f"event:{event['step']}@1"} for event in events[-2:]]
    assert summary["omitted_event_count"] == 2
    assert store.get("event", "1")["payload"] == events[0]

    too_small = store.operation_summary(state, character_budget=1)
    assert too_small["earlier_executor_summary"] is None
    assert too_small["earlier_summary_omitted"] is True
    assert too_small["events"] == []
    assert too_small["omitted_event_count"] == 4


async def test_submission_phase_rejects_history_tool_even_if_model_ignores_catalog(
    setup, monkeypatch
):
    from agent.revisable.session import terminal_registry
    from agent.session import AgentSession
    from agent.tool_registry import AgentToolResult
    from shared.revisable import Review

    store, state = setup
    session = AgentSession("reviewer", "test", max_total_rounds=3)
    session.set_stable_system("Review")

    async def submit(args, ctx):
        return AgentToolResult(terminal_value=Review.model_validate(args))

    registry = terminal_registry(session, Review, "submit_reviewer_decision", "Review", submit)
    register_memory_tools(registry, "reviewer", store, state)
    count = 0

    async def complete(*args, **kwargs):
        nonlocal count
        count += 1
        if count < 3:
            return response(("read_history", {"query": "missing"}))
        return response(
            ("submit_reviewer_decision", {"decision": "inconclusive", "reason": "Unknown"})
        )

    monkeypatch.setattr("agent.session.complete", complete)
    result = await session.run([], tool_registry=registry, reserve_terminal_round=True)
    assert result.tool_calls[0].status == ToolStatus.SUCCEEDED
    assert result.tool_calls[1].status == ToolStatus.PRECONDITION_NOT_MET
    assert result.context_state["history_reads"] == 1


async def test_inline_notes_roll_back_together_when_one_source_is_invalid(setup):
    store, state = setup
    session = ExecutionSession("executor", "test")
    session.store, session.state = store, state
    ctx = context()
    ctx.state["active_observation_id"] = "obs_3"
    result = await session._build_registry({}).execute(
        "submit_executor_step",
        {
            "decision": "advance",
            "completed_stage_id": state.revisable.stage_id,
            "summary": "Collected",
            "observation_id": "obs_3",
            "notes": [
                {
                    "note_key": "first",
                    "title": "First",
                    "content": "9",
                    "observation_ids": ["obs_1"],
                },
                {
                    "note_key": "second",
                    "title": "Second",
                    "content": "9",
                    "observation_ids": ["foreign"],
                },
            ],
        },
        ctx,
    )
    assert result.terminal_value is None
    assert store.records("note") == []
    assert "directive" not in ctx.state


async def test_cancellation_after_model_reply_prevents_device_dispatch(tmp_path, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None, data_dir=tmp_path, agent_architecture="plan_executor", default_model="test"
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    task = db.create_task("Go home", AgentState(instruction="Go home"), device_serial="fixture")

    async def complete(model, messages, *, tools, **kwargs):
        if any(tool["function"]["name"] == "submit_planner_decision" for tool in tools):
            return response(
                (
                    "submit_planner_decision",
                    {
                        "decision": "execute",
                        "reason": "Open home",
                        "plan": {"current_stage": {"goal": "Go home"}},
                    },
                )
            )
        anchor = next(
            json.loads(message["content"])["runtime_update"]
            for message in reversed(messages)
            if isinstance(message.get("content"), str)
            and message["content"].startswith('{"runtime_update"')
        )
        runtime.request_cancel(task.id)
        return response(
            (
                "submit_executor_step",
                {
                    "decision": "act",
                    "summary": "Go home",
                    "observation_id": anchor["current_observation_id"],
                    "action": {"type": "home"},
                },
            )
        )

    monkeypatch.setattr("agent.session.complete", complete)
    assert await runtime.run_task(task.id) == TaskStatus.CANCELLED
    assert driver.actions == []
    db.close()


def test_cancelled_runtime_does_not_start_or_count_another_model_request(tmp_path):
    import asyncio

    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver

    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture="plan_executor")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    runtime = create_orchestrator(
        db,
        TraceWriter(db, artifacts),
        settings=settings,
        artifacts=artifacts,
        driver=FixtureDriver(),
    )
    state = AgentState(instruction="Go home")
    task = db.create_task(state.instruction, state)
    meter = runtime._model_call_meter(task.id, state, "executor")
    runtime.request_cancel(task.id)
    with pytest.raises(asyncio.CancelledError):
        meter("primary", {})
    assert state.role_invocation_count == 0
    db.close()


def test_explanatory_prose_does_not_invalidate_a_structurally_valid_decision():
    from pydantic import ValidationError

    from agent.revisable.session import ExecutorSubmission
    from shared.revisable import Review

    explanation = "Observed state and reasons for the next decision. " * 50
    plan = PlannerDecision(
        decision="execute", reason=explanation, plan=Plan(current_stage=Stage(goal=explanation))
    )
    review = Review(decision="execute", reason=explanation)
    submission = ExecutorSubmission(
        decision="act",
        summary=explanation,
        observation_id="obs_current",
        action={"type": "tap", "index": 0},
    )
    assert plan.reason == review.reason == submission.summary == explanation
    with pytest.raises(ValidationError, match="Only act requires an action"):
        ExecutorSubmission(decision="act", summary=explanation, observation_id="obs_current")
    with pytest.raises(ValidationError, match="Only advance requires completed_stage_id"):
        ExecutorSubmission(decision="advance", summary=explanation, observation_id="obs_current")


async def test_stage_lookup_and_reused_frame_match_the_same_visit(setup):
    from types import SimpleNamespace

    store, state = setup
    package = SimpleNamespace(
        observation_id="reused_frame",
        ui=SimpleNamespace(app_id="app.a"),
        text_for_llm="Search results: exact title",
        image_for_llm=None,
        clean_png=None,
        annotated_png=None,
    )
    state.step_number = 1
    state.revisable.plan.current_stage.goal = "Collect the first title"
    old_stage = state.revisable.stage_id
    store.observe(package, state)
    state.step_number = 7
    state.revisable.revision = 2
    state.revisable.plan = Plan(current_stage=Stage(goal="Search the saved title"))
    store.observe(package, state)
    new_stage = state.revisable.stage_id
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    stages = await registry.execute("read_history", {"query": "COLLECT"}, context())
    assert "Collect the first title" in stages.data["items"][0]["text"]
    assert stages.data["items"][0]["source"].startswith(f"stage:{old_stage}@")
    visits = store.get("observation", "reused_frame")["payload"]["visits"]
    assert visits == [{"step": 1, "stage_id": old_stage}, {"step": 7, "stage_id": new_stage}]
    found = await registry.execute("read_history", {"query": "exact"}, context())
    assert found.data["items"][0]["observation_id"] == "reused_frame"
    assert "Search results: exact title" in found.data["items"][0]["text"]


async def test_effect_observation_links_events_and_searches_reports_without_full_payloads(setup):
    store, state = setup
    report = "attempt " * 900 + "literal_marker"
    event = {
        "step": 1,
        "stage_id": state.revisable.stage_id,
        "observation_id": "obs_1",
        "executor_report": report,
        "action_result": {
            "success": True,
            "receipt": {
                "observation_id": "obs_1",
                "effect_observation_id": "obs_2",
            },
        },
    }
    store.put("event", "1", event)
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    found = await registry.execute("read_history", {"query": "literal_marker"}, context())
    assert len(found.data["items"]) == 1
    assert "literal_marker" in found.data["items"][0]["text"]
    assert len(json.dumps(found.data)) < 1000
    full = await registry.execute("read_history", {
        "source": found.data["items"][0]["source"], "full": True,
    }, context())
    assert json.loads(full.data["items"][0]["text"]) == event


async def test_note_directory_is_bounded_searchable_and_preserves_full_versioned_sources(setup):
    from agent.revisable.tools import WriteNote, save_note

    store, state = setup
    sources = [f"source_{index}" for index in range(20)]
    for source in sources:
        store.put("observation", source, store.get("observation", "obs_1")["payload"])
    for index in range(30):
        state.step_number = index
        save_note(
            store,
            WriteNote(
                note_key=f"note_{index}",
                title=f"Item {index}",
                content=("detail " * 700) + f"needle_{index}_end",
                observation_ids=sources,
            ),
            state,
        )
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    packet = store.context(state, "obs_3")
    assert packet["omitted_note_count"] > 0
    assert sum(len(json.dumps(row, ensure_ascii=False)) for row in packet["notes"]) <= 2400
    assert "observation_ids" not in packet["notes"][0]
    assert "source" in packet["notes"][0]
    found = await registry.execute("read_history", {"query": "needle_0_end"}, context())
    assert len(found.data["items"]) == 1
    assert found.data["items"][0]["note_key"] == "note_0"
    assert "needle_0_end" in found.data["items"][0]["text"]
    full = await registry.execute("read_history", {"source": "note_0", "full": True}, context())
    assert full.data["items"][0]["observation_ids"] == sources
    assert full.data["items"][0]["text"].endswith("needle_0_end")
    assert store.get("note", "note_0")["payload"]["observation_ids"] == sources
    save_note(store, WriteNote(note_key="note_0", content="Correction"), state)
    old = await registry.execute("read_history", {"source": "note:note_0@1", "full": True}, context())
    assert old.data["items"][0]["text"].endswith("needle_0_end")
    bounded = await registry.execute("read_history", {"query": "needle"}, context())
    assert len(bounded.data["items"]) == 6
    assert bounded.data["more_matches"] == 23
    assert sum(len(item.get("text", "")) for item in bounded.data["items"]) <= 3000


async def test_compaction_excludes_recent_whole_steps_and_accumulates_again(setup, monkeypatch):
    store, state = setup
    for step in range(4):
        state.step_number = step
        store.save_dialogue([{"role": "user", "content": f"DECISION_{step}" * 400}], state)
        store.save_dialogue([{"role": "user", "content": "identical receipt"}], state)
    original_refs = state.revisable.dialogue_refs[:]
    assert len(set(original_refs)) == 8
    assert [store.get("dialogue", ref)["payload"]["step"] for ref in original_refs] == [
        0,
        0,
        1,
        1,
        2,
        2,
        3,
        3,
    ]
    retained_before = store.read_dialogue(original_refs[-4:])
    summaries = []

    async def complete(model, messages, **kwargs):
        payload = next(
            json.loads(message["content"])
            for message in messages
            if isinstance(message.get("content"), str)
            and message["content"].startswith('{')
            and "previous_items" in json.loads(message["content"])
        )
        summaries.append(payload)
        assert payload["original_instruction"] == state.instruction
        return response(("save_summary", {"results": [{"text": f"Summary {len(summaries)}",
            "sources": [payload["records"][0]["source"]]}], "decisions_and_attempts": [], "critical_context": []}))

    monkeypatch.setattr("agent.session.complete", complete)

    async def restore():
        return await restore_dialogue(
            store,
            state,
            model="test",
            settings=Settings(_env_file=None, executor_context_tokens=1),
            meter=None,
            event_sink=None,
        )

    restored = await restore()
    assert state.revisable.dialogue_refs == original_refs[-4:]
    assert restored[1:] == retained_before
    assert store.execution_context(state, "obs_3")["runtime_update"]["latest_plan"] == state.revisable.plan.model_dump()
    assert "DECISION_0" in json.dumps(summaries[0])
    assert "DECISION_2" not in json.dumps(summaries[0])
    assert "DECISION_3" not in json.dumps(summaries[0])
    assert store.records("compaction")[0]["payload"]["source_steps"] == [0, 1]
    assert store.records("compaction")[0]["payload"]["retained_steps"] == [2, 3]

    for step in (4, 5):
        state.step_number = step
        store.save_dialogue([{"role": "user", "content": f"DECISION_{step}" * 400}], state)
        store.save_dialogue([{"role": "user", "content": "identical receipt"}], state)
    second_retained = store.read_dialogue(state.revisable.dialogue_refs[-4:])
    restored = await restore()
    assert summaries[1]["previous_items"][0]["text"] == "Summary 1"
    assert "DECISION_2" in json.dumps(summaries[1])
    assert "DECISION_4" not in json.dumps(summaries[1])
    assert restored[1:] == second_retained
    assert len(store.read_dialogue(original_refs)) == 8


async def test_stage_directory_is_bounded_and_old_goals_remain_queryable(setup):
    store, state = setup
    for revision in range(1, 13):
        state.revisable.revision = revision
        state.step_number = revision
        state.revisable.plan = Plan(
            current_stage=Stage(goal=f"Goal {revision}: " + "precise requirement " * 28),
        )
        store.record_stage(state)
    packet = store.context(state, "obs_3", "planner")
    directory = packet["stage_directory"]
    assert len(directory["stages"]) == 3
    assert directory["total"] == 12
    assert directory["next_offset"] == 3
    assert sum(len(json.dumps(row, ensure_ascii=False)) for row in directory["stages"]) <= 2400
    registry = AgentToolRegistry()
    register_memory_tools(registry, "planner", store, state)
    ctx = ToolExecutionContext(AgentRole.PLANNER, "stage-lookup", {})
    old = await registry.execute("read_history", {"query": "1:"}, ctx)
    assert len(old.data["items"]) == 2  # Goal 1 and Goal 11 are literal matches.
    exact = await registry.execute("read_history", {"source": "stage:plan_1_stage_1"}, ctx)
    assert "Goal 1:" in exact.data["items"][0]["text"]
    other = TaskStore(store.db, store.artifacts, "unrelated-task")
    assert other.records("stage") == []


async def test_note_reference_schema_and_recovery_are_consistent(setup):
    store, state = setup
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, state)
    ctx = context()
    written = await registry.execute("write_note", {"note_key": "saved_title", "content": "Exact title"}, ctx)
    assert written.data == {"note_key": "saved_title", "version": 1}
    wrong = await registry.execute("read_history", {"source": state.revisable.stage_id}, ctx)
    assert wrong.status != ToolStatus.SUCCEEDED
    found = await registry.execute("read_history", {"query": "Exact"}, ctx)
    assert found.data["items"][0]["note_key"] == "saved_title"
    recovered = await registry.execute("read_history", {"source": written.data["note_key"]}, context())
    assert recovered.data["items"][0]["text"] == "saved_title\nExact title"
    for spec in registry.specs_for_role(AgentRole.EXECUTOR):
        assert "key" not in spec.parameters["properties"]
        if spec.name == "write_note":
            assert "note_key" in spec.parameters["required"]
            assert spec.parameters["properties"]["note_key"]["description"]

@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
@pytest.mark.parametrize("completion_at", ["initial", "advance", "finish"])
async def test_task_completion_requires_the_designated_decision_role(
    tmp_path, monkeypatch, architecture, completion_at
):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None, data_dir=tmp_path, agent_architecture=architecture, default_model="test"
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    task = db.create_task(
        "Inspect the current screen",
        AgentState(instruction="Inspect the current screen"),
        device_serial="fixture",
    )
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        name = next(
            t["function"]["name"] for t in tools if t["function"]["name"].startswith("submit_")
        )
        calls.append(name)
        if name == "submit_planner_decision":
            if completion_at == "initial" or calls.count(name) > 1:
                return response(
                    (
                        name,
                        {
                            "decision": "complete",
                            "reason": "The requested state is already visible",
                        },
                    )
                )
            return response(
                (
                    name,
                    {
                        "decision": "execute",
                        "reason": "Inspect current state",
                        "plan": {
                            "current_stage": {"goal": "Inspect the current screen"},
                            "assumption_roadmap": ["An unverified future outcome"],
                        },
                    },
                )
            )
        if name == "submit_reviewer_decision":
            return response(
                (name, {"decision": "complete", "reason": "The requested state is visible"})
            )
        anchor = next(
            json.loads(m["content"])["runtime_update"]
            for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"')
        )
        args = {
            "decision": completion_at,
            "summary": "Actual state observed",
            "observation_id": anchor["current_observation_id"],
        }
        if completion_at == "advance":
            args["completed_stage_id"] = anchor["stage_id"]
        return response((name, args))

    monkeypatch.setattr("agent.session.complete", complete)
    assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
    assert driver.actions == []
    assert calls[-1] == (
        "submit_planner_decision" if architecture == "plan_executor" else "submit_reviewer_decision"
    )
    assert calls.count("submit_executor_step") == (completion_at != "initial")
    # Three-role finish goes straight to Reviewer; it does not add a Planner verdict first.
    assert calls.count("submit_planner_decision") == (
        2
        if completion_at == "advance"
        or (completion_at == "finish" and architecture == "plan_executor")
        else 1
    )
    completed = db.get_task(task.id)
    assert completed.state.revisable.completion_reason == (
        "The requested state is already visible"
        if architecture == "plan_executor"
        else "The requested state is visible"
    )
    db.close()


@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
@pytest.mark.parametrize("answer", ["6", "First title, Second title", "The requested setting is enabled."])
async def test_completion_preserves_answer_format_without_appending_reason(
    tmp_path, monkeypatch, architecture, answer
):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None, data_dir=tmp_path, agent_architecture=architecture, default_model="test"
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    task = db.create_task(
        "Report the observed result in the requested format",
        AgentState(instruction="Report the observed result in the requested format"),
        device_serial="fixture",
    )
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        name = next(
            t["function"]["name"] for t in tools
            if t["function"]["name"].startswith("submit_")
        )
        calls.append(name)
        return response((name, {"decision": "complete", "reason": answer}))

    monkeypatch.setattr("agent.session.complete", complete)
    try:
        assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
        assert db.get_task(task.id).state.revisable.completion_reason == answer
        assert driver.actions == []
        assert calls[-1] == (
            "submit_planner_decision" if architecture == "plan_executor"
            else "submit_reviewer_decision"
        )
    finally:
        db.close()


async def test_reviewer_cannot_resume_device_work_without_a_current_goal(tmp_path, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus

    settings = Settings(
        _env_file=None, data_dir=tmp_path, agent_architecture="plan_reviewer", default_model="test"
    )
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(
        db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver
    )
    state = AgentState(instruction="Inspect the result")
    state.revisable.next_role = "reviewer"
    task = db.create_task(state.instruction, state, device_serial="fixture")
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        name = next(
            t["function"]["name"] for t in tools if t["function"]["name"].startswith("submit_")
        )
        calls.append(name)
        if name == "submit_reviewer_decision":
            return response((name, {"decision": "execute", "reason": "A fresh check is needed"}))
        assert "A fresh check is needed" in json.dumps(messages)
        return response(
            (name, {"decision": "inconclusive", "reason": "No supported check is available"})
        )

    monkeypatch.setattr("agent.session.complete", complete)
    assert await runtime.run_task(task.id) == TaskStatus.FAILED
    assert calls == ["submit_reviewer_decision", "submit_planner_decision"]
    assert driver.actions == []
    assert db.get_task(task.id).failure_reason.startswith("planner_inconclusive:")
    db.close()


async def test_oversized_goals_return_bounded_matching_excerpts(setup):
    store, state = setup
    goal = "Required exact information " * 400 + "unique ending"
    state.revisable.plan.current_stage.goal = goal
    store.record_stage(state)
    registry = AgentToolRegistry()
    register_memory_tools(registry, "planner", store, state)
    result = await registry.execute(
        "read_history",
        {"query": "ending"},
        ToolExecutionContext(AgentRole.PLANNER, "lookup", {}),
    )
    assert len(result.data["items"]) == 1
    row = result.data["items"][0]
    assert row["truncated"] and "unique ending" in row["text"]
    assert len(row["text"]) <= 500
    assert store.get("stage", state.revisable.stage_id)["payload"]["goal"] == goal

async def test_observation_failure_persists_stage_evidence_in_trace(tmp_path, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from driver.observation_deadline import ObservationStageError
    from shared.schemas import TaskStatus

    settings = Settings(_env_file=None, data_dir=tmp_path,
                        agent_architecture='plan_executor', default_model='test')
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  artifacts=artifacts, driver=FixtureDriver())
    failure = ObservationStageError('observation_capture', 'outer_deadline_exhausted',
        timed_out=True, budget_ms=12000, elapsed_ms=12001, capture_attempt_count=1,
        provider_attempts=[{'provider':'adb_screencap','status':'ok'}],
        cancelled_tasks=['current-observation-tree'],
        stage_timings=[{'stage':'tree_collector','outcome':'cancelled'}])
    async def fail(**kwargs):
        raise failure
    monkeypatch.setattr(runtime, '_observe', fail)
    task = db.create_task('Inspect the screen', AgentState(instruction='Inspect the screen'),
                          device_serial='fixture')
    try:
        assert await runtime.run_task(task.id) == TaskStatus.FAILED
        event = next(e for e in db.list_traces(task.id)
                     if e.payload.get('event') == 'observation_capture_failed')
        assert event.payload['provider_attempts'] == failure.provider_attempts
        assert event.payload['cancelled_tasks'] == failure.cancelled_tasks
        assert event.payload['stage_timings'] == failure.stage_timings
        assert event.payload['capture_attempt_count'] == 1
    finally:
        db.close()
