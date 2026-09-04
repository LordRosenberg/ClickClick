"""ADB-direct driver: uiautomator XML parsing, command construction, get_frame."""

import asyncio
from pathlib import Path
import time

import pytest

from driver import adb
from driver.android import AndroidDriver, _KEYCODES
from perception.uiautomator import extract_package, parse_uiautomator_xml
from shared.schemas import Action, ActionResult

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" package="com.example.demo"
        content-desc="" bounds="[0,0][1080,2400]" clickable="false">
    <node index="0" text="OK" class="android.widget.Button" package="com.example.demo"
          content-desc="" bounds="[100,200][400,300]" clickable="true"/>
    <node index="1" text="label" class="android.widget.TextView" package="com.example.demo"
          content-desc="" bounds="[100,400][800,460]" clickable="false"/>
  </node>
</hierarchy>"""

EXTENDED_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="搜索框" class="android.widget.FrameLayout" package="com.example.demo"
        content-desc="搜索框说明" hint="搜索关键词" bounds="[0,0][1080,2400]" clickable="false"
        focusable="true" scrollable="false" editable="true"
        long-clickable="true" checkable="false" checked="false"
        selected="true" password="false" resource-id="com.example.demo:id/search_container" />
</hierarchy>"""


def _async_returns(value):
    """Build a one-line coroutine that resolves to `value`. Used to fake adb
    async wrappers in monkeypatch setattrs (e.g. `adb.ime_list_enabled_async`)."""
    async def _coro(*_args, **_kwargs):
        return value

    return _coro


def test_activity_identity_prefers_top_resumed_over_secondary_resumed() -> None:
    identity = adb._parse_current_activity_identity("""
      mResumedActivity: ActivityRecord{1 u0 com.secondary/.SplitActivity t2}
      topResumedActivity=ActivityRecord{2 u0 com.primary/.Main$Inner t1}
      mResumedActivity: ActivityRecord{3 u0 com.primary/.Main$Inner t1}
    """)

    assert identity["package"] == "com.primary"
    assert identity["activity"] == ".Main$Inner"
    assert identity["conflict"] is False
    assert len(identity["sources"]) == 1


def test_activity_identity_reports_conflicting_resumed_facts_without_top() -> None:
    identity = adb._parse_current_activity_identity("""
      mResumedActivity: ActivityRecord{1 u0 com.one/.Main t1}
      mResumedActivity: ActivityRecord{2 u0 com.two/.Main t2}
    """)

    assert identity["package"] == "com.one"
    assert identity["conflict"] is True


def test_activity_identity_accepts_oem_resumed_activity_field() -> None:
    identity = adb._parse_current_activity_identity("""
      Resumed activities in task display areas (from top to bottom):
        Resumed: ActivityRecord{1 u0 com.example/.Ignored t1}
      ResumedActivity: ActivityRecord{2 u0 com.example/.Main t1}
      mFocusedApp=ActivityRecord{2 u0 com.example/.Main t1}
    """)

    assert identity["package"] == "com.example"
    assert identity["activity"] == ".Main"
    assert identity["conflict"] is False


@pytest.mark.asyncio
async def test_android_identity_accepts_transaction_owned_timeout(monkeypatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    async def identity(serial, **kwargs):
        calls.append((serial, kwargs))
        return {"package": "com.example", "conflict": False}

    monkeypatch.setattr(adb, "current_activity_identity_async", identity)
    result = await AndroidDriver(serial="S1").current_foreground_identity(
        timeout_s=1.25
    )

    assert result["package"] == "com.example"
    assert calls == [("S1", {"timeout": 1.25})]


@pytest.mark.asyncio
async def test_async_activity_identity_uses_cancellable_adb_process(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []

    async def run(args, *, timeout):
        calls.append((args, timeout))
        return b"topResumedActivity=ActivityRecord{1 u0 com.example/.Main t1}"

    monkeypatch.setattr(adb, "_run_async", run)
    identity = await adb.current_activity_identity_async("S1", timeout=1.5)

    assert identity["package"] == "com.example"
    assert calls[0][0][-4:] == ["shell", "dumpsys", "activity", "activities"]
    assert calls[0][1] == 1.5


@pytest.mark.asyncio
async def test_async_activity_identity_preserves_typed_subprocess_timeout(
    monkeypatch,
) -> None:
    async def run(_args, *, timeout):
        assert timeout == 1.2
        raise adb.AdbTimeoutError("adb command timed out: dumpsys activity")

    monkeypatch.setattr(adb, "_run_async", run)
    identity = await adb.current_activity_identity_async("S1", timeout=1.2)

    assert identity["package"] == ""
    assert identity["conflict"] is False
    assert identity["timed_out"] is True
    assert "timed out" in str(identity["error"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (b"Physical size: 1200x2670\n", (1200, 2670)),
        (b"Physical size: 1200x2670\nOverride size: 1080x2400\n", (1080, 2400)),
    ],
)
async def test_display_size_prefers_current_override(monkeypatch, output, expected):
    async def shell(*_args, **_kwargs):
        return output

    monkeypatch.setattr(adb, "shell_async", shell)
    assert await adb.display_size_async("S1") == expected


@pytest.mark.asyncio
async def test_successful_dispatched_action_commits_scrcpy_frame_boundary(monkeypatch):
    events: list[object] = []
    provider = type("Provider", (), {
        "frame_boundary": lambda self: events.append("snapshot") or (7, 12),
        "mark_action_complete": lambda self, value: events.append(("commit", value)),
    })()
    driver = AndroidDriver(stream_provider=provider)

    async def success(_action, *, before_dispatch):
        before_dispatch()
        events.append("dispatch")
        return ActionResult(success=True, message="tap")

    monkeypatch.setattr(driver, "_act", success)
    assert (await driver.act(Action(type="tap", x=1, y=2))).success
    assert events == ["snapshot", "dispatch", ("commit", (7, 12))]


@pytest.mark.asyncio
async def test_failed_or_wait_action_does_not_mark_scrcpy_boundary(monkeypatch):
    boundaries: list[object] = []
    provider = type("Provider", (), {
        "frame_boundary": lambda self: (7, 12),
        "mark_action_complete": lambda self, value: boundaries.append(value)
    })()
    driver = AndroidDriver(stream_provider=provider)

    async def failure(_action, *, before_dispatch):
        before_dispatch()
        return ActionResult(success=False, message="not dispatched")

    monkeypatch.setattr(driver, "_act", failure)
    assert not (await driver.act(Action(type="tap", x=1, y=2))).success

    async def waited(_action, *, before_dispatch):
        del before_dispatch
        return ActionResult(success=True, message="sleep")

    monkeypatch.setattr(driver, "_act", waited)
    assert (await driver.act(Action(type="sleep", duration_ms=1))).success
    assert boundaries == []


@pytest.mark.asyncio
async def test_launch_boundary_is_taken_after_resolution_and_before_adb(monkeypatch):
    events: list[object] = []
    provider = type("Provider", (), {
        "frame_boundary": lambda self: events.append("snapshot") or (2, 8),
        "mark_action_complete": lambda self, value: events.append(("commit", value)),
    })()
    driver = AndroidDriver(stream_provider=provider)

    async def resolve(_app):
        events.append("resolve")
        return "com.example.demo"

    async def launch(_serial, package):
        events.append(("adb", package))

    monkeypatch.setattr(driver, "_resolve_app_name", resolve)
    monkeypatch.setattr(adb, "am_start_async", launch)

    result = await driver.act(Action(type="launch", app="Demo"))

    assert result.success is True
    assert events == [
        "resolve",
        "snapshot",
        ("adb", "com.example.demo"),
        ("commit", (2, 8)),
    ]


@pytest.mark.asyncio
async def test_get_frame_captures_scrcpy_and_collector_tree_concurrently(monkeypatch):
    """One capture keeps independently valid scrcpy and tree evidence."""
    import asyncio
    from types import SimpleNamespace

    from driver.scrcpy_observation import FrameGeometry, FrameHandle

    order: list[str] = []
    tree_finished = False
    pixels = _content_png()
    frame = None

    class Provider:
        async def start(self):
            order.append("provider_start")

        def current(self):
            nonlocal frame
            order.append("frame")
            frame = FrameHandle(
                pixels,
                timestamp=time.monotonic(),
                generation=7,
                geometry=FrameGeometry(1080, 2400, 1080, 2400),
            )
            return SimpleNamespace(status="healthy", frame=frame)

    driver = AndroidDriver(stream_provider=Provider(), collector_enabled=True)

    async def collector_tree(*, timeout_s, provider="combined"):
        nonlocal tree_finished
        del timeout_s, provider
        order.append("tree_start")
        await asyncio.sleep(0)
        tree_finished = True
        order.append("tree_done")
        return {
            "class": "Root",
            "text": "current tree",
            "children": [],
            "_capture": {
                "complete": True,
                "coordinate_compatible": True,
            },
        }

    async def forbidden_screencap(_serial, *, timeout=None):
        raise AssertionError("healthy scrcpy frame should be used")

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(adb, "screencap_async", forbidden_screencap)

    tree, shot = await driver.get_frame()

    assert tree["text"] == "current tree"
    assert shot == pixels
    assert "tree_done" in order
    assert "frame" in order


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("mInteractive=true", True),
        ("mInteractive=false", False),
        ("mWakefulness=Awake", True),
        ("mWakefulness=Asleep", False),
        ("Display Power: state=ON", True),
        ("Display Power: state=OFF", False),
        ("unrecognised vendor output", None),
    ],
)
def test_screen_interactive_parses_power_state(monkeypatch, output, expected):
    monkeypatch.setattr(adb, "adb_bin", lambda: "adb")
    monkeypatch.setattr(adb, "_run", lambda *_args, **_kwargs: output.encode())

    assert adb.screen_interactive("serial-1") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [True, None])
