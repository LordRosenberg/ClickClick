"""Focused routing tests for the Planner/Executor/Reviewer runtime."""

from __future__ import annotations

import pytest

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    AgentState,
    ExecutorStepSubmit,
    PlannerDecision,
    PlannerMode,
    ReviewerAcceptedProgress,
    ReviewerDecision,
    ReviewerVerdict,
    SubgoalContractBody,
    TaskContractBody,
    TaskStatus,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer


def _execute(name: str, target: str = "must_happen:1") -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=name,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{name} is established"],
        ),
        plan=[name],
        target_requirement_ref=target,
    )


def _boundary(decision: str, summary: str = "boundary ready"):
    return ExecutorStepSubmit(decision=decision, summary=summary).to_step()


def _review(
    verdict: ReviewerVerdict,
    *,
    packet: str,
    reason: str = "reviewed",
    progress: list[ReviewerAcceptedProgress] | None = None,
) -> ReviewerDecision:
    return ReviewerDecision(
        verdict=verdict,
        reason=reason,
        accepted_progress=list(progress or []),
        evidence_handles=["current"],
        packet_digest=packet,
    )


def _progress(ref: str, statement: str) -> ReviewerAcceptedProgress:
    return ReviewerAcceptedProgress(
        requirement_ref=ref,
        statement=statement,
        evidence_handles=["current"],
    )


def _stack(tmp_path, planner, reviewer, executor):
    db = Database(tmp_path / "runtime.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        driver=FixtureDriver(),
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    return db, runtime


@pytest.mark.asyncio
async def test_accept_advances_to_planner_and_preserves_progress(tmp_path) -> None:
    scope = TaskContractBody(
        must_happen=["first result", "second result"],
    )
    planner = FakePlanner([
        _execute("first", "must_happen:1"),
        _execute("second", "must_happen:2"),
    ])
    reviewer = FakeReviewer([
        _review(
            ReviewerVerdict.ACCEPT,
            packet="p1",
            progress=[_progress("must_happen:1", "first result established")],
        ),
        _review(
            ReviewerVerdict.DONE,
            packet="p2",
            progress=[_progress("must_happen:2", "second result established")],
        ),
    ], task_scope=scope)
    executor = FakeExecutor([
        _boundary("request_review", "first established"),
        _boundary("request_review", "second established"),
    ])
    db, runtime = _stack(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("establish both results")

    task = db.get_task(task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert [p.requirement_ref for p in task.state.task_memory.progress] == [
        "must_happen:1", "must_happen:2",
    ]
    assert len(planner.seen_states) == 2


@pytest.mark.asyncio
async def test_retry_reuses_same_subgoal_without_planner(tmp_path) -> None:
    planner = FakePlanner([_execute("show result")])
    reviewer = FakeReviewer([
        _review(ReviewerVerdict.RETRY, packet="retry", reason="wrong target"),
        _review(
            ReviewerVerdict.DONE,
            packet="done",
            progress=[_progress("must_happen:1", "result established")],
        ),
    ], task_scope=TaskContractBody(must_happen=["result established"]))
    executor = FakeExecutor([
        _boundary("request_review", "wrong target was opened"),
        _boundary("request_review", "correct result is visible"),
    ])
    db, runtime = _stack(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("show result")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert len(planner.seen_states) == 1
    assert [len(rows) for rows in executor.seen_logs] == [0, 1]


@pytest.mark.asyncio
async def test_executor_replan_is_reviewed_before_planner(tmp_path) -> None:
    planner = FakePlanner([_execute("route A"), _execute("route B")])
    reviewer = FakeReviewer([
        _review(ReviewerVerdict.REPLAN, packet="replan", reason="route A unavailable"),
        _review(
            ReviewerVerdict.DONE,
            packet="done",
            progress=[_progress("must_happen:1", "route B established result")],
        ),
    ], task_scope=TaskContractBody(must_happen=["result established"]))
    executor = FakeExecutor([
        _boundary("request_replan", "route A cannot continue"),
        _boundary("request_review", "route B established the result"),
    ])
    db, runtime = _stack(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("establish result")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert reviewer.seen_boundary_reasons == [
        "executor_replan_requested", "executor_review_requested",
    ]
    assert planner.seen_deviations[1] == "route A unavailable"


@pytest.mark.asyncio
async def test_planner_review_after_replan_closes_stale_active_binding(tmp_path) -> None:
    planner = FakePlanner([
        _execute("obsolete route"),
        PlannerDecision(
            mode=PlannerMode.REVIEW,
            review_requirement_ref="must_happen:1",
        ),
    ])
    reviewer = FakeReviewer([
        _review(ReviewerVerdict.REPLAN, packet="replan", reason="route obsolete"),
        _review(
            ReviewerVerdict.DONE,
            packet="done",
            progress=[_progress("must_happen:1", "result already established")],
        ),
    ], task_scope=TaskContractBody(must_happen=["result established"]))
    executor = FakeExecutor([
        _boundary("request_replan", "current route is obsolete"),
    ])
    db, runtime = _stack(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("establish result")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    planner_review_state = reviewer.seen_boundary_states[1]
    assert planner_review_state.active_completion_contract is None
    assert planner_review_state.current_subgoal == ""
    assert planner_review_state.task_memory.events


@pytest.mark.asyncio
async def test_blocked_terminates_without_another_planner(tmp_path) -> None:
    planner = FakePlanner([_execute("unsafe route")])
    reviewer = FakeReviewer([
        _review(
            ReviewerVerdict.BLOCKED,
            packet="blocked",
            reason="continuation would violate the task prohibition",
        ),
    ], task_scope=TaskContractBody(must_happen=["result established"]))
    executor = FakeExecutor([
        _boundary("request_replan", "only unsafe continuation remains"),
    ])
    db, runtime = _stack(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("establish result safely")

    task = db.get_task(task_id)
    assert task.status == TaskStatus.FAILED
    assert "prohibition" in task.failure_reason
    assert len(planner.seen_states) == 1


def test_cancel_path_has_no_removed_candidate_lifecycle(tmp_path) -> None:
    db = Database(tmp_path / "runtime.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: FakePlanner([]),
        lambda: FakeReviewer([], task_scope=TaskContractBody()),
        lambda: FakeExecutor([]),
        driver=FixtureDriver(),
        artifacts=artifacts,
    )
    state = AgentState(instruction="cancel me")
    task = db.create_task(state.instruction, state)

    status = runtime._cancel(task.id, state, mode="soft")  # noqa: SLF001

    assert status == TaskStatus.CANCELLED
    assert db.get_task(task.id).status == TaskStatus.CANCELLED
