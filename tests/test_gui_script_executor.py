from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

from agent.revisable.store import TaskStore
from agent.skills.script_checkpoint import ScriptCheckpoint, ScriptDispatchBlocked
from agent.skills.script_runner import ScriptStepReceipt, TakeoverContext, TakeoverDecision
from agent.skills.script_schema import GuiScript
from driver.fixture import DEFAULT_TREE, FixtureDriver
from evaluation import gui_scripts
from evaluation.gui_script_executor import recover_step
from evaluation.gui_script_host import evaluation_settings
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.recovery_context import ExecutionContext
from shared.recovery_runtime import InfrastructureInterrupted
from shared.recovery_store import RecoveryStore
from shared.schemas import Action, AgentState


async def test_explicit_replay_replans_a_failed_step_through_formal_roles(tmp_path, monkeypatch):
    class RedesignDriver(FixtureDriver):
        def __init__(self):
            super().__init__(deepcopy(DEFAULT_TREE))
            self._png = None
            self.button = self.tree["tree"]["children"][0]
            self.button["text"] = "Open item"
            self.ledger = {
                "class": "android.widget.LinearLayout", "resource_id": "com.example.demo:id/recent_items",
                "bounds": [100, 600, 700, 1100], "children": [{
                    "class": "android.widget.TextView", "text": "Item ID A400",
                    "resource_id": "com.example.demo:id/item_number", "bounds": [100, 620, 550, 740],
                }],
            }
            self.tree["tree"]["children"].append(self.ledger)

        async def read_app_build(self, app):
            return {"package": app, "version_code": "2"}

        async def act(self, action):
            result = await super().act(action)
            self.button.update(text="Item opened", clickable=False, **{"class": "android.widget.TextView"})
            self.ledger["children"].append({
                "class": "android.widget.TextView", "text": "Item ID A812",
                "resource_id": "com.example.demo:id/item_number", "bounds": [100, 770, 550, 900],
            })
            return result

    app = {"type": "app_is", "app_id": "com.example.demo"}
    opened = {"type": "target_present", "target": {"text": "Item opened"}}
    script = GuiScript.model_validate({
        "script_id": "open-item", "app_id": "com.example.demo", "initial": [app], "final": [opened],
        "steps": [{"id": "open", "before": [app], "after": [opened], "timeout_seconds": 3.0,
                   "confirmation": {"scope": {"resource_id": "recent_items"},
                                    "target": {"resource_id": "item_number"},
                                    "identifier_prefix": "Item ID "},
                   "action": {"type": "tap", "target": {"text": "Play"}}}],
    })
    calls = []

    async def complete(model, messages, *, tools, **kwargs):
        names = {tool["function"]["name"] for tool in tools}
        if "submit_planner_decision" in names:
            name = "submit_planner_decision"
            anchor = next(json.loads(message["content"]) for message in reversed(messages)
                          if isinstance(message["content"], str)
                          and message["content"].startswith('{"original_instruction"'))
            assert "retired" in anchor["feedback"]
            assert anchor["script_takeover"]["checkpoint"]["cursor"] == 0
            payload = {"decision": "execute", "reason": "Use the redesigned item screen",
                       "plan": {"current_stage": {"goal": "Open the same item with the new control"}}}
        else:
            assert "submit_executor_step" in names
            name = "submit_executor_step"
            anchor = next(json.loads(message["content"])["runtime_update"] for message in reversed(messages)
                          if isinstance(message["content"], str)
                          and message["content"].startswith('{"runtime_update"'))
            scope = anchor["script_takeover"]
            assert scope["step"]["id"] == "open"
            assert scope["script_revision"] == 1
            payload = {"observation_id": anchor["current_observation_id"]}
            if not calls:
                payload.update(decision="replan", summary="The Play control is retired after the redesign")
            else:
                payload.update(decision="act", summary="Open the same item", action={"type": "tap", "index": 0})
        calls.append(name)
        return GatewayResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=[
            ToolCall(id=f"call_{len(calls)}", name=name, arguments=json.dumps(payload)),
        ])

    monkeypatch.setattr("agent.session.complete", complete)
    driver = RedesignDriver()

    @contextmanager
    def exclusive(*args, **kwargs):
        yield SimpleNamespace(quarantined=False, bind_replay=lambda runtime, task_id: None)

    async def with_driver(settings, serial, run, **kwargs):
        return await run(driver)

    monkeypatch.setattr(gui_scripts, "exclusive_emulator", exclusive)
    monkeypatch.setattr(gui_scripts, "with_android_driver", with_driver)
    source, output = tmp_path / "script.json", tmp_path / "replay"
    source.write_text(script.model_dump_json(), encoding="utf-8")
    report = await gui_scripts._replay(SimpleNamespace(
        script=source, output=output, goal="Open the item", serial=driver.serial, seconds=150.0,
        takeover_seconds=120.0,
        env_file=None, parameters=None, assets=None, ocr_executable=None, assist_policy=None,
        verify=False, repair=False, snapshot=None, avd="fixture", adb="unused",
    ))
    result = report["first"]
    assert result["status"] == "succeeded", result
    assert calls == ["submit_executor_step", "submit_planner_decision", "submit_executor_step"]
    assert [(action.type, action.x, action.y) for action in driver.actions] == [("tap_xy", 250, 250)]
    settings = evaluation_settings(None, output / "first-runtime")
    db = Database(settings.db_path, read_only=True)
    try:
        store = TaskStore(db, ArtifactStore(settings.artifacts_dir, create=False), result["task_id"])
        assert store.get("planner", "0")["payload"]["decision"]["decision"] == "execute"
        confirmation = store.get("gui_script_step_completion", "open")["payload"]
        assert confirmation["next_index"] == 1 and confirmation["confirmation_id"] == "A812"
        assert confirmation["evidence"] == ["A400", "A812"]
        assert [row["payload"]["decision"] for row in store.records("event")] == ["replan", "act"]
    finally:
        db.close()