async def test_wake_and_unlock_is_inert_unless_screen_confirmed_off(
    monkeypatch, interactive
):
    calls: list[tuple] = []
    drv = AndroidDriver(serial="serial-1")

    monkeypatch.setattr(adb, "screen_interactive_async", _async_returns(interactive))

    async def fake_keyevent(*args):
        calls.append(("keyevent", *args))

    async def fake_dismiss(*args):
        calls.append(("dismiss", *args))

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "dismiss_keyguard_async", fake_dismiss)

    await drv.wake_and_unlock()

    assert calls == []


@pytest.mark.asyncio
async def test_wake_and_unlock_wakes_off_screen_without_touch_gesture(monkeypatch):
    calls: list[tuple] = []
    drv = AndroidDriver(serial="serial-1")

    monkeypatch.setattr(adb, "screen_interactive_async", _async_returns(False))

    async def fake_keyevent(*args):
        calls.append(("keyevent", *args))

    async def fake_dismiss(*args):
        calls.append(("dismiss", *args))

    async def fake_sleep(delay):
        calls.append(("sleep", delay))

    async def forbidden_swipe(*_args):
        raise AssertionError("wake_and_unlock must never inject a swipe")

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "dismiss_keyguard_async", fake_dismiss)
    monkeypatch.setattr(adb, "input_swipe_async", forbidden_swipe)
    monkeypatch.setattr("driver.android.asyncio.sleep", fake_sleep)

    await drv.wake_and_unlock()

    assert calls == [
        ("keyevent", "serial-1", 224),
        ("sleep", 0.4),
        ("dismiss", "serial-1"),
    ]


def test_parse_uiautomator_xml_maps_fields():
    tree = parse_uiautomator_xml(SAMPLE_XML)
    assert tree["class"] == "android.widget.FrameLayout"
    assert tree["bounds"] == [0, 0, 1080, 2400]
    assert tree["clickable"] is False
    assert len(tree["children"]) == 2
    btn = tree["children"][0]
    assert btn["class"] == "android.widget.Button"
    assert btn["text"] == "OK"
    assert btn["bounds"] == [100, 200, 400, 300]
    assert btn["clickable"] is True
    assert btn["desc"] == ""


def test_parse_uiautomator_xml_preserves_extended_interaction_fields():
    tree = parse_uiautomator_xml(EXTENDED_XML)
    assert tree["text"] == "搜索框"
    assert tree["desc"] == "搜索框说明"
    assert tree["hint"] == "搜索关键词"
    assert tree["focusable"] is True
    assert tree["scrollable"] is False
    assert tree["editable"] is True
    assert tree["long_clickable"] is True
    assert tree["checkable"] is False
    assert tree["checked"] is False
    assert tree["selected"] is True
    assert tree["password"] is False
    assert tree["resource_id"] == "com.example.demo:id/search_container"


def test_parse_uiautomator_xml_empty():
    tree = parse_uiautomator_xml("")
    assert tree == {"class": "", "children": []}


def test_extract_package():
    assert extract_package(SAMPLE_XML) == "com.example.demo"
    assert extract_package("") == ""


def test_keycode_map_covers_common_keys():
    assert _KEYCODES["back"] == 4
    assert _KEYCODES["home"] == 3
    assert _KEYCODES["enter"] == 66


def test_android_driver_tap_resolves_index_to_center(monkeypatch):
    drv = AndroidDriver()
    calls: list[tuple] = []

    def fake_tap(serial, x, y):
        calls.append((x, y))

    monkeypatch.setattr(adb, "input_tap", fake_tap)
    # Simulate a cached last UI with one element at bounds [100,200,400,300].
    from perception.normalizer import normalize_a11y_tree
    from shared.schemas import CanonicalUI

    ui = normalize_a11y_tree(
        {"class": "Root", "children": [
            {"class": "android.widget.Button", "text": "OK", "clickable": True, "bounds": [100, 200, 400, 300]},
        ]}
    )
    drv._last_ui = ui

    import asyncio

    res = asyncio.run(drv.act(Action(type="tap", index=0)))
    assert res.success
    assert calls == [(250, 250)]  # center of [100,200,400,300]


def test_android_driver_tap_unknown_index_fails():
    drv = AndroidDriver()
    import asyncio

    res = asyncio.run(drv.act(Action(type="tap", index=99)))
    assert not res.success
    assert "unresolved" in res.message


