"""Validation coverage for focused-role orchestration contracts."""

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    Action,
    ActionReceipt,
    ActionResult,
    ExecutorStepSubmit,
    SubgoalContractBody,
    EffectOutcome,
    ObservationMode,
    PlannerDecision,
    PlannerMode,
    ReviewerAcceptedProgress,
    ReviewerDecision,
    ReviewerVerdict,
    TaskStatus,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer, fake_task_scope


class BoundPlanner(FakePlanner):
    async def decide(self, state, package, **kwargs):
        decision, refs, observation_refs = await super().decide(state, package, **kwargs)
        refs["active_package"] = package
        observation_refs["observation_id"] = package.observation_id
        return decision, refs, observation_refs


class BoundReviewer(FakeReviewer):
    async def decide(self, state, package, **kwargs):
        decision, refs, observation_refs = await super().decide(state, package, **kwargs)
        refs["active_package"] = package
        observation_refs["observation_id"] = package.observation_id
        return decision, refs, observation_refs


def execute(name: str) -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=name,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{name} complete"],
        ),
        plan=[name],
        target_requirement_ref="final_ui_state:1",
    )


def review(verdict: ReviewerVerdict, reason: str):
    return ReviewerDecision(
        verdict=verdict,
        reason=reason,
        accepted_progress=(
            [ReviewerAcceptedProgress(
                requirement_ref="final_ui_state:1",
                statement="The current subgoal is established",
                evidence_handles=["current"],
            )]
            if verdict == ReviewerVerdict.ACCEPT else []
        ),
        evidence_handles=["current"] if verdict in {
            ReviewerVerdict.ACCEPT,
            ReviewerVerdict.DONE,
            ReviewerVerdict.BLOCKED,
        } else [],
        packet_digest=f"packet-{verdict.value}",
    )


def stack(tmp_path: Path, planner, reviewer, executor, **kwargs):
    db = Database(tmp_path / "validation.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    orch = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        driver=FixtureDriver(),
        max_steps=kwargs.get("max_steps", 20),
        max_role_invocations=kwargs.get("max_role_invocations", 200),
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    return db, orch


@pytest.mark.asyncio
async def test_current_visual_evidence_is_not_gap_gated(tmp_path: Path):
    planner = BoundPlanner([execute("inspect")])
    reviewer = BoundReviewer(
        [review(ReviewerVerdict.DONE, "visible")],
        task_scope=fake_task_scope(),
    )
    executor = FakeExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="inspect is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, planner, reviewer, executor)

    task_id = await orch.start_task("inspect")
    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    for package in planner.seen_packages + reviewer.seen_packages:
        assert package.gap_reasons == []
        assert package.clean_png is not None
        assert package.image_for_llm is not None
        assert package.mode == ObservationMode.TREE_PLUS_IMAGE


@pytest.mark.asyncio
async def test_request_review_routes_to_reviewer_then_planner(tmp_path: Path):
    planner = BoundPlanner([execute("attempt"), execute("alternative")])
    reviewer = BoundReviewer(
        [
            review(
                ReviewerVerdict.ACCEPT,
                "three attempts were reviewed before replanning",
            ),
            review(ReviewerVerdict.DONE, "alternative worked"),
        ],
        task_scope=fake_task_scope(),
    )
    executor = FakeExecutor([
        Action(type="sleep", duration_ms=1),
        Action(type="sleep", duration_ms=1),
        Action(type="sleep", duration_ms=1),
        ExecutorStepSubmit(
            decision="request_review", summary="three attempts were reviewed",
        ).to_step(),
        Action(type="sleep", duration_ms=1),
        ExecutorStepSubmit(
            decision="request_review", summary="alternative worked",
        ).to_step(),
    ], results=[
        ActionResult(
            success=True,
            receipt=ActionReceipt(
                dispatch_succeeded=True,
                effect_outcome=EffectOutcome.CONFIRMED,
                observation_accepted=True,
            ),
        )
        for _ in range(3)
    ])
    db, orch = stack(
        tmp_path,
        planner,
        reviewer,
        executor,
    )

    task_id = await orch.start_task("recover after repeated executor attempts")
    task = db.get_task(task_id)

    assert task.status == TaskStatus.SUCCEEDED
    assert reviewer.seen_boundary_reasons == [
        "executor_review_requested",
        "executor_review_requested",
    ]
    assert planner.seen_deviations == [
        "",
        "three attempts were reviewed before replanning",
    ]
    attempts = [
        event for event in task.state.task_memory.events
        if event.kind == "attempt"
    ]
    assert len(attempts) == 6
    assert [len(log) for log in executor.seen_logs] == [0, 1, 2, 3, 0, 1]


@pytest.mark.asyncio
async def test_reviewer_rejection_reissues_work_without_false_done(tmp_path: Path):
    planner = BoundPlanner([execute("first attempt"), execute("corrected attempt")])
    reviewer = BoundReviewer(
        [
            review(ReviewerVerdict.REPLAN, "completion lacks evidence"),
            review(ReviewerVerdict.DONE, "evidence now sufficient"),
        ],
        task_scope=fake_task_scope(),
    )
    executor = FakeExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="first attempt may be complete",
        ).to_step(),
        ExecutorStepSubmit(
            decision="request_review", summary="corrected attempt is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, planner, reviewer, executor)

    task_id = await orch.start_task("require evidence")
    task = db.get_task(task_id)

    assert task.status == TaskStatus.SUCCEEDED
    assert len(planner.seen_states) == 2
    assert len(reviewer.seen_boundary_states) == 2
    assert task.state.step_number == 2


@pytest.mark.asyncio
async def test_canonical_action_slice_changes_only_after_new_planner_decision(tmp_path: Path):
    planner = BoundPlanner([execute("first"), execute("second")])
    reviewer = BoundReviewer(
        [
            review(ReviewerVerdict.ACCEPT, "first accepted"),
            review(ReviewerVerdict.DONE, "all done"),
        ],
        task_scope=fake_task_scope(),
    )
    executor = FakeExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="first is complete",
        ).to_step(),
        ExecutorStepSubmit(
            decision="request_review", summary="second is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, planner, reviewer, executor)

    task_id = await orch.start_task("two boundaries")
    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert [len(log) for log in executor.seen_logs] == [0, 0]
    assert reviewer.seen_terminal_reviews == [False, False]


@pytest.mark.asyncio
async def test_global_role_invocation_limit_fails_closed(tmp_path: Path):
    planner = BoundPlanner([execute("never complete")])
    reviewer = BoundReviewer([], task_scope=fake_task_scope())
    executor = FakeExecutor([Action(type="sleep", duration_ms=1)] * 10)
    db, orch = stack(
        tmp_path,
        planner,
        reviewer,
        executor,
        max_role_invocations=3,
    )

    task_id = await orch.start_task("exhaust role calls")
    task = db.get_task(task_id)

    assert task.status == TaskStatus.FAILED
    assert task.failure_reason == "role_invocation_limit_exhausted:3/3"


def test_model_switching_keeps_shared_decision_model():
    from shared.config import Settings
    from shared.model_router import ModelRole, ModelRouter

    first = ModelRouter.from_settings(Settings(default_model="kimi-k3"))
    second = ModelRouter.from_settings(Settings(default_model="glm-4-plus"))

    assert first.for_role(ModelRole.PLANNER) == "kimi-k3"
    assert first.for_role(ModelRole.REVIEWER) == "kimi-k3"
    assert second.for_role(ModelRole.PLANNER) == "glm-4-plus"
    assert second.for_role(ModelRole.REVIEWER) == "glm-4-plus"
