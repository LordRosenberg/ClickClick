"""Production wiring and observation freshness for both supported architectures."""
import json

import pytest
from pydantic import ValidationError

from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus
from tests.test_revisable_runtime import response


@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
@pytest.mark.parametrize("capture_fails", [False, True])
async def test_planner_handoff_refreshes_after_compaction_before_color_and_index_action(
    tmp_path, monkeypatch, architecture, capture_fails,
):
    import copy
    import io
    from PIL import Image, ImageDraw
    from agent.revisable.dialogue import restore_dialogue
    from agent.read_tools import make_inspect_image_regions_handler

    settings = Settings(_env_file=None, data_dir=tmp_path,
                        agent_architecture=architecture, default_model="test")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    driver.tree = copy.deepcopy(driver.tree)
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  driver=driver, artifacts=artifacts)
    task = db.create_task("Inspect and select Play", device_serial="fixture")
    packages, model_observations, measurements = [], [], []
    restored = False
    original_observe = runtime._observe

    async def observe(**kwargs):
        if packages:
            assert restored, "handoff capture must follow even slow history compaction"
            if capture_fails:
                raise RuntimeError("handoff capture unavailable")
        package = await original_observe(**kwargs)
        packages.append(package)
        return package

    async def compact(*args, **kwargs):
        nonlocal restored
        history = await restore_dialogue(*args, **kwargs)
        # Simulate a banner moving the same indexed control while compaction
        # is in flight. The capture must include both new pixels and bounds.
        driver.tree["tree"]["children"][0]["bounds"] = [100, 600, 400, 700]
        img = Image.open(io.BytesIO(driver._png)).convert("RGB")
        ImageDraw.Draw(img).rectangle([100, 600, 400, 700], fill=(239, 83, 70))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        driver._png = buf.getvalue()
        restored = True
        return history

    def inspection_handler():
        inspect = make_inspect_image_regions_handler()
        async def measured(args, context):
            result = await inspect(args, context)
            measurements.append(result.data)
            return result
        return measured

    async def complete(model, messages, *, tools, **kwargs):
        name = next(t["function"]["name"] for t in tools
                    if t["function"]["name"].startswith("submit_"))
        if name == "submit_planner_decision":
            return response((name, {"decision": "execute", "reason": "Select the control",
                "plan": {"current_stage": {"goal": "Inspect and select Play"}}}))
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.startswith('{"runtime_update"'):
                update = json.loads(content)["runtime_update"]
                if "current_observation_id" in update:
                    observation_id = update["current_observation_id"]
        model_observations.append(observation_id)
        assert len(packages) == 2
        assert observation_id == packages[1].observation_id != packages[0].observation_id
        target = next(e for e in packages[1].ui.elements if e.text == "Play")
        assert target.bounds == [100, 600, 400, 700]
        if len(model_observations) == 1:
            return response(("inspect_image_regions", {"observation_id": observation_id,
                "targets": [{"index": target.index}], "metrics": ["median_rgb"]}))
        assert measurements[0]["regions"][f"index:{target.index}"]["median_rgb"] == [239, 83, 70]
        return response((name, {"decision": "act", "summary": "Select measured control",
            "observation_id": observation_id, "action": {"type": "tap", "index": target.index}}))

    monkeypatch.setattr(runtime, "_observe", observe)
    monkeypatch.setattr("agent.revisable.roles.restore_dialogue", compact)
    monkeypatch.setattr("agent.executor.make_inspect_image_regions_handler", inspection_handler)
    monkeypatch.setattr("agent.session.complete", complete)
    try:
        result = await runtime.run_task(task.id, max_device_actions=1)
        if capture_fails:
            assert result == TaskStatus.FAILED
            assert not model_observations and not driver.actions
        else:
            assert result == TaskStatus.RUNNING, db.get_task(task.id).failure_reason
            assert len(model_observations) == 2  # Read-tool continuation reuses the frame.
            assert len(driver.actions) == 1
            assert (driver.actions[0].x, driver.actions[0].y) == (250, 650)
            events = [e for e in db.list_traces(task.id)
                      if e.payload.get("event") == "executor_handoff_observation"]
            assert len(events) == 1
    finally:
        db.close()


@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
@pytest.mark.parametrize("action_type", ["sleep", "home"])
async def test_next_decision_refreshes_after_delay_and_reuses_action_postcondition(
    tmp_path, monkeypatch, architecture, action_type,
):
    settings = Settings(_env_file=None, data_dir=tmp_path,
                        agent_architecture=architecture, default_model="test")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  driver=driver, artifacts=artifacts)
    task = db.create_task("Wait and inspect", AgentState(instruction="Wait and inspect"),
                          device_serial="fixture")
    observed = []

    async def complete(model, messages, *, tools, **kwargs):
        name = next(t["function"]["name"] for t in tools
                    if t["function"]["name"].startswith("submit_"))
        if name == "submit_planner_decision":
            return response((name, {"decision": "execute", "reason": "Inspect after action",
                                    "plan": {"current_stage": {"goal": "Wait and inspect"}}}))
        if name == "submit_reviewer_decision":
            return response((name, {"decision": "complete", "reason": "Current state inspected"}))
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.startswith('{"runtime_update"'):
                update = json.loads(content)["runtime_update"]
                if "current_observation_id" in update:
                    observation_id = update["current_observation_id"]
        observed.append(observation_id)
        args = {"decision": "review", "summary": "Current state inspected",
                "observation_id": observation_id}
        if len(observed) == 1:
            args.update(decision="act", action={"type": action_type})
        return response((name, args))

    monkeypatch.setattr("agent.session.complete", complete)
    try:
        assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
        assert len(observed) == 2
        step = db.list_steps(task.id)[0]
        if action_type == "sleep":
            assert observed[1] != observed[0]
            assert step["post_observation_id"] == ""
            assert step["action_receipt"]["observation_capture_count"] == 0
        else:
            assert observed[1] == step["post_observation_id"]
        events = db.list_traces(task.id)
        tick = next(e for e in events if e.kind == "executor_tick")
        assert "remaining_steps" not in tick.payload["runtime_budget"]
        assert tick.payload["runtime_budget"]["executor_decisions"] > 0
    finally:
        db.close()