def test_android_driver_long_press_via_swipe(monkeypatch):
    """long_press resolves to a swipe with same start/end + duration."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    calls: list[tuple] = []

    def fake_swipe(serial, x1, y1, x2, y2, dur):
        calls.append((x1, y1, x2, y2, dur))

    monkeypatch.setattr(adb, "input_swipe", fake_swipe)
    from perception.normalizer import normalize_a11y_tree

    ui = normalize_a11y_tree(
        {"class": "Root", "children": [
            {"class": "android.widget.Button", "text": "OK", "clickable": True, "bounds": [100, 200, 400, 300]},
        ]}
    )
    drv._last_ui = ui

    res = asyncio.run(drv.act(Action(type="long_press", index=0, duration_ms=1500)))
    assert res.success
    assert res.message == "long_press"
    # Same start/end point → emulates a long press.
    assert calls == [(250, 250, 250, 250, 1500)]


def test_android_driver_scroll_directions(monkeypatch):
    """Content direction maps to the opposite screen-relative finger gesture."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    drv._last_frame_geometry = (1080, 2400)  # noqa: SLF001
    calls: list[tuple] = []

    def fake_swipe(serial, x1, y1, x2, y2, dur):
        calls.append((x1, y1, x2, y2))

    monkeypatch.setattr(adb, "input_swipe", fake_swipe)

    for d in ("up", "down", "left", "right"):
        res = asyncio.run(drv.act(Action(type="scroll", direction=d)))
        assert res.success, f"scroll {d} failed: {res.message}"
        assert res.message == f"scroll:{d}"
    assert calls == [
        (540, 984, 540, 1416),   # up reveals content above: finger down
        (540, 1416, 540, 984),   # down reveals content below: finger up
        (324, 1200, 756, 1200),  # left reveals content left: finger right
        (756, 1200, 324, 1200),  # right reveals content right: finger left
    ]


def test_android_driver_scroll_without_viewport_does_not_dispatch(monkeypatch):
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    calls: list[tuple] = []
    monkeypatch.setattr(adb, "input_swipe", lambda *args: calls.append(args))

    result = asyncio.run(drv.act(Action(type="scroll", direction="up")))
    assert result.success is False
    assert "geometry unavailable" in result.message
    assert calls == []


def test_adb_screenshot_refreshes_rotated_scroll_geometry(monkeypatch):
    import asyncio
    from io import BytesIO

    from PIL import Image
    from driver import adb

    out = BytesIO()
    Image.new("RGB", (2400, 1080), "black").save(out, format="PNG")
    drv = AndroidDriver()
    drv._last_frame_geometry = (1080, 2400)  # noqa: SLF001
    calls: list[tuple] = []

    async def fake_screencap(serial):
        del serial
        return out.getvalue()

    monkeypatch.setattr(adb, "screencap_async", fake_screencap)
    monkeypatch.setattr(adb, "input_swipe", lambda *args: calls.append(args))

    asyncio.run(drv.screenshot())
    result = asyncio.run(drv.act(Action(type="scroll", direction="up")))

    assert result.success is True
    assert drv.last_frame_geometry == (2400, 1080)
    assert result.detail["from"][0] == 1200
    assert calls


def test_android_driver_scroll_missing_direction_fails():
    import asyncio

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="scroll")))
    assert not res.success
    assert "direction" in res.message


def test_android_driver_key_normal_when_no_duration(monkeypatch):
    """key without duration_ms → normal keyevent, no --longpress."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    keyevent_calls: list = []
    longpress_calls: list = []

    async def fake_keyevent(serial, keycode):
        keyevent_calls.append(keycode)

    async def fake_keyevent_longpress(serial, keycode):
        longpress_calls.append(keycode)

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "input_keyevent_longpress_async", fake_keyevent_longpress)

    res = asyncio.run(drv.act(Action(type="key", key="delete")))
    assert res.success
    assert res.message == "key:delete"
    # _KEYCODES["delete"] == 67; driver resolves the name before calling adb.
    assert keyevent_calls == [67]
    assert longpress_calls == []


def test_android_driver_key_longpress_when_duration_ge_500(monkeypatch):
    """key with duration_ms >= 500 → --longpress keyevent."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    keyevent_calls: list = []
    longpress_calls: list = []

    async def fake_keyevent(serial, keycode):
        keyevent_calls.append(keycode)

    async def fake_keyevent_longpress(serial, keycode):
        longpress_calls.append(keycode)

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "input_keyevent_longpress_async", fake_keyevent_longpress)

    res = asyncio.run(drv.act(Action(type="key", key="delete", duration_ms=1000)))
    assert res.success
    assert res.message == "key:delete:longpress"
    assert longpress_calls == [67]
    assert keyevent_calls == []
    assert res.detail.get("duration_ms") == 1000


def test_android_driver_key_longpress_does_not_change_semantics_on_unsupported(monkeypatch):
    """If --longpress raises, no normal keyevent substitute is dispatched."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    keyevent_calls: list = []
    longpress_calls: list = []

    async def fake_keyevent(serial, keycode):
        keyevent_calls.append(keycode)

    async def fake_keyevent_longpress(serial, keycode):
        longpress_calls.append(keycode)
        raise RuntimeError("Unknown option: --longpress")

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "input_keyevent_longpress_async", fake_keyevent_longpress)

    res = asyncio.run(drv.act(Action(type="key", key="delete", duration_ms=1000)))
    assert not res.success
    assert res.message == "key:delete:longpress_failed"
    assert longpress_calls == [67]
    assert keyevent_calls == []
    assert res.detail.get("longpress_unsupported") is True
    assert res.detail.get("dispatch_uncertain") is True


def test_android_driver_back_key_without_duration_uses_normal_keyevent(monkeypatch):
    """`back` action (no duration_ms) → normal keyevent with keycode 4."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    keyevent_calls: list = []
    longpress_calls: list = []

    async def fake_keyevent(serial, keycode):
        keyevent_calls.append(keycode)

    async def fake_keyevent_longpress(serial, keycode):
        longpress_calls.append(keycode)

    monkeypatch.setattr(adb, "input_keyevent_async", fake_keyevent)
    monkeypatch.setattr(adb, "input_keyevent_longpress_async", fake_keyevent_longpress)

    res = asyncio.run(drv.act(Action(type="back")))
    assert res.success
    assert res.message == "key:back"
    assert keyevent_calls == [4]  # _KEYCODES["back"] == 4
    assert longpress_calls == []


def test_android_driver_drag(monkeypatch):
    """drag uses start/end coordinates + duration via input swipe."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    calls: list[tuple] = []

    def fake_swipe(serial, x1, y1, x2, y2, dur):
        calls.append((x1, y1, x2, y2, dur))

    monkeypatch.setattr(adb, "input_swipe", fake_swipe)
    res = asyncio.run(drv.act(Action(type="drag", x=100, y=200, x2=300, y2=400, duration_ms=500)))
    assert res.success
    assert res.message == "drag"
    assert calls == [(100, 200, 300, 400, 500)]


def test_android_driver_launch_by_package(monkeypatch):
    """launch with a package name passes through directly."""
    import asyncio

    from driver import adb

    drv = AndroidDriver()
    calls: list[str] = []

    def fake_am_start(serial, app):
        calls.append(app)

    monkeypatch.setattr(adb, "am_start", fake_am_start)
    res = asyncio.run(drv.act(Action(type="launch", app="com.example.demo")))
    assert res.success
    assert calls == ["com.example.demo"]


async def _async_none(_app):
    return None


async def _fake_am_start(serial, app):
    return None


class _FakeResolver:
    """Test double for `NameResolver` — maps display names deterministically."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[tuple[str, str | None]] = []

    async def resolve(self, app: str, serial: str | None) -> str | None:
        self.calls.append((app, serial))
        # Package-looking input passes through (mirrors the real resolver).
        if "." in app and len(app.split(".")) >= 2:
            return app
        return self.mapping.get(app)


def test_android_driver_launch_by_display_name_resolves(monkeypatch):
    """launch with a display name resolves via the injected two-stage resolver."""
    import asyncio

    from driver import adb

    drv = AndroidDriver(resolver=_FakeResolver({"小红书": "com.xingin.xhs"}))
    calls: list[str] = []

    def fake_am_start(serial, app):
        calls.append(app)

    monkeypatch.setattr(adb, "am_start", fake_am_start)
    res = asyncio.run(drv.act(Action(type="launch", app="小红书")))
    assert res.success
    assert res.message == "launch:com.xingin.xhs"
    assert calls == ["com.xingin.xhs"]


