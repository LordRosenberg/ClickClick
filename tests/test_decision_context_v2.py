"""Role-scoped Decision Context v2 projections for the simplified protocol."""

from __future__ import annotations

import json

import pytest

from agent.decision_context import (
    historical_intermediate_tree_projection,
    render_executor_observation_v2,
    render_historical_intermediate_observation,
    render_planner_observation_v2,
    render_role_tree,
    semantic_tree_projection,
)
from agent.executor import _compound_history_messages, build_executor_prompt
from perception.input_evidence import build_interaction_state
from perception.observation import ObservationPackage
from shared.config import Settings
from shared.schemas import ActionTargetSnapshot, AgentState, CanonicalUI, ObservationMode, SubmittedActionSnapshot, UIElement


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


def _payload(rendered: str, marker: str):
    return json.loads(rendered.split(marker, 1)[1].splitlines()[0])


def _tree(rendered: str) -> str:
    return rendered.split("\nTREE:\n", 1)[1] if "\nTREE:\n" in rendered else ""


def test_role_tree_recomputes_depth_and_keeps_readable_state_channels() -> None:
    tree = [
        UIElement(index=-1, role="FrameLayout", interactable=False, children=[1], depth=4),
        UIElement(
            index=7,
            role="android.widget.Button",
            desc="Continue",
            clickable=True,
            states={"focusable": True},
            resource_id="continue_button",
            depth=9,
        ),
    ]

    executor = render_role_tree(tree, include_action_indexes=True)
    reviewer = render_role_tree(tree, include_action_indexes=False)

    assert executor == "depth=0 | [7] Button | accessibility_label=\"Continue\" | focusable"
    assert reviewer == (
        "depth=0 | Button | accessibility_label=\"Continue\" | clickable | focusable"
    )
    assert "resource_id" not in executor


def test_role_tree_uses_resource_id_only_as_unlabelled_fallback() -> None:
    labelled = UIElement(
        index=0, role="Button", text="Save", resource_id="save_button",
    )
    unlabelled = UIElement(
        index=1, role="Button", resource_id="share_button",
    )

    rendered = render_role_tree(
        [labelled, unlabelled], include_action_indexes=True,
    )

    assert "text=\"Save\"" in rendered
    assert "resource_id=\"save_button\"" not in rendered
    assert "resource_id=\"share_button\"" in rendered


def test_tree_row_grammar_is_smaller_than_repeated_structured_labels() -> None:
    tree = [
        UIElement(
            index=index,
            role="android.widget.Button",
            text=f"Result {index}",
            desc=f"Open result {index}",
            clickable=True,
            states={"focusable": True},
            depth=3,
        )
        for index in range(20)
    ]

    rows = render_role_tree(tree, include_action_indexes=True)
    structured = json.dumps(
        semantic_tree_projection(tree),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(rows) < len(structured)
    assert rows.count("depth=0") == len(tree)


def test_historical_intermediate_tree_keeps_structure_and_removes_action_noise() -> None:
    tree = [
        UIElement(
            index=-1, role="List", interactable=False, children=[1, 3], depth=0,
        ),
        UIElement(
            index=-1, role="Card", interactable=False, children=[2], depth=1,
        ),
        UIElement(
            index=4, role="TextView", text="Mango Chicken Curry",
            desc="Description A", clickable=True,
            states={"focusable": True, "focused": True}, depth=2,
        ),
        UIElement(
            index=5, role="TextView", text="Mango Chicken Curry",
            desc="Description B", clickable=True,
            states={"selected": True, "enabled": True}, depth=1,
        ),
    ]
    ui = CanonicalUI(
        app_id="com.flauschcode.broccoli",
        elements=[tree[2], tree[3]], semantic_tree=tree,
    )
    package = ObservationPackage(
        ui=ui, mode=ObservationMode.TREE_ONLY, text_for_llm="",
        image_for_llm=None, annotated_png=None, gap_reasons=[],
        observation_id="obs-detail",
    )

    rows = historical_intermediate_tree_projection(package)
    encoded = json.dumps(rows, ensure_ascii=False)
    assert encoded.count("Mango Chicken Curry") == 2
    assert "Description A" in encoded and "Description B" in encoded
    assert [row["depth"] for row in rows] == [0, 1, 2, 1]
    assert all("states" not in row and "editability" not in row for row in rows)
    assert "index" not in encoded and "clickable" not in encoded

    rendered = render_historical_intermediate_observation(package)
    assert "HISTORICAL INTERMEDIATE" in rendered
    assert '"actionable":false' in rendered
    assert "latest current state" in rendered


def test_compound_history_message_precedes_current_and_has_no_actionable_tree() -> None:
    historical = _package()
    historical.observation_id = "obs-historical"
    historical.clean_png = None
    historical.image_for_llm = None
    history_messages = _compound_history_messages(historical)
    current = {"role": "user", "content": "CURRENT ACTIONABLE STATE"}
    combined = [*history_messages, current]

    assert len(history_messages) == 1
    assert "HISTORICAL INTERMEDIATE" in history_messages[0]["content"]
    assert "[0]" not in history_messages[0]["content"]
    assert combined[-1] is current
