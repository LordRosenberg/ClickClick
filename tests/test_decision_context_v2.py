"""Role-scoped Decision Context v2 projections for the simplified protocol."""

from __future__ import annotations

import json

import pytest

from agent.decision_context import (
    render_executor_observation_v2,
    render_planner_observation_v2,
    render_planner_task_anchor,
    render_reviewer_packet,
    reviewer_packet_payload,
    reviewer_protocol_metadata,
)
from agent.executor import build_executor_prompt
from agent.reviewer import Reviewer
from perception.input_evidence import build_interaction_state
from perception.observation import ObservationPackage
from shared.config import Settings
from shared.schemas import (
    ActiveCompletionContract,
    ActiveTaskCompletionContract,
    ActionTargetSnapshot,
    AgentState,
    CanonicalUI,
    FactEntry,
    MemoryEvent,
    ObservationMode,
    ProgressEntry,
    ReviewerVerdict,
    SubmittedActionSnapshot,
    SubgoalContractBody,
    TaskContractBody,
    TaskMemory,
    UIElement,
)


def _package() -> ObservationPackage:
    field = UIElement(
        index=0,
        role="android.widget.EditText",
        text="OpenAI",
        desc="搜索输入框",
        hint="搜索",
        bounds=[10, 20, 600, 100],
        states={"editable": True, "focusable": True, "focused": True},
        depth=1,
    )
    ui = CanonicalUI(
        app_id="com.example.search",
        activity=".TraceOnly",
        elements=[field],
        semantic_tree=[field],
    )
    return ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm=(
            'foreground_package=com.example.search\n'
            '[0] EditText raw_text="OpenAI" raw_a11y_label="搜索输入框" '
            'raw_hint="搜索" editable focused'
        ),
        image_for_llm=b"pixels",
        clean_png=b"pixels",
        annotated_png=None,
        gap_reasons=[],
        interaction_state=build_interaction_state(ui),
        actionable=True,
        index_actionable=True,
        frame_width=1080,
        frame_height=2400,
        model_image_width=486,
        model_image_height=1080,
        capture_meta={"evidence_tier": "indexed_tree_image"},
        observation_id="obs-current",
        evidence_ref="evidence-current",
    )


def _event(*, step: int, lineage: str, intent: str, index: int) -> MemoryEvent:
    return MemoryEvent(
        kind="attempt",
        step=step,
        lineage_id=lineage,
        subgoal="Open the first visible result",
        model_intent=intent,
        action_type="tap",
        submitted_action_type="tap",
        submitted_action=SubmittedActionSnapshot(type="tap", index=index),
        target=ActionTargetSnapshot(
            index=index,
            role="Button",
            raw_text=f"result-{index}",
            bounds=[10, 100, 200, 160],
        ),
        dispatch_status="dispatched",
        post_dispatch_observation="accepted",
    )


def _state() -> AgentState:
    return AgentState(
        instruction="Search for OpenAI and report the first visible title",
        plan=["Open the first result", "Report its title"],
        current_subgoal="Open the first visible result",
        task_completion_contract=ActiveTaskCompletionContract(
            contract_id="task-contract",
            revision=1,
            body=TaskContractBody(
                must_happen=["Submit the exact OpenAI query"],
                final_ui_state=["The first result is open"],
                answer=["Report the first visible title"],
                disqualifying_clauses=["Do not follow an account"],
            ),
        ),
        active_completion_contract=ActiveCompletionContract(
            contract_id="subgoal-contract",
            lineage_id="active",
            boundary_generation=1,
            target_requirement_ref="final_ui_state:1",
            body=SubgoalContractBody(
                success_conditions=["The first visible result is open"],
                disqualifying_clauses=["Do not open a different result"],
            ),
        ),
        active_timeline_lineage_ids=["active"],
        task_memory=TaskMemory(
            facts={
                "query": FactEntry(
                    value="OpenAI",
                    step=1,
                    evidence_handles=["task-action:1"],
                    packet_digest="sha256:query",
                ),
            },
            progress=[
                ProgressEntry(
                    progress_id="p1",
                    requirement_ref="must_happen:1",
                    statement="The exact OpenAI query was submitted",
                    evidence_handles=["task-action:1"],
                    packet_digest="sha256:accepted",
                    accepted_step=1,
                ),
            ],
            events=[
                _event(step=1, lineage="old", intent="Submit OpenAI", index=1),
                _event(
                    step=2,
                    lineage="active",
                    intent="Open the first visible result",
                    index=2,
                ),
            ],
        ),
    )