def test_android_driver_launch_unresolved_display_name_fails(monkeypatch):
    """A display name the resolver can't match returns a failure ActionResult."""
    import asyncio

    drv = AndroidDriver(resolver=_FakeResolver({}))  # nothing resolves
    res = asyncio.run(drv.act(Action(type="launch", app="不存在的应用")))
    assert not res.success
    assert "unresolved" in res.message


def test_android_driver_launch_resolver_exception_fails(monkeypatch):
    """A resolver exception degrades to a failure ActionResult, not a raise."""
    import asyncio

    class _Boom:
        async def resolve(self, app, serial):
            raise RuntimeError("resolver down")

    drv = AndroidDriver(resolver=_Boom())
    res = asyncio.run(drv.act(Action(type="launch", app="小红书")))
    assert not res.success
    assert "unresolved" in res.message


def test_android_driver_launch_no_resolver_still_passes_packages(monkeypatch):
    """Rollback path: with no resolver wired, package names still launch and
    display names fail (no LLM/adb resolution is attempted)."""
    import asyncio

    from driver import adb

    calls: list[str] = []

    def fake_am_start(serial, app):
        calls.append(app)

    monkeypatch.setattr(adb, "am_start", fake_am_start)
    drv = AndroidDriver()  # no resolver
    # Package name → passthrough still works.
    res = asyncio.run(drv.act(Action(type="launch", app="com.example.demo")))
    assert res.success
    assert calls == ["com.example.demo"]
    # Display name → failure (no resolution attempted).
    res2 = asyncio.run(drv.act(Action(type="launch", app="小红书")))
    assert not res2.success
    assert "unresolved" in res2.message


def test_fixture_driver_supports_new_actions():
    """FixtureDriver accepts long_press / scroll / drag."""
    import asyncio

    from driver.fixture import FixtureDriver

    drv = FixtureDriver()
    assert asyncio.run(drv.act(Action(type="long_press", index=0, duration_ms=500))).success
    assert asyncio.run(drv.act(Action(type="scroll", direction="up"))).success
    assert asyncio.run(drv.act(Action(type="drag", x=1, y=2, x2=3, y2=4, duration_ms=100))).success
    # Display-name launch in fixture → failure (no resolution).
    res = asyncio.run(drv.act(Action(type="launch", app="小红书")))
    assert not res.success
    assert "unresolved" in res.message


def test_fixture_driver_scroll_missing_direction_fails():
    import asyncio

    from driver.fixture import FixtureDriver

    drv = FixtureDriver()
    res = asyncio.run(drv.act(Action(type="scroll")))
    assert not res.success


def test_fixture_driver_get_frame():
    from driver.fixture import FixtureDriver

    drv = FixtureDriver()
    import asyncio

    tree, shot = asyncio.run(drv.get_frame())
    assert isinstance(tree, dict)
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"


def test_adb_async_wrappers_offload_to_thread(monkeypatch):
    """`*_async` wrappers run the sync kernel on a worker thread, not the
    event loop thread, so a blocking adb call cannot stall the loop."""
    import asyncio
    import threading

    from driver import adb

    loop_thread = threading.get_ident()
    seen: dict[str, object] = {}

    def fake_dump(serial, *, max_attempts=3, min_bytes=100):
        seen["thread"] = threading.get_ident()
        seen["ran"] = True
        return "<hierarchy/>"

    def fake_screencap(serial):
        seen["shot_thread"] = threading.get_ident()
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(adb, "uiautomator_dump", fake_dump)
    monkeypatch.setattr(adb, "screencap", fake_screencap)

    async def main():
        # While the (fake) blocking kernel "runs", the loop can make progress
        # on another task — proving the call is offloaded to a thread.
        progress = 0

        async def ticker():
            nonlocal progress
            for _ in range(5):
                await asyncio.sleep(0)
                progress += 1

        async def call():
            xml = await adb.uiautomator_dump_async(None)
            shot = await adb.screencap_async(None)
            return xml, shot

        ticker_task = asyncio.create_task(ticker())
        xml, shot = await call()
        await ticker_task
        return xml, shot, progress

    xml, shot, progress = asyncio.run(main())
    assert seen.get("ran") is True
    # Kernel ran on a different thread than the event loop caller.
    assert seen["thread"] != loop_thread
    assert seen["shot_thread"] != loop_thread
    # The loop made progress concurrently (ticker advanced) — non-blocking.
    assert progress > 0


def test_adb_async_timeout_surfaces_adb_error(monkeypatch):
    """A subprocess `TimeoutExpired` under `to_thread` MUST surface as `AdbError`."""
    import asyncio
    import subprocess

    from driver import adb
    from driver.adb import AdbError

    def fake_run_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0] if args else "adb", timeout=30.0)

    monkeypatch.setattr(subprocess, "run", fake_run_timeout)
    # `adb_bin` is fine (only invoked inside _run, which we bypass via the
    # monkeypatched subprocess.run). But `_run` calls `subprocess.run` directly.

    async def main():
        return await adb.uiautomator_dump_async(None)

    with pytest.raises(AdbError):
        asyncio.run(main())


