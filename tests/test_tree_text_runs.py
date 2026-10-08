"""Structural and evidence regressions for the app-independent text codec."""
from copy import deepcopy
import random

import pytest

from agent.decision_context import render_executor_observation_v2, render_planner_observation_v2, render_role_tree
from agent.tree_text_runs import expand_text_runs
from shared.schemas import UIElement
from tests.test_decision_context_v2 import _package


def leaves(values):
    return [UIElement(index=-1, role="TextView", text=value, interactable=False) for value in values]


def branch(values):
    return [UIElement(index=-1, role="View", resource_id="container", interactable=False,
                      children=list(range(1, len(values) + 1))), *leaves(values)]


def render(tree, enabled):
    return render_role_tree(tree, include_action_indexes=True, compact_text_runs=enabled)


def test_round_trip_keeps_unicode_quotes_repeated_values_newlines_and_delimiters():
    tree = branch(['张三', 'a "quoted" value\nnext line', '[x] | texts=["fake"]', '重复', '重复'])
    before = deepcopy(tree)
    plain, packed = render(tree, False), render(tree, True)
    assert " | texts=" in packed and len(packed) < len(plain)
    assert expand_text_runs(packed) == plain
    assert tree == before


@pytest.mark.parametrize("change", [
    {"index": 7}, {"interactable": True}, {"clickable": True},
    {"states": {"selected": True}}, {"states": {"password": True}},
    {"states": {"custom_state": True}}, {"hint": "hint"}, {"desc": "label"},
    {"window_wrapper": True}, {"children": [0]},
])
def test_controls_and_stateful_nodes_never_enter_arrays(change):
    tree = branch(["a", "b", "control", "c", "d"])
    tree[3] = tree[3].model_copy(update=change)
    assert render(tree, True) == render(tree, False)


def test_pruned_parent_boundaries_are_not_inferred_from_equal_depth():
    tree = [UIElement(index=-1, role="FrameLayout", interactable=False, children=[1, 2]),
            *leaves(["a", "b"]),
            UIElement(index=-1, role="FrameLayout", interactable=False, children=[4, 5]),
            *leaves(["c", "d"])]
    assert render(tree, True) == render(tree, False)


def test_missing_duplicate_parent_links_and_intervening_nodes_do_not_group():
    roots = leaves(["a", "b", "c"])
    assert render(roots, True) == render(roots, False)
    ambiguous = branch(["a", "b", "c"])
    ambiguous.append(UIElement(index=-1, role="FrameLayout", interactable=False, children=[2]))
    assert render(ambiguous, True) == render(ambiguous, False)
    interrupted = branch(["a", "b", "c", "d"])
    interrupted[2] = UIElement(index=-1, role="Other", interactable=False)
    assert render(interrupted, True) == render(interrupted, False)


def test_window_provenance_is_a_group_boundary():
    tree = branch(["a", "b", "c", "d"])
    tree[3].window_id = 2
    assert render(tree, True) == render(tree, False)


def test_capture_enabled_flag_does_not_suppress_plain_text_encoding():
    tree = branch(["a", "b", "c"])
    for node in tree[1:]:
        node.states = {"enabled": True, "focused": False, "selected": False}
    plain, packed = render(tree, False), render(tree, True)
    assert 'texts=["a","b","c"]' in packed
    assert expand_text_runs(packed) == plain


def test_nested_content_never_merges_with_parent_text():
    tree = branch(["a", "b", "c"])
    tree[3].children = [4, 5, 6]
    tree.extend(leaves(["nested1", "nested2", "nested3"]))
    packed = render(tree, True)
    assert 'texts=["a"' not in packed
    assert 'texts=["nested1","nested2","nested3"]' in packed
    assert expand_text_runs(packed) == render(tree, False)


def test_renderer_flag_changes_only_executor_tree_not_durable_ui_or_planner(monkeypatch):
    package = _package()
    package.ui.semantic_tree = branch(["a", "b", "c"])
    before = package.ui.model_dump()
    monkeypatch.delenv("CLICKCLICK_TEXT_RUNS_ENABLED", raising=False)
    plain = render_executor_observation_v2(package)
    planner = render_planner_observation_v2(package)
    monkeypatch.setenv("CLICKCLICK_TEXT_RUNS_ENABLED", "true")
    for app in ["com.example.document", "com.example.message", "com.example.editor"]:
        package.ui.app_id = app
        packed = render_executor_observation_v2(package)
        assert " | texts=" in packed
        assert expand_text_runs(packed.split("\nTREE:\n", 1)[1]) == plain.split("\nTREE:\n", 1)[1]
    package.ui.app_id = before["app_id"]
    assert render_planner_observation_v2(package) == planner
    assert package.ui.model_dump() == before


def test_randomized_structural_round_trips():
    rng = random.Random(61005)
    values = ["重复", "", "a\nb", '"quoted"', " | depth=4", "text", "0"]
    activated = 0
    for _ in range(100):
        tree = []
        for _ in range(rng.randrange(1, 8)):
            start = len(tree)
            count = rng.randrange(3, 12)
            tree.append(UIElement(index=-1, role="FrameLayout", interactable=False,
                                  children=list(range(start + 1, start + count + 1))))
            tree.extend(leaves(rng.choices(values, k=count)))
            for node in tree[start + 1:]:
                if rng.random() < 0.1:
                    node.states = {"selected": True}
        before = deepcopy(tree)
        packed, plain = render(tree, True), render(tree, False)
        activated += " | texts=" in packed
        assert expand_text_runs(packed) == plain
        assert tree == before
    assert activated > 0
