"""Resolve explicit disambiguation within one accepted GUI observation."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import combinations
import re
from typing import Literal

from shared.schemas import UIElement


Relation = Literal["nearest", "above", "below", "left_of", "right_of"]
Bounds = tuple[int, int, int, int]
_NUMBERED_DESCRIPTION = re.compile(r"(.+\D)\d{1,6}\Z")


def invariant_target(selector):
    """Retain everything except the numeric description of a named native control."""
    if (selector.resource_id and selector.desc and _NUMBERED_DESCRIPTION.fullmatch(selector.desc)
            and selector.occurrence is None and selector.relative_to is None
            and selector.expected_count in (None, 1)):
        return selector.model_copy(update={"desc": None})
    return None


def numbered_description_changed(old, current):
    """Recognize a changed numeric suffix with the same descriptive prefix."""
    if not isinstance(old, str) or not isinstance(current, str) or old == current:
        return False
    before = _NUMBERED_DESCRIPTION.fullmatch(old)
    after = _NUMBERED_DESCRIPTION.fullmatch(current)
    return bool(before and after and before.group(1) == after.group(1))


def _bounds(element: UIElement) -> Bounds | None:
    if len(element.bounds) != 4:
        return None
    left, top, right, bottom = element.bounds
    if left >= right or top >= bottom:
        return None
    return left, top, right, bottom


def _scope(element: UIElement) -> tuple[int | None, int | None]:
    return element.display_id, element.window_id


def _overlap(start: int, end: int, other_start: int, other_end: int) -> bool:
    return max(start, other_start) < min(end, other_end)


def _distance(candidate: Bounds, anchor: Bounds, relation: Relation) -> int | None:
    left, top, right, bottom = candidate
    anchor_left, anchor_top, anchor_right, anchor_bottom = anchor
    if relation == "nearest":
        # Doubled centers preserve exact ordering and ties without rounding.
        return (left + right - anchor_left - anchor_right) ** 2 + (top + bottom - anchor_top - anchor_bottom) ** 2
    if relation in {"above", "below"}:
        if not _overlap(left, right, anchor_left, anchor_right):
            return None
        gap = anchor_top - bottom if relation == "above" else top - anchor_bottom
    else:
        if not _overlap(top, bottom, anchor_top, anchor_bottom):
            return None
        gap = anchor_left - right if relation == "left_of" else left - anchor_right
    return gap if gap >= 0 else None


def _nearest(matches: Sequence[UIElement], anchor: UIElement, relation: Relation) -> UIElement | None:
    anchor_box = _bounds(anchor)
    if anchor_box is None:
        return None
    scored = []
    for element in matches:
        if _scope(element) != _scope(anchor):
            continue
        if (element == anchor or (0 <= element.index == anchor.index)
                or (element.node_handle and element.node_handle == anchor.node_handle)):
            continue
        box = _bounds(element)
        if box is None:
            return None
        distance = _distance(box, anchor_box, relation)
        if distance is not None:
            scored.append((distance, element))
    if not scored:
        return None
    best = min(distance for distance, _ in scored)
    nearest = [element for distance, element in scored if distance == best]
    return nearest[0] if len(nearest) == 1 else None


def _occurrence(matches: Sequence[UIElement], occurrence: int) -> UIElement | None:
    if len({_scope(element) for element in matches}) != 1:
        return None
    positioned = []
    for element in matches:
        box = _bounds(element)
        if box is None:
            return None
        positioned.append((box, element))
    if any(_overlap(box[0], box[2], other[0], other[2]) and _overlap(box[1], box[3], other[1], other[3])
           for (box, _), (other, _) in combinations(positioned, 2)):
        return None
    return sorted(positioned, key=lambda item: (item[0][1], item[0][0]))[occurrence][1]


def choose_candidate(
    matches: Sequence[UIElement],
    *,
    relative_anchor: UIElement | None = None,
    relation: Relation | None = None,
    occurrence: int | None = None,
    expected_count: int | None = None,
) -> UIElement | None:
    """Choose a target from current, semantically scoped matches.

    The caller supplies a uniquely resolved anchor from the same observation.
    An ordinal without a declared count requires explicit scope in the caller.
    Counts apply before ranking; ordinals use current top, then left positions.
    Failure to choose a unique candidate does not establish that it is absent.
    """
    if not matches or (expected_count is not None and (
            type(expected_count) is not int or expected_count != len(matches))):
        return None
    if relative_anchor is not None or relation is not None:
        if (occurrence is not None or relative_anchor is None
                or relation not in {"nearest", "above", "below", "left_of", "right_of"}):
            return None
        return _nearest(matches, relative_anchor, relation)
    if occurrence is not None:
        if type(occurrence) is not int or not 0 <= occurrence < len(matches):
            return None
        return _occurrence(matches, occurrence)
    return matches[0] if len(matches) == 1 else None
