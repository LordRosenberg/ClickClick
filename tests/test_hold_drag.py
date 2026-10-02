"""Continuous hold-and-drag must survive grounding and keep old gestures intact."""
import asyncio

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent.executor import _submitted_action_snapshot, rescale_action_xy
from agent.session import executor_action_variants_schema
from driver import adb
from driver.android import AndroidDriver
from shared.schemas import Action


@pytest.mark.parametrize("hold", [None, False, True])
def test_drag_uses_one_continuous_primitive_and_preserves_default(monkeypatch, hold):
    calls = []
    monkeypatch.setattr(adb, "input_swipe", lambda *args: calls.append(("swipe", args)))
    monkeypatch.setattr(adb, "input_hold_drag", lambda *args: calls.append(("hold", args)))
    action = Action(type="drag", x=10, y=20, x2=110, y2=220, hold_before_move=hold)
    transformed = rescale_action_xy(action, sx=2, sy=3)
    assert _submitted_action_snapshot(transformed).hold_before_move is hold
    assert Action.model_validate_json(transformed.model_dump_json()).hold_before_move is hold
    result = asyncio.run(AndroidDriver(serial="test-device").act(transformed))
    assert result.success
    assert calls == [("hold" if hold else "swipe", ("test-device", 20, 60, 220, 660, 500))]


def test_hold_drag_command_retains_contact_in_one_android_input_command(monkeypatch):
    calls = []
    monkeypatch.setattr(adb, "adb_bin", lambda: "adb")
    monkeypatch.setattr(adb, "_run", lambda command: calls.append(command))
    adb.input_hold_drag("device", 10, 20, 110, 220, 800)
    assert calls == [["adb", "-s", "device", "shell", "input", "draganddrop",
                      "10", "20", "110", "220", "800"]]


def test_hold_option_only_exposed_for_drag_and_rejects_wrong_types():
    schema = Draft202012Validator(executor_action_variants_schema())
    payload = {"type": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4, "hold_before_move": True}
    schema.validate(payload)
    assert list(schema.iter_errors({**payload, "type": "swipe"}))
    with pytest.raises(ValidationError, match="only supported for drag"):
        Action.model_validate({**payload, "type": "swipe"})
    with pytest.raises(ValidationError):
        Action.model_validate({**payload, "hold_before_move": "false"})
    # Ordinary actions do not add an irrelevant false option to model history.
    assert "hold_before_move" not in Action(type="back").model_dump(exclude_none=True)


def test_failed_hold_drag_is_not_replayed_as_a_swipe(monkeypatch):
    calls = []
    def fail(*args):
        calls.append("hold")
        raise RuntimeError("gesture outcome unknown")
    monkeypatch.setattr(adb, "input_hold_drag", fail)
    monkeypatch.setattr(adb, "input_swipe", lambda *args: calls.append("swipe"))
    with pytest.raises(RuntimeError, match="outcome unknown"):
        asyncio.run(AndroidDriver().act(Action(type="drag", x=1, y=2, x2=3, y2=4, hold_before_move=True)))
    assert calls == ["hold"]