async def test_formal_compound_input_returns_to_verification_when_second_dispatch_is_blocked(tmp_path, monkeypatch):
    app = {"type": "app_is", "app_id": "com.example.demo"}
    script = GuiScript.model_validate({
        "script_id": "recover-compound", "app_id": "com.example.demo", "initial": [app], "final": [app],
        "steps": [{"id": "submit", "before": [app], "after": [app],
                   "action": {"type": "tap", "target": {"text": "Submit"}}}],
    })
    step, = script.steps
    settings = evaluation_settings(None, tmp_path)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    state = AgentState(instruction="Submit once")
    task = db.create_task(state.instruction, state, device_serial="fixture")
    store = RecoveryStore(db, artifacts)
    store.enable(task.id)
    execution = ExecutionContext(store, store.claim(task.id, "fixture"), state)
    execution.driver = object()
    checkpoint = ScriptCheckpoint(execution, script, {})
    checkpoint.bind_attempt(0, 1, before_observation_id="before", baseline=["old"], phase="formal")
    checked = []

    async def observe():
        return None

    async def verify():
        checked.append(True)
        return frozenset({TakeoverDecision.STOP})

    async def interrupted_loop(self, *args, **kwargs):
        action = Action(type="tap_xy", x=10, y=20)
        first = execution.prepare_action(action)
        store.receipt(first, {"success": True, "detail": {}})
        try:
            execution.prepare_action(action)
        except ScriptDispatchBlocked as blocked:
            raise InfrastructureInterrupted("ScriptDispatchBlocked") from blocked
        raise AssertionError("second input passed the pending-operation gate")

    monkeypatch.setattr("agent.revisable.orchestrator.PlanOrchestrator._run_loop", interrupted_loop)
    context = TakeoverContext(script, step, None, {}, ScriptStepReceipt(
        step_id=step.id, attempt=1, succeeded=False, execution="not_dispatched", reason="old_control_missing",
    ), None, observe, verify)
    try:
        assert await recover_step(context, settings=settings, execution=execution,
                                  transaction=SimpleNamespace(builder=None)) is TakeoverDecision.STOP
        assert checked == [True]
        assert checkpoint.cursor == 0
        assert len(store.operations(task.id)) == 1
    finally:
        db.close()
