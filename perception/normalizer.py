"""Accessibility tree → CanonicalUI with hierarchy-preserving normalization.

The tree is kept as a pruned semantic skeleton: non-interactable context nodes
(containers, text labels, icons) are retained without an action index so the
model can infer element-to-label associations from the preserved hierarchy.
Only interactable nodes receive a per-frame action `index` and appear in
`CanonicalUI.elements` (the action namespace). `elements` is the strict
projection of `semantic_tree`'s interactable nodes.
"""

from __future__ import annotations

import json
from typing import Any

from shared.schemas import CanonicalUI, UIElement
from perception.input_evidence import (
    build_interaction_state,
    editability_evidence,
    element_identity,
)
from perception.filters import TreeFilter

# Roles treated as interactable (DroidRun-like granularity).
INTERACTABLE_HINTS = {
    "button",
    "imagebutton",
    "edittext",
    "textfield",
    "checkbox",
    "switch",
    "radiobutton",
    "clickable",
    "spinner",
    "seekbar",
    "tab",
}
TREE_OWNERSHIP_TELEMETRY_LIMIT = 64

def _is_interactable(node: dict[str, Any]) -> bool:
    """Return True if a raw a11y node should receive an action index."""
    if node.get("window_wrapper"):
        return False
    if node.get("clickable") or node.get("checkable") or node.get("editable"):
        return True
    role = str(node.get("class") or node.get("role") or "").lower()
    if any(h in role for h in INTERACTABLE_HINTS):
        return True
    return False


def _resource_id_short(node: dict[str, Any]) -> str:
    """Extract the short form of a raw node's resource-id.

    `"com.app:id/foo"` → `"foo"`. A value with no `":id/"` separator is
    returned unchanged (`"no_colon"` → `"no_colon"`). Missing/empty → `""`.
    Accepts both the raw `resource-id` key and the normalized `resource_id`
    (`perception/uiautomator.py` emits the latter).
    """
    raw = node.get("resource-id")
    if raw is None:
        raw = node.get("resource_id")
    value = str(raw or "")
    if not value:
        return ""
    marker = ":id/"
    if marker in value:
        return value.split(marker, 1)[1]
    return value


# Interaction-relevant raw node keys preserved verbatim into `UIElement.states`.
# A key absent from the raw node is skipped rather than forced to None.
_STATE_KEYS: tuple[str, ...] = (
    "enabled",
    "focused",
    "checked",
    "selected",
    "long_clickable",
    "scrollable",
    "password",
    "focusable",
    "editable",
    "active",
)


def _packages(node: Any) -> list[str]:
    """Return the complete sorted non-empty package set in one source."""
    if not isinstance(node, dict):
        return []
    packages = {str(node.get("package") or "").strip()}
    for child in node.get("children") or []:
        packages.update(_packages(child))
    packages.discard("")
    return sorted(packages)


def _first_package(node: Any) -> str:
    packages = _packages(node)
    return packages[0] if packages else ""


def _descendants(node: Any) -> list[dict[str, Any]]:
    """Return source nodes below one wrapper without interpreting their meaning."""
    if not isinstance(node, dict):
        return []
    rows = [] if node.get("window_wrapper") else [node]
    for child in node.get("children") or []:
        rows.extend(_descendants(child))
    return rows


def _node_is_model_visible(node: dict[str, Any]) -> bool:
    return bool(
        _is_interactable(node)
        or node.get("text")
        or node.get("contentDescription")
        or node.get("desc")
        or node.get("hint")
        or node.get("hint_text")
        or node.get("placeholder")
        or any(node.get(name) for name in (
            "focused", "selected", "checked", "editable", "scrollable",
        ))
    )