def test_android_driver_get_frame_runs_off_loop(monkeypatch):
    """`AndroidDriver.get_frame()` awaits asynchronous capture stages."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from driver.observation_deadline import ObservationStageError

    rec: dict[str, object] = {}

    async def fake_dump(serial, *, max_attempts=1, timeout=None):
        await asyncio.sleep(0)
        rec["dumped"] = True
        return (
            "<?xml version='1.0'?><hierarchy rotation='0'><node "
            "index='0' text='' class='android.widget.FrameLayout' "
            "package='com.x' content-desc='' bounds='[0,0][100,100]' "
            "clickable='false'/></hierarchy>"
        )

    async def fake_screencap(serial, *, timeout=None):
        await asyncio.sleep(0)
        rec["captured"] = True
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr(adb, "uiautomator_dump_async", fake_dump)
    monkeypatch.setattr(adb, "screencap_async", fake_screencap)

    drv = AndroidDriver()

    async def main():
        progress = 0

        async def ticker():
            nonlocal progress
            for _ in range(5):
                await asyncio.sleep(0)
                progress += 1

        t = asyncio.create_task(ticker())
        tree, shot = await drv.get_frame()
        await t
        return tree, shot, progress

    tree, shot, progress = asyncio.run(main())
    assert rec == {"dumped": True, "captured": True}
    assert progress > 0
    assert isinstance(tree, dict)
    assert shot[:8] == b"\x89PNG\r\n\x1a\n"


# --- fix-stale-uiautomator-dump: rm + size-check + retry -----------------


def _build_subprocess_run_stub(
    *,
    wc_sizes: list[int],
    dump_exit_codes: list[int] | None = None,
    rm_exit_codes: list[int] | None = None,
    pull_exit_codes: list[int] | None = None,
):
    """Build a fake `subprocess.run` for the dump retry loop.

    Inspects argv to dispatch:
      - `rm -f ...` → success (or per-call rm exit code)
      - `uiautomator dump ...` → success (or per-call exit code)
      - `wc -c ...` → returns `wc_sizes[i]` bytes on call `i`
      - `pull ...` → success (or per-call exit code); writes a 2400-byte XML
        to the local_path target so read_text returns something
    """
    from driver.adb import _DEVICE_DUMP_PATH

    call_count = {"wc": 0, "dump": 0, "rm": 0, "pull": 0}

    def fake_run(args, **kwargs):
        # `args` is a list; identify by subcommand.
        if "rm" in args:
            idx = call_count["rm"]
            call_count["rm"] += 1
            exit_codes = rm_exit_codes or [0]
            rc = exit_codes[idx] if idx < len(exit_codes) else exit_codes[-1]
            if rc != 0:
                import subprocess as _sp

                raise _sp.CalledProcessError(rc, args, stderr=b"rm: failed")
            return _CompletedProcess(args, 0, b"", b"")

        if "uiautomator" in args:
            idx = call_count["dump"]
            call_count["dump"] += 1
            exit_codes = dump_exit_codes or [0]
            rc = exit_codes[idx] if idx < len(exit_codes) else exit_codes[-1]
            if rc != 0:
                import subprocess as _sp

                raise _sp.CalledProcessError(rc, args, stderr=b"uiautomator: failed")
            return _CompletedProcess(args, 0, b"", b"")

        if "wc" in args:
            idx = call_count["wc"]
            call_count["wc"] += 1
            size = wc_sizes[idx] if idx < len(wc_sizes) else wc_sizes[-1]
            return _CompletedProcess(args, 0, f"{size} {_DEVICE_DUMP_PATH}\n".encode(), b"")

        if "pull" in args:
            idx = call_count["pull"]
            call_count["pull"] += 1
            # Write a fresh XML to the local_path target on the pull.
            local_path = args[-1]
            Path(local_path).write_text(
                f"<?xml version='1.0'?><hierarchy pull_call='{idx}'/>",
                encoding="utf-8",
            )
            return _CompletedProcess(args, 0, b"", b"")

        # Default: success with empty output (e.g. adb -s devices).
        return _CompletedProcess(args, 0, b"", b"")

    return fake_run, call_count


class _CompletedProcess:
    """Minimal stand-in for subprocess.CompletedProcess used by the stub."""

    def __init__(self, args, returncode, stdout, stderr):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_uiautomator_dump_happy_path(monkeypatch, tmp_path):
    """Stable page → one round, returns the fresh XML, no retry."""
    import subprocess

    from driver import adb

    fake_run, calls = _build_subprocess_run_stub(wc_sizes=[2400])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adb, "_DEVICE_DUMP_PATH", "/sdcard/clickclick_window_dump.xml")

    xml = adb.uiautomator_dump(None)
    assert "<hierarchy" in xml
    assert "pull_call='0'" in xml
    # rm fired once, dump fired once, wc fired once, pull fired once.
    assert calls["rm"] == 1
    assert calls["dump"] == 1
    assert calls["wc"] == 1
    assert calls["pull"] == 1


def test_uiautomator_dump_retry_on_size_too_small(monkeypatch, tmp_path):
    """First dump writes 0 bytes; retry succeeds with 2400 bytes."""
    import subprocess

    from driver import adb

    fake_run, calls = _build_subprocess_run_stub(wc_sizes=[0, 2400])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adb, "_DEVICE_DUMP_PATH", "/sdcard/clickclick_window_dump.xml")

    xml = adb.uiautomator_dump(None, max_attempts=3, min_bytes=100)
    # First attempt: dump wrote 0 bytes → no pull. Second attempt: dump
    # wrote 2400 bytes → pull index 0 (this is the only pull that ran).
    assert "pull_call='0'" in xml
    # First attempt: dump ran, wc said 0, no pull. Second attempt: dump ran, wc OK, pull.
    assert calls["dump"] == 2
    assert calls["wc"] == 2
    assert calls["pull"] == 1


def test_uiautomator_dump_terminal_failure_raises(monkeypatch, tmp_path):
    """All 3 attempts write < min_bytes → AdbError, no pull."""
    import subprocess

    from driver import adb
    from driver.adb import AdbError

    fake_run, calls = _build_subprocess_run_stub(wc_sizes=[5, 3, 8])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adb, "_DEVICE_DUMP_PATH", "/sdcard/clickclick_window_dump.xml")

    with pytest.raises(AdbError) as excinfo:
        adb.uiautomator_dump(None, max_attempts=3, min_bytes=100)
    # Error message MUST mention the size, not silently fall back to old data.
    assert "bytes" in str(excinfo.value)
    assert "stale" in str(excinfo.value)
    # All 3 attempts ran, but no pull ever happened.
    assert calls["dump"] == 3
    assert calls["wc"] == 3
    assert calls["pull"] == 0


def test_uiautomator_dump_nonzero_exit_triggers_retry(monkeypatch, tmp_path):
    """dump returns non-zero on attempt 1 → retry → success on attempt 2."""
    import subprocess

    from driver import adb

    fake_run, calls = _build_subprocess_run_stub(
        wc_sizes=[2400, 2400],
        dump_exit_codes=[1, 0],
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adb, "_DEVICE_DUMP_PATH", "/sdcard/clickclick_window_dump.xml")

    xml = adb.uiautomator_dump(None)
    assert "pull_call='0'" in xml
    assert calls["dump"] == 2
    assert calls["pull"] == 1


def test_uiautomator_dump_rm_failure_is_nonfatal(monkeypatch, tmp_path):
    """If `rm` fails, the dump must still run (rm is best-effort cleanup)."""
    import subprocess

    from driver import adb

    fake_run, calls = _build_subprocess_run_stub(wc_sizes=[2400], rm_exit_codes=[1])
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(adb, "_DEVICE_DUMP_PATH", "/sdcard/clickclick_window_dump.xml")

    xml = adb.uiautomator_dump(None)
    assert "<hierarchy" in xml
    assert calls["dump"] == 1
    assert calls["pull"] == 1


def test_uiautomator_dump_async_forwards_kwargs(monkeypatch):
    """`uiautomator_dump_async` MUST forward max_attempts/min_bytes to the
    sync kernel (else callers cannot tune retry behavior)."""
    import asyncio

    from driver import adb

    seen_kwargs: dict = {}

    def fake_dump(serial, *, max_attempts=3, min_bytes=100):
        seen_kwargs["max_attempts"] = max_attempts
        seen_kwargs["min_bytes"] = min_bytes
        return "<hierarchy/>"

    monkeypatch.setattr(adb, "uiautomator_dump", fake_dump)

    async def main():
        return await adb.uiautomator_dump_async(None, max_attempts=7, min_bytes=512)

    xml = asyncio.run(main())
    assert xml == "<hierarchy/>"
    assert seen_kwargs == {"max_attempts": 7, "min_bytes": 512}


def test_uiautomator_dump_async_defaults_match_sync(monkeypatch):
    """`uiautomator_dump_async()` with no kwargs uses the module-level
    defaults (3 attempts, 100-byte floor)."""
    import asyncio

    from driver import adb

    seen_kwargs: dict = {}

    def fake_dump(serial, *, max_attempts=3, min_bytes=100):
        seen_kwargs["max_attempts"] = max_attempts
        seen_kwargs["min_bytes"] = min_bytes
        return "<hierarchy/>"

    monkeypatch.setattr(adb, "uiautomator_dump", fake_dump)

    async def main():
        return await adb.uiautomator_dump_async(None)

    asyncio.run(main())
    assert seen_kwargs == {"max_attempts": 3, "min_bytes": 100}


def test_device_file_size_parses_wc_output(monkeypatch):
    """`_device_file_size` parses `wc -c` output `  2048 path\\n` → 2048."""
    import subprocess

    from driver import adb

    def fake_run(args, **kwargs):
        return _CompletedProcess(args, 0, b"   2400 /sdcard/foo.xml\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    size = adb._device_file_size(None, "/sdcard/foo.xml")
    assert size == 2400


def test_device_file_size_invalid_output_raises(monkeypatch):
    """Garbage wc output → AdbError, not a silent 0."""
    import subprocess

    from driver import adb
    from driver.adb import AdbError

    def fake_run(args, **kwargs):
        return _CompletedProcess(args, 0, b"not a number\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdbError):
        adb._device_file_size(None, "/sdcard/foo.xml")


# --- frame-gate-aligned-observation --------------------------------------


def _png_bytes(rgb: tuple[int, int, int]) -> bytes:
    """Render a solid-color PNG. Pillow is already a project dep."""
    import io

    from PIL import Image

    img = Image.new("RGB", (320, 640), color=rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _content_png(*, seed: int = 0) -> bytes:
    """PNG with enough structure to pass the transition-blank heuristic."""
    import io

    from PIL import Image, ImageDraw

    # seed shifts colors so two content frames can differ for mismatch tests.
    img = Image.new("RGB", (320, 640), color=(30 + seed, 30, 40))
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 10, 150, 200), fill=(220, 60 + seed, 60))
    draw.rectangle((160, 220, 300, 500), fill=(60, 180, 220 - min(seed, 200)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_MIN_HIERARCHY_XML = (
    '<?xml version="1.0"?><hierarchy rotation="0">'
    '<node index="0" text="ok" class="android.widget.Button" '
    'package="com.x" content-desc="" bounds="[0,0][100,100]" '
    'clickable="true"/></hierarchy>'
)

# Pad to satisfy rich-tree byte threshold (>=400) for blank×rich tests.
_RICH_HIERARCHY_XML = (
    '<?xml version="1.0"?><hierarchy rotation="0">'
    + "".join(
        f'<node index="{i}" text="item{i}" class="android.widget.TextView" '
        f'package="com.x" content-desc="" bounds="[{i},{i}][{i+10},{i+10}]" '
        f'clickable="false"/>'
        for i in range(12)
    )
    + "</hierarchy>"
)


def test_get_frame_keeps_one_tree_and_one_screenshot_without_order_proof(monkeypatch):
    """The driver keeps both sources without claiming semantic simultaneity."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver

    content = _content_png()
    calls: list[str] = []

    async def fake_screencap(_serial, *, timeout=None):
        calls.append("screencap")
        return content

    async def fake_dump(_serial, *, max_attempts=1, timeout=None):
        calls.append("dump")
        return _MIN_HIERARCHY_XML

    monkeypatch.setattr(adb, "screencap_async", fake_screencap)
    monkeypatch.setattr(adb, "uiautomator_dump_async", fake_dump)

    drv = AndroidDriver(capture_budget_ms=800)
    tree, shot = asyncio.run(drv.get_frame())
    assert shot == content
    assert sorted(calls) == ["dump", "screencap"]
    capture = tree["_capture"]
    assert capture["coordinate_compatible"] is True
    assert capture["tree_provider"] == "uiautomator_dump"
    assert capture["pixel_provider"] == "adb_screencap"


