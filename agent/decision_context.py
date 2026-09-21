"""Deterministic Decision Context v2 role projections.

This module selects and orders exact runtime/model-authored facts. It never
classifies UI meaning, action equivalence, progress, effectiveness, or task
completion.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

from perception.input_evidence import build_interaction_state, editability_evidence, element_identity, interaction_envelope
from perception.observation import ObservationPackage
from perception.observation import has_model_visible_tree_content
from shared.schemas import CanonicalUI, UIElement

INTERACTION_ACK_MODEL_VISIBLE_ENV = "CLICKCLICK_INTERACTION_ACK_MODEL_VISIBLE"
SURFACE_BOUNDS_ENV = "CLICKCLICK_SURFACE_BOUNDS_ENABLED"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _without_empty(value: Any) -> Any:
    """Recursively omit empty optional values from a model-facing object."""
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            compact = _without_empty(item)
            if compact not in (None, "", [], {}) or (key == "raw_text" and item == ""):
                compacted[key] = compact
        return compacted
    if isinstance(value, list):
        return [
            compact
            for item in value
            if (compact := _without_empty(item)) not in (None, "", [], {})
        ]
    return value


def _observation_capabilities(package: ObservationPackage) -> dict[str, Any]:
    image_available = bool(package.image_for_llm)
    tree_semantics_available = has_model_visible_tree_content(package.ui)
    coordinate_available = bool(
        package.actionable
        and image_available
        and package.model_image_width > 0
        and package.model_image_height > 0
    )
    evidence_tier = str(package.capture_meta.get("evidence_tier") or "")
    if not evidence_tier:
        if package.mode.value == "image-only":
            evidence_tier = "image_only" if image_available else "unavailable"
        elif tree_semantics_available and image_available:
            evidence_tier = (
                "indexed_tree_image"
                if package.index_actionable else "nonindexed_tree_image"
            )
        elif tree_semantics_available:
            evidence_tier = "tree_only"
        elif image_available:
            evidence_tier = "image_only"
        else:
            evidence_tier = "unavailable"
    return {
        "evidence_tier": evidence_tier,
        "index_actions_available": bool(
            package.actionable
            and package.index_actionable
            and tree_semantics_available
            and package.ui.elements
        ),
        "coordinate_actions_available": coordinate_available,
        **(
            {"image_size": [package.model_image_width, package.model_image_height]}
            if image_available
            and package.model_image_width > 0
            and package.model_image_height > 0
            else {}
        ),
        **(
            {"gap_reason": package.gap_reasons[0]}
            if package.gap_reasons else {}
        ),
    }


def _decision_observation_capabilities(package: ObservationPackage) -> dict[str, Any]:
    """Expose evidence quality only; non-acting roles cannot use locators."""
    capabilities = _observation_capabilities(package)
    return {
        key: capabilities[key]
        for key in ("evidence_tier", "gap_reason")
        if key in capabilities
    }


def _semantic_interaction_state(package: ObservationPackage) -> dict[str, Any]:
    """Project focus semantics for non-acting roles without locators."""
    payload = interaction_envelope(package.interaction_state)
    for key in ("focused_element", "focused_editable"):
        focus = payload.get(key)
        if isinstance(focus, dict):
            focus.pop("index", None)
    focused = payload.get("focused_element")
    editable = payload.get("focused_editable")
    if isinstance(focused, dict) and isinstance(editable, dict):
        if focused.get("identity") == editable.get("identity"):
            payload.pop("focused_element", None)
    return {
        key: value for key, value in payload.items()
        if value not in (None, "", [], {})
    }


def _executor_interaction_state(package: ObservationPackage) -> dict[str, Any]:
    payload = interaction_envelope(package.interaction_state)
    focused = payload.get("focused_element")
    editable = payload.get("focused_editable")
    if isinstance(focused, dict) and isinstance(editable, dict):
        if focused.get("identity") == editable.get("identity"):
            payload.pop("focused_element", None)
    return {
        key: value for key, value in payload.items()
        if value not in (None, "", [], {})
    }


def _focused_editable_identity(package: ObservationPackage) -> str:
    interaction = package.interaction_state
    if interaction is None or interaction.focused_editable is None:
        return ""
    return interaction.focused_editable.identity


def _short_role(role: str) -> str:
    return (role or "View").rsplit(".", 1)[-1] or "View"


def _raw_fields(
    element: UIElement,
    *,
    editability: str | None = None,
    projected_focused_editable_identity: str = "",
) -> dict[str, str]:
    if bool((element.states or {}).get("password")):
        return {"raw_fields_redacted": True}
    editability = editability or editability_evidence(element)
    if (
        projected_focused_editable_identity
        and bool((element.states or {}).get("focused"))
        and editability == "editable"
        and element_identity(element) == projected_focused_editable_identity
    ):
        return {}
    preserve_empty_text = editability == "editable"
    fields = {
        "raw_text": element.text,
        "raw_a11y_label": element.desc,
        "raw_hint": element.hint,
    }
    return {
        name: value
        for name, value in fields.items()
        if value or (name == "raw_text" and preserve_empty_text)
    }


_SEMANTIC_CONTAINER_ROLES = {
    "dialog",
    "drawerlayout",
    "gridview",
    "horizontalscrollview",
    "listview",
    "recyclerview",
    "scrollview",
    "tablayout",
    "tabwidget",
    "viewpager",
    "webview",
}

_TREE_STATE_ORDER = (
    "clickable",
    "focusable",
    "focused",
    "editable",
    "selected",
    "checked",
    "scrollable",
    "long_clickable",
    "password",
)


def _model_source_fields(element: UIElement) -> dict[str, Any]:
    if bool((element.states or {}).get("password")):
        return {"source_fields_redacted": True}
    editability = editability_evidence(element)
    return {
        key: value
        for key, value in {
            "text": element.text,
            "accessibility_label": element.desc,
            "hint": element.hint,
        }.items()
        if value or (key == "text" and editability == "editable")
    }


def _resource_id_fallback(element: UIElement, source_fields: dict[str, Any]) -> str:
    if any(source_fields.get(key) for key in ("text", "accessibility_label", "hint")):
        return ""
    value = element.resource_id.strip()
    if "resource_name_obfuscated" in value.casefold():
        return ""
    return value


def _tree_states(element: UIElement) -> dict[str, bool]:
    states = element.states or {}
    return {
        name: True
        for name in _TREE_STATE_ORDER
        if (element.clickable if name == "clickable" else bool(states.get(name)))
    }


def _retain_tree_element(
    element: UIElement,
    *,
    include_action_indexes: bool,
    multiple_windows: bool,
) -> bool:
    sources = _model_source_fields(element)
    states = _tree_states(element)
    role = _short_role(element.role).casefold()
    if element.window_wrapper:
        return bool(
            (element.window_type is not None and element.window_type != 1)
            or (multiple_windows and element.window_type is None)
        )
    return bool(
        sources
        or states
        or _resource_id_fallback(element, sources)
        or role in _SEMANTIC_CONTAINER_ROLES
        or (include_action_indexes and element.interactable and element.index >= 0)
    )


def _projected_tree_rows(
    elements: Iterable[UIElement],
    *,
    include_action_indexes: bool,
) -> list[tuple[int, UIElement]]:
    """Return retained preorder rows with depth recomputed after pruning."""
    tree = list(elements)
    if not tree:
        return []
    children = {
        index: [child for child in element.children if 0 <= child < len(tree)]
        for index, element in enumerate(tree)
    }
    child_indexes = {child for values in children.values() for child in values}
    roots = [index for index in range(len(tree)) if index not in child_indexes]
    multiple_windows = sum(element.window_wrapper for element in tree) > 1
    rows: list[tuple[int, UIElement]] = []
    visited: set[int] = set()

    def visit(index: int, depth: int) -> None:
        if index in visited:
            return
        visited.add(index)
        element = tree[index]
        retained = _retain_tree_element(
            element,
            include_action_indexes=include_action_indexes,
            multiple_windows=multiple_windows,
        )
        if retained:
            rows.append((depth, element))
        child_depth = depth + 1 if retained else depth
        for child in children[index]:
            visit(child, child_depth)

    for root in roots:
        visit(root, 0)
    for index in range(len(tree)):
        visit(index, 0)
    return rows


def render_role_tree(
    elements: Iterable[UIElement],
    *,
    include_action_indexes: bool,
    projected_focused_editable_identity: str = "",
    image_bounds_by_identity: dict[int, list[int]] | None = None,
) -> str:
    """Render the shared, locator-aware role Tree grammar."""
    lines: list[str] = []
    for depth, element in _projected_tree_rows(
        elements,
        include_action_indexes=include_action_indexes,
    ):
        indexed = bool(
            include_action_indexes and element.interactable and element.index >= 0
        )
        head = (
            f"[{element.index}] {_short_role(element.role)}"
            if indexed else _short_role(element.role)
        )
        fields: list[str] = [f"depth={depth}", head]
        actual_sources = _model_source_fields(element)
        sources = (
            {}
            if projected_focused_editable_identity
            and bool((element.states or {}).get("focused"))
            and editability_evidence(element) == "editable"
            and element_identity(element) == projected_focused_editable_identity
            else actual_sources
        )
        for name in ("text", "accessibility_label", "hint"):
            if name in sources:
                fields.append(f"{name}={_json(sources[name])}")
        if sources.get("source_fields_redacted"):
            fields.append("source_fields_redacted")
        editability = editability_evidence(element)
        if editability == "conflict":
            fields.append("editability=conflict")
        states = _tree_states(element)
        for name in _TREE_STATE_ORDER:
            if states.get(name) and not (name == "clickable" and indexed):
                fields.append(name)
        resource_id = _resource_id_fallback(element, actual_sources)
        if resource_id:
            fields.append(f"resource_id={_json(resource_id)}")
        image_bounds = (image_bounds_by_identity or {}).get(id(element))
        if image_bounds is not None:
            fields.append(f"image_bounds={_json(image_bounds)}")
        if element.window_wrapper and element.window_type is not None:
            fields.append(f"window_type={element.window_type}")
        lines.append(" | ".join(fields))
    return "\n".join(lines)


def semantic_tree_projection(
    elements: Iterable[UIElement],
    *,
    projected_focused_editable_identity: str = "",
) -> list[dict[str, Any]]:
    """Preserve hierarchy/provenance while removing action coordinates and ids."""
    elements = list(elements)
    if not projected_focused_editable_identity:
        interaction = build_interaction_state(CanonicalUI(
            semantic_tree=elements,
            elements=[element for element in elements if element.interactable],
        ))
        if interaction.focused_editable is not None:
            projected_focused_editable_identity = (
                interaction.focused_editable.identity
            )
    rows: list[dict[str, Any]] = []
    for element in elements:
        states = element.states or {}
        editability = editability_evidence(element)
        structural_states = {
            name: True
            for name in (
                "clickable",
                "checkable",
                "editable",
                "focusable",
                "focused",
                "selected",
                "checked",
                "scrollable",
                "password",
            )
            if (
                element.clickable if name == "clickable" else bool(states.get(name))
            )
        }
        row: dict[str, Any] = {
            "depth": max(0, int(element.depth or 0)),
            "role": _short_role(element.role),
            **_raw_fields(
                element,
                editability=editability,
                projected_focused_editable_identity=(
                    projected_focused_editable_identity
                ),
            ),
        }
        if editability == "conflict":
            row["editability"] = "conflict"
        if structural_states:
            row["states"] = structural_states
        if element.window_wrapper:
            row["window"] = True
        rows.append(row)
    return rows


def _decision_tree_elements(package: ObservationPackage) -> list[UIElement]:
    """Select capture-bound foreground windows without changing canonical UI."""
    tree = package.ui.semantic_tree
    foreground = package.ui.app_id.strip()
    exact_window_ids = {
        int(row["window_id"])
        for row in package.capture_meta.get("tree_ownership_candidates") or []
        if row.get("source_kind") == "window"
        and row.get("packages") == [foreground]
        and row.get("window_id") is not None
    }
    active_window_ids = {
        int(element.window_id)
        for element in tree
        if element.window_wrapper
        and element.window_id is not None
        and any(
            bool((element.states or {}).get(key))
            for key in ("active", "focused")
        )
    }
    retained_window_ids = exact_window_ids | active_window_ids
    if not retained_window_ids:
        retained = tree
    else:
        retained_indexes = [
            index for index, element in enumerate(tree)
            if element.window_id in retained_window_ids
        ]
        if not retained_indexes:
            return tree
        index_map = {
            original: projected for projected, original in enumerate(retained_indexes)
        }
        retained = [
            tree[index].model_copy(update={
                "children": [
                    index_map[child]
                    for child in tree[index].children
                    if child in index_map
                ],
            })
            for index in retained_indexes
        ]
    return retained


def decision_semantic_tree_projection(
    package: ObservationPackage,
) -> list[dict[str, Any]]:
    """Legacy structured projection retained for internal callers and tests."""
    retained = _decision_tree_elements(package)
    focused_editable = (
        package.interaction_state.focused_editable
        if package.interaction_state is not None
        else None
    )
    rows = semantic_tree_projection(
        retained,
        projected_focused_editable_identity=(
            focused_editable.identity if focused_editable is not None else ""
        ),
    )
    meaningful_states = {
        "checkable", "checked", "editable", "focused", "password",
        "scrollable", "selected",
    }
    return [
        row
        for row in rows
        if (
            any(key.startswith("raw_") for key in row)
            or meaningful_states.intersection((row.get("states") or {}).keys())
        )
    ]


def historical_intermediate_tree_projection(
    package: ObservationPackage,
) -> list[dict[str, Any]]:
    """Project the existing compressed hierarchy without action or interaction fields."""
    rows = semantic_tree_projection(_decision_tree_elements(package))
    projected: list[dict[str, Any]] = []
    for row in rows:
        projected.append({
            key: value
            for key, value in row.items()
            if key not in {"states", "editability"}
        })
    return projected


def render_historical_intermediate_observation(
    package: ObservationPackage,
) -> str:
    """Render non-actionable compound evidence with explicit temporal identity."""
    metadata = {
        "temporal_role": "historical_intermediate",
        "actionable": False,
        "observation_id": package.observation_id,
        "foreground_package": package.ui.app_id.strip(),
        "instruction": (
            "Evidence for comparison only. Do not use this tree or image as an "
            "action basis; ground subsequent actions only in the latest current state."
        ),
    }
    tree = historical_intermediate_tree_projection(package)
    return (
        "HISTORICAL INTERMEDIATE — evidence only, not an action basis:\n"
        + _json(_without_empty(metadata))
        + ("\nTREE (non-actionable):\n" + _json(tree) if tree else "")
    )


def _coordinate_surface_image_bounds(
    package: ObservationPackage, elements: Iterable[UIElement],
) -> dict[int, list[int]]:
    """Select a few current a11y surfaces and invert current frame geometry."""
    if os.getenv(SURFACE_BOUNDS_ENV, "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}
    model_w, model_h = package.model_image_width, package.model_image_height
    frame_w, frame_h = package.frame_width, package.frame_height
    if min(model_w, model_h, frame_w, frame_h) <= 0:
        return {}
    crop = package.crop_box or (0.0, 0.0, float(frame_w), float(frame_h))
    left, top, right, bottom = crop
    if right <= left or bottom <= top:
        return {}

    def point(x: float, y: float) -> tuple[float, float]:
        su = (x - left) / (right - left)
        sv = (y - top) / (bottom - top)
        rotation = package.rotation_degrees % 360
        if rotation == 0:
            u, v = su, sv
        elif rotation == 90:
            u, v = 1.0 - sv, su
        elif rotation == 180:
            u, v = 1.0 - su, 1.0 - sv
        elif rotation == 270:
            u, v = sv, 1.0 - su
        else:
            return (-1.0, -1.0)
        return (u * model_w, v * model_h)

    selected: dict[int, list[int]] = {}
    surface_terms = ("canvas", "drawing", "draw", "surface", "map", "chart", "image")
    role_surface_terms = ("canvas", "drawing", "draw", "surface", "map", "chart")
    for element in sorted(elements, key=lambda e: not e.interactable):
        if len(selected) >= 4 or element.window_wrapper:
            continue
        if len(element.bounds) != 4:
            continue
        semantic = " ".join((element.resource_id, element.text, element.desc)).lower()
        role = element.role.lower()
        # A generic ImageView class is common chrome, not evidence of a
        # coordinate target. "image" must come from authored semantics.
        if not (
            any(term in semantic for term in surface_terms)
            or any(term in role for term in role_surface_terms)
        ):
            continue
        x1, y1, x2, y2 = element.bounds
        if x2 <= x1 or y2 <= y1:
            continue
        if (x2 - x1) * (y2 - y1) >= 0.90 * frame_w * frame_h:
            continue
        corners = [point(x, y) for x, y in ((x1, y1), (x2, y1), (x1, y2), (x2, y2))]
        if any(x < 0 or y < 0 for x, y in corners):
            continue
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        bounds = [
            max(0, round(min(xs))), max(0, round(min(ys))),
            min(model_w, round(max(xs))), min(model_h, round(max(ys))),
        ]
        if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            selected[id(element)] = bounds
    return selected


def render_executor_observation_v2(
    package: ObservationPackage, *, include_surface_bounds: bool = False,
) -> str:
    foreground = package.ui.app_id.strip()
    if not foreground:
        raise ValueError("exact foreground application is required for Executor input")
    metadata = _without_empty({
        "foreground_package": foreground,
        "capabilities": _observation_capabilities(package),
        "focused_interaction": _executor_interaction_state(package),
    })
    # Same mechanical window selection as Planner/Reviewer: keep the exact
    # foreground-App window plus active/focused overlays; omit inactive
    # status/notification/navigation chrome from model input.
    current_elements = _decision_tree_elements(package)
    tree = render_role_tree(
        current_elements,
        include_action_indexes=bool(
            package.actionable and package.index_actionable
        ),
        projected_focused_editable_identity=_focused_editable_identity(package),
        image_bounds_by_identity=(
            _coordinate_surface_image_bounds(package, current_elements)
            if include_surface_bounds else None
        ),
    )
    return (
        "CURRENT OBSERVATION:\n" + _json(metadata)
        + ("\nTREE:\n" + tree if tree else "")
    )


def render_planner_observation_v2(package: ObservationPackage) -> str:
    foreground = package.ui.app_id.strip()
    if not foreground:
        raise ValueError("exact foreground application is required for Planner input")
    metadata = _without_empty({
        "foreground_package": foreground,
        "capabilities": _decision_observation_capabilities(package),
        "focused_interaction": _semantic_interaction_state(package),
    })
    tree = render_role_tree(
        _decision_tree_elements(package),
        include_action_indexes=False,
        projected_focused_editable_identity=_focused_editable_identity(package),
    )
    return (
        "CURRENT OBSERVATION:\n" + _json(metadata)
        + ("\nTREE:\n" + tree if tree else "")
    )
