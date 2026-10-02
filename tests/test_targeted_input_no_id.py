"""No-id replacement accepts unique native hints, never positional guesses."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.targeted_input import focused_same_target, replace_target_text, supported_target
from shared.schemas import Action, ActionResult, CanonicalUI, UIElement


def form(*, focused=None, values=None, indexes=(4, 6, 7), hints=None, ime=True):
    hints = hints or ("Enter email address", "Subject", "Compose email")
    nodes = [UIElement(index=-1, role="android.view.ViewGroup", resource_id="compose_form",
                       interactable=False, children=[1, 2, 3], window_id=12, display_id=0, window_type=1)]
    for i, (index, hint) in enumerate(zip(indexes, hints)):
        nodes.append(UIElement(index=index, role="android.widget.EditText", hint=hint,
                               text=(values or {}).get(i, ""), bounds=[10, 100+i*100, 500, 180+i*100],
                               window_id=12, display_id=0, window_type=1,
                               states={"editable": True, "enabled": True, "focused": i == focused}))
    if ime:
        nodes.append(UIElement(index=-1, role="Window", window_wrapper=True, window_type=2,
                               window_id=13, interactable=False))
    return SimpleNamespace(accepted=True, index_actionable=True, interaction_state=None,
                           ui=CanonicalUI(app_id="test.mail", activity=".ComposeActivity",
                                          elements=nodes[1:4], semantic_tree=nodes, capture_complete=True))


def test_unique_hint_survives_value_index_and_keyboard_geometry_changes():
    before = form()
    target = supported_target(before, 6)
    assert target is before.ui.elements[1]
    after = form(focused=1, indexes=(10, 20, 30), values={1: "new subject"})
    after.ui.elements[1].bounds = [0, 40, 500, 90]
    assert focused_same_target(before, target, after) is after.ui.elements[1]


@pytest.mark.parametrize("change", ["duplicate_hint", "no_hint", "no_activity", "no_app", "no_display",
                                   "ime_editor", "no_parent", "incomplete", "password", "disabled"])
def test_unsupported_no_id_never_falls_back_to_text_or_position(change):
    before = form()
    target = before.ui.elements[1]
    if change == "duplicate_hint":
        before.ui.elements[2].hint = target.hint
    elif change == "no_hint":
        target.hint = ""
        target.text = target.desc = "Subject"
    elif change == "no_activity":
        before.ui.activity = ""
    elif change == "no_app":
        before.ui.app_id = ""
    elif change == "no_display":
        target.display_id = None
    elif change == "ime_editor":
        target.window_type = 2
    elif change == "no_parent":
        before.ui.semantic_tree[0].children = []
    elif change == "incomplete":
        before.ui.capture_complete = False
    elif change == "password":
        target.states["password"] = True
    else:
        target.states["enabled"] = False
    assert supported_target(before, 6) is None


@pytest.mark.parametrize("change", ["window", "activity", "app", "display", "parent", "hint",
                                   "duplicate", "focus_jump", "competing_focus", "incomplete"])
@pytest.mark.asyncio
async def test_changed_form_or_ambiguous_focus_stops_before_clear(change):
    before = form(ime=False)
    after = form(focused=1)
    target = after.ui.elements[1]
    if change == "window":
        target.window_id = 99
    elif change == "activity":
        after.ui.activity = ".OtherCompose"
    elif change == "app":
        after.ui.app_id = "other.mail"
    elif change == "display":
        target.display_id = 1
    elif change == "parent":
        after.ui.semantic_tree[0].resource_id = "another_form"
    elif change == "hint":
        target.hint = "Another field"
    elif change == "duplicate":
        after.ui.elements[2].hint = target.hint
    elif change == "focus_jump":
        target.states["focused"] = False
        after.ui.elements[2].states["focused"] = True
    elif change == "competing_focus":
        after.ui.elements[2].states["focused"] = True
    else:
        after.ui.capture_complete = False
    calls = []
    async def act(action, current, **kwargs):
        calls.append(action.type)
        return ActionResult(success=True), after
    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act),
        Action(type="replace_text", index=6, text="subject"), before,
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1)
    assert calls == ["tap_xy"]
    assert not result.success
    assert result.detail["input_steps"]["clear"] == "not_attempted"


@pytest.mark.parametrize("outcome", ["exact", "different", "lost_identity"])
@pytest.mark.asyncio
async def test_no_id_write_is_once_and_requires_confirmed_readback(outcome):
    before = form(focused=1)
    after = form(focused=1, values={1: "final" if outcome != "different" else "old"})
    if outcome == "lost_identity":
        after.ui.elements[1].hint = ""
    calls = []
    async def observe(**kwargs):
        return deepcopy(before)
    async def act(action, current, **kwargs):
        calls.append(action.type)
        assert action.index is None
        return ActionResult(success=True), after
    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act, observe_current=observe),
        Action(type="replace_text", index=6, text="final"), before,
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1)
    assert calls == ["replace_text"]
    assert result.success is (outcome == "exact")
    assert result.detail["device_action_units"] == 1
    assert result.detail["input_steps"]["readback"] == {
        "exact": "exact_match", "different": "different", "lost_identity": "unknown",
    }[outcome]
