"""Tiered filters: Concise pruning, Detailed visibility ratio + keyboard strip."""

from perception.filters import ConciseFilter, DetailedFilter
from perception.normalizer import normalize_a11y_tree


def _node(cls, bounds, text="", clickable=False, children=None, package=""):
    return {
        "class": cls,
        "text": text,
        "clickable": clickable,
        "bounds": bounds,
        "children": children or [],
        "package": package,
    }


def test_concise_prunes_offscreen_node():
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("android.widget.Button", [2000, 2000, 2100, 2100], text="Off", clickable=True),
        _node("android.widget.Button", [100, 100, 200, 200], text="On", clickable=True),
    ])
    pruned = ConciseFilter(screen=(1080, 2400)).filter(tree)
    ui = normalize_a11y_tree(pruned)
    texts = [e.text for e in ui.elements]
    assert "On" in texts
    assert "Off" not in texts


def test_concise_prunes_tiny_node():
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("android.widget.Button", [10, 10, 12, 12], text="Tiny", clickable=True),
        _node("android.widget.Button", [100, 100, 200, 200], text="Big", clickable=True),
    ])
    pruned = ConciseFilter().filter(tree)
    ui = normalize_a11y_tree(pruned)
    texts = [e.text for e in ui.elements]
    assert "Big" in texts
    assert "Tiny" not in texts


def test_concise_keeps_invisible_container_with_visible_child():
    # Container off-screen but wraps an on-screen button → container kept as pass-through.
    tree = _node("FrameLayout", [-5000, -5000, 1000, 1000], children=[
        _node("android.widget.Button", [100, 100, 200, 200], text="On", clickable=True),
    ])
    pruned = ConciseFilter().filter(tree)
    ui = normalize_a11y_tree(pruned)
    assert any(e.text == "On" for e in ui.elements)


def test_detailed_prunes_low_visibility_node():
    # Node mostly off-screen: only 5% visible → below 10% threshold.
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("android.widget.Button", [-990, 0, 10, 100], text="Edge", clickable=True),
        _node("android.widget.Button", [100, 100, 200, 200], text="Full", clickable=True),
    ])
    pruned = DetailedFilter(screen=(1080, 2400), visibility_ratio=0.10).filter(tree)
    ui = normalize_a11y_tree(pruned)
    texts = [e.text for e in ui.elements]
    assert "Full" in texts
    assert "Edge" not in texts


def test_detailed_strips_keyboard_subtree():
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("KeyboardView", [0, 1800, 1080, 2400], package="com.google.android.inputmethod.latin",
              children=[_node("android.widget.Button", [100, 1900, 200, 2000], text="A", clickable=True)]),
        _node("android.widget.Button", [100, 100, 200, 200], text="AppBtn", clickable=True),
    ])
    pruned = DetailedFilter(strip_keyboard=True).filter(tree)
    ui = normalize_a11y_tree(pruned)
    texts = [e.text for e in ui.elements]
    assert "AppBtn" in texts
    assert "A" not in texts


def test_detailed_keyboard_strip_disabled_keeps_keyboard():
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("android.widget.Button", [100, 1900, 200, 2000], text="A", clickable=True,
              package="com.google.android.inputmethod.latin"),
    ])
    pruned = DetailedFilter(strip_keyboard=False).filter(tree)
    ui = normalize_a11y_tree(pruned)
    assert any(e.text == "A" for e in ui.elements)


def test_filter_tier_recorded_on_canonical_ui():
    tree = _node("Root", [0, 0, 1080, 2400], children=[
        _node("android.widget.Button", [100, 100, 200, 200], text="OK", clickable=True),
    ])
    ui = normalize_a11y_tree(tree, tree_filter=DetailedFilter())
    assert ui.filter_tier == "detailed"
    ui2 = normalize_a11y_tree(tree, tree_filter=ConciseFilter())
    assert ui2.filter_tier == "concise"


def test_default_filter_does_not_guess_screen_geometry():
    tree = _node("Root", [0, 0, 2670, 1200], children=[
        _node("android.widget.Button", [2400, 100, 2600, 300], text="Right", clickable=True),
    ])
    ui = normalize_a11y_tree(tree, tree_filter=DetailedFilter())
    assert any(element.text == "Right" for element in ui.elements)
