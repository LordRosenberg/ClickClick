"""Tiered accessibility-tree filtering (DroidRun Concise/Detailed style).

Filters operate on the raw nested-dict tree BEFORE normalization, returning a
pruned copy. Pruning is bottom-up: a node is kept if it passes the tier's checks
OR it has at least one retained descendant (so invisible containers that wrap
visible content survive as pass-throughs, preserving hierarchy).
"""

from __future__ import annotations

import copy
from typing import Any, Protocol

DEFAULT_MIN_SIZE = 5
# Detailed tier visibility ratio: visible area / node area.
DEFAULT_VISIBILITY_RATIO = 0.02
KEYBOARD_PACKAGE_HINTS = ("inputmethod", ".ime.")


def _bounds(node: dict[str, Any]) -> list[int]:
    b = node.get("bounds")
    if isinstance(b, list) and len(b) == 4:
        return [int(x) for x in b]
    return [0, 0, 0, 0]


def _area(b: list[int]) -> int:
    if len(b) != 4:
        return 0
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _intersect(b: list[int], screen: tuple[int, int] | None) -> list[int]:
    """Intersection of bounds b with the screen rect [0,0,w,h]."""
    if len(b) != 4:
        return [0, 0, 0, 0]
    if screen is None:
        return b if _area(b) > 0 else [0, 0, 0, 0]
    x1 = max(b[0], 0)
    y1 = max(b[1], 0)
    x2 = min(b[2], screen[0])
    y2 = min(b[3], screen[1])
    if x2 <= x1 or y2 <= y1:
        return [0, 0, 0, 0]
    return [x1, y1, x2, y2]


def _is_keyboard(node: dict[str, Any]) -> bool:
    pkg = str(node.get("package") or node.get("resource_id") or "").lower()
    return any(h in pkg for h in KEYBOARD_PACKAGE_HINTS)


class TreeFilter(Protocol):
    """Tiered accessibility-tree filter."""

    tier: str

    def filter(self, tree: dict[str, Any]) -> dict[str, Any]:  # noqa: A003
        """Return a pruned copy of the tree."""
        ...


class ConciseFilter:
    """Default tier: prune off-screen and tiny (<min_size both dims) nodes."""

    tier = "concise"

    def __init__(
        self,
        *,
        screen: tuple[int, int] | None = None,
        min_size: int = DEFAULT_MIN_SIZE,
    ) -> None:
        self.screen = screen
        self.min_size = min_size

    def filter(self, tree: dict[str, Any]) -> dict[str, Any]:
        return _filter_node(tree, self._passes, screen=self.screen)

    def _passes(self, node: dict[str, Any]) -> bool:
        if node.get("visible") is False:
            return False
        b = _bounds(node)
        if _area(b) == 0:
            # Zero-area node: keep only if it has retained children (handled by caller).
            return False
        # Off-screen entirely?
        if _area(_intersect(b, self.screen)) == 0:
            return False
        # Tiny in both dimensions?
        w, h = b[2] - b[0], b[3] - b[1]
        if w < self.min_size and h < self.min_size:
            return False
        return True


class DetailedFilter:
    """Detailed tier: concise checks + visibility-ratio + optional keyboard strip."""

    tier = "detailed"

    def __init__(
        self,
        *,
        screen: tuple[int, int] | None = None,
        min_size: int = DEFAULT_MIN_SIZE,
        visibility_ratio: float = DEFAULT_VISIBILITY_RATIO,
        strip_keyboard: bool = False,
    ) -> None:
        self.screen = screen
        self.min_size = min_size
        self.visibility_ratio = visibility_ratio
        self.strip_keyboard = strip_keyboard

    def filter(self, tree: dict[str, Any]) -> dict[str, Any]:
        if self.strip_keyboard and _is_keyboard(tree):
            return {"class": tree.get("class", ""), "children": []}
        return _filter_node(
            tree,
            self._passes,
            screen=self.screen,
            drop_subtree=_is_keyboard if self.strip_keyboard else None,
        )

    def _passes(self, node: dict[str, Any]) -> bool:
        if node.get("visible") is False:
            return False
        if self.strip_keyboard and _is_keyboard(node):
            return False
        b = _bounds(node)
        area = _area(b)
        if area == 0:
            return False
        if _area(_intersect(b, self.screen)) == 0:
            return False
        w, h = b[2] - b[0], b[3] - b[1]
        if w < self.min_size and h < self.min_size:
            return False
        # Visibility ratio: visible area / node area below threshold → prune.
        if area > 0:
            ratio = _area(_intersect(b, self.screen)) / area
            if ratio < self.visibility_ratio:
                return False
        return True


def _filter_node(
    node: dict[str, Any],
    passes,
    *,
    screen: tuple[int, int] | None,
    drop_subtree=None,
) -> dict[str, Any]:
    """Bottom-up prune: keep node if it passes OR has retained children.

    If `drop_subtree(node)` is truthy, the entire subtree is pruned (no recursion).
    """
    if drop_subtree is not None and drop_subtree(node):
        pruned = copy.copy(node)
        pruned["children"] = []
        return pruned
    result = copy.copy(node)
    kept_children: list[dict[str, Any]] = []
    for child in node.get("children") or []:
        if isinstance(child, dict):
            kept = _filter_node(child, passes, screen=screen, drop_subtree=drop_subtree)
            if _node_retained(kept, passes):
                kept_children.append(kept)
    result["children"] = kept_children
    return result


def _node_retained(node: dict[str, Any], passes) -> bool:
    """True if the node itself passes or it has any retained descendant."""
    if passes(node):
        return True
    for child in node.get("children") or []:
        if isinstance(child, dict) and _node_retained(child, passes):
            return True
    return False
