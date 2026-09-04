"""Decision Context v2 prompt structure and cache-boundary tests."""

from __future__ import annotations

import json
from typing import Any

from agent.executor import build_executor_prompt
from agent.prompt_measurement import build_role_component_report, measure_text_component
from agent.prompts import render_executor_system
from perception.input_evidence import build_interaction_state
from perception.normalizer import render_semantic_tree
from perception.observation import ObservationPackage
from shared.schemas import (
    ActiveCompletionContract,
    ActiveTaskCompletionContract,
    ActionTargetSnapshot,
    AgentState,
    CanonicalUI,
    SubgoalContractBody,
    FactEntry,
    MemoryEvent,
    ObservationMode,
    ProgressEntry,
    SubmittedActionSnapshot,
    TaskContractBody,
    TaskMemory,
    UIElement,
)


def _pkg(
    *,
    app_id: str = "com.example.search",
    raw_text: str = "old query",
    raw_label: str = "Search query",
    raw_hint: str = "Enter a place",
    extra_nodes: int = 0,
) -> ObservationPackage:
    root = UIElement(
        index=-1,
        role="android.view.ViewGroup",
        text="Search page",
        depth=0,
        interactable=False,
        children=[1],
    )
    field = UIElement(
        index=4,
        role="android.widget.EditText",
        text=raw_text,
        desc=raw_label,
        hint=raw_hint,
        bounds=[10, 20, 410, 100],
        states={"focused": True, "focusable": True, "editable": True},
        depth=1,
        resource_id="query_field",
    )
    extras = [
        UIElement(
            index=-1,
            role="android.widget.TextView",
            text=f"result {number}",
            depth=1,
            interactable=False,
        )
        for number in range(extra_nodes)
    ]
    semantic_tree = [root, field, *extras]
    ui = CanonicalUI(
        app_id=app_id,
        activity=".SearchActivity",
        elements=[field],
        semantic_tree=semantic_tree,
    )
    tree_text = render_semantic_tree(ui)
    return ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=tree_text,
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        interaction_state=build_interaction_state(ui),
    )


def _state(*, instruction: str = "Replace the old query with Eiffel Tower") -> AgentState:
    lineage = "semantic-query-lineage"
    return AgentState(
        instruction=instruction,
        current_subgoal="Replace the focused query with Eiffel Tower",
        task_completion_contract=ActiveTaskCompletionContract(
            body=TaskContractBody(
                must_happen=["Replace the query with Eiffel Tower"],
                final_ui_state=["The Eiffel Tower result is visible"],
            ),
        ),
        active_completion_contract=ActiveCompletionContract(
            target_requirement_ref="must_happen:1",
            body=SubgoalContractBody(
                success_conditions=["The focused query is Eiffel Tower"],
            ),
        ),
        active_timeline_lineage_ids=[lineage],
        task_memory=TaskMemory(
            facts={"destination": FactEntry(value="Eiffel Tower", step=1)},
            progress=[ProgressEntry(
                progress_id="progress-1",
                statement="Open search",
                evidence_handles=["current"],
                packet_digest="packet-1",
            )],
            events=[
                MemoryEvent(
                    kind="attempt",
                    step=2,
                    lineage_id=lineage,
                    model_intent="Focus the query field",
                    action_type="tap",
                    action_signature="tap:index:4",
                    intent_sha256="a" * 64,
                    submitted_action_type="tap",
                    submitted_action=SubmittedActionSnapshot(type="tap", index=4),
                    target=ActionTargetSnapshot(
                        index=4,
                        role="android.widget.EditText",
                        raw_text="old query",
                        raw_a11y_label="Search query",
                        raw_hint="Enter a place",
                        bounds=[10, 20, 410, 100],
                        editability="editable",
                        focused=True,
                    ),
                    dispatch_status="dispatched",
                    post_dispatch_observation="accepted",
                ),
                MemoryEvent(
                    kind="attempt",
                    step=3,
                    lineage_id=lineage,
                    model_intent="Replace the query with the requested value",
                    action_type="replace_text",
                    action_signature="replace_text:redacted",
                    intent_sha256="b" * 64,
                    submitted_action_type="replace_text",
                    submitted_action=SubmittedActionSnapshot(
                        type="replace_text", text="Eiffel Tower"
                    ),
                    dispatch_status="dispatched",
                    post_dispatch_observation="accepted",
                ),
            ],
        ),
    )


def _section(rendered: str, heading: str) -> dict[str, Any]:
    marker = heading + ":\n"
    assert marker in rendered
    body = rendered.split(marker, 1)[1].split("\n\n", 1)[0]
    return json.loads(body)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        descendants = (_keys(child) for child in value.values())
        return set(value).union(*descendants)
    if isinstance(value, list):
        return set().union(*(_keys(child) for child in value), set())
    return set()








def test_executor_task_anchor_contains_only_atomic_subgoal_contract():
    state = _state(instruction="Keep the exact requested query")
    system, task_anchor, history, _ = build_executor_prompt(
        state.current_subgoal, _pkg(), state=state
    )
    task = _section(task_anchor, "TASK ANCHOR")

    assert system == render_executor_system()
    assert task["original_instruction"] == state.instruction
    assert task["current_subgoal"] == state.current_subgoal
    assert task["target_requirement_ref"] == "must_happen:1"
    assert task["active_subgoal_contract"] == {
        "success_conditions": ["The focused query is Eiffel Tower"],
        "disqualifying_clauses": [],
    }
    assert state.instruction not in system
    assert _section(history, "ACCEPTED RESULTS AND ACTIVE ATTEMPTS")




def test_unified_timeline_preserves_intent_and_mechanics_without_semantic_labels():
    state = _state()
    _, _, history_text, _ = build_executor_prompt(
        state.current_subgoal, _pkg(), state=state
    )
    rows = _section(
        history_text, "ACCEPTED RESULTS AND ACTIVE ATTEMPTS",
    )["active_action_timeline"]

    assert rows[0]["intent"] == "Focus the query field"
    assert rows[0]["submitted_action"]["type"] == "tap"
    assert rows[0]["target_at_submission"]["raw_text"] == "old query"
    assert "index" not in rows[0]["submitted_action"]
    assert "index" not in rows[0]["target_at_submission"]
    assert "bounds" not in rows[0]["target_at_submission"]
    assert rows[1]["submitted_action"]["type"] == "replace_text"
    assert rows[1]["dispatch"] == "dispatched"
    assert rows[1]["post_action_capture"] == {"status": "available"}
    forbidden = {
        "success",
        "effective",
        "same_semantic_action",
        "goal_progress",
        "minor_change",
        "phase_change",
    }
    assert forbidden.isdisjoint(_keys(rows))














def test_executor_prompt_requires_semantic_reconciliation_and_root_cause_diagnosis():
    prompt = " ".join(render_executor_system().lower().split())
    assert "before `act`, reconcile the missing effect" in prompt
    assert "semantic timeline" in prompt
    assert "repeated same-purpose actions without relevant progress" in prompt
    assert "reconsider target, grounding, feasibility, or method" in prompt
    assert "dispatch and capture do not prove the intended effect" in prompt