def _payload(rendered: str, marker: str) -> dict:
    return json.loads(rendered.split(marker, 1)[1])


def test_executor_context_is_active_subgoal_scoped_and_actionable() -> None:
    state = _state()
    _system, anchor_text, history_text, observation_text = build_executor_prompt(
        state.current_subgoal,
        _package(),
        state=state,
    )
    anchor = _payload(anchor_text, "TASK ANCHOR:\n")
    history = _payload(
        history_text,
        "ACCEPTED RESULTS AND ACTIVE ATTEMPTS:\n",
    )
    observation = _payload(observation_text, "CURRENT OBSERVATION:\n")

    assert anchor["current_subgoal"] == state.current_subgoal
    assert anchor["target_requirement_ref"] == "final_ui_state:1"
    assert anchor["active_subgoal_contract"] == {
        "success_conditions": ["The first visible result is open"],
        "disqualifying_clauses": ["Do not open a different result"],
    }
    assert "plan" not in anchor
    assert history["accepted_progress"] == [{
        "requirement_ref": "must_happen:1",
        "statement": "The exact OpenAI query was submitted",
    }]
    assert history["remembered_facts"] == {"query": "OpenAI"}
    assert history["active_action_timeline"] == [{
        "intent": "Open the first visible result",
        "submitted_action": {"type": "tap"},
        "dispatch": "dispatched",
        "post_action_capture": {"status": "available"},
        "target_at_submission": {"role": "Button", "raw_text": "result-2"},
    }]
    assert observation["focused_interaction"]["focused_editable"]["index"] == 0
    assert "[0] EditText" in observation["semantic_tree"]


def test_planner_context_is_task_level_and_locator_free() -> None:
    state = _state()
    anchor = _payload(
        render_planner_task_anchor(state, deviation="The first tap had no effect"),
        "PLANNER TASK ANCHOR:\n",
    )
    observation = _payload(
        render_planner_observation_v2(_package()),
        "CURRENT OBSERVATION:\n",
    )

    assert anchor["task_contract"]["must_happen"][0]["status"] == "accepted"
    assert anchor["task_contract"]["final_ui_state"][0]["status"] == "active"
    assert anchor["accepted_progress"] == [{
        "requirement_ref": "must_happen:1",
        "statement": "The exact OpenAI query was submitted",
    }]
    assert anchor["remembered_facts"] == {"query": "OpenAI"}
    assert anchor["plan"] == ["Open the first result", "Report its title"]
    assert anchor["last_reviewed_subgoal"] == "Open the first visible result"
    assert anchor["current_deviation_or_blocker"] == "The first tap had no effect"
    assert anchor["active_action_timeline"] == [{
        "intent": "Open the first visible result",
        "submitted_action": {"type": "tap"},
        "dispatch": "dispatched",
        "post_action_capture": {"status": "available"},
    }]
    assert "active_subgoal_contract" not in anchor
    assert "index" not in observation["focused_interaction"]["focused_editable"]
    assert all("index" not in row and "bounds" not in row for row in observation["semantic_tree"])


def test_reviewer_context_is_requirement_and_evidence_scoped() -> None:
    state = _state()
    payload, handles = reviewer_packet_payload(
        state,
        _package(),
        executor_report="The first result appears open",
        boundary_reason="executor_review_requested",
    )
    metadata = reviewer_protocol_metadata(state, terminal_review=False)

    assert payload["task_contract"]["final_ui_state"][0] == {
        "requirement_ref": "final_ui_state:1",
        "status": "active",
        "text": "The first result is open",
    }
    assert payload["accepted_progress"] == [{
        "progress_id": "p1",
        "requirement_ref": "must_happen:1",
        "statement": "The exact OpenAI query was submitted",
        "evidence_handle": "progress:1",
    }]
    assert payload["remembered_facts"] == {
        "query": {"value": "OpenAI", "evidence_handle": "fact:query"},
    }
    assert len(payload["task_action_audit"]) == 2
    assert payload["active_subgoal_boundary"] == {
        "current_subgoal": "Open the first visible result",
        "target_requirement_ref": "final_ui_state:1",
        "completion_contract": {
            "success_conditions": ["The first visible result is open"],
            "disqualifying_clauses": ["Do not open a different result"],
        },
    }
    assert payload["executor_report"]["summary"] == "The first result appears open"
    assert "plan" not in payload
    assert "review_trigger" not in payload
    assert "index" not in payload["current"]["focused_interaction"]["focused_editable"]
    assert {
        "current",
        "evidence-current",
        "task-action:1",
        "task-action:2",
        "progress:1",
        "fact:query",
        "executor-report",
        "boundary",
    }.issubset(handles)
    assert metadata["reviewer_requirement_categories"] == {
        "must_happen:1": "must_happen",
        "final_ui_state:1": "final_ui_state",
        "answer:1": "answer",
    }
    assert metadata["reviewer_dispatched_action_handles"] == [
        "task-action:1",
        "task-action:2",
    ]


