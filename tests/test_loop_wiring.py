"""Focused wiring tests for the Reviewer -> Planner -> Executor loop."""

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    Action,
    ExecutorStepSubmit,
    SubgoalContractBody,
    ObservationMode,
    PlannerDecision,
    PlannerMode,
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


class CapturingExecutor(FakeExecutor):
    def __init__(self, actions):
        super().__init__(actions)
        self.seen_packages = []

    async def act_once(self, subgoal, package=None, prior_result="", **kwargs):
        self.seen_packages.append(package)
        return await super().act_once(subgoal, package, prior_result, **kwargs)


def execute(subgoal: str, remaining: list[str] | None = None) -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=subgoal,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{subgoal} is established"],
        ),
        plan=[subgoal, *(remaining or [])],
        target_requirement_ref="final_ui_state:1",
    )


def done() -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.DONE,
        reason="task scope is satisfied",
        evidence_handles=["current"],
        packet_digest="packet-done"
    )


def continue_review(reason: str = "more work remains") -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.REPLAN,
        reason=reason,
        evidence_handles=["current"],
        packet_digest="packet-continue",
    )


def stack(tmp_path: Path, driver, planner, reviewer, executor):
    db = Database(tmp_path / "loop.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    orch = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        driver=driver,
        max_steps=10,
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    return db, orch


@pytest.mark.asyncio
async def test_real_observation_package_reaches_all_three_roles(tmp_path: Path):
    planner = BoundPlanner([execute("tap play")])
    reviewer = BoundReviewer([done()], task_scope=fake_task_scope())
    executor = CapturingExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="tap play is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, FixtureDriver(), planner, reviewer, executor)

    task_id = await orch.start_task("tap play")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    packages = planner.seen_packages + executor.seen_packages + reviewer.seen_packages
    assert len(packages) == 3
    assert all(package.mode == ObservationMode.TREE_PLUS_IMAGE for package in packages)
    assert all(package.clean_png and package.image_for_llm for package in packages)
    assert all("Play" in package.text_for_llm for package in packages)
    assert planner.seen_packages[0].observation_id == executor.seen_packages[0].observation_id
    assert reviewer.seen_packages[0].observation_id == executor.seen_packages[0].observation_id


@pytest.mark.asyncio
async def test_ordinary_executor_ticks_do_not_reinvoke_cognitive_roles(tmp_path: Path):
    planner = BoundPlanner([execute("wait then finish")])
    reviewer = BoundReviewer([done()], task_scope=fake_task_scope())
    executor = CapturingExecutor([
        (Action(type="sleep", duration_ms=1), False),
        ExecutorStepSubmit(
            decision="request_review", summary="wait then finish is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, FixtureDriver(), planner, reviewer, executor)

    task_id = await orch.start_task("wait then finish")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert len(planner.seen_states) == 1
    assert len(reviewer.seen_boundary_states) == 1
    assert len(executor.seen_packages) == 2
    assert [len(log) for log in executor.seen_logs] == [0, 1]


@pytest.mark.asyncio
async def test_reviewer_nonterminal_verdict_returns_to_planner(tmp_path: Path):
    planner = BoundPlanner([execute("first"), execute("replacement")])
    reviewer = BoundReviewer(
        [continue_review("first result is insufficient"), done()],
        task_scope=fake_task_scope(),
    )
    executor = CapturingExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="first is complete",
        ).to_step(),
        ExecutorStepSubmit(
            decision="request_review", summary="replacement is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, FixtureDriver(), planner, reviewer, executor)

    task_id = await orch.start_task("allow reviewer correction")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert planner.seen_deviations == ["", "first result is insufficient"]
    assert len(reviewer.seen_boundary_states) == 2
    assert len(executor.seen_packages) == 2


@pytest.mark.asyncio
async def test_executor_trace_records_observation_cost(tmp_path: Path):
    planner = BoundPlanner([execute("finish")])
    reviewer = BoundReviewer([done()], task_scope=fake_task_scope())
    executor = CapturingExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="finish is complete",
        ).to_step(),
    ])
    db, orch = stack(tmp_path, FixtureDriver(), planner, reviewer, executor)

    task_id = await orch.start_task("finish")
    decisions = [
        trace for trace in db.list_traces(task_id)
        if trace.kind in {"planner_decision", "reviewer_decision"}
    ]

    assert len(decisions) == 2
    assert all(trace.payload["estimated_tokens"] > 0 for trace in decisions)
    assert all(trace.payload["observation_id"] for trace in decisions)
