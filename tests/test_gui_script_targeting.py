"""Duplicate controls need unambiguous evidence from the current frame."""

import pytest

from agent.skills.script_targeting import choose_candidate
from shared.schemas import UIElement


def control(index, bounds, *, window=4, display=0):
    return UIElement(index=index, text="Repeated", bounds=list(bounds), window_id=window, display_id=display)


def test_exact_match_without_qualification_must_be_unique():
    first = control(4, (10, 20, 50, 40))
    second = control(2, (10, 60, 50, 80))
    assert choose_candidate([first]) is first
    assert choose_candidate([first, second]) is None
    assert choose_candidate([first], expected_count=2) is None


def test_ordinal_uses_fresh_screen_order_and_checked_cardinality():
    lower = control(0, (5, 80, 30, 100))
    right = control(5, (55, 10, 80, 30))
    left = control(9, (10, 10, 40, 30))
    matches = [lower, right, left]

    assert choose_candidate(matches, occurrence=0, expected_count=3) is left
    assert choose_candidate(matches, occurrence=1, expected_count=3) is right
    assert choose_candidate(matches, occurrence=2, expected_count=3) is lower
    assert choose_candidate(matches, occurrence=1, expected_count=4) is None
    assert choose_candidate(matches, occurrence=3, expected_count=3) is None


@pytest.mark.parametrize("second", [
    control(1, (15, 10, 40, 35)),
    control(1, (0, 40, 30, 60), window=8),
    control(1, (0, 40, 30, 60), display=2),
    control(1, (0, 40, 30, 40)),
])
def test_ordinal_rejects_overlapping_mixed_scope_and_invalid_geometry(second):
    first = control(8, (0, 0, 30, 30))
    assert choose_candidate([first, second], occurrence=0, expected_count=2) is None


@pytest.mark.parametrize("relation,best,far,wrong_axis", [
    ("above", (45, 20, 55, 38), (45, 0, 55, 10), (70, 30, 90, 39)),
    ("below", (45, 62, 55, 80), (45, 90, 55, 100), (70, 61, 90, 80)),
    ("left_of", (20, 45, 38, 55), (0, 45, 10, 55), (30, 70, 39, 90)),
    ("right_of", (62, 45, 80, 55), (90, 45, 100, 55), (61, 70, 75, 90)),
])
def test_directional_match_uses_nearest_control_on_the_anchor_axis(relation, best, far, wrong_axis):
    anchor = control(10, (40, 40, 60, 60))
    target = control(8, best)
    matches = [control(2, far), control(3, wrong_axis), target]
    assert choose_candidate(matches, relative_anchor=anchor, relation=relation) is target


def test_nearest_ignores_other_surfaces_and_cannot_select_the_anchor_itself():
    anchor = control(4, (40, 40, 60, 60))
    other_window = control(5, (42, 41, 59, 60), window=2)
    other_display = control(6, (40, 42, 60, 60), display=3)
    close = control(7, (63, 41, 81, 59))
    far = control(8, (65, 30, 135, 46))

    assert choose_candidate([anchor.model_copy(), other_window, other_display, far, close],
                            relative_anchor=anchor, relation="nearest") is close


def test_distance_tie_or_unobservable_candidate_stays_ambiguous():
    anchor = control(10, (40, 40, 60, 60))
    left = control(6, (0, 40, 20, 60))
    right = control(7, (80, 40, 100, 60))
    missing = control(8, ())

    assert choose_candidate([right, left], relative_anchor=anchor, relation="nearest") is None
    assert choose_candidate([left, missing], relative_anchor=anchor, relation="nearest") is None
    assert choose_candidate([left], relative_anchor=missing, relation="nearest") is None


def test_malformed_or_mixed_disambiguation_never_uses_plain_uniqueness():
    anchor = control(10, (40, 40, 60, 60))
    candidate = control(8, (80, 40, 100, 60))
    assert choose_candidate([candidate], relative_anchor=anchor) is None
    assert choose_candidate([candidate], relation="nearest") is None
    assert choose_candidate([candidate], relative_anchor=anchor, relation="nearest", occurrence=0) is None
    assert choose_candidate([candidate], occurrence=True) is None
    assert choose_candidate([candidate], expected_count=True) is None