def tree_ownership_candidates(
    tree: Any,
    *,
    tree_filter: TreeFilter | None = None,
) -> list[dict[str, Any]]:
    """Describe source-typed application-window owners and retained counts.

    Window metadata and raw counts come from the provider response. Retained
    counts are computed from the same filtered projection used by perception.
    The facts deliberately contain no page/App semantics and are not rendered
    into the model context.
    """
    if not isinstance(tree, dict):
        return []
    declared_windows = [
        child for child in tree.get("children") or []
        if isinstance(child, dict) and child.get("window_wrapper")
    ]
    declared_window_ids = [child.get("window_id") for child in declared_windows]
    if declared_windows and (
        any(
            isinstance(window_id, bool)
            or not isinstance(window_id, int)
            or window_id < 0
            for window_id in declared_window_ids
        )
        or len(set(declared_window_ids)) != len(declared_window_ids)
    ):
        return []
    raw_windows = [
        child for child in declared_windows
        if int(child.get("window_type") or 0) == 1
        and int(child.get("display_id") or 0) == 0
    ]
    if not raw_windows:
        if declared_windows:
            return []
        raw_windows = [tree]

    filtered_tree = (
        tree_filter.filter(tree)
        if tree_filter is not None else tree
    )
    filtered_windows = {
        child.get("window_id"): child
        for child in (
            filtered_tree.get("children") or []
            if isinstance(filtered_tree, dict) else []
        )
        if isinstance(child, dict) and child.get("window_wrapper")
    }
    candidates: list[dict[str, Any]] = []
    windowed = raw_windows != [tree]
    for raw_window in raw_windows:
        filtered_window = (
            filtered_windows.get(raw_window.get("window_id"), {"children": []})
            if windowed else filtered_tree
        )
        raw_nodes = _descendants(raw_window)
        retained_nodes = _descendants(filtered_window)
        raw_visible = [
            node for node in raw_nodes if _node_is_model_visible(node)
        ]
        retained_visible = [
            node for node in retained_nodes if _node_is_model_visible(node)
        ]
        candidates.append({
            "source_kind": "window" if windowed else "flat",
            "packages": _packages(raw_window),
            "window_id": raw_window.get("window_id"),
            "window_type": (
                int(raw_window.get("window_type") or 0) if windowed else None
            ),
            "window_layer": (
                int(raw_window.get("window_layer") or 0) if windowed else None
            ),
            "display_id": (
                int(raw_window.get("display_id") or 0) if windowed else None
            ),
            "active": bool(raw_window.get("active")),
            "focused": bool(raw_window.get("focused")),
            "semantic_node_count": len(raw_nodes),
            "actionable_node_count": sum(
                _is_interactable(node) for node in raw_nodes
            ),
            "model_visible_node_count": len(raw_visible),
            "retained_semantic_node_count": len(retained_nodes),
            "retained_actionable_node_count": sum(
                _is_interactable(node) for node in retained_nodes
            ),
            "retained_model_visible_node_count": len(retained_visible),
        })
    return candidates


def tree_ownership_source_complete(
    tree: dict[str, Any], candidates: list[dict[str, Any]],
) -> bool:
    """Require provider-declared completeness without inventing source facts."""
    capture = tree.get("_capture")
    if not isinstance(capture, dict) or capture.get("complete") is not True:
        return False
    declared_windows = [
        child for child in tree.get("children") or []
        if isinstance(child, dict) and child.get("window_wrapper")
    ]
    windowed = bool(declared_windows)
    if not windowed:
        return True
    window_ids = [window.get("window_id") for window in declared_windows]
    if (
        any(
            isinstance(window_id, bool)
            or not isinstance(window_id, int)
            or window_id < 0
            for window_id in window_ids
        )
        or len(set(window_ids)) != len(window_ids)
    ):
        return False
    window_count = capture.get("window_count")
    return bool(
        isinstance(window_count, int)
        and not isinstance(window_count, bool)
        and window_count == len(declared_windows)
        and all(not window.get("capture_reason") for window in declared_windows)
    )


