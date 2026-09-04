"""render_semantic_tree: hierarchy, action indexes, and source-typed fields."""

from perception.normalizer import normalize_a11y_tree, render_semantic_tree


def test_render_shows_index_for_interactable_only():
    tree = {
        "class": "Root",
        "children": [
            {"class": "android.widget.Button", "text": "OK", "clickable": True, "bounds": [0, 0, 10, 10]},
            {"class": "android.widget.TextView", "text": "label", "bounds": [0, 0, 10, 10]},
        ],
    }
    ui = normalize_a11y_tree(tree, app_id="demo")
    out = render_semantic_tree(ui)
    # Interactable button gets an [index]; the TextView skeleton does not.
    assert "[0]" in out
    assert "OK" in out
    assert "label" in out
    # The TextView line has no bracket index.
    label_line = [ln for ln in out.splitlines() if "label" in ln][0]
    assert "[" not in label_line


def test_render_indents_children():
    tree = {
        "class": "RecyclerView",
        "children": [
            {
                "class": "Card",
                "children": [
                    {"class": "android.widget.TextView", "text": "张三", "bounds": [0, 0, 10, 10]},
                    {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
                ],
            },
        ],
    }
    ui = normalize_a11y_tree(tree)
    out = render_semantic_tree(ui)
    lines = out.splitlines()
    # Find the Card line and its child ImageButton line.
    card_idx = next(i for i, ln in enumerate(lines) if "Card" in ln)
    btn_idx = next(i for i, ln in enumerate(lines) if "ImageButton" in ln)
    assert btn_idx > card_idx
    assert len(lines[btn_idx]) - len(lines[btn_idx].lstrip()) > (
        len(lines[card_idx]) - len(lines[card_idx].lstrip())
    )
    assert "[0]" in lines[btn_idx]


def test_unlabeled_icon_keeps_sibling_context_in_hierarchy_without_synthetic_ctx():
    tree = {
        "class": "Card",
        "children": [
            {"class": "android.widget.TextView", "text": "张三", "bounds": [0, 0, 10, 10]},
            {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
        ],
    }
    ui = normalize_a11y_tree(tree)
    out = render_semantic_tree(ui)
    btn_line = [ln for ln in out.splitlines() if "ImageButton" in ln][0]
    assert "ctx=" not in btn_line
    assert any("张三" in line for line in out.splitlines())


def test_unlabeled_icon_keeps_ancestor_context_in_hierarchy_without_synthetic_ctx():
    tree = {
        "class": "Group",
        "text": "收件箱",
        "children": [
            {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
        ],
    }
    ui = normalize_a11y_tree(tree)
    out = render_semantic_tree(ui)
    btn_line = [ln for ln in out.splitlines() if "ImageButton" in ln][0]
    assert "ctx=" not in btn_line
    assert any("收件箱" in line for line in out.splitlines())


def test_labeled_element_gets_no_ctx():
    tree = {
        "class": "Root",
        "children": [
            {"class": "android.widget.Button", "text": "Delete", "clickable": True, "bounds": [0, 0, 5, 5]},
        ],
    }
    ui = normalize_a11y_tree(tree)
    out = render_semantic_tree(ui)
    btn_line = [ln for ln in out.splitlines() if "Delete" in ln][0]
    assert "ctx=" not in btn_line


def test_render_collapses_empty_framework_chains_and_obfuscated_ids():
    tree = {
        "class": "android.widget.FrameLayout",
        "children": [{
            "class": "android.widget.LinearLayout",
            "children": [{
                "class": "android.widget.FrameLayout",
                "text": "FrameLayout",
                "clickable": True,
                "resource-id": "com.demo:id/0_resource_name_obfuscated",
                "bounds": [0, 0, 100, 100],
            }],
        }],
    }
    out = render_semantic_tree(normalize_a11y_tree(tree))
    rendered_nodes = out.splitlines()[1:]
    assert len(rendered_nodes) == 1
    assert 'FrameLayout raw_text="FrameLayout"' in rendered_nodes[0]
    assert "resource_name_obfuscated" not in out


def test_render_omits_inactive_noninteractive_system_window():
    from driver.accessibility import AccessibilitySnapshot, AccessibilityWindow

    status = {
        "class": "android.widget.TextView", "text": "99% battery",
        "package": "com.android.systemui", "bounds": [0, 0, 100, 20],
    }
    app = {
        "class": "android.widget.Button", "text": "Open", "clickable": True,
        "package": "com.demo", "bounds": [0, 20, 100, 100],
    }
    raw = AccessibilitySnapshot(
        generation=1, captured_monotonic_ms=1, complete=True, reasons=[], windows=[
            AccessibilityWindow(2, 3, 2, [0, 0, 100, 20], False, False, 0, status),
            AccessibilityWindow(1, 1, 1, [0, 0, 100, 100], True, True, 0, app),
        ],
    ).to_raw_tree()
    out = render_semantic_tree(normalize_a11y_tree(raw))
    assert "99% battery" not in out
    assert "Open" in out


def test_unlabeled_with_no_nearby_text_has_empty_ctx():
    tree = {
        "class": "Root",
        "children": [
            {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
        ],
    }
    ui = normalize_a11y_tree(tree)
    out = render_semantic_tree(ui)
    btn_line = [ln for ln in out.splitlines() if "ImageButton" in ln][0]
    assert "ctx=" not in btn_line  # no nearby text → no ctx hint shown