def test_tree_timeout_preserves_pixels_captured_within_outer_fuse(monkeypatch):
    """A paid-for valid screenshot survives an independent tree timeout."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    content = _content_png()
    budgets: dict[str, float] = {}

    async def slow_dump(_serial, *, max_attempts=1, timeout=None):
        budgets["tree"] = float(timeout or 0)
        await asyncio.sleep(float(timeout or 0))
        raise adb.AdbError("adb command timed out: uiautomator_dump")

    async def fake_screencap(_serial, *, timeout=None):
        budgets["pixels"] = float(timeout or 0)
        return content

    monkeypatch.setattr(adb, "uiautomator_dump_async", slow_dump)
    monkeypatch.setattr(adb, "screencap_async", fake_screencap)

    drv = AndroidDriver(capture_budget_ms=800)
    tree, shot = asyncio.run(drv.get_frame())

    assert shot == content
    assert tree["_capture"]["complete"] is False
    assert tree["_capture"]["coordinate_compatible"] is False
    assert 0.7 <= budgets["tree"] <= 0.8
    assert budgets["pixels"] > 0


def test_device_file_size_missing_file_clear_error(monkeypatch):
    """wc ENOENT → AdbError mentions dump produced no file + path."""
    import subprocess

    from driver import adb
    from driver.adb import AdbError

    def fake_run(args, **kwargs):
        import subprocess as _sp

        raise _sp.CalledProcessError(
            1,
            args,
            stderr=b"wc: /sdcard/clickclick_window_dump.xml: No such file or directory\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AdbError) as excinfo:
        adb._device_file_size(None, "/sdcard/clickclick_window_dump.xml")
    msg = str(excinfo.value)
    assert "produced no file" in msg
    assert "/sdcard/clickclick_window_dump.xml" in msg


def test_uiautomator_dump_backoff_between_standalone_retries(monkeypatch):
    """Standalone multi-attempt dump sleeps between failed rounds."""
    import subprocess

    from driver import adb

    sleeps: list[float] = []
    monkeypatch.setattr(adb.time, "sleep", lambda s: sleeps.append(s))
    fake_run, calls = _build_subprocess_run_stub(wc_sizes=[0, 0, 2400])
    monkeypatch.setattr(subprocess, "run", fake_run)

    xml = adb.uiautomator_dump(None, max_attempts=3, min_bytes=100)
    assert "<hierarchy" in xml
    assert calls["dump"] == 3
    assert sleeps == [adb._DUMP_BACKOFF_S, adb._DUMP_BACKOFF_S]


# --- adbkeyboard IME channel (non-ASCII `type`) ---------------------------


def test_is_ascii_text_accepts_pure_ascii_printable():
    from driver.android import _is_ascii_text

    assert _is_ascii_text("hello") is True
    assert _is_ascii_text("hello world 123") is True
    assert _is_ascii_text("") is True  # empty is vacuously printable ASCII


def test_is_ascii_text_rejects_non_ascii():
    from driver.android import _is_ascii_text

    assert _is_ascii_text("深圳南山区租房") is False
    assert _is_ascii_text("café") is False
    assert _is_ascii_text("emoji 😀") is False


def test_is_ascii_text_rejects_control_chars():
    """Control chars (\\x00-\\x1F, \\x7F) crash InputShellCommand too."""
    from driver.android import _is_ascii_text

    assert _is_ascii_text("hello\x00world") is False
    assert _is_ascii_text("hello\nworld") is False
    assert _is_ascii_text("hello\tworld") is False
    assert _is_ascii_text("hi\x7fthere") is False


def test_android_driver_type_ascii_uses_input_text(monkeypatch):
    """Pure-ASCII `type` keeps the legacy `adb shell input text` path."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    text_calls: list[tuple] = []
    broadcast_calls: list[str] = []

    async def fake_input_text(serial, t):
        text_calls.append((serial, t))

    async def fake_broadcast(serial, t):
        broadcast_calls.append(t)

    monkeypatch.setattr(adb, "input_text_async", fake_input_text)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="hello world")))
    assert res.success
    assert text_calls == [(None, "hello world")]
    assert broadcast_calls == []
    assert res.detail["channel"] == "input_text"
    assert res.detail["text_chars"] == len("hello world")


