from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.executor import resolve_tap_index
from driver.accessibility import AccessibilityCollectorClient
from driver.android import AndroidDriver
from shared.schemas import Action, CanonicalUI, UIElement


def bound_tap():
    return resolve_tap_index(Action(type="tap", index=19), CanonicalUI(elements=[
        UIElement(index=19, bounds=[10, 20, 30, 40], node_handle="original-node"),
    ]), native_node_click=True)


def test_binding_is_not_new_tree_index_or_coordinates(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_NODE_CLICK_ENABLED", "1")
    action = bound_tap()
    assert action.type == "tap" and action._node_handle == "original-node"
    assert action.x is None and action.y is None
    assert "original-node" not in action.model_dump_json()
    assert not Action.model_validate(action.model_dump())._node_handle
    # Neither a model-authored field nor persisted action can inject a handle.
    assert not Action(type="tap", index=19, _node_handle="injected")._node_handle
    ui = CanonicalUI(elements=[UIElement(index=19, bounds=[10, 20, 30, 40], node_handle="original-node")])
    assert resolve_tap_index(Action(type="tap", index=19), ui) == Action(type="tap_xy", x=20, y=30)
    monkeypatch.setenv("CLICKCLICK_NODE_CLICK_ENABLED", "0")
    assert bound_tap() == Action(type="tap_xy", x=20, y=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,attempted", [
    ("obsolete", False), ("identity_changed", False), ("expired_or_consumed", False),
    ("not_performed", True), ("outcome_unknown", True), ("performed", True),
])
async def test_native_result_never_falls_back_to_adb(monkeypatch, status, attempted):
    from driver import adb
    tap = AsyncMock(side_effect=AssertionError("must not tap stale coordinates"))
    monkeypatch.setattr(adb, "input_tap_async", tap)
    driver = AndroidDriver(serial="test", collector_enabled=True)
    driver._collector = SimpleNamespace(click_node=AsyncMock(return_value={
        "node_click_status": status, "action_attempted": attempted,
        "performed": status == "performed" if attempted else None,
    }))
    result = await driver.act(bound_tap())
    assert result.success == (status == "performed")
    assert result.detail["node_click_status"] == status
    driver._collector.click_node.assert_awaited_once_with("original-node")
    tap.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_is_uncertain_and_not_replayed(monkeypatch):
    from driver import adb
    monkeypatch.setattr(adb, "input_tap_async", AsyncMock(side_effect=AssertionError("no retry")))
    client = AccessibilityCollectorClient("test")
    control = AsyncMock(side_effect=TimeoutError())
    client._channel = SimpleNamespace(control=control)
    driver = AndroidDriver(serial="test", collector_enabled=True)
    driver._collector = client
    result = await driver.act(bound_tap())
    assert result.detail["node_click_status"] == "outcome_unknown"
    control.assert_awaited_once()
