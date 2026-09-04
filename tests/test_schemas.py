"""Schema field defaults and serialization round-trip for the extended models."""

from shared.schemas import CanonicalUI, UIElement


def test_uielement_defaults_for_interactable():
    e = UIElement(index=0, role="Button", text="OK")
    assert e.interactable is True
    assert e.children == []
    assert e.depth == 0
    assert e.ctx == ""


def test_uielement_skeleton_node_marks_non_interactable():
    e = UIElement(index=-1, role="TextView", text="label", interactable=False)
    assert e.interactable is False
    assert e.index == -1


def test_canonicalui_defaults():
    ui = CanonicalUI(app_id="demo")
    assert ui.semantic_tree == []
    assert ui.filter_tier == "concise"
    assert ui.estimated_tokens == 0


def test_canonicalui_serialization_roundtrip():
    ui = CanonicalUI(
        app_id="demo",
        elements=[UIElement(index=0, role="Button", text="OK", bounds=[1, 2, 3, 4])],
        semantic_tree=[
            UIElement(index=-1, role="TextView", text="label", interactable=False, depth=0),
            UIElement(index=0, role="Button", text="OK", bounds=[1, 2, 3, 4], interactable=True, depth=1),
        ],
        filter_tier="detailed",
        estimated_tokens=42,
    )
    data = ui.model_dump()
    ui2 = CanonicalUI.model_validate(data)
    assert ui2.filter_tier == "detailed"
    assert ui2.estimated_tokens == 42
    assert len(ui2.semantic_tree) == 2
    assert ui2.semantic_tree[0].interactable is False
    assert ui2.elements[0].text == "OK"
