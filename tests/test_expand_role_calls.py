"""Role-call projection keeps every persisted Planner/Reviewer/Executor call."""

from __future__ import annotations

from pathlib import Path

from control_api.services import ObservabilityQueries, expand_role_calls
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import TraceEvent


def test_expand_folded_steps_uses_unique_occurrence_keys() -> None:
    calls = expand_role_calls([
        {
            "step_seq": 0,
            "observation": {"som_ref": "s0"},
            "reviewer": {"kind": "reviewer_decision"},
            "planner": {"kind": "planner_decision"},
            "executor": {"kind": "executor_tick"},
            "loop": None,
        },
    ])

    assert [call["call_key"] for call in calls] == [
        "0:reviewer:1",
        "0:planner:1",
        "0:executor:1",
    ]
    assert [call["role"] for call in calls] == [
        "reviewer", "planner", "executor",
    ]


def test_timeline_preserves_repeated_roles_at_same_action_step(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "console.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    task = db.create_task("exercise zero-action decision routing")
    events = [
        ("planner_decision", "p1", "planner", "obs-1"),
        ("reviewer_decision", "r1", "reviewer", "obs-1"),
        ("planner_decision", "p2", "planner", "obs-2"),
        ("reviewer_decision", "r2", "reviewer", "obs-2"),
        ("executor_tick", "e1", "executor", "obs-2"),
    ]
    for kind, invocation_id, role, observation_id in events:
        db.add_trace(TraceEvent(
            task_id=task.id,
            step_seq=0,
            kind=kind,  # type: ignore[arg-type]
            message=invocation_id,
            payload={
                "observation_id": observation_id,
                "agent_rounds": [{"round_id": invocation_id}],
                "role": role,
            },
        ))

    timeline = ObservabilityQueries(db, artifacts).timeline(task.id)

    assert [call["call_key"] for call in timeline["calls"]] == [
        "0:planner:1",
        "0:reviewer:1",
        "0:planner:2",
        "0:reviewer:2",
        "0:executor:1",
    ]
    assert [call["role"] for call in timeline["calls"]] == [
        "planner", "reviewer", "planner", "reviewer", "executor",
    ]
    assert timeline["metrics"]["role_calls"] == {
        "planner": 2,
        "reviewer": 2,
        "executor": 1,
    }
    assert len({call["call_key"] for call in timeline["calls"]}) == 5
