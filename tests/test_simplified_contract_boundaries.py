"""Focused tests for the simplified task contract and Executor boundaries."""

import pytest
from pydantic import ValidationError

from agent.decision_context import task_contract_projection
from agent.orchestrator import Orchestrator
from agent.session import tools_for_role
from agent.traces import TraceWriter
from agent.task_memory import apply_reviewer_progress
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    ActiveTaskCompletionContract,
    AgentState,
    ExecutorDecisionKind,
    ExecutorStepSubmit,
    PlannerDecision,
    PlannerMode,
    ReviewerDecision,
    ReviewerRememberedFact,
    ReviewerVerdict,
    SubgoalContractBody,
    TaskContractBody,
    TaskStatus,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer


def test_task_contract_projects_stable_requirement_refs() -> None:
    state = AgentState(
        instruction="search and report",
        task_completion_contract=ActiveTaskCompletionContract(
            body=TaskContractBody(
                must_happen=["Search for exact query"],
                final_ui_state=["Results are visible"],
                answer=["Report the first title"],
                disqualifying_clauses=["Do not follow any account"],
            )
        ),
    )
    assert task_contract_projection(state) == {
        "must_happen": [{
            "requirement_ref": "must_happen:1",
            "status": "pending",
            "text": "Search for exact query",
        }],
        "final_ui_state": [{
            "requirement_ref": "final_ui_state:1",
            "status": "pending",
            "text": "Results are visible",
        }],
        "answer": [{
            "requirement_ref": "answer:1",
            "status": "pending",
            "text": "Report the first title",
        }],
        "disqualifying_clauses": ["Do not follow any account"],
    }


@pytest.mark.parametrize("decision", ["request_review", "request_replan"])
def test_executor_boundary_decisions_forbid_device_action(decision: str) -> None:
    parsed = ExecutorStepSubmit.model_validate({
        "decision": decision,
        "summary": "Current evidence establishes the requested value",
    })
    assert parsed.action is None
    assert parsed.to_step().decision.value == decision
    with pytest.raises(ValidationError):
        ExecutorStepSubmit.model_validate({
            "decision": decision,
            "summary": "boundary",
            "action": {"type": "tap", "index": 1},
        })


def test_executor_act_requires_one_device_action() -> None:
    with pytest.raises(ValidationError):
        ExecutorStepSubmit.model_validate({"decision": "act", "summary": "tap"})
    parsed = ExecutorStepSubmit.model_validate({
        "decision": "act",
        "summary": "Open the exact result",
        "action": {"type": "tap", "index": 1},
    })
    assert parsed.decision == ExecutorDecisionKind.ACT


def test_tool_protocol_has_no_old_terminal_actions_or_need_evidence() -> None:
    executor_schema = next(
        tool["function"]["parameters"]
        for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    reviewer_schema = next(
        tool["function"]["parameters"]
        for tool in tools_for_role("reviewer")
        if tool["function"]["name"] == "submit_reviewer_decision"
    )
    serialized_executor = str(executor_schema)
    serialized_reviewer = str(reviewer_schema)
    for old_action in ("complete", "remember", "claim", "replan"):
        assert f"'{old_action}'" not in serialized_executor
    assert "need_evidence" not in serialized_reviewer


def test_reviewer_is_only_source_of_runtime_remembered_fact() -> None:
    state = AgentState(instruction="remember the visible value")
    decision = ReviewerDecision(
        verdict=ReviewerVerdict.ACCEPT,
        reason="value is visible",
        remembered_facts=[ReviewerRememberedFact(
            key="visible_value",
            value="WLAN",
            evidence_handles=["current"],
        )],
        evidence_handles=["current"],
        packet_digest="packet-1",
    )
    delta = apply_reviewer_progress(state, decision, 1)
    assert delta["remembered_fact_keys"] == ["visible_value"]
    assert state.task_memory.facts["visible_value"].value == "WLAN"
    assert state.task_memory.facts["visible_value"].source == "reviewer"


@pytest.mark.asyncio
async def test_retry_returns_to_same_executor_without_planner_loop(tmp_path) -> None:
    planner = FakePlanner([PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal="Expose the requested value",
        completion_contract=SubgoalContractBody(
            success_conditions=["The requested value is visible"],
        ),
        plan=["Expose the value", "Report it"],
        target_requirement_ref="final_ui_state:1",
    )])
    reviewer = FakeReviewer(
        [
            ReviewerDecision(
                verdict=ReviewerVerdict.RETRY,
                reason="The value is not yet visible",
                packet_digest="retry-packet",
            ),
            ReviewerDecision(
                verdict=ReviewerVerdict.DONE,
                reason="The value is now visible",
                evidence_handles=["current"],
                packet_digest="done-packet",
            ),
        ],
        task_scope=TaskContractBody(final_ui_state=["The requested value is visible"]),
    )
    executor = FakeExecutor([
        ExecutorStepSubmit(
            decision="request_review",
            summary="No requested value is visible yet",
        ).to_step(),
        ExecutorStepSubmit(
            decision="request_review",
            summary="The requested value WLAN is visible",
        ).to_step(),
    ])
    db = Database(tmp_path / "runtime.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        driver=FixtureDriver(),
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    task_id = await orchestrator.start_task("Report the visible value")
    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert len(planner.seen_states) == 1
    assert reviewer.seen_boundary_reasons == [
        "executor_review_requested",
        "executor_review_requested",
    ]
    assert [len(items) for items in executor.seen_logs] == [0, 1]
