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
from shared.schemas import ActionTargetSnapshot, AgentState, CanonicalUI, ObservationMode, SubmittedActionSnapshot, UIElement


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


def _section(rendered: str, heading: str) -> dict[str, Any]:
    marker = heading + ":\n"
    assert marker in rendered
    body = rendered.split(marker, 1)[1].splitlines()[0]
    return json.loads(body)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        descendants = (_keys(child) for child in value.values())
        return set(value).union(*descendants)
    if isinstance(value, list):
        return set().union(*(_keys(child) for child in value), set())
    return set()


def test_executor_prompt_requires_semantic_reconciliation_and_root_cause_diagnosis():
    prompt = " ".join(render_executor_system().lower().split())
    assert "repeated attempts without relevant progress" in prompt
    assert "supported change of target or method" in prompt
    assert "mechanical acknowledgement" in prompt
    assert "report the blocker" in prompt