def test_planner_review_trigger_cannot_inherit_active_subgoal_boundary() -> None:
    state = _state()
    payload, handles = reviewer_packet_payload(
        state,
        _package(),
        boundary_reason="planner_review_requested",
        terminal_review=True,
        review_requirement_ref="answer:1",
    )

    assert payload["review_trigger"] == {
        "requirement_ref": "answer:1",
    }
    assert "active_subgoal_boundary" not in payload
    assert "boundary" not in payload
    assert "plan" not in payload
    assert "boundary" not in handles


def test_new_reviewer_feedback_is_shared_without_retyping_semantics() -> None:
    state = _state()
    state.recovery_state.last_reviewer_verdict = ReviewerVerdict.RETRY
    state.recovery_state.last_reviewer_reason = "The selected result did not open"
    expected = {
        "verdict": "retry",
        "reason": "The selected result did not open",
    }

    planner = _payload(
        render_planner_task_anchor(state),
        "PLANNER TASK ANCHOR:\n",
    )
    executor = _payload(
        build_executor_prompt(state.current_subgoal, _package(), state=state)[1],
        "TASK ANCHOR:\n",
    )
    reviewer, _handles = reviewer_packet_payload(state, _package())

    assert planner["last_reviewer_feedback"] == expected
    assert executor["last_reviewer_feedback"] == expected
    assert reviewer["last_reviewer_feedback"] == expected


def test_missing_post_action_observation_is_not_relabelled_current() -> None:
    state = _state()
    package = _package()
    state.task_memory.events[-1].post_observation_id = package.observation_id

    payload, handles = reviewer_packet_payload(
        state,
        package,
        boundary_reason="post_action_observation_missing",
        current_evidence=False,
    )

    assert "current" not in payload
    assert payload["pre_action_observation"]["evidence_handle"] == "before-action"
    assert "current" not in handles
    assert "before-action" in handles


@pytest.mark.asyncio
async def test_reviewer_registers_all_and_only_current_observation_handles(
    monkeypatch,
) -> None:
    captured: list[list[str]] = []
    reviewer = Reviewer(model="chatgpt/gpt-5.4", settings=Settings())

    async def fake_run_decision(state, package, **kwargs):
        del state
        kwargs["dynamic_projection"](package)
        captured.append(list(kwargs["context_state"]["reviewer_current_evidence_handles"]))
        return object(), {}, {}

    monkeypatch.setattr(reviewer._runner, "run_decision", fake_run_decision)  # noqa: SLF001
    try:
        await reviewer.decide(_state(), _package())
        await reviewer.decide(
            _state(),
            _package(),
            boundary_reason="post_action_observation_missing",
        )
    finally:
        await reviewer.aclose()

    assert captured == [["current", "evidence-current"], []]


def test_reviewer_digest_and_internal_handles_stay_off_model_wire() -> None:
    rendered, digest, handles = render_reviewer_packet(
        _state(),
        _package(),
        executor_report="candidate",
    )
    payload = _payload(rendered, "REVIEW PACKET:\n")

    assert digest
    assert handles
    assert digest not in rendered
    assert "packet_digest" not in payload
    assert "evidence_handles" not in payload


def test_all_role_observations_require_exact_foreground() -> None:
    package = _package()
    package.ui.app_id = ""

    with pytest.raises(ValueError, match="foreground application"):
        render_executor_observation_v2(package)
    with pytest.raises(ValueError, match="foreground application"):
        render_planner_observation_v2(package)
    with pytest.raises(ValueError, match="foreground application"):
        reviewer_packet_payload(_state(), package)