def tree_ownership_facts(
    tree: Any,
    *,
    foreground_package: str,
    tree_filter: TreeFilter | None = None,
) -> dict[str, Any]:
    """Classify exact/missing/conflict/ambiguous source ownership."""
    candidates = tree_ownership_candidates(tree, tree_filter=tree_filter)
    ordered_candidates = sorted(
        enumerate(candidates),
        key=lambda pair: (
            -int(bool(pair[1]["active"] or pair[1]["focused"])),
            -int(pair[1]["window_layer"] or 0),
            pair[0],
        ),
    )
    telemetry_candidates = [
        row for _, row in ordered_candidates[:TREE_OWNERSHIP_TELEMETRY_LIMIT]
    ]
    source_complete = tree_ownership_source_complete(tree, candidates)
    telemetry_truncated = len(candidates) > TREE_OWNERSHIP_TELEMETRY_LIMIT
    telemetry = {
        "tree_ownership_candidates": telemetry_candidates,
        "tree_ownership_candidate_count": len(candidates),
        "tree_ownership_candidates_truncated": telemetry_truncated,
        "tree_ownership_candidates_complete": bool(
            source_complete and not telemetry_truncated
        ),
    }
    if not candidates or not source_complete:
        return {
            **telemetry,
            "tree_ownership_status": "missing",
            "tree_application_package": "",
        }

    # Choose the source window before inspecting its content. Otherwise an
    # empty focused target window could be silently replaced by a lower
    # inactive foreign window that happens to contain visible nodes.
    best_focus = max(bool(row["active"] or row["focused"]) for row in candidates)
    focused = [
        row for row in candidates
        if bool(row["active"] or row["focused"]) == best_focus
    ]
    best_layer = max(int(row["window_layer"] or 0) for row in focused)
    selected = [
        row for row in focused if int(row["window_layer"] or 0) == best_layer
    ]
    if any(len(row["packages"]) > 1 for row in selected):
        status, owner = "ambiguous", ""
    elif any(
        not row["packages"]
        or int(row["retained_model_visible_node_count"]) <= 0
        for row in selected
    ):
        status, owner = "missing", ""
    else:
        packages = sorted({row["packages"][0] for row in selected})
        if len(packages) != 1:
            status, owner = "ambiguous", ""
        else:
            owner = packages[0]
            status = (
                "exact" if owner == foreground_package.strip() else "conflict"
            )
    return {
        **telemetry,
        "tree_ownership_status": status,
        "tree_application_package": owner,
    }


def foreground_window_package(tree: Any) -> str:
    """Prefer the focused application window over higher system overlays."""
    if not isinstance(tree, dict):
        return ""
    windows = [
        child for child in tree.get("children") or []
        if isinstance(child, dict) and child.get("window_wrapper")
    ]
    preference = (
        lambda item: int(item.get("window_type") or 0) == 1
        and bool(item.get("active") or item.get("focused")),
        lambda item: bool(item.get("active") or item.get("focused")),
        lambda item: int(item.get("window_type") or 0) == 1,
    )
    for preferred in preference:
        for window in windows:
            if preferred(window):
                package = _first_package(window)
                if package:
                    return package
    return ""


def foreground_application_window_identity(tree: Any) -> dict[str, Any]:
    """Return the focused application-window owner, if the tree contains one."""
    empty: dict[str, Any] = {
        "package": "",
        "window_type": None,
        "window_layer": None,
        "display_id": None,
    }
    if not isinstance(tree, dict):
        return empty
    windows = [
        child for child in tree.get("children") or []
        if isinstance(child, dict)
        and child.get("window_wrapper")
        and int(child.get("window_type") or 0) == 1
        and int(child.get("display_id") or 0) == 0
    ]
    if not windows:
        if any(
            isinstance(child, dict) and child.get("window_wrapper")
            for child in tree.get("children") or []
        ):
            return empty
        package = _first_package(tree)
        return {**empty, "package": package, "window_type": 1 if package else None}
    focused = [
        window for window in windows
        if bool(window.get("active") or window.get("focused"))
    ]
    selected = max(
        focused or windows,
        key=lambda window: int(window.get("window_layer") or 0),
    )
    return {
        "package": _first_package(selected),
        "window_type": 1,
        "window_layer": int(selected.get("window_layer") or 0),
        "display_id": int(selected.get("display_id") or 0),
    }