def test_removed_architecture_and_role_are_rejected():
    from agent.tool_registry import AgentRole
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_architecture="contract")
    with pytest.raises(ValueError):
        AgentRole("contract_author")
    assert "task_completion_contract" not in AgentState.model_fields


@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
def test_androidworld_constructs_real_runtime_and_uses_action_budget(tmp_path, monkeypatch, architecture):
    import agent.integrations.android_world as adapter
    from agent.revisable.orchestrator import PlanOrchestrator
    monkeypatch.setattr(adapter, "get_driver", lambda *a, **kw: FixtureDriver())
    runtime = adapter._ClickClickRuntime(
        Settings(_env_file=None, data_dir=tmp_path, agent_architecture=architecture), "fixture")
    try:
        assert isinstance(runtime.orchestrator, PlanOrchestrator)
        task_id = runtime.create_task("Inspect screen")
        async def run(task_id, **kwargs):
            return TaskStatus.RUNNING
        monkeypatch.setattr(runtime.orchestrator, "run_task", run)
        assert runtime.run_one_step(task_id, max_steps=7) == TaskStatus.RUNNING
        assert runtime.db.get_task(task_id).state.revisable.limits.device_actions == 7
        assert runtime.orchestrator.max_steps != 7
    finally:
        runtime.close()


def test_existing_plan_checkpoint_keeps_plan_and_notes_after_field_removal(tmp_path):
    from shared.revisable import Plan, Stage
    settings = Settings(_env_file=None, data_dir=tmp_path)
    db = Database(settings.db_path)
    try:
        state = AgentState(instruction="Keep my plan")
        state.revisable.plan = Plan(current_stage=Stage(goal="Inspect the saved file"))
        state.revisable.limits.device_actions = 17
        task = db.create_task(state.instruction, state)
        db.append_agent_record(task.id, "note", "saved-filename", {"content": "Drawing.html"})
        raw = state.model_dump(mode="json")
        raw.update(task_completion_contract=None, task_memory={}, next_role="contract_author")
        db._conn.execute("UPDATE tasks SET state_json=? WHERE id=?", (json.dumps(raw), task.id))
        db._conn.commit()
        restored = db.get_task(task.id)
        assert restored.state.revisable.plan == state.revisable.plan
        assert restored.state.revisable.limits.device_actions == 17
        assert restored.state.revisable.next_role == "planner"
        assert db.get_agent_record(task.id, "note", "saved-filename")["payload"]["content"] == "Drawing.html"
        assert "task_completion_contract" not in restored.state.model_dump()
    finally:
        db.close()


def test_learner_reads_bounded_task_events_from_current_store(tmp_path):
    from agent.skills.learner import _compact_trace
    settings = Settings(_env_file=None, data_dir=tmp_path)
    db = Database(settings.db_path)
    try:
        task = db.create_task("Inspect drawing")
        other = db.create_task("Unrelated")
        db.append_agent_record(other.id, "event", "1", {"summary": "Do not include"})
        for index in range(3):
            db.append_agent_record(task.id, "event", str(index), {"step": index, "summary": f"Action {index}"})
        assert _compact_trace(task, settings=settings, max_steps=2) == [
            {"step": 1, "summary": "Action 1"}, {"step": 2, "summary": "Action 2"},
        ]
        # Closing the reader must not close or write through the task's connection.
        assert db.get_task(task.id).instruction == "Inspect drawing"
    finally:
        db.close()


@pytest.mark.parametrize("architecture", ["plan_executor", "plan_reviewer"])
@pytest.mark.parametrize("hard", [False, True])
async def test_current_runtime_cancels_during_observation(tmp_path, architecture, hard):
    import asyncio

    class WaitingDriver(FixtureDriver):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def get_frame(self):
            self.entered.set()
            await self.release.wait()
            return await super().get_frame()

    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture=architecture)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = WaitingDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  driver=driver, artifacts=artifacts)
    task = db.create_task("Inspect the screen", device_serial="fixture")
    running = asyncio.create_task(runtime.run_task(task.id))
    try:
        await asyncio.wait_for(driver.entered.wait(), timeout=5)
        if hard:
            running.cancel()
        else:
            runtime.request_cancel(task.id)
            driver.release.set()
        assert await asyncio.wait_for(running, timeout=5) == TaskStatus.CANCELLED
        assert db.get_task(task.id).status == TaskStatus.CANCELLED
        assert driver.actions == []
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        db.close()