def test_android_driver_type_non_ascii_uses_broadcast(monkeypatch):
    """Non-ASCII `type` routes through ADBKeyBoard broadcast, not `input text`."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    text_calls: list[str] = []
    broadcast_calls: list[str] = []
    ime_setup_calls: list[str] = []

    async def fake_input_text(serial, t):
        text_calls.append(t)

    async def fake_broadcast(serial, t):
        broadcast_calls.append(t)

    async def fake_ime_set(serial, ime_id):
        ime_setup_calls.append(ime_id)

    monkeypatch.setattr(adb, "input_text_async", fake_input_text)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)
    monkeypatch.setattr(adb, "ime_set_async", fake_ime_set)
    monkeypatch.setattr(
        adb, "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="深圳南山区租房")))
    assert res.success
    # ASCII path not touched.
    assert text_calls == []
    # Broadcast carries the original CJK string verbatim.
    assert broadcast_calls == ["深圳南山区租房"]
    # Already selected → no enable/set needed.
    assert ime_setup_calls == []
    assert res.detail["channel"] == "ime"
    assert res.detail["text_chars"] == len("深圳南山区租房")


def test_android_driver_type_non_ascii_selects_enabled_non_default_ime(monkeypatch):
    """Enabled is not selected: each non-ASCII input verifies the default IME."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    selected = "com.android.inputmethod.latin/.LatinIME"
    default_calls: list[str] = []
    set_calls: list[str] = []
    broadcast_calls: list[str] = []

    async def fake_default_ime(serial):
        default_calls.append(selected)
        return selected

    async def fake_ime_set(serial, ime_id):
        nonlocal selected
        set_calls.append(ime_id)
        selected = ime_id

    async def fake_broadcast(serial, t):
        broadcast_calls.append(t)

    monkeypatch.setattr(adb, "default_ime_async", fake_default_ime)
    monkeypatch.setattr(adb, "ime_set_async", fake_ime_set)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver()
    res1 = asyncio.run(drv.act(Action(type="type", text="深圳")))
    assert res1.success
    # First call observes another default, selects ADBKeyBoard, and verifies it.
    assert default_calls == [
        "com.android.inputmethod.latin/.LatinIME",
        "com.android.adbkeyboard/.AdbIME",
    ]
    assert set_calls == ["com.android.adbkeyboard/.AdbIME"]
    assert broadcast_calls == ["深圳"]

    # Second call verifies selection again, without another set.
    res2 = asyncio.run(drv.act(Action(type="type", text="南山")))
    assert res2.success
    assert default_calls[-1] == "com.android.adbkeyboard/.AdbIME"
    assert len(default_calls) == 3
    assert set_calls == ["com.android.adbkeyboard/.AdbIME"]
    assert broadcast_calls == ["深圳", "南山"]


@pytest.mark.asyncio
async def test_android_driver_readiness_selects_ime_before_task(monkeypatch):
    """Per-task readiness switches ADBKeyBoard before the first type action."""
    from driver import adb
    from driver.android import AndroidDriver

    selected = "system/.Ime"
    set_calls: list[str] = []
    sleep_calls: list[float] = []

    async def fake_default_ime(_serial):
        return selected

    async def fake_ime_set(_serial, ime_id):
        nonlocal selected
        set_calls.append(ime_id)
        selected = ime_id

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(adb, "default_ime_async", fake_default_ime)
    monkeypatch.setattr(adb, "ime_set_async", fake_ime_set)
    monkeypatch.setattr(adb, "screen_interactive_async", _async_returns(True))
    monkeypatch.setattr("driver.android.asyncio.sleep", fake_sleep)

    drv = AndroidDriver(collector_enabled=False)
    result = await drv.readiness()

    assert set_calls == ["com.android.adbkeyboard/.AdbIME"]
    assert sleep_calls == [0.35]
    assert result["test_ime"] == {
        "ready": True,
        "ime_id": "com.android.adbkeyboard/.AdbIME",
        "previous_default": "system/.Ime",
        "switched": True,
        "reason": "",
    }


@pytest.mark.asyncio
async def test_android_driver_readiness_keeps_selected_ime_without_wait(monkeypatch):
    """Already-selected ADBKeyBoard needs neither another switch nor delay."""
    from driver import adb
    from driver.android import AndroidDriver

    set_calls: list[str] = []
    sleep_calls: list[float] = []

    async def fake_ime_set(_serial, ime_id):
        set_calls.append(ime_id)

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(
        adb,
        "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )
    monkeypatch.setattr(adb, "ime_set_async", fake_ime_set)
    monkeypatch.setattr(adb, "screen_interactive_async", _async_returns(True))
    monkeypatch.setattr("driver.android.asyncio.sleep", fake_sleep)

    drv = AndroidDriver(collector_enabled=False)
    result = await drv.readiness()

    assert set_calls == []
    assert sleep_calls == []
    assert result["test_ime"]["ready"] is True
    assert result["test_ime"]["switched"] is False


@pytest.mark.asyncio
async def test_android_driver_readiness_probes_start_concurrently(monkeypatch):
    from driver import adb
    from driver.android import AndroidDriver

    started: set[str] = set()
    all_started = asyncio.Event()

    async def rendezvous(name: str):
        started.add(name)
        if len(started) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.2)

    async def warm(*, timeout):
        del timeout
        await rendezvous("collector")
        return {"ready": True}

    async def ensure_ime():
        await rendezvous("ime")
        return True

    async def interactive(_serial):
        await rendezvous("interactive")
        return True

    monkeypatch.setattr(adb, "default_ime_async", _async_returns("system/.Ime"))
    monkeypatch.setattr(adb, "screen_interactive_async", interactive)
    drv = AndroidDriver(collector_enabled=True)
    monkeypatch.setattr(drv._collector, "warm", warm)
    monkeypatch.setattr(drv, "_ensure_adbkeyboard", ensure_ime)

    result = await drv.readiness()

    assert started == {"collector", "ime", "interactive"}
    assert result["status"] == "ready"
    assert set(result["timing_ms"]) == {
        "collector", "test_ime", "screen_interactive", "total",
    }


def test_android_driver_type_non_ascii_ime_setup_fails(monkeypatch):
    """`ime set` failure surfaces a typed error; no silent NPE fallback."""
    import asyncio

    from driver import adb
    from driver.adb import AdbError
    from driver.android import AndroidDriver
    from shared.schemas import Action

    broadcast_calls: list[str] = []

    async def fake_ime_set(serial, ime_id):
        raise AdbError(f"adb command failed (1): Error: selected IME not available")

    async def fake_broadcast(serial, t):
        broadcast_calls.append(t)

    monkeypatch.setattr(
        adb, "default_ime_async",
        _async_returns("com.android.inputmethod.latin/.LatinIME"),
    )
    monkeypatch.setattr(adb, "ime_set_async", fake_ime_set)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="深圳")))
    assert not res.success
    assert res.message == "type: adbkeyboard_unavailable"
    assert res.detail["channel"] == "ime"
    assert res.detail["ime_id"] == "com.android.adbkeyboard/.AdbIME"
    assert res.detail["reason"] == "ime_setup_failed"
    # No broadcast went out — we surfaced the typed failure instead.
    assert broadcast_calls == []