def top_window_identity(tree: Any) -> dict[str, Any]:
    """Return deterministic ownership facts for the highest default-display window.

    This deliberately does not infer the foreground application.  Android's
    resumed Activity is the authority for that fact; this helper only preserves
    the independent window-tree side of the grounding barrier.
    """
    empty: dict[str, Any] = {
        "package": "",
        "window_type": None,
        "window_layer": None,
        "display_id": None,
    }
    if not isinstance(tree, dict):
        return empty
    windows = [
        child for child in tree.get("children") or []
        if isinstance(child, dict) and child.get("window_wrapper")
    ]
    if not windows:
        package = _first_package(tree)
        return {**empty, "package": package}
    default_windows = [
        window for window in windows
        if int(window.get("display_id") or 0) == 0
    ]
    candidates = default_windows or windows
    top = max(
        candidates,
        key=lambda window: (
            int(window.get("window_layer") or 0),
            bool(window.get("active")),
            bool(window.get("focused")),
        ),
    )
    return {
        "package": _first_package(top),
        "window_type": int(top.get("window_type") or 0),
        "window_layer": int(top.get("window_layer") or 0),
        "display_id": int(top.get("display_id") or 0),
    }


def _bounds_of(node: dict[str, Any]) -> list[int]:
    """Normalize bounds to [x1,y1,x2,y2]."""
    b = node.get("bounds")
    if isinstance(b, list) and len(b) == 4:
        return [int(x) for x in b]
    if isinstance(b, dict):
        return [
            int(b.get("left", 0)),
            int(b.get("top", 0)),
            int(b.get("right", 0)),
            int(b.get("bottom", 0)),
        ]
    return [0, 0, 0, 0]


def _roots_of(tree: dict[str, Any] | list[Any]) -> list[dict[str, Any]]:
    """Resolve the list of root nodes from various raw tree shapes."""
    if isinstance(tree, list):
        return [n for n in tree if isinstance(n, dict)]
    if isinstance(tree, dict) and "children" in tree:
        return [tree]
    if isinstance(tree, dict) and "nodes" in tree:
        return [n for n in tree["nodes"] if isinstance(n, dict)]
    return [tree] if isinstance(tree, dict) else []


