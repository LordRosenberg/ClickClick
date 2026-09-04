"""a11y-rich-element-hints: resource-id parsing, states expansion, hint rendering.

Covers Tasks 1-4 of `openspec/changes/archive/2026-07-27-a11y-rich-element-hints`.
The change was archived with all tasks checked but the code never landed; these
tests pin the behaviour so that cannot silently recur.
"""

from __future__ import annotations

from perception.normalizer import (
    _resource_id_short,
    format_hints,
    normalize_a11y_tree,
    render_semantic_tree,
)
from shared.schemas import UIElement


# --- Task 1: _resource_id_short ---


def test_resource_id_short_strips_package_prefix():
    assert _resource_id_short({"resource-id": "com.app:id/foo"}) == "foo"


def test_resource_id_short_empty_value():
    assert _resource_id_short({"resource-id": ""}) == ""


def test_resource_id_short_missing_key():
    assert _resource_id_short({}) == ""


def test_resource_id_short_without_id_marker_returns_value():
    assert _resource_id_short({"resource-id": "no_colon"}) == "no_colon"


def test_resource_id_short_accepts_normalized_key():
    """`perception/uiautomator.py` emits `resource_id`, not `resource-id`."""
    assert _resource_id_short({"resource_id": "com.app:id/bar"}) == "bar"


def test_normalizer_populates_resource_id():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.Button",
                "text": "B",
                "clickable": True,
                "bounds": [0, 0, 50, 50],
                "resource-id": "com.app:id/btn",
            }
        ],
    }
    el = normalize_a11y_tree(tree).elements[0]
    assert el.resource_id == "btn"


# --- Task 2: states carries all 8 interaction keys ---


def test_states_preserves_all_eight_keys():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.Button",
                "clickable": True,
                "bounds": [0, 0, 50, 50],
                "enabled": True,
                "focused": False,
                "checked": True,
                "selected": False,
                "long_clickable": True,
                "scrollable": True,
                "password": False,
                "focusable": True,
                "editable": True,
            }
        ],
    }
    states = normalize_a11y_tree(tree).elements[0].states
    assert set(states) == {
        "enabled", "focused", "checked", "selected",
        "long_clickable", "scrollable", "password", "focusable", "editable",
    }


def test_states_skips_absent_keys_rather_than_forcing_none():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.Button",
                "clickable": True,
                "bounds": [0, 0, 50, 50],
                "enabled": True,
            }
        ],
    }
    assert list(normalize_a11y_tree(tree).elements[0].states) == ["enabled"]


# --- Task 3: format_hints ---


def test_format_hints_empty_when_no_state_and_no_resource_id():
    assert format_hints(UIElement(index=0)) == ""


def test_format_hints_clickable_only():
    assert format_hints(UIElement(index=0, clickable=True)) == "clickable"


def test_format_hints_clickable_long_clickable_and_id():
    el = UIElement(
        index=0, clickable=True, states={"long_clickable": True}, resource_id="back"
    )
    assert format_hints(el) == "clickable long-clickable id=back"


def test_format_hints_edittext_password_focusable():
    el = UIElement(
        index=0,
        role="android.widget.EditText",
        states={"focusable": True, "password": True},
        resource_id="pwd",
    )
    assert format_hints(el) == "password focusable editable id=pwd"


def test_format_hints_preserves_explicit_editability_conflict():
    el = UIElement(
        index=0,
        role="android.widget.EditText",
        states={"editable": False, "focusable": True, "focused": True},
    )
    assert format_hints(el) == "focusable editability=conflict focused"


def test_format_hints_scrollable_list():
    el = UIElement(
        index=0,
        role="androidx.recyclerview.widget.RecyclerView",
        states={"scrollable": True},
        resource_id="feed",
    )
    assert format_hints(el) == "scrollable id=feed"


def test_format_hints_plain_view_is_empty():
    assert format_hints(UIElement(index=0, role="android.view.View")) == ""


def test_format_hints_editable_custom_control():
    el = UIElement(
        index=0,
        role="android.widget.FrameLayout",
        states={"editable": True, "focusable": True},
        resource_id="search_box",
    )
    assert format_hints(el) == "focusable editable id=search_box"


def test_format_hints_selected_tab():
    el = UIElement(
        index=0,
        clickable=True,
        states={"selected": True},
        resource_id="tab_video",
    )
    assert format_hints(el) == "clickable selected id=tab_video"


def test_format_hints_checked_without_selected():
    el = UIElement(
        index=0,
        clickable=True,
        states={"checked": True, "selected": False},
    )
    assert format_hints(el) == "clickable checked"
    assert "selected" not in format_hints(el)


def test_format_hints_falsy_selected_omitted():
    el = UIElement(index=0, clickable=True, states={"selected": False})
    assert format_hints(el) == "clickable"


def test_format_hints_focused_not_surfaced():
    el = UIElement(index=0, states={"focused": True})
    assert format_hints(el) == ""
    assert "focused" not in format_hints(
        UIElement(index=0, clickable=True, states={"focused": True})
    )


def test_format_hints_focused_edittext_and_custom_editable():
    standard = UIElement(
        index=0, role="android.widget.EditText", states={"focused": True, "focusable": True}
    )
    custom = UIElement(
        index=1, role="android.widget.FrameLayout", states={"focused": True, "editable": True}
    )
    assert format_hints(standard) == "focusable editable focused"
    assert format_hints(custom) == "editable focused"
    assert "selected" not in format_hints(standard)


def test_format_hints_falsy_focus_and_enabled_are_silent():
    assert format_hints(UIElement(index=0, states={"focused": False, "enabled": True})) == ""


def test_render_includes_selected_hint():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": "视频",
                "clickable": True,
                "selected": True,
                "bounds": [0, 0, 100, 40],
                "resource-id": "com.app:id/tab_video",
            }
        ],
    }
    out = render_semantic_tree(normalize_a11y_tree(tree))
    assert "selected" in out
    assert "id=tab_video" in out


# --- Task 4: render_semantic_tree integration ---


def test_render_appends_hints_to_interactable_line():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.ImageView",
                "contentDescription": "Back",
                "clickable": True,
                "bounds": [0, 0, 50, 50],
                "long_clickable": True,
                "resource-id": "com.app:id/back",
            }
        ],
    }
    out = render_semantic_tree(normalize_a11y_tree(tree))
    assert "clickable long-clickable id=back" in out


def test_render_omits_hints_and_trailing_space_when_stateless():
    tree = {
        "class": "Root",
        "children": [
            {"class": "android.widget.Button", "text": "B", "bounds": [0, 0, 50, 50]}
        ],
    }
    out = render_semantic_tree(normalize_a11y_tree(tree))
    for line in out.splitlines():
        assert line == line.rstrip(), f"trailing whitespace: {line!r}"


def test_render_puts_ctx_before_hints():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.TextView",
                "text": "Settings",
                "bounds": [0, 0, 200, 40],
            },
            {
                "class": "android.widget.ImageView",
                "clickable": True,
                "bounds": [0, 50, 50, 100],
                "resource-id": "com.app:id/gear",
            },
        ],
    }
    out = render_semantic_tree(normalize_a11y_tree(tree))
    line = next(ln for ln in out.splitlines() if "id=gear" in ln)
    if "ctx=" in line:
        assert line.index("ctx=") < line.index("id=gear")