def test_android_driver_type_non_ascii_verifies_ime_selection(monkeypatch):
    """Reject success when the OEM keeps another IME after `ime set`."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    broadcast_calls: list[str] = []

    monkeypatch.setattr(
        adb, "default_ime_async",
        _async_returns("com.android.inputmethod.latin/.LatinIME"),
    )
    monkeypatch.setattr(adb, "ime_set_async", _async_returns(None))

    async def fake_broadcast(serial, text):
        broadcast_calls.append(text)

    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="深圳")))

    assert not res.success
    assert res.message == "type: adbkeyboard_unavailable"
    assert broadcast_calls == []


def test_android_driver_type_non_ascii_broadcast_fails(monkeypatch):
    """IME ready but broadcast raises → typed error, no false success."""
    import asyncio

    from driver import adb
    from driver.adb import AdbError
    from driver.android import AndroidDriver
    from shared.schemas import Action

    async def fake_broadcast(serial, t):
        raise AdbError("adb command failed (255): Broadcast completed: result=0")

    monkeypatch.setattr(
        adb, "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="深圳")))
    assert not res.success
    assert res.message == "type: ime_broadcast_failed"
    assert res.detail["channel"] == "ime"
    assert "Broadcast completed" in res.detail["error"]


def test_android_driver_type_non_ascii_ime_auto_setup_false_skips_setup(monkeypatch):
    """`ime_auto_setup=False` short-circuits the lazy setup but still surfaces
    a typed failure when no IME is available — explicit operator rollback."""
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    default_calls: list[int] = []

    async def fake_default_ime(serial):
        default_calls.append(1)
        return "system/.Ime"

    async def fake_broadcast(serial, t):
        return None

    monkeypatch.setattr(adb, "default_ime_async", fake_default_ime)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_broadcast)

    drv = AndroidDriver(ime_auto_setup=False)
    res = asyncio.run(drv.act(Action(type="type", text="深圳")))
    assert not res.success
    # Setup NOT probed (operator rolled it back).
    assert default_calls == []
    assert res.message == "type: adbkeyboard_unavailable"


def test_android_driver_type_empty_text_fails():
    """Defensive: empty `text` returns a failure ActionResult before any
    routing decision."""
    import asyncio

    from driver.android import AndroidDriver
    from shared.schemas import Action

    drv = AndroidDriver()
    res = asyncio.run(drv.act(Action(type="type", text="")))
    assert not res.success
    assert res.message == "type missing text"


def test_adb_input_text_via_broadcast_command_shape(monkeypatch):
    """input_text_via_broadcast builds `am broadcast -a ADB_INPUT_TEXT
    --es msg '<text>'` — the `--es` extra carries the text as a Java
    `String`, which is the only path that handles CJK/emoji safely."""
    captured: list[list[str]] = []

    def fake_run(args, *, capture_bytes=False, timeout=30.0):
        captured.append(list(args))
        return b""

    monkeypatch.setattr(adb, "_run", fake_run)

    import shlex

    text = "张凌赫|出道六周年快乐 走过的每一步都算数 '完整版'"
    adb.input_text_via_broadcast(None, text)
    assert len(captured) == 1
    args = captured[0]
    # Locate `--es msg <text>` and verify remote-shell parsing recovers one
    # unchanged Java String rather than treating `|` as a pipeline.
    idx_msg = args.index("--es")
    assert args[idx_msg + 1] == "msg"
    assert shlex.split(args[idx_msg + 2]) == [text]
    # The intent action must be present too.
    assert "ADB_INPUT_TEXT" in args
    assert "broadcast" in args


def test_adb_clear_text_via_broadcast_command_shape(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(args, *, capture_bytes=False, timeout=30.0):
        captured.append(list(args))
        return b""

    monkeypatch.setattr(adb, "_run", fake_run)

    adb.clear_text_via_broadcast("device-1")

    assert len(captured) == 1
    args = captured[0]
    assert args[args.index("-a") + 1] == "ADB_CLEAR_TEXT"
    assert args[:3] == [adb.adb_bin(), "-s", "device-1"]
    assert "broadcast" in args


def test_android_driver_replace_text_clears_then_inputs_exact_value(monkeypatch):
    import asyncio

    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action

    calls: list[tuple[str, str | None]] = []
    text = "张凌赫|出道六周年快乐 走过的每一步都算数"

    async def fake_clear(serial):
        calls.append(("clear", serial))

    async def fake_input(serial, value):
        calls.append(("input", value))

    monkeypatch.setattr(
        adb,
        "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )
    monkeypatch.setattr(adb, "clear_text_via_broadcast_async", fake_clear)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_input)

    result = asyncio.run(
        AndroidDriver(serial="device-1").act(Action(type="replace_text", text=text))
    )

    assert result.success
    assert calls == [("clear", "device-1"), ("input", text)]
    assert result.message == "replace_text"
    assert result.detail == {
        "channel": "ime",
        "operation": "replace_text",
        "stages": ["clear", "input"],
        "text_chars": len(text),
    }


def test_android_driver_replace_text_stops_after_clear_failure(monkeypatch):
    import asyncio

    from driver import adb
    from driver.adb import AdbError
    from driver.android import AndroidDriver
    from shared.schemas import Action

    input_calls: list[str] = []

    async def fake_clear(_serial):
        raise AdbError("clear failed")

    async def fake_input(_serial, value):
        input_calls.append(value)

    monkeypatch.setattr(
        adb,
        "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )
    monkeypatch.setattr(adb, "clear_text_via_broadcast_async", fake_clear)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_input)

    result = asyncio.run(
        AndroidDriver().act(Action(type="replace_text", text="final"))
    )

    assert not result.success
    assert result.message == "replace_text: ime_clear_failed"
    assert result.detail["stage"] == "clear"
    assert input_calls == []


def test_android_driver_replace_text_reports_input_failure_after_clear(monkeypatch):
    import asyncio

    from driver import adb
    from driver.adb import AdbError
    from driver.android import AndroidDriver
    from shared.schemas import Action

    calls: list[str] = []

    async def fake_clear(_serial):
        calls.append("clear")

    async def fake_input(_serial, _value):
        calls.append("input")
        raise AdbError("input failed")

    monkeypatch.setattr(
        adb,
        "default_ime_async",
        _async_returns("com.android.adbkeyboard/.AdbIME"),
    )
    monkeypatch.setattr(adb, "clear_text_via_broadcast_async", fake_clear)
    monkeypatch.setattr(adb, "input_text_via_broadcast_async", fake_input)

    result = asyncio.run(
        AndroidDriver().act(Action(type="replace_text", text="final"))
    )

    assert not result.success
    assert result.message == "replace_text: ime_broadcast_failed"
    assert result.detail["stage"] == "input"
    assert calls == ["clear", "input"]


def test_adb_ascii_input_quotes_remote_shell_metacharacters(monkeypatch):
    import shlex

    captured: list[list[str]] = []

    def fake_run(args, *, capture_bytes=False, timeout=30.0):
        captured.append(list(args))
        return b""

    monkeypatch.setattr(adb, "_run", fake_run)

    adb.input_text(None, "title|value & more")

    assert len(captured) == 1
    assert shlex.split(captured[0][-1]) == ["title|value%s&%smore"]


def test_exact_forward_reconciliation_preserves_preexisting_port(monkeypatch):
    removed: list[int] = []

    async def fake_run(args, *, timeout):
        assert args[-2:] == ["forward", "--list"]
        return (
            b"S tcp:43100 localabstract:collector\n"
            b"S tcp:43210 localabstract:collector\n"
            b"S tcp:49999 localabstract:other\n"
        )

    async def fake_remove(serial, port, *, timeout):
        assert serial == "S"
        removed.append(port)

    monkeypatch.setattr(adb, "_run_async", fake_run)
    monkeypatch.setattr(adb, "remove_forward_async", fake_remove)

    result = asyncio.run(adb.remove_forwards_to_localabstract_async(
        "S", "collector", preserve_ports={43100}, timeout=0.5,
    ))

    assert result == [43210]
    assert removed == [43210]