def normalize_a11y_tree(
    tree: dict[str, Any] | list[Any],
    *,
    platform: str = "android",
    app_id: str = "",
    activity: str = "",
    allow_tree_app_fallback: bool = True,
    tree_filter: TreeFilter | None = None,
) -> CanonicalUI:
    """Convert a raw accessibility tree into CanonicalUI with hierarchy preserved.

    Builds `semantic_tree` (all retained nodes, skeleton included) and `elements`
    (interactable projection with per-frame action indices). Indices are
    recomputed every call (per-frame renumbering) in depth-first order.
    `children` holds positions into the `semantic_tree` list. When `tree_filter`
    is provided it is applied to the raw tree before normalization, and its
    `tier` is recorded on the result.
    """
    filter_tier = "concise"
    if tree_filter is not None:
        if isinstance(tree, dict):
            tree = tree_filter.filter(tree)
            filter_tier = tree_filter.tier
        # If tree is a list shape, filtering is skipped (filter expects a dict root).
    roots = _roots_of(tree)
    semantic_tree: list[UIElement] = []
    action_counter = [0]  # mutable to avoid nonlocal boilerplate

    def walk(
        node: dict[str, Any], depth: int, window: dict[str, int | None] | None = None
    ) -> int:
        pos = len(semantic_tree)
        current_window = dict(window or {})
        if node.get("window_wrapper") and "window_id" in node:
            current_window = {
                "window_id": int(node["window_id"]),
                "window_type": int(node.get("window_type", 0)),
                "window_layer": int(node.get("window_layer", 0)),
                "display_id": int(node.get("display_id", 0)),
            }
        is_inter = _is_interactable(node)
        idx = action_counter[0] if is_inter else -1
        if is_inter:
            action_counter[0] += 1
        el = UIElement(
            index=idx,
            role=str(node.get("class") or node.get("role") or ""),
            text=str(node.get("text") or ""),
            desc=str(node.get("contentDescription") or node.get("desc") or ""),
            hint=str(
                node.get("hint")
                or node.get("hint_text")
                or node.get("placeholder")
                or ""
            ),
            bounds=_bounds_of(node),
            clickable=bool(node.get("clickable", is_inter)),
            states={k: node.get(k) for k in _STATE_KEYS if k in node},
            resource_id=_resource_id_short(node),
            depth=depth,
            interactable=is_inter,
            children=[],
            window_id=current_window.get("window_id"),
            window_type=current_window.get("window_type"),
            window_layer=current_window.get("window_layer"),
            display_id=current_window.get("display_id"),
            window_wrapper=bool(node.get("window_wrapper")),
        )
        semantic_tree.append(el)
        child_positions: list[int] = []
        for child in node.get("children") or []:
            if isinstance(child, dict):
                child_positions.append(walk(child, depth + 1, current_window))
        el.children = child_positions
        return pos

    for r in roots:
        walk(r, 0)

    elements = [e for e in semantic_tree if e.interactable]

    summary_parts = [
        e.text or e.desc
        for e in elements
        if not bool((e.states or {}).get("password")) and (e.text or e.desc)
    ][:12]
    capture = tree.get("_capture") if isinstance(tree, dict) else None
    capture = capture if isinstance(capture, dict) else {}
    return CanonicalUI(
        platform=platform,
        app_id=(
            app_id
            or (
                foreground_window_package(tree) or _first_package(tree)
                if allow_tree_app_fallback
                else ""
            )
        ),
        activity=activity,
        elements=elements,
        semantic_tree=semantic_tree,
        page_summary="; ".join(summary_parts)[:500],
        filter_tier=filter_tier,
        capture_provider=str(capture.get("provider") or "unknown"),
        capture_complete=capture.get("complete") is True,
        capture_generation=int(capture.get("generation") or 0),
        capture_reasons=[str(reason) for reason in capture.get("reasons", [])][:8],
    )


def _short_role(role: str) -> str:
    """Shorten a fully-qualified class name to its last segment."""
    if not role:
        return "View"
    return role.rsplit(".", 1)[-1] or role


def format_hints(el: UIElement) -> str:
    """Compact human-readable hints for LLM tree rendering.

    Order is fixed so the output is stable across frames: clickable,
    scrollable, long-clickable, password, focusable, editable,
    focused, selected, checked, then `id=<resource_id>`. Returns `""` when the
    element carries no interaction-relevant state and no resource-id.
    Truthy focus is emitted only for editable/focusable elements; enabled is silent.
    """
    parts: list[str] = []
    states = el.states or {}

    if el.clickable:
        parts.append("clickable")
    if states.get("scrollable"):
        parts.append("scrollable")
    if states.get("long_clickable"):
        parts.append("long-clickable")
    if states.get("password"):
        parts.append("password")
    if states.get("focusable") and not el.clickable:
        parts.append("focusable")

    editability = editability_evidence(el)
    if editability == "editable":
        parts.append("editable")
    elif editability == "conflict":
        parts.append("editability=conflict")

    if states.get("focused") and (
        editability == "editable" or states.get("focusable")
    ):
        parts.append("focused")

    if states.get("selected"):
        parts.append("selected")
    if states.get("checked"):
        parts.append("checked")

    resource_id = el.resource_id
    if resource_id and "resource_name_obfuscated" in resource_id.casefold():
        resource_id = ""
    if resource_id:
        parts.append(f"id={resource_id}")
    return " ".join(parts)


