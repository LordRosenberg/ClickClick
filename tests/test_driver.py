"""Driver fixture + stub + RPC tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from driver.fixture import FixtureDriver
from driver.rpc_server import create_driver_app
from driver.stubs import IOSDriver, WindowsDriver
from shared.protocol import UnsupportedPlatformError
from shared.schemas import Action


@pytest.mark.asyncio
async def test_fixture_driver_ui_and_act():
    drv = FixtureDriver()
    ui = await drv.get_ui_state()
    assert ui.elements
    assert ui.elements[0].text == "Play"
    shot = await drv.screenshot()
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"
    res = await drv.act(Action(type="tap", index=0))
    assert res.success


@pytest.mark.asyncio
async def test_driver_rpc_asgi():
    app = create_driver_app(FixtureDriver())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        h = await client.get("/health")
        assert h.json()["ok"] is True
        r = await client.post("/rpc", json={"method": "get_ui_state", "params": {}, "id": 1})
        body = r.json()
        assert "result" in body
        assert body["result"]["elements"]


@pytest.mark.asyncio
async def test_windows_ios_stubs():
    with pytest.raises(UnsupportedPlatformError):
        await WindowsDriver().get_ui_state()
    with pytest.raises(UnsupportedPlatformError):
        await IOSDriver().act(Action(type="tap", index=0))
    with pytest.raises(UnsupportedPlatformError):
        await WindowsDriver().current_foreground_identity()


@pytest.mark.asyncio
async def test_driver_client_rpc_get_frame_and_act():
    """Remote RPC round-trip: `DriverClient` over ASGI transport against the
    driver app returns an aligned (tree, png) frame and lands `act(tap_xy)`
    on the backing driver."""
    from httpx import ASGITransport, AsyncClient

    from agent.driver_client import DriverClient

    backing = FixtureDriver()
    app = create_driver_app(backing)
    transport = ASGITransport(app=app)
    client = DriverClient("http://driver.test", transport=transport)

    tree, shot = await client.get_frame()
    assert isinstance(tree, dict)
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"

    from driver.observation_deadline import ObservationDeadline

    deadline = ObservationDeadline("current", 500)
    tree, shot, metadata = await client.capture_deadline_frame(deadline)
    assert isinstance(tree, dict)
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"
    assert metadata["provider"] == "remote_get_frame"

    identity = await client.current_foreground_identity()
    assert identity["package"] == "com.example.demo"
    assert identity["activity"] == ".MainActivity"
    assert identity["conflict"] is False

    res = await client.act(Action(type="tap_xy", x=250, y=250))
    assert res.success
    # The action landed on the backing fixture driver.
    assert len(backing.actions) == 1
    assert backing.actions[0].type == "tap_xy"
    assert (backing.actions[0].x, backing.actions[0].y) == (250, 250)

    # health also round-trips.
    h = await client.health()
    assert h["ok"] is True

    assert (await client.begin_task_session("task-1"))["status"] == "disabled"
    assert (await client.end_task_session("task-1"))["status"] == "not_active"
    assert backing.task_sessions == [("begin", "task-1"), ("end", "task-1")]


@pytest.mark.asyncio
async def test_driver_client_preserves_identity_timeout_fact_over_rpc():
    from agent.driver_client import DriverClient

    class IdentityTimeoutDriver(FixtureDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            assert timeout_s == 1.2
            return {
                "package": "",
                "activity": "",
                "component": "",
                "sources": [],
                "conflict": False,
                "timed_out": True,
                "error": "adb command timed out",
            }

    transport = ASGITransport(app=create_driver_app(IdentityTimeoutDriver()))
    client = DriverClient("http://driver.test", transport=transport)

    identity = await client.current_foreground_identity(timeout_s=1.2)

    assert identity["package"] == ""
    assert identity["timed_out"] is True
    assert identity["error"] == "adb command timed out"


@pytest.mark.asyncio
async def test_driver_client_preserves_typed_remote_capture_failure():
    from agent.driver_client import DriverClient
    from driver.observation_deadline import ObservationDeadline, ObservationStageError

    class FailingDriver(FixtureDriver):
        async def capture_deadline_frame(self, deadline):
            attempt = {
                "provider": "scrcpy_start", "status": "timeout",
                "budget_ms": 50.0, "elapsed_ms": 50.0,
                "error": "scrcpy_start_timeout",
            }
            deadline.record_attempt(attempt)
            raise ObservationStageError(
                "provider_wait", "scrcpy_start_timeout",
                elapsed_ms=50.0, budget_ms=50.0, timed_out=True,
                provider_attempts=list(deadline.provider_attempts),
            )

    transport = ASGITransport(app=create_driver_app(FailingDriver()))
    client = DriverClient("http://driver.test", transport=transport)
    deadline = ObservationDeadline("current", 500)

    with pytest.raises(ObservationStageError) as raised:
        await client.capture_deadline_frame(deadline)

    assert raised.value.stage == "provider_wait"
    assert raised.value.reason == "scrcpy_start_timeout"
    assert raised.value.timed_out is True
    assert raised.value.provider_attempts == deadline.provider_attempts
    assert deadline.provider_attempts[0]["provider"] == "scrcpy_start"


@pytest.mark.asyncio
async def test_remote_capture_has_server_local_hard_fuse():
    import asyncio

    from agent.driver_client import DriverClient
    from driver.observation_deadline import ObservationDeadline, ObservationStageError

    cancelled = asyncio.Event()

    class HangingDriver(FixtureDriver):
        async def capture_deadline_frame(self, _deadline):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    transport = ASGITransport(app=create_driver_app(HangingDriver()))
    client = DriverClient("http://driver.test", transport=transport)

    with pytest.raises(ObservationStageError) as raised:
        await client.capture_deadline_frame(ObservationDeadline("current", 40))

    assert raised.value.stage == "remote_capture"
    assert raised.value.reason == "server_deadline_exhausted"
    assert raised.value.timed_out is True
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_driver_client_rejects_empty_url():
    from agent.driver_client import DriverClient

    with pytest.raises(ValueError):
        DriverClient("")


@pytest.mark.asyncio
async def test_driver_client_does_not_inherit_host_proxy(monkeypatch):
    from agent.driver_client import DriverClient

    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "1", "result": {"ok": True}}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("agent.driver_client.httpx.AsyncClient", Client)
    result = await DriverClient("http://driver.test")._rpc("health")

    assert result == {"ok": True}
    assert captured["trust_env"] is False


@pytest.mark.device
@pytest.mark.asyncio
async def test_android_device_smoke():
    """Optional smoke: requires CLICKCLICK device + Portal."""
    pytest.skip("enable manually with a connected device")


@pytest.mark.asyncio
async def test_android_driver_set_last_ui_keeps_frame_cache_current():
    """`set_last_ui` injects the current frame so driver-side index resolution
    uses it instead of a stale `_last_ui` (the `get_frame()` path does not
    update `_last_ui` itself)."""
    from driver.android import AndroidDriver
    from perception.normalizer import normalize_a11y_tree
    from shared.schemas import Action, CanonicalUI

    drv = AndroidDriver(serial="fake")
    # No frame yet -> index resolution fails.
    assert drv._last_ui is None
    x, y = drv._resolve_xy(Action(type="tap", index=0))
    assert x is None and y is None

    # Inject a frame with one interactable element at index 0.
    ui = normalize_a11y_tree(
        {"class": "FrameLayout", "bounds": [0, 0, 1080, 2400], "children": [
            {"class": "android.widget.Button", "text": "Play", "clickable": True,
             "bounds": [100, 200, 400, 300]},
        ]}
    )
    drv.set_last_ui(ui)
    assert drv._last_ui is ui
    # Now index 0 resolves to the element center.
    x, y = drv._resolve_xy(Action(type="tap", index=0))
    assert x == 250 and y == 250


@pytest.mark.asyncio
async def test_android_driver_long_press_index_uses_current_frame():
    """A `long_press(index)` reaching the driver without agent-side resolution
    resolves against the current frame (via `_last_ui` kept fresh by
    `set_last_ui`), not a stale earlier frame."""
    from driver.android import AndroidDriver
    from perception.normalizer import normalize_a11y_tree
    from shared.schemas import Action

    drv = AndroidDriver(serial="fake")
    ui = normalize_a11y_tree(
        {"class": "FrameLayout", "bounds": [0, 0, 1080, 2400], "children": [
            {"class": "android.widget.Button", "text": "Hold", "clickable": True,
             "bounds": [10, 10, 110, 110]},
        ]}
    )
    drv.set_last_ui(ui)
    # `_resolve_xy` is the shared index->coords path for tap and long_press.
    x, y = drv._resolve_xy(Action(type="long_press", index=0))
    assert x == 60 and y == 60
    # scroll coords also use the injected frame's root bounds.
    x1, y1, x2, y2 = drv._scroll_coords("up")
    assert (x1, y1, x2, y2) == (540, 984, 540, 1416)


def test_scroll_uses_exact_frame_geometry_with_zero_area_windowset_root():
    from driver.android import AndroidDriver
    from driver.scrcpy_observation import FrameGeometry
    from perception.normalizer import normalize_a11y_tree

    drv = AndroidDriver(serial="fake")
    drv.set_last_ui(normalize_a11y_tree({
        "class": "android.view.accessibility.WindowSet",
        "window_wrapper": True,
        "bounds": [0, 0, 0, 0],
        "children": [],
    }))
    drv._last_frame_geometry = FrameGeometry(1200, 2670, 1200, 2670)  # noqa: SLF001
    assert drv._scroll_coords("up") == (600, 1095, 600, 1575)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Causal capture + dispatch-only action boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_android_driver_get_frame_returns_one_tree_and_one_pixel_capture(monkeypatch):
    """One fresh capture obtains one tree and one pixel source without ordering proof."""
    from driver import adb
    from driver.android import AndroidDriver

    drv = AndroidDriver(serial="fake", capture_budget_ms=500)
    calls: list[str] = []

    async def fake_dump(serial, *, max_attempts=1, timeout=None):
        calls.append("dump")
        return (
            '<?xml version="1.0"?><hierarchy rotation="0">'
            '<node text="Ready" class="android.widget.TextView" '
            'package="com.example.demo" bounds="[0,0][100,100]"/>'
            "</hierarchy>"
        )

    async def fake_screencap(serial, *, timeout=None):
        calls.append("screencap")
        return await FixtureDriver().screenshot()

    monkeypatch.setattr(adb, "uiautomator_dump_async", fake_dump)
    monkeypatch.setattr(adb, "screencap_async", fake_screencap)

    tree, shot = await drv.get_frame()
    capture = tree["_capture"]
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"
    assert tree["text"] == "Ready"
    assert sorted(calls) == ["dump", "screencap"]
    assert capture["coordinate_compatible"] is True


@pytest.mark.asyncio
async def test_android_driver_actions_dispatch_without_implicit_sleep(monkeypatch):
    """The driver never treats a fixed delay as proof that an effect completed."""
    import asyncio as _asyncio

    from driver import adb
    from driver.android import AndroidDriver

    drv = AndroidDriver(serial="fake")
    taps: list[tuple[float, float]] = []
    sleeps: list[float] = []
    real_sleep = _asyncio.sleep

    async def fake_tap(serial, x, y):
        taps.append((x, y))

    async def spy_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(adb, "input_tap_async", fake_tap)
    monkeypatch.setattr("driver.android.asyncio.sleep", spy_sleep)

    result = await drv.act(Action(type="tap_xy", x=100, y=200))
    assert result.success
    assert taps == [(100, 200)]
    assert sleeps == []
