from pathlib import Path

import pytest

from agent.skills.script_checkpoint import ScriptCheckpoint, ScriptDispatchBlocked
from agent.skills.script_schema import GuiScript
from shared.db import Database
from shared.recovery_context import ExecutionContext
from shared.recovery_store import RecoveryConflict, RecoveryStore
from shared.schemas import Action, AgentState


@pytest.fixture
def replay(tmp_path: Path):
    db = Database(tmp_path / "clickclick.db")
    state = AgentState(instruction="Submit the form once")
    task_id = db.create_task(state.instruction, state, device_serial="fixture").id
    store = RecoveryStore(db)
    store.enable(task_id)
    owner = store.claim(task_id, "original")
    execution = ExecutionContext(store, owner, state)
    condition = {"type": "app_is", "app_id": "com.example.form"}
    script = GuiScript.model_validate({
        "script_id": "submit-form", "app_id": "com.example.form",
        "initial": [condition], "final": [condition], "steps": [{
            "id": "submit", "before": [condition], "after": [condition],
            "action": {"type": "tap", "target": {"text": "Submit"}},
        }],
    })
    try:
        yield store, execution, script
    finally:
        db.close()


def _bind(checkpoint, attempt=1, phase="action"):
    checkpoint.bind_attempt(0, attempt, before_observation_id="before", baseline=["old"], phase=phase)


def _confirm(checkpoint, identifiers, observation_id="after"):
    checkpoint.confirm(0, observation_id=observation_id, evidence=identifiers, receipt={
        "step_id": "submit", "succeeded": True, "attempt": 1,
    })


@pytest.mark.parametrize("result_state", ["t1_only", "settled_unknown", "settled_known"])
def test_binding_blocks_replay_and_other_roles_after_interruption(replay, result_state):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint)
    first = execution.prepare_action(Action(type="tap", x=1, y=1))
    if result_state == "settled_unknown":
        store.record_unknown(first)
    if result_state == "settled_known":
        store.receipt(first, {"success": True, "detail": {}})
    if result_state != "t1_only":
        with store.db.transaction():
            store.settle(execution.owner.task_id, 0)
    successor = store.claim(execution.owner.task_id, "recovered", takeover=True)
    resumed = ExecutionContext(store, successor, execution.state)
    checkpoint = ScriptCheckpoint(resumed, script, {})
    assert checkpoint.pending(0)["operation_id"] == first
    assert checkpoint.cursor == 0
    with pytest.raises(ScriptDispatchBlocked, match="binding_required"):
        resumed.prepare_action(Action(type="tap", x=13, y=31))
    for phase in ("action", "limited", "formal"):
        _bind(checkpoint, attempt=2, phase=phase)
        with pytest.raises(ScriptDispatchBlocked, match="requires_result_verification"):
            resumed.prepare_action(Action(type="tap", x=13, y=31))
    assert len(store.operations(successor.task_id)) == 1
    assert not checkpoint.correlates(0, ["old"])
    assert not checkpoint.correlates(0, ["new", "ambiguous"])
    assert checkpoint.correlates(0, ["old", "new"])
    with pytest.raises(RecoveryConflict, match="fresh_observation"):
        _confirm(checkpoint, ["new"], "before")
    _confirm(checkpoint, ["new"])
    durable = store.db.get_agent_record(successor.task_id, "gui_script_step_completion", "submit")["payload"]
    assert (durable["operation_id"], durable["confirmation_id"], durable["next_index"]) == (first, "new", 1)
    assert checkpoint.cursor == 1 and checkpoint.receipts[0]["succeeded"]
    assert not store.operations(successor.task_id, pending=True)
    if result_state == "t1_only":
        assert resumed.state.step_number == 1
        assert resumed.state.revisable.execution_count == 1
        assert store.operations(successor.task_id)[0]["result_state"] == "unknown_recorded"


def test_trusted_rejection_allows_retry(replay):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint)
    first = execution.prepare_action(Action(type="tap", x=1, y=1))
    store.receipt(first, {"success": False, "detail": {"device_dispatch": "not_dispatched"}})
    assert checkpoint.pending(0) is None
    _bind(checkpoint, attempt=2)
    assert execution.prepare_action(Action(type="tap", x=1, y=1)) != first


def test_conflicting_dispatch_report_does_not_allow_a_retry(replay):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint)
    operation_id = execution.prepare_action(Action(type="tap", x=1, y=1))
    store.receipt(operation_id, {"success": False, "detail": {
        "device_action_units": 0, "device_dispatch": "unknown", "node_click_status": "outcome_unknown",
    }})
    assert checkpoint.pending(0)["operation_id"] == operation_id
    with pytest.raises(ScriptDispatchBlocked, match="requires_result_verification"):
        execution.prepare_action(Action(type="tap", x=1, y=1))


def test_interruption_proof_and_cursor_share_one_transaction(replay, monkeypatch):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint)
    first = execution.prepare_action(Action(type="tap", x=1, y=1))
    with pytest.raises(RecoveryConflict, match="correlated_evidence"):
        _confirm(checkpoint, ["old"])

    def fail_manifest(*args, **kwargs):
        raise RuntimeError("instrumented failure")

    monkeypatch.setattr(store, "publish_manifest", fail_manifest)
    with pytest.raises(RuntimeError, match="instrumented failure"):
        _confirm(checkpoint, ["new"])
    assert checkpoint.cursor == 0
    assert checkpoint.pending(0)["operation_id"] == first
    assert checkpoint.pending(0)["result_state"] is None
    assert execution.state.step_number == 0
    assert execution.state.revisable.execution_count == 0
    assert store.db.get_agent_record(execution.owner.task_id, "gui_script_step_completion", "submit") is None


def test_only_same_turn_known_success_can_complete_without_unique_identifier(replay):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint)
    operation_id = execution.prepare_action(Action(type="tap", x=1, y=1))
    store.receipt(operation_id, {"success": True, "detail": {}})
    evidence = [{"visible": "Submission success"}]
    with pytest.raises(RecoveryConflict, match="correlated_evidence"):
        _confirm(checkpoint, evidence)
    checkpoint.confirm(0, observation_id="after", evidence=evidence, same_turn_known=True,
                       receipt={"step_id": "submit", "succeeded": True, "attempt": 1})
    assert checkpoint.cursor == 1


def test_verified_limited_repair_does_not_finish_parent_step(replay):
    store, execution, script = replay
    checkpoint = ScriptCheckpoint(execution, script, {})
    _bind(checkpoint, phase="limited")
    modal = execution.prepare_action(Action(type="tap", x=1, y=1))
    with pytest.raises(RecoveryConflict, match="result_uncertain"):
        checkpoint.resolve_limited(0, observation_id="after", evidence={"modal": "dismissed"})
    store.receipt(modal, {"success": True, "detail": {}})
    checkpoint.resolve_limited(0, observation_id="after", evidence={"modal": "dismissed"})
    assert checkpoint.cursor == 0 and checkpoint.pending(0) is None
    _bind(checkpoint, attempt=2)
    assert execution.prepare_action(Action(type="tap", x=13, y=31)) != modal
