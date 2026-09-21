"""Schema validation and SQLite migration smoke tests (closed-loop model)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.db import Database
from shared.revisable import Plan, PlanRuntime, Stage
from shared.model_router import ModelRole, ModelRouter
from shared.schemas import Action, ActionPipeline, ActionPipelineStage, AgentState, CanonicalUI, ExecutorStep, TraceEvent, UIElement


def test_canonical_ui_and_action_roundtrip():
    ui = CanonicalUI(
        elements=[UIElement(index=0, role="Button", text="Play", bounds=[1, 2, 3, 4], clickable=True)]
    )
    assert ui.elements[0].index == 0
    a = Action(type="tap", index=0)
    assert a.model_dump()["type"] == "tap"


def test_live_action_schema_rejects_obsolete_wait_alias():
    with pytest.raises(ValidationError):
        Action.model_validate({"type": "wait", "duration_ms": 1})


def test_executor_step_action_pipeline_roundtrip():
    final = Action(type="tap_xy", x=20, y=30)
    step = ExecutorStep(
        action=final,
        action_pipeline=ActionPipeline(
            stages=[
                ActionPipelineStage(
                    stage="submitted", action=Action(type="tap_xy", x=10, y=15),
                    coordinate_space="image", reason="model_submitted",
                ),
                ActionPipelineStage(
                    stage="dispatched", action=final,
                    coordinate_space="device", reason="driver_dispatched",
                ),
            ],
        ),
    )
    rebuilt = ExecutorStep.model_validate_json(step.model_dump_json())
    assert rebuilt.action == rebuilt.action_pipeline.stages[-1].action


def test_obsolete_snap_pipeline_fields_are_ignored():
    step = ExecutorStep.model_validate({
        "action": {"type": "tap_xy", "x": 837, "y": 360.5},
        "action_pipeline": {
            "stages": [],
            "grounding_target": {"index": 22, "bounds": [350, 191, 850, 361]},
            "snap_outcome": "snapped",
            "snap_reason": "snap_eligible_target",
        },
    })
    dumped = step.action_pipeline.model_dump()
    assert "grounding_target" not in dumped
    assert "snap_outcome" not in dumped
    assert "snap_reason" not in dumped


def test_input_safety_degraded_is_a_valid_trace_event_kind():
    event = TraceEvent(task_id="task", kind="agent_input_safety_degraded")
    assert event.kind == "agent_input_safety_degraded"


def test_tool_call_recovery_is_a_valid_trace_event_kind():
    event = TraceEvent(task_id="task", kind="agent_tool_call_recovery")
    assert event.kind == "agent_tool_call_recovery"


def test_sqlite_migrate_and_task_state_roundtrip(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    state = AgentState(
        instruction="do something",
        current_subgoal="a",
        revisable=PlanRuntime(plan=Plan(current_stage=Stage(goal="a"), assumption_roadmap=["b"])),
        step_number=0,
    )
    task = db.create_task("do something", state)
    fetched = db.get_task(task.id)
    assert fetched is not None
    assert fetched.plan == ["a"]
    assert fetched.current_subgoal == "a"
    assert fetched.state is not None
    assert fetched.state.instruction == "do something"

    # The current subgoal is already the first entry in Planner's compact plan.
    state.current_subgoal = "b"
    state.revisable.plan = Plan(current_stage=Stage(goal="b"), assumption_roadmap=["c"])
    state.step_number = 2
    db.update_task(task.id, state=state)
    again = db.get_task(task.id)
    assert again.plan == ["b"]
    assert again.current_subgoal == "b"
    assert again.step_number == 2
    db.close()


def test_task_and_trace_transaction_rolls_back_together(tmp_path: Path):
    db = Database(tmp_path / "atomic.db")
    original = AgentState(instruction="atomic")
    task = db.create_task("atomic", original)
    changed = original.model_copy(deep=True)
    changed.revisable.next_role = "reviewer"

    with pytest.raises(RuntimeError, match="interrupt"):
        with db.transaction():
            db.update_task(task.id, state=changed)
            db.add_trace(TraceEvent(task_id=task.id, kind="reviewer_decision"))
            raise RuntimeError("interrupt")

    persisted = db.get_task(task.id)
    assert persisted is not None and persisted.state is not None
    assert persisted.state.revisable.next_role == "planner"
    assert db.list_traces(task.id) == []


def test_step_and_routed_state_commit_atomically(tmp_path: Path):
    db = Database(tmp_path / "atomic.db")
    original = AgentState(instruction="remember", current_subgoal="capture")
    task = db.create_task(original.instruction, original)
    routed = original.model_copy(deep=True)
    routed.step_number = 1
    routed.revisable.next_role = "reviewer"

    with pytest.raises(TypeError):
        db.add_step_and_update_state(
            task.id, "capture", 0, {"not_json": object()}, routed,
        )

    rolled_back = db.get_task(task.id)
    assert rolled_back is not None and rolled_back.state is not None
    assert rolled_back.state.step_number == 0
    assert rolled_back.state.revisable.next_role == original.revisable.next_role
    assert db.list_steps(task.id) == []

    db.add_step_and_update_state(task.id, "capture", 0, {"ok": True}, routed)
    committed = db.get_task(task.id)
    assert committed is not None and committed.state is not None
    assert committed.state.step_number == 1
    assert committed.state.revisable.next_role == "reviewer"
    assert db.list_steps(task.id)[0]["ok"] is True
    db.close()


def test_model_router_same_default():
    r = ModelRouter(decision="sonnet", executor="sonnet")
    assert r.for_role(ModelRole.PLANNER) == r.for_role(ModelRole.EXECUTOR)
    assert r.for_role(ModelRole.REVIEWER) == r.for_role(ModelRole.EXECUTOR)

    dedicated = ModelRouter(
        decision="reviewer-model",
        executor="executor-model",
    )
    assert dedicated.for_role(ModelRole.REVIEWER) == "reviewer-model"