def _raw_field_text(
    el: UIElement,
    *,
    projected_focused_editable_identity: str = "",
) -> str:
    """Render platform channels without collapsing their provenance."""
    states = el.states or {}
    if bool(states.get("password")):
        return "raw_fields_redacted=true"
    if (
        projected_focused_editable_identity
        and bool(states.get("focused"))
        and editability_evidence(el) == "editable"
        and element_identity(el) == projected_focused_editable_identity
    ):
        # The exact source channels are projected once through
        # focused_interaction.focused_editable for this same observation.
        return ""
    role = _short_role(el.role).casefold()
    structurally_editable = bool(
        states.get("editable") is True
        or "edittext" in role
        or "textfield" in role
    )
    fields = (
        ("raw_text", el.text),
        ("raw_a11y_label", el.desc),
        ("raw_hint", el.hint),
    )
    return " ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in fields
        if value or (name == "raw_text" and structurally_editable)
    )


def render_semantic_tree(ui: CanonicalUI, *, include_action_indexes: bool = True) -> str:
    """Render the pruned hierarchy as an indented tree for LLM observation.

    Interactable leaf nodes are marked with their per-frame `[index]`; non-
    interactable context nodes show role plus separate raw text, accessibility
    label, and hint channels. The action namespace (`elements`) is the
    projection of the interactable nodes shown here.
    """
    tree = ui.semantic_tree
    if not tree:
        return f"app={ui.app_id}\n(empty tree)"
    interaction = build_interaction_state(ui)
    projected_focused_editable_identity = (
        interaction.focused_editable.identity
        if interaction.focused_editable is not None
        else ""
    )
    # Roots = positions with no parent.
    parent_of: dict[int, int] = {}
    for pos, node in enumerate(tree):
        for cpos in node.children:
            parent_of[cpos] = pos
    roots = [pos for pos in range(len(tree)) if pos not in parent_of]
    lines = [f"foreground_package={ui.app_id}"]

    actionable_cache: dict[int, bool] = {}

    def has_actionable_descendant(pos: int) -> bool:
        cached = actionable_cache.get(pos)
        if cached is not None:
            return cached
        el = tree[pos]
        value = el.interactable or any(has_actionable_descendant(child) for child in el.children)
        actionable_cache[pos] = value
        return value

    def emit(pos: int, indent: int) -> None:
        el = tree[pos]
        if (
            el.window_wrapper and el.window_id is not None
            and el.window_type != 1
            and not el.states.get("active") and not el.states.get("focused")
            and not has_actionable_descendant(pos)
        ):
            # Inactive status/navigation-bar windows contain clocks, signal
            # strength, notifications, and deep framework skeletons that do
            # not affect the current app action. Active/focused overlays and
            # any system window with controls remain visible.
            return
        raw_fields = _raw_field_text(
            el,
            projected_focused_editable_identity=(
                projected_focused_editable_identity
            ),
        )
        if (
            not el.interactable and not el.window_wrapper and not raw_fields
            and len(el.children) == 1
        ):
            # Collapse pass-through framework containers while retaining
            # branching containers that encode useful label/control grouping.
            emit(el.children[0], indent)
            return
        if not el.interactable and not el.window_wrapper and not raw_fields and not el.children:
            return
        pad = "  " * indent
        if el.interactable:
            field_suffix = f" {raw_fields}" if raw_fields else ""
            prefix = f"[{el.index}] " if include_action_indexes else ""
            line = f"{pad}{prefix}{_short_role(el.role)}{field_suffix}"
            hints = format_hints(el)
            if hints:
                line += f" {hints}"
        else:
            if el.window_wrapper and el.window_id is not None:
                flags = []
                if el.states.get("active"):
                    flags.append("active")
                if el.states.get("focused"):
                    flags.append("focused")
                suffix = f" {' '.join(flags)}" if flags else ""
                line = (
                    f"{pad}Window id={el.window_id} type={el.window_type} "
                    f"layer={el.window_layer} display={el.display_id}{suffix}"
                )
            elif raw_fields:
                line = f"{pad}{_short_role(el.role)} {raw_fields}"
            else:
                line = f"{pad}{_short_role(el.role)}"
        lines.append(line)
        for cpos in el.children:
            emit(cpos, indent + 1)

    for r in roots:
        emit(r, 0)
    return "\n".join(lines)
