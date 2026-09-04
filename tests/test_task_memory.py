"""TaskMemory tests for Reviewer-owned progress and remembered facts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.decision_context import render_executor_history_v2
from agent.task_memory import apply_reviewer_progress, record_attempt
from shared.schemas import (
    Action,
    AgentState,
    ExecutorStep,
    ExecutorStepSubmit,
    FactEntry,
    ReviewerAcceptedProgress,
    ReviewerDecision,
    ReviewerDecisionSubmit,
    ReviewerRememberedFact,
    ReviewerVerdict,
    SubmittedActionSnapshot,
)


def test_coordinate_signature_keeps_exact_image_basis() -> None:
    from agent import task_memory as task_memory_module

    first = ExecutorStep(
        action=Action(type="tap_xy", x=120, y=880),
        submitted_action_snapshot=SubmittedActionSnapshot(
            type="tap_xy", x=120, y=880, image_size=(486, 1080),
        ),
    )
    second = first.model_copy(deep=True)
    second.submitted_action_snapshot = SubmittedActionSnapshot(
        type="tap_xy", x=120, y=880, image_size=(1080, 2400),
    )
    assert task_memory_module._action_signature(first) != (
        task_memory_module._action_signature(second)
    )


def test_boundary_request_is_a_report_not_a_device_dispatch() -> None:
    state = AgentState(instruction="report the visible value")
    step = ExecutorStepSubmit(
        decision="request_review",
        summary="The requested value is visible",
    ).to_step(basis_observation_id="obs-1", evidence_refs=["current"])

    record_attempt(
        state.task_memory,
        step,
        subgoal="Expose the requested value",
        step=0,
        lineage_id="lineage-1",
    )

    event = state.task_memory.events[-1]
    assert event.action_type == "request_review"
    assert event.submitted_action is None
    assert event.dispatch_status is None
    assert event.post_dispatch_observation == "not_applicable"
    assert event.model_intent == "The requested value is visible"


def test_reviewer_progress_and_fact_commit_atomically() -> None:
    state = AgentState(instruction="remember and report")
    decision = ReviewerDecision(
        verdict=ReviewerVerdict.ACCEPT,
        accepted_progress=[ReviewerAcceptedProgress(
            requirement_ref="must_happen:1",
            statement="The source value WLAN was read",
            evidence_handles=["current"],
        )],
        remembered_facts=[ReviewerRememberedFact(
            key="source_value",
            value="WLAN",
            evidence_handles=["current"],
        )],
        reason="The source value is visible",
        evidence_handles=["current"],
        packet_digest="packet-1",
    )

    delta = apply_reviewer_progress(state, decision, step=2)

    assert len(state.task_memory.progress) == 1
    assert state.task_memory.progress[0].requirement_ref == "must_happen:1"
    assert state.task_memory.facts["source_value"].value == "WLAN"
    assert state.task_memory.facts["source_value"].source == "reviewer"
    assert delta["remembered_fact_keys"] == ["source_value"]


def test_reviewer_commit_is_replay_idempotent() -> None:
    state = AgentState(instruction="report")
    decision = ReviewerDecision(
        verdict=ReviewerVerdict.ACCEPT,
        accepted_progress=[ReviewerAcceptedProgress(
            requirement_ref="final_ui_state:1",
            statement="The result is visible",
            evidence_handles=["current"],
        )],
        remembered_facts=[ReviewerRememberedFact(
            key="visible_value",
            value="WLAN",
            evidence_handles=["current"],
        )],
        reason="visible",
        evidence_handles=["current"],
        packet_digest="packet-stable",
    )

    first = apply_reviewer_progress(state, decision, step=3)
    second = apply_reviewer_progress(state, decision, step=3)

    assert first == second
    assert len(state.task_memory.progress) == 1
    assert len([event for event in state.task_memory.events if event.kind == "fact"]) == 1


def test_duplicate_fact_keys_are_rejected_before_memory_mutation() -> None:
    with pytest.raises(ValidationError, match="remembered fact keys must be unique"):
        ReviewerDecisionSubmit(
            verdict="accept",
            remembered_facts=[
                ReviewerRememberedFact(
                    key="value", value="A", evidence_handles=["current"],
                ),
                ReviewerRememberedFact(
                    key="value", value="B", evidence_handles=["current"],
                ),
            ],
            reason="visible",
        )


@pytest.mark.parametrize("verdict", ["accept", "retry", "replan", "done", "blocked"])
def test_every_reviewer_verdict_requires_delivered_evidence(verdict: str) -> None:
    payload = {"verdict": verdict, "reason": "reviewed"}
    with pytest.raises(ValidationError, match="requires evidence"):
        ReviewerDecisionSubmit.model_validate(payload)


def test_executor_history_uses_direct_accepted_names() -> None:
    state = AgentState(instruction="use the remembered value")
    state.task_memory.facts["source_value"] = FactEntry(
        value="WLAN",
        evidence_handles=["current"],
        packet_digest="packet-1",
    )
    rendered = render_executor_history_v2(state)
    assert "remembered_facts" in rendered
    assert "accepted_memory" not in rendered
    assert rendered.startswith("ACCEPTED RESULTS AND ACTIVE ATTEMPTS:\n")
