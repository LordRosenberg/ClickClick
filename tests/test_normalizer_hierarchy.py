"""Normalizer: hierarchy preservation, skeleton nodes, projection consistency."""

from perception.normalizer import normalize_a11y_tree

TREE = {
    "class": "Root",
    "children": [
        {
            "class": "android.widget.Button",
            "text": "OK",
            "clickable": True,
            "bounds": [10, 10, 100, 80],
        },
        {
            "class": "android.widget.TextView",
            "text": "label",
            "clickable": False,
            "bounds": [10, 100, 200, 140],
        },
    ],
}


def test_elements_only_contains_interactable():
    ui = normalize_a11y_tree(TREE, app_id="a")
    assert len(ui.elements) == 1
    assert ui.elements[0].text == "OK"
    assert ui.elements[0].index == 0
    assert ui.elements[0].interactable is True


def test_semantic_tree_keeps_skeleton_nodes():
    ui = normalize_a11y_tree(TREE, app_id="a")
    # Root + Button + TextView = 3 nodes retained.
    assert len(ui.semantic_tree) == 3
    texts = [e.text for e in ui.semantic_tree]
    assert "OK" in texts and "label" in texts
    # The TextView skeleton node has no action index.
    tv = next(e for e in ui.semantic_tree if e.text == "label")
    assert tv.interactable is False
    assert tv.index == -1


def test_elements_are_projection_of_semantic_tree():
    ui = normalize_a11y_tree(TREE, app_id="a")
    interactable_in_tree = [e for e in ui.semantic_tree if e.interactable]
    assert len(interactable_in_tree) == len(ui.elements)
    # Bounds must match between projection and tree.
    for el, st in zip(ui.elements, interactable_in_tree):
        assert el.bounds == st.bounds
        assert el.index == st.index


def test_children_and_depth_filled():
    ui = normalize_a11y_tree(TREE, app_id="a")
    root = ui.semantic_tree[0]
    assert root.depth == 0
    assert len(root.children) == 2  # positions of Button and TextView
    for cpos in root.children:
        child = ui.semantic_tree[cpos]
        assert child.depth == 1


def test_indices_unique_and_per_frame():
    ui1 = normalize_a11y_tree(TREE, app_id="a")
    ui2 = normalize_a11y_tree(TREE, app_id="a")
    assert [e.index for e in ui1.elements] == [0]
    assert [e.index for e in ui2.elements] == [0]


def test_nested_hierarchy_preserved():
    nested = {
        "class": "RecyclerView",
        "children": [
            {
                "class": "Card",
                "children": [
                    {"class": "android.widget.TextView", "text": "张三", "bounds": [0, 0, 10, 10]},
                    {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
                ],
            },
            {
                "class": "Card",
                "children": [
                    {"class": "android.widget.TextView", "text": "李四", "bounds": [0, 0, 10, 10]},
                    {"class": "android.widget.ImageButton", "text": "", "clickable": True, "bounds": [0, 0, 5, 5]},
                ],
            },
        ],
    }
    ui = normalize_a11y_tree(nested)
    # Two interactable ImageButtons → indices 0 and 1 in DFS order.
    assert [e.index for e in ui.elements] == [0, 1]
    # Cards are skeleton (non-interactable) but retained.
    cards = [e for e in ui.semantic_tree if e.role == "Card"]
    assert len(cards) == 2
    assert all(not c.interactable for c in cards)


def test_uiautomator_dict_input_consumed():
    """uiautomator XML parser output (nested dict) is consumable by normalizer."""
    from perception.uiautomator import parse_uiautomator_xml

    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.example"
        content-desc="" bounds="[0,0][1080,2400]" clickable="false">
    <node index="0" text="OK" class="android.widget.Button" package="com.example"
          content-desc="" bounds="[100,200][400,300]" clickable="true"/>
  </node>
</hierarchy>"""
    tree = parse_uiautomator_xml(xml)
    ui = normalize_a11y_tree(tree)
    assert len(ui.elements) == 1
    assert ui.elements[0].text == "OK"
    assert ui.elements[0].bounds == [100, 200, 400, 300]
    assert ui.app_id == "com.example"


def test_custom_search_container_becomes_interactable_with_extra_signals():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.FrameLayout",
                "text": "搜索框",
                "desc": "搜索框",
                "focusable": True,
                "editable": True,
                "resource_id": "com.miui.home:id/search_container",
                "bounds": [20, 2200, 1060, 2350],
            }
        ],
    }
    ui = normalize_a11y_tree(tree)
    assert len(ui.elements) == 1
    assert ui.elements[0].index == 0
    assert ui.elements[0].text == "搜索框"
    assert ui.elements[0].states["editable"] is True


def test_labeled_container_without_extra_signals_stays_non_interactable():
    tree = {
        "class": "Root",
        "children": [
            {
                "class": "android.widget.FrameLayout",
                "text": "搜索框",
                "desc": "搜索框",
                "bounds": [20, 2200, 1060, 2350],
            }
        ],
    }
    ui = normalize_a11y_tree(tree)
    assert ui.elements == []
    node = next(e for e in ui.semantic_tree if e.text == "搜索框")
    assert node.interactable is False
    assert node.index == -1
