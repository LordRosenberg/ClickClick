from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import sys
import time
from typing import AsyncIterator

import pytest
from PIL import Image

import agent.read_tools as read_tools
from agent.action_observation import (
    ActionObservationTransaction,
)
from agent.read_tools import make_observe_screen_handler
from agent.observation_space import make_registry_entry
from agent.session import AgentSession
from agent.skills.library import SkillLibrary
from agent.tool_registry import AgentRole, AgentToolResult, ToolExecutionContext, ToolStatus
from driver import adb
from driver.accessibility import AccessibilityTransportError
from driver.android import (
    AndroidDriver,
)
from driver.factory import _build_android_driver
from driver.observation_deadline import (
    TEMPORAL_DEADLINE_MS,
    ObservationDeadline,
    ObservationStageError,
)
from driver.scrcpy_mirror import MirrorRegistry
from driver.scrcpy_observation import FrameGeometry, FrameHandle
from perception.observation import ObservationBuilder, ObservationPackage, PreparedObservation
from perception.som import A11Y_COLOR
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import Action, CanonicalUI, ObservationMode, UIElement


def _png(color: str = "white") -> bytes:
    out = BytesIO()
    Image.new("RGB", (32, 64), color).save(out, format="PNG")
    return out.getvalue()


def test_temporal_observation_deadline_matches_real_device_calibration():
    assert TEMPORAL_DEADLINE_MS == 8_000


def _package(color: str = "white") -> ObservationPackage:
    image = _png(color)
    return ObservationPackage(
        ui=CanonicalUI(app_id="com.example", activity=".Main"),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm="tree",
        image_for_llm=image, annotated_png=image, gap_reasons=[],
        frame_width=32, frame_height=64,
    )


def _visible_dump_tree() -> dict:
    return {
        "class": "Root",
        "bounds": [0, 0, 32, 64],
        "children": [{
            "class": "android.widget.TextView",
            "text": "current",
            "bounds": [0, 0, 32, 20],
            "children": [],
        }],
        "_capture": {
            "provider": "uiautomator_dump",
            "complete": True,
            "tree_ready_monotonic_ms": time.monotonic() * 1000.0,
        },
    }


@pytest.mark.asyncio
async def test_tree_collector_failure_is_returned_to_outer_resample(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)
    provider_calls: list[str] = []

    dump_calls = 0

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        provider_calls.append(provider)
        raise RuntimeError("persistent collector timed out")

    async def dump(_deadline):
        nonlocal dump_calls
        dump_calls += 1
        return _visible_dump_tree()

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)

    with pytest.raises(ObservationStageError) as raised:
        await driver._deadline_tree(  # noqa: SLF001
            ObservationDeadline("current", 12_000)
        )

    assert provider_calls == ["primary"]
    assert dump_calls == 0
    assert [row["provider"] for row in raised.value.provider_attempts] == [
        "accessibility_collector_primary"
    ]


@pytest.mark.asyncio
async def test_tree_transport_error_is_preserved_in_one_provider_ledger(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)
    exchange = {
        "exchange_ordinal": 1,
        "status": "failed",
        "failure_class": "timeout",
        "failure_stage": "snapshot_exchange",
    }

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        if provider == "primary":
            raise AccessibilityTransportError(
                "opaque transport failure",
                failure_class="timeout",
                stage="snapshot_exchange",
                exchanges=[exchange],
            )
        return _visible_dump_tree()

    async def dump(_deadline):
        return _visible_dump_tree()

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)
    with pytest.raises(ObservationStageError) as raised:
        await driver._deadline_tree(  # noqa: SLF001
            ObservationDeadline("current", 12_000)
        )

    attempt = raised.value.provider_attempts[0]
    assert attempt["provider"] == "accessibility_collector_primary"
    assert attempt["status"] == "timeout"
    assert attempt["error"] == "opaque transport failure"
    assert len(raised.value.provider_attempts) == 1


@pytest.mark.asyncio
async def test_tree_collector_success_is_one_logical_provider_attempt(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)
    exchanges = [{
        "exchange_ordinal": 1,
        "status": "succeeded",
        "failure_class": "",
        "final_route": "primary",
    }]

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        assert provider == "primary"
        tree = _visible_dump_tree()
        tree["_capture"].update({
            "provider": provider,
            "complete": True,
            "collector_exchanges": exchanges,
        })
        return tree

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    tree = await driver._deadline_tree(  # noqa: SLF001
        ObservationDeadline("current", 12_000)
    )

    attempts = tree["_capture"]["tree_provider_attempts"]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["status"] == "ok"
    assert attempt["provider"] == "accessibility_collector_primary"


@pytest.mark.asyncio
async def test_second_capture_uses_ready_collector(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)
    dump_calls = 0

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        tree = _visible_dump_tree()
        tree["_capture"].update({"provider": provider, "complete": True})
        return tree

    async def dump(_deadline):
        nonlocal dump_calls
        dump_calls += 1
        raise AssertionError("ready collector must not dump on capture 2")

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)
    monkeypatch.setattr(driver._collector, "diagnostics", lambda: {"ready": True})
    deadline = ObservationDeadline("current", 12_000)
    deadline.capture_ordinal = 2

    tree = await driver._deadline_tree(deadline)

    assert dump_calls == 0
    assert tree["_capture"]["tree_provider_attempts"][0]["provider"] == (
        "accessibility_collector_primary"
    )


@pytest.mark.asyncio
async def test_second_capture_dumps_when_collector_is_not_ready(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)
    collector_calls = 0

    async def collector_tree(*, timeout_s, provider="primary"):
        nonlocal collector_calls
        collector_calls += 1
        raise AssertionError("not-ready collector must not be polled")

    async def dump(_deadline):
        return _visible_dump_tree()

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)
    monkeypatch.setattr(driver._collector, "diagnostics", lambda: {"ready": False})
    deadline = ObservationDeadline("current", 12_000)
    deadline.capture_ordinal = 2

    tree = await driver._deadline_tree(deadline)

    assert collector_calls == 0
    assert tree["_capture"]["tree_provider_attempts"][0]["provider"] == (
        "uiautomator_dump"
    )


@pytest.mark.asyncio
async def test_generation_break_skips_same_capture_fallbacks(monkeypatch):
    driver = AndroidDriver(collector_enabled=True)

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        assert provider == "primary"
        tree = _visible_dump_tree()
        tree["_capture"].update({
            "complete": False,
            "reasons": ["generation_changed"],
        })
        return tree

    async def forbidden_dump(_deadline):
        raise AssertionError("generation break belongs to outer full resample")

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", forbidden_dump)

    with pytest.raises(ObservationStageError) as raised:
        await driver._deadline_tree(ObservationDeadline("current", 12_000))

    assert raised.value.stage == "accessibility_tree"
    assert raised.value.reason == "generation_changed"
    assert [row["provider"] for row in raised.value.provider_attempts] == [
        "accessibility_collector_primary",
    ]


@pytest.mark.asyncio
async def test_final_generation_break_preserves_paid_pixels_as_image_only(monkeypatch):
    driver = AndroidDriver()

    async def generation_break(_deadline):
        raise ObservationStageError(
            "accessibility_tree",
            "generation_changed",
            provider_attempts=[{
                "provider": "accessibility_collector_primary",
                "status": "error",
                "capture_ordinal": 2,
            }],
        )

    async def pixels(_deadline):
        return _png()

    monkeypatch.setattr(driver, "_deadline_tree", generation_break)
    monkeypatch.setattr(driver, "_deadline_screencap", pixels)
    deadline = ObservationDeadline("current", 3500)
    deadline.capture_ordinal = 2

    tree, shot, metadata = await driver.capture_deadline_frame(deadline)

    assert shot == _png()
    assert metadata["active_capture_ordinal"] == 2
    assert metadata["complete"] is False
    assert metadata["coordinate_compatible"] is False
    assert metadata["pixel_provider"] == "adb_screencap"
    assert tree["_capture"]["tree_providers_exhausted"] is True


class _Source:
    def __init__(self, key: str) -> None:
        self.key = key
        self.started = 0
        self.stopped = 0
        self.alive = False
        self.codec_string = "avc1.42E01E"

    async def start(self) -> None:
        self.started += 1
        self.alive = True

    async def stop(self) -> None:
        self.stopped += 1
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        del chunk_size
        await asyncio.Event().wait()
        if False:
            yield b""


def test_factory_uses_one_scrcpy_provider_for_current_and_temporal(monkeypatch):
    monkeypatch.setattr(AndroidDriver, "connect", lambda self, serial=None: serial or "S1")
    driver = _build_android_driver(Settings(), serial="S1")
    sentinel = object()
    driver._stream_provider.temporal = lambda *_args: sentinel
    assert driver.query_stream_temporal(0, 1, 2) is sentinel


def test_cli_single_device_resolution_keeps_scrcpy_provider(monkeypatch):
    monkeypatch.setattr("driver.adb.sole_online_device", lambda: "S1")
    monkeypatch.setattr(AndroidDriver, "connect", lambda self, serial=None: serial or "S1")

    driver = _build_android_driver(Settings(), serial=None)

    assert driver.serial == "S1"
    assert driver._stream_provider is not None
    assert driver._stream_provider.device_key == "S1"


def test_factory_keeps_provider_and_reports_missing_decoder(monkeypatch):
    monkeypatch.setattr(AndroidDriver, "connect", lambda self, serial=None: serial or "S1")
    driver = _build_android_driver(Settings(), serial="S1")
    monkeypatch.setattr(driver._stream_provider, "decoder_available", lambda: False)
    state = driver.observation_diagnostics("current")
    assert driver._stream_provider is not None
    assert state["available"] is False
    assert state["dependencies"]["decoder"] is False


@pytest.mark.asyncio
async def test_direct_screenshot_and_temporal_prefer_scrcpy(monkeypatch):
    geometry = FrameGeometry(32, 64, 32, 64)
    frame = FrameHandle(_png("blue"), time.monotonic(), 1, geometry)
    temporal = SimpleNamespace(status="ok", frames=(frame,))

    class Provider:
        starts = 0

        async def start(self):
            self.starts += 1
            return True

        def current(self):
            return SimpleNamespace(status="healthy", frame=frame)

        def temporal(self, *_args):
            return temporal

    async def unexpected_adb(*_args, **_kwargs):
        raise AssertionError("healthy scrcpy must avoid ADB screencap")

    monkeypatch.setattr(adb, "screencap_async", unexpected_adb)
    provider = Provider()
    driver = AndroidDriver(stream_provider=provider)

    assert await driver.screenshot() == frame.data
    assert await driver.sample_stream_temporal(
        0, time.monotonic(), 1, ObservationDeadline("temporal", 1000)
    ) is temporal
    assert provider.starts == 2


@pytest.mark.asyncio
async def test_stream_temporal_waits_for_forward_window_before_querying():
    temporal = SimpleNamespace(status="ok", frames=())

    class Provider:
        queried_at = 0.0

        async def start(self):
            return True

        def temporal(self, *_args):
            self.queried_at = time.monotonic()
            return temporal

    provider = Provider()
    driver = AndroidDriver(stream_provider=provider)
    start = time.monotonic()
    end = start + 0.02

    assert await driver.sample_stream_temporal(
        start, end, 2, ObservationDeadline("temporal", 1000)
    ) is temporal
    assert provider.queried_at >= end


@pytest.mark.asyncio
async def test_direct_screenshot_falls_back_to_adb(monkeypatch):
    class Provider:
        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(status="unavailable", frame=None)

    adb_image = _png("red")

    async def adb_capture(*_args, **_kwargs):
        return adb_image

    monkeypatch.setattr(adb, "screencap_async", adb_capture)
    driver = AndroidDriver(stream_provider=Provider())
    assert await driver.screenshot() == adb_image


@pytest.mark.asyncio
async def test_registry_shares_console_and_agent_then_stops_after_idle():
    source = _Source("S1")
    registry = MirrorRegistry(idle_shutdown_seconds=0.02)
    registry.set_source_factory(lambda _key: source)
    session, console = await registry.acquire("S1", "console")
    same, agent = await registry.acquire("S1", "agent")
    assert same is session
    assert source.started == 1
    await registry.release("S1", console)
    await registry.release("S1", agent)
    assert source.stopped == 0
    await asyncio.sleep(0.04)
    assert source.stopped == 1


@pytest.mark.asyncio
async def test_registry_reacquire_during_idle_keeps_generation_and_source():
    source = _Source("S1")
    registry = MirrorRegistry(idle_shutdown_seconds=0.04)
    registry.set_source_factory(lambda _key: source)
    session, first = await registry.acquire("S1", "console")
    await registry.release("S1", first)
    await asyncio.sleep(0.01)
    _, second = await registry.acquire("S1", "agent")
    assert second.generation == first.generation == session.generation
    assert source.started == 1
    await registry.release("S1", second)
    await registry.shutdown()


def test_provider_diagnostics_expose_health_and_freshness(monkeypatch):
    from driver.scrcpy_observation import ScrcpyObservationProvider

    registry = MirrorRegistry()
    provider = ScrcpyObservationProvider(
        registry, "S1", lambda: FrameGeometry(32, 64, 32, 64),
        frame_max_age_ms=1000,
    )
    monkeypatch.setattr(provider, "decoder_available", lambda: True)
    provider.status = "healthy"
    provider.decoder.status = "healthy"
    provider._session = SimpleNamespace(
        available=True, generation=3, is_alive=lambda: True
    )
    provider._task = SimpleNamespace(done=lambda: False)
    provider.ring.append(FrameHandle(_png(), time.monotonic(), 3, FrameGeometry(32, 64, 32, 64)))
    state = provider.diagnostics(selected_mode="temporal")
    assert state["available"] is True
    assert state["healthy"] is True
    assert state["selected"] is True
    assert state["ring"]["fresh"] is True


def _live_provider(*, frame_age_s: float = 0.0, frame_generation: int = 3):
    from driver.scrcpy_observation import ScrcpyObservationProvider

    provider = ScrcpyObservationProvider(
        MirrorRegistry(), "S1", lambda: FrameGeometry(32, 64, 32, 64),
        frame_max_age_ms=1000,
    )
    provider._session = SimpleNamespace(
        available=True, generation=3, is_alive=lambda: True
    )
    provider._task = SimpleNamespace(done=lambda: False)
    provider.status = "healthy"
    provider.decoder.status = "healthy"
    frame = FrameHandle(
        _png("blue"), time.monotonic() - frame_age_s, frame_generation,
        FrameGeometry(32, 64, 32, 64),
    )
    frame = provider.ring.append(frame)
    return provider, frame


def test_old_static_frame_is_live_static_reuse(monkeypatch):
    provider, frame = _live_provider(frame_age_s=10.0)
    monkeypatch.setattr(provider, "decoder_available", lambda: True)

    result = provider.current()
    state = provider.diagnostics(selected_mode="current")

    assert result.status == "healthy"
    assert result.frame is frame
    assert result.validation == "live_static_reuse"
    assert state["healthy"] is True
    assert state["ring"]["fresh"] is False
    assert state["ring"]["visual_age_ms"] >= 10_000
    assert state["selected"] is True


def test_one_post_transition_frame_does_not_need_duplicate_stability():
    provider, _ = _live_provider()
    newer = FrameHandle(
        _png("red"), time.monotonic(), 3, FrameGeometry(32, 64, 32, 64)
    )
    newer = provider.ring.append(newer)

    result = provider.current()

    assert result.status == "healthy"
    assert result.frame is newer


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("source", "source_dead"),
        ("consumer", "consumer_dead"),
        ("generation", "generation_mismatch"),
        ("decoder", "decoder_error"),
    ],
)
def test_current_rejects_explicit_live_validity_failures(mutation, reason):
    provider, _ = _live_provider()
    if mutation == "source":
        provider._session = SimpleNamespace(
            available=True, generation=3, is_alive=lambda: False
        )
    elif mutation == "consumer":
        provider._task = SimpleNamespace(done=lambda: True)
    elif mutation == "generation":
        provider._session = SimpleNamespace(
            available=True, generation=4, is_alive=lambda: True
        )
    else:
        provider.decoder.status = "decode_error"

    assert provider.current().detail == reason


def test_post_action_old_frame_is_rejected_without_mutating_temporal_history():
    provider, frame = _live_provider(frame_age_s=2.0)
    original_timestamp = frame.timestamp
    provider.mark_action_complete((3, frame.frame_id))

    result = provider.current()
    temporal = provider.temporal(time.monotonic() - 1.0, time.monotonic(), 2)

    assert result.status == "unavailable"
    assert result.validation == "post_action_frame_missing"
    assert result.frame_after_boundary is False
    assert frame.timestamp == original_timestamp
    assert len(provider.ring._frames) == 1
    assert temporal.status == "partial"
    assert temporal.frames == ()


def test_aged_frame_after_action_boundary_remains_current_evidence():
    provider, first = _live_provider(frame_age_s=3.0)
    provider.mark_action_complete((3, first.frame_id))
    newer = provider.ring.append(FrameHandle(
        _png("red"), time.monotonic() - 2.0, 3,
        FrameGeometry(32, 64, 32, 64),
    ))

    result = provider.current()

    assert result.status == "healthy"
    assert result.frame is newer
    assert result.validation == "post_action_frame"
    assert result.frame_after_boundary is True


def test_frame_ring_ids_are_generation_local():
    from driver.scrcpy_observation import FrameRing

    ring = FrameRing()
    geometry = FrameGeometry(32, 64, 32, 64)
    first = ring.append(FrameHandle(_png("blue"), time.monotonic(), 3, geometry))
    other_generation = ring.append(
        FrameHandle(_png("red"), time.monotonic(), 4, geometry)
    )

    assert first.frame_id == 1
    assert other_generation.frame_id == 1
    second = ring.append(FrameHandle(_png("green"), time.monotonic(), 3, geometry))
    assert second.frame_id == 2


def test_decoder_fps_cap_never_drops_encoded_input(monkeypatch):
    from driver.scrcpy_observation import FrameRing, PyAVDecoder

    decode_calls: list[bytes] = []

    class FakeFrame:
        def to_image(self):
            return Image.new("RGB", (24, 48), "blue")

    class FakeCodec:
        def decode(self, packet):
            decode_calls.append(packet)
            return [FakeFrame()]

    fake_codec = FakeCodec()
    fake_av = SimpleNamespace(
        CodecContext=SimpleNamespace(create=lambda *_args: fake_codec),
        Packet=lambda payload: payload,
    )
    monkeypatch.setitem(sys.modules, "av", fake_av)
    ring = FrameRing()
    decoder = PyAVDecoder(
        ring, lambda: FrameGeometry(0, 0, 1200, 2670), max_fps=1.0,
    )

    assert decoder.feed(b"first", 7) == 1
    assert decoder.feed(b"second", 7) == 0
    assert decode_calls == [b"first", b"second"]
    assert len(ring._frames) == 1
    frame = ring._frames[0]
    assert frame.data.startswith(b"\x89PNG")
    assert (frame.geometry.stream_width, frame.geometry.stream_height) == (24, 48)
    assert (frame.geometry.device_width, frame.geometry.device_height) == (1200, 2670)


def test_decoder_keeps_context_and_recovers_after_rejected_packet(monkeypatch):
    from driver.scrcpy_observation import FrameRing, PyAVDecoder

    class FakeFrame:
        def to_image(self):
            return Image.new("RGB", (24, 48), "blue")

    class FakeCodec:
        def __init__(self):
            self.calls = 0

        def decode(self, _packet):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("broken predictive packet")
            return [FakeFrame()]

    codec = FakeCodec()
    create_calls = 0

    def create_codec(*_args):
        nonlocal create_calls
        create_calls += 1
        return codec

    fake_av = SimpleNamespace(
        CodecContext=SimpleNamespace(create=create_codec),
        Packet=lambda payload: payload,
    )
    monkeypatch.setitem(sys.modules, "av", fake_av)
    decoder = PyAVDecoder(
        FrameRing(), lambda: FrameGeometry(0, 0, 1200, 2670), max_fps=6.0,
    )

    assert decoder.feed(b"broken", 1) == 0
    assert decoder.status == "corrupt_stream"
    assert decoder.decode_errors == 1
    assert decoder.last_error == "ValueError: broken predictive packet"
    assert decoder.feed(b"recovery", 1) == 1
    assert decoder.status == "healthy"
    assert decoder.last_error is None
    assert create_calls == 1

    decoder.reset()
    assert decoder.status == "reset"
    assert decoder._codec is None


def test_scrcpy_geometry_aligns_som_and_model_actions_to_device_space():
    image = _png("white")
    element = UIElement(
        index=1, text="target", bounds=[300, 668, 900, 2002],
        clickable=True, interactable=True,
    )
    ui = CanonicalUI(elements=[element], semantic_tree=[element])
    prepared = PreparedObservation(
        raw_tree={"_capture": {
            "frame_geometry": [1200, 2670],
            "complete": True,
            "coordinate_compatible": True,
        }},
        screenshot=image,
        ui=ui,
        gap_reasons=[],
        frame_geometry=(1200, 2670),
    )
    package = ObservationBuilder().package(prepared, attach_image=True)

    assert (package.frame_width, package.frame_height) == (1200, 2670)
    rendered = Image.open(BytesIO(package.annotated_png)).convert("RGB")
    # Device bounds scale to the 32x64 stream image around [8,16,24,48].
    assert any(
        rendered.getpixel((x, y)) == A11Y_COLOR
        for x in range(6, 27) for y in range(14, 51)
    )
    entry = make_registry_entry(
        observation_id="obs", coordinate_space_id="space", ui=ui,
        actionable=True, captured_monotonic_ms=1,
        model_image_size=(32, 64), frame_geometry=(1200, 2670),
    )
    assert entry.transform is not None
    x, y = entry.transform.transform_point(16, 32)
    assert x == pytest.approx(600)
    assert y == pytest.approx(1335)


@pytest.mark.asyncio
async def test_stream_temporal_uses_one_ending_tree_for_three_ring_images():
    geometry = FrameGeometry(32, 64, 32, 64)
    handles = tuple(
        FrameHandle(_png(color), float(index + 1), 7, geometry)
        for index, color in enumerate(("red", "green", "blue"))
    )

    class Driver:
        calls = 0
        serial = "S1"
        query_args = None

        def query_stream_temporal(self, *args):
            self.query_args = args
            return SimpleNamespace(frames=handles, status="ok")

        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "com.example",
                "activity": ".Main",
                "component": "com.example/.Main",
                "sources": [],
                "conflict": False,
            }

        async def capture_deadline_frame(self, _deadline):
            self.calls += 1
            return (
                {
                    "class": "Root",
                    "package": "com.example",
                    "text": "current",
                    "bounds": "[0,0][32,64]",
                    "children": [],
                },
                _png(),
                {
                    "provider": "scrcpy",
                    "pixel_provider": "scrcpy",
                    "pixel_monotonic_ms": time.monotonic() * 1000.0,
                    "coherence_status": "passive_static_reuse",
                    "generation": 7,
                    "coordinate_compatible": True,
                    "complete": True,
                },
            )

    driver = Driver()
    started = time.monotonic()
    result = await read_tools._stream_temporal(
        driver,
        ObservationBuilder(),
        read_tools.TemporalObservationRequest(frames=3),
        ObservationDeadline("temporal", 1000),
    )
    assert result is not None
    assert len(result.frames) == 3
    assert driver.calls == 1
    assert driver.query_args is not None
    assert driver.query_args[0] >= started
    assert driver.query_args[1] - driver.query_args[0] == pytest.approx(0.8)
    assert result.temporal_complete is True
    assert [frame.ui.app_id for frame in result.frames] == ["", "", "com.example"]


def test_observation_builder_does_not_synthesize_exact_app_from_tree_package():
    package = ObservationBuilder().build(({
        "class": "Root",
        "package": "com.tree.only",
        "children": [{
            "class": "Button", "text": "Go", "package": "com.tree.only",
            "bounds": [0, 0, 10, 10], "clickable": True,
        }],
    }, _png()))

    assert package.ui.app_id == ""


@pytest.mark.asyncio
async def test_cancellable_subprocess_timeout_terminates_and_reaps():
    started = time.monotonic()
    with pytest.raises(adb.AdbError, match="timed out"):
        await adb._run_async(
            [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.02
        )
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_temporal_fallback_keeps_first_frame_when_later_capture_times_out():
    class Driver(_FallbackDriver):
        async def capture_deadline_frame(self, _deadline):
            self.calls += 1
            if self.calls > 1:
                raise ObservationStageError("screencap", "timed out", timed_out=True)
            tree = {
                "class": "Root", "package": "com.example",
                "children": [{
                    "class": "TextView", "package": "com.example",
                    "text": "current", "bounds": [0, 0, 32, 20],
                }],
            }
            return tree, self.images[0], {
                "provider": "adb_fallback", "generation": 0,
                "pixel_provider": "adb_screencap",
                "pixel_monotonic_ms": time.monotonic() * 1000.0,
                "fallback_edges": [], "coordinate_compatible": True,
                "coordinate_compatible": True, "complete": True,
            }

    driver = Driver([_png("blue")])
    result = await read_tools._fallback_temporal(
        driver,
        ObservationBuilder(),
        read_tools.TemporalObservationRequest(frames=2, duration_ms=1),
        None,
        ObservationDeadline("temporal", 4500),
    )

    assert result.status == "degraded_current"
    assert len(result.frames) == 1
    assert result.failed_stage == "screencap"
    assert driver.calls >= 2
    assert result.provider == "adb_fallback"


@pytest.mark.asyncio
async def test_temporal_bounded_sampling_reports_actual_pixel_provider():
    class Driver(_FallbackDriver):
        async def capture_deadline_frame(self, _deadline):
            self.calls += 1
            tree = {
                "class": "Root", "package": "com.example",
                "text": "current", "children": [],
            }
            return tree, self.images[0], {
                "provider": "scrcpy", "generation": 7,
                "pixel_provider": "scrcpy",
                "pixel_monotonic_ms": time.monotonic() * 1000.0,
                "coherence_status": "passive_static_reuse",
                "fallback_edges": [], "coordinate_compatible": True, "complete": True,
            }

    result = await read_tools._fallback_temporal(
        Driver([_png("blue")], generation=7),
        ObservationBuilder(),
        read_tools.TemporalObservationRequest(frames=2, duration_ms=1),
        None,
        ObservationDeadline("temporal", 4500),
    )

    assert result.provider == "scrcpy"
    assert len(result.frames) == 2


@pytest.mark.asyncio
async def test_temporal_fallback_preserves_equal_pixels_at_distinct_times():
    image = _png("blue")
    baseline = _package("blue")
    baseline.captured_monotonic_ms = time.monotonic() * 1000.0
    setattr(baseline, "_observation_device", "S1")
    setattr(baseline, "_observation_generation", 1)
    setattr(baseline, "_observation_complete", True)

    result = await read_tools._fallback_temporal(
        _FallbackDriver([image], generation=1),
        ObservationBuilder(),
        read_tools.TemporalObservationRequest(frames=2, duration_ms=1),
        baseline,
        ObservationDeadline("temporal", 4500),
    )

    assert result.status == "complete"
    assert len(result.frames) == 2
    assert result.frames[0].captured_monotonic_ms < result.frames[1].captured_monotonic_ms
    assert (result.frames[0].clean_png or result.frames[0].image_for_llm) == (
        result.frames[1].clean_png or result.frames[1].image_for_llm
    )


@pytest.mark.asyncio
async def test_temporal_fallback_captures_two_fresh_frames_when_baseline_expired():
    baseline = _package("red")
    baseline.captured_monotonic_ms = 0
    driver = _FallbackDriver([_png("green"), _png("blue")], generation=1)

    result = await read_tools._fallback_temporal(
        driver,
        ObservationBuilder(),
        read_tools.TemporalObservationRequest(frames=2, duration_ms=1),
        baseline,
        ObservationDeadline("temporal", 4500),
    )

    assert result.status == "complete"
    assert len(result.frames) == 2
    assert driver.calls == 2


class _FallbackDriver:
    serial = "S1"

    def __init__(self, images: list[bytes], generation: int = 1) -> None:
        self.images = images
        self.calls = 0
        self.generation = generation

    def observation_diagnostics(self, mode=None):
        return {
            "generation": self.generation, "healthy": False, "status": "fallback",
            "selected_mode": None, "configured": {}, "ring": {},
        }

    async def current_foreground_identity(self, *, timeout_s=None):
        return {
            "package": "com.example",
            "activity": ".Main",
            "component": "com.example/.Main",
            "sources": [],
            "conflict": False,
        }

    async def get_frame(self):
        image = self.images[min(self.calls, len(self.images) - 1)]
        self.calls += 1
        return {
            "class": "Root",
            "package": "com.example",
            "bounds": "[0,0][32,64]",
            "children": [{
                "class": "TextView", "package": "com.example",
                "text": "current", "bounds": [0, 0, 32, 20],
            }],
            "_capture": {
                "complete": True,
                "coordinate_compatible": True,
                "coordinate_compatible": True,
            },
        }, image


@pytest.mark.asyncio
async def test_current_always_captures_fresh_observation():
    baseline = _package("red")
    setattr(baseline, "_observation_device", "S1")
    setattr(baseline, "_observation_generation", 7)
    driver = _FallbackDriver([_png("blue")])
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(), artifacts=None, baseline_package=baseline,
    )
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR, invocation_id="i",
        state={"active_package": baseline},
    )

    result = await handler({"mode": "current"}, context)

    assert result.status == ToolStatus.SUCCEEDED
    assert result.data["observation_id"] != baseline.observation_id
    assert result.actionable_observation_id == result.data["observation_id"]
    assert driver.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejection",
    ["stale", "incomplete", "device_mismatch", "missing_image", "missing_geometry", "inactive"],
)
async def test_current_baseline_rejection_runs_fresh_capture(rejection: str):
    baseline = _package("red")
    setattr(baseline, "_observation_device", "S1")
    if rejection == "stale":
        baseline.captured_monotonic_ms = 0
    elif rejection == "incomplete":
        setattr(baseline, "_observation_complete", False)
    elif rejection == "device_mismatch":
        setattr(baseline, "_observation_device", "S2")
    elif rejection == "missing_image":
        baseline.annotated_png = None
        baseline.image_for_llm = None
    elif rejection == "missing_geometry":
        baseline.frame_width = 0

    driver = _FallbackDriver([_png("blue")])
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(), artifacts=None, baseline_package=baseline,
    )
    active = _package("green") if rejection == "inactive" else baseline
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR, invocation_id="i",
        state={"active_package": active},
    )

    result = await handler({"mode": "current"}, context)

    assert result.status == ToolStatus.SUCCEEDED
    assert result.data["observation_id"] != baseline.observation_id
    assert driver.calls == 1


@pytest.mark.asyncio
async def test_current_capture_labels_automatic_adb_fallback(monkeypatch):
    driver = AndroidDriver()
    calls = {"tree": 0, "adb": 0}

    async def tree(_deadline):
        calls["tree"] += 1
        return {
            "class": "Root",
            "children": [{
                "class": "TextView", "text": "ready",
                "bounds": [0, 0, 32, 20],
            }],
        }

    async def shot(_deadline):
        calls["adb"] += 1
        return _png()

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", shot)

    _, _, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3500)
    )

    assert metadata["provider"] == "adb_fallback"
    assert metadata["fallback_edges"] == [{
        "from": "direct_capture", "to": "adb_screencap",
        "reason": "provider_unavailable",
    }]
    adb_attempt = next(
        row for row in metadata["provider_attempts"]
        if row.get("provider") == "adb_screencap"
        and row.get("status") == "ok"
    )
    assert adb_attempt["capture_ordinal"] == 1
    assert calls == {"tree": 1, "adb": 1}


@pytest.mark.asyncio
async def test_adb_screencap_failure_is_retained_in_provider_ledger(monkeypatch):
    driver = AndroidDriver()

    async def tree(_deadline):
        return {
            "class": "Root", "children": [],
            "_capture": {
                "complete": True,
                "tree_provider_attempts": [{
                    "provider": "primary", "status": "ok",
                    "budget_ms": 1.0, "elapsed_ms": 1.0, "error": "",
                }],
            },
        }

    async def fail_shot(_deadline):
        raise ObservationStageError(
            "screencap", "decoder unavailable",
            elapsed_ms=12.0, budget_ms=50.0,
        )

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", fail_shot)

    tree, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 500)
    )

    assert tree["_capture"]["complete"] is True
    assert pixels == b""
    attempt = next(
        row for row in metadata["provider_attempts"]
        if row.get("provider") == "adb_screencap"
    )
    assert attempt["status"] == "error"
    assert attempt["error"] == "decoder unavailable"
    assert metadata["pixel_provider"] == "unavailable"


@pytest.mark.asyncio
async def test_scrcpy_start_timeout_is_recorded_before_adb_fallback(monkeypatch):
    cancelled = asyncio.Event()

    class Provider:
        async def start(self):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    driver = AndroidDriver(stream_provider=Provider())

    async def tree(_deadline):
        return {"class": "Root", "children": [], "_capture": {"complete": True}}

    monkeypatch.setattr(driver, "_deadline_tree", tree)

    _, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 80)
    )

    assert cancelled.is_set()
    assert pixels == b""
    assert metadata["fallback_edges"] == [{
        "from": "scrcpy",
        "to": "adb_screencap",
        "reason": "scrcpy_frame_error",
    }]
    scrcpy_attempt = next(
        row for row in metadata["provider_attempts"]
        if row.get("provider") == "scrcpy"
    )
    assert scrcpy_attempt["status"] == "error"
    assert scrcpy_attempt["failure_class"] == "scrcpy_frame_error"


@pytest.mark.asyncio
async def test_outer_deadline_reports_and_reaps_inflight_capture_tasks(monkeypatch):
    cancelled: set[str] = set()
    driver = AndroidDriver()

    async def hang(name):
        try:
            await asyncio.Future()
        finally:
            cancelled.add(name)

    monkeypatch.setattr(driver, "_deadline_tree", lambda _deadline: hang("tree"))
    monkeypatch.setattr(
        driver, "_deadline_screencap", lambda _deadline: hang("pixels")
    )

    async def identity(*, timeout_s=None):
        return {
            "package": "com.example", "activity": ".Main",
            "component": "com.example/.Main", "conflict": False,
        }

    monkeypatch.setattr(driver, "current_foreground_identity", identity)
    transaction = ActionObservationTransaction(
        driver, ObservationBuilder(), current_deadline_ms=100,
    )

    with pytest.raises(ObservationStageError) as raised:
        await transaction.observe_current(deadline_ms=100)

    assert raised.value.stage == "observation_capture"
    assert raised.value.reason == "outer_deadline_exhausted"
    assert raised.value.timed_out is True
    assert set(raised.value.cancelled_tasks) == {
        "current-observation-tree", "current-observation-pixels",
    }
    identity_attempt = next(
        attempt for attempt in raised.value.provider_attempts
        if attempt["provider"] == "foreground_identity"
    )
    assert identity_attempt["status"] == "ok"
    assert cancelled == {"tree", "pixels"}


@pytest.mark.asyncio
async def test_explicit_capture_cancellation_is_not_rewritten_as_timeout(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    driver = AndroidDriver()

    async def hang_tree(_deadline):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(driver, "_deadline_tree", hang_tree)
    deadline = ObservationDeadline("current", 1000)
    capture = asyncio.create_task(driver.capture_deadline_frame(deadline))
    await entered.wait()
    capture.cancel()

    with pytest.raises(asyncio.CancelledError):
        await capture

    assert cancelled.is_set()
    assert set(deadline.cancelled_tasks) == {
        "current-observation-tree", "current-observation-pixels",
    }


@pytest.mark.asyncio
async def test_low_remaining_budget_does_not_bypass_healthy_scrcpy(monkeypatch):
    frame = FrameHandle(
        _png("blue"),
        timestamp=time.monotonic() + 1.0,
        generation=4,
        geometry=FrameGeometry(32, 64, 32, 64),
    )

    class Provider:
        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(status="healthy", frame=frame)

    driver = AndroidDriver(stream_provider=Provider())

    async def tree(_deadline):
        await asyncio.sleep(0.15)
        return {
            "class": "Root", "text": "ready", "children": [],
            "_capture": {"complete": True},
        }

    async def unexpected_adb(_deadline):
        raise AssertionError("a budget preference must not bypass healthy scrcpy")

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", unexpected_adb)

    _, captured_pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 300)
    )

    assert captured_pixels == frame.data
    assert metadata["pixel_provider"] == "scrcpy"
    assert metadata["fallback_edges"] == []


@pytest.mark.asyncio
async def test_refresh_stream_frame_advances_scrcpy_ring():
    from driver.scrcpy_observation import ScrcpyObservationProvider

    class Source:
        reset_calls = 0

        async def reset_video(self):
            Source.reset_calls += 1
            return True

    class Session:
        source = Source()
        generation = 1

        def is_alive(self):
            return True

    provider = ScrcpyObservationProvider(
        registry=SimpleNamespace(),
        device_key="S1",
        frame_max_age_ms=1000,
    )
    provider._session = Session()
    provider.ring.append(
        FrameHandle(_png("blue"), time.monotonic(), 1, FrameGeometry(32, 64, 32, 64)),
    )

    async def append_later():
        await asyncio.sleep(0.05)
        provider.ring.append(
            FrameHandle(_png("red"), time.monotonic(), 1, FrameGeometry(32, 64, 32, 64)),
        )

    class StreamProvider:
        def __init__(self):
            self.inner = provider

        async def start(self):
            return True

        def frame_boundary(self):
            return provider.frame_boundary()

        async def await_fresh_frame(self, *, after_id: int, timeout_s: float):
            return await provider.await_fresh_frame(
                after_id=after_id,
                timeout_s=timeout_s,
            )

    driver = AndroidDriver(stream_provider=StreamProvider())
    task = asyncio.create_task(append_later())
    refreshed = await driver.refresh_stream_frame(
        ObservationDeadline("orchestrator_round", 3000),
    )
    await task

    assert refreshed is True
    assert Source.reset_calls == 1


@pytest.mark.asyncio
async def test_capture_pixels_refreshes_scrcpy_stream_before_read(monkeypatch):
    stale = FrameHandle(
        _png("blue"),
        time.monotonic() - 10.0,
        3,
        FrameGeometry(32, 64, 32, 64),
    )
    fresh = FrameHandle(
        _png("red"),
        time.monotonic(),
        3,
        FrameGeometry(32, 64, 32, 64),
    )

    class Provider:
        action_frame_boundary = None

        def frame_boundary(self):
            return (3, 1)

        def current(self):
            return SimpleNamespace(status="healthy", frame=stale)

        async def await_fresh_frame(self, *, after_id: int, timeout_s: float):
            assert after_id == 1
            assert timeout_s > 0
            return fresh

    driver = AndroidDriver(stream_provider=Provider())

    async def tree(_deadline):
        return {
            "class": "Root", "text": "ready", "children": [],
            "_capture": {"complete": True},
        }

    async def unexpected_adb(_deadline):
        raise AssertionError("fresh scrcpy frame should avoid adb fallback")

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", unexpected_adb)

    _, captured_pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3000)
    )

    assert captured_pixels == fresh.data
    assert metadata["pixel_provider"] == "scrcpy"
    refresh_attempt = next(
        row for row in metadata["provider_attempts"]
        if row.get("phase") == "pixel_refresh"
    )
    assert refresh_attempt["fresh_after_id"] == 1


@pytest.mark.asyncio
async def test_await_fresh_frame_waits_for_ring_advance():
    from driver.scrcpy_observation import ScrcpyObservationProvider

    class Source:
        reset_calls = 0

        async def reset_video(self):
            Source.reset_calls += 1
            return True

    class Session:
        source = Source()
        generation = 1

        def is_alive(self):
            return True

    provider = ScrcpyObservationProvider(
        registry=SimpleNamespace(),
        device_key="S1",
        frame_max_age_ms=1000,
    )
    provider._session = Session()
    provider.ring.append(
        FrameHandle(_png("blue"), time.monotonic(), 1, FrameGeometry(32, 64, 32, 64)),
    )

    async def append_later():
        await asyncio.sleep(0.05)
        provider.ring.append(
            FrameHandle(_png("red"), time.monotonic(), 1, FrameGeometry(32, 64, 32, 64)),
        )

    task = asyncio.create_task(append_later())
    frame = await provider.await_fresh_frame(after_id=1, timeout_s=1.0)
    await task

    assert Source.reset_calls == 1
    assert frame is not None
    assert frame.frame_id == 2


@pytest.mark.asyncio
async def test_scrcpy_action_boundary_failure_uses_one_sequential_adb(monkeypatch):
    class Provider:
        action_frame_boundary = (4, 9)

        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(
                status="unavailable",
                frame=None,
                detail="post_action_frame_missing",
                source_healthy=True,
            )

    driver = AndroidDriver(stream_provider=Provider())

    async def tree(_deadline):
        return {"class": "Root", "text": "ready", "children": []}

    pixels = _png("green")

    async def fresh_adb(_deadline):
        return pixels

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", fresh_adb)

    _, captured_pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 1000)
    )

    assert captured_pixels == pixels
    assert metadata["provider"] == "adb_fallback"
    assert metadata["fallback_edges"] == [{
        "from": "scrcpy",
        "to": "adb_screencap",
        "reason": "post_action_frame_missing",
    }]


@pytest.mark.asyncio
async def test_one_healthy_current_scrcpy_head_needs_no_duplicate_stability(monkeypatch):
    frame = FrameHandle(
        _png("green"), timestamp=time.monotonic(), generation=4,
        geometry=FrameGeometry(32, 64, 32, 64), frame_id=12,
    )

    class Provider:
        action_frame_boundary = None

        def frame_boundary(self):
            return 4, 12

        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(
                status="healthy", frame=frame,
                detail="live_frame", source_healthy=True,
            )

        def diagnostics(self, *, selected_mode=None):
            return {"generation": 4, "healthy": True}

    driver = AndroidDriver(stream_provider=Provider())
    monkeypatch.setattr(
        driver, "_deadline_tree",
        lambda _deadline: asyncio.sleep(
            0, result={"class": "Root", "text": "static", "children": []}
        ),
    )
    monkeypatch.setattr(
        driver, "_deadline_screencap",
        lambda _deadline: (_ for _ in ()).throw(
            AssertionError("a healthy static scrcpy frame must avoid ADB")
        ),
    )

    _, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3500)
    )

    assert pixels == frame.data
    assert metadata["provider"] == "scrcpy"
    assert metadata["coordinate_compatible"] is True


@pytest.mark.asyncio
async def test_unavailable_scrcpy_fallback_reads_tree_once(monkeypatch):
    class Provider:
        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(status="unavailable", frame=None, detail="stale_ring")

    driver = AndroidDriver(
        stream_provider=Provider(),
    )
    tree_calls = 0

    async def tree(_deadline):
        nonlocal tree_calls
        tree_calls += 1
        return {"class": "Root", "text": "ready", "children": []}

    async def shot(_deadline):
        return _png()

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", shot)

    _, _, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3500)
    )

    assert tree_calls == 1
    assert metadata["provider"] == "adb_fallback"
    assert metadata["fallback_edges"] == [{
        "from": "scrcpy", "to": "adb_screencap",
        "reason": "scrcpy_frame_unavailable",
    }]
    scrcpy_attempt = next(
        row for row in metadata["provider_attempts"]
        if row["provider"] == "scrcpy"
    )
    assert scrcpy_attempt["error"] == "scrcpy_frame_unavailable"
    assert scrcpy_attempt["failure_class"] == "scrcpy_frame_unavailable"


@pytest.mark.asyncio
async def test_eligible_baseline_reuses_one_fresh_ending_capture():
    baseline = _package("red")
    setattr(baseline, "_observation_device", "S1")
    setattr(baseline, "_observation_generation", 1)
    driver = _FallbackDriver([_png("blue")], generation=1)
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(),
        artifacts=None,
        baseline_package=baseline,
    )
    result = await handler(
        {"mode": "temporal", "frames": 2, "duration_ms": 1},
        ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )
    assert result.status == ToolStatus.SUCCEEDED
    assert result.data["frame_count"] == 2
    assert "baseline_reused" not in result.data
    assert "provider_state" not in result.data
    assert driver.calls == 1


@pytest.mark.asyncio
async def test_generation_mismatch_rejects_baseline():
    baseline = _package("red")
    setattr(baseline, "_observation_device", "S1")
    setattr(baseline, "_observation_generation", 1)
    driver = _FallbackDriver([_png("green"), _png("blue")], generation=2)
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(),
        artifacts=None,
        baseline_package=baseline,
    )
    result = await handler(
        {"mode": "temporal", "frames": 2, "duration_ms": 1},
        ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )
    assert "baseline_reused" not in result.data
    assert result.status == ToolStatus.SUCCEEDED
    assert result.data["frame_count"] == 2
    assert driver.calls == 2


@pytest.mark.asyncio
async def test_temporal_never_stitches_fresh_packages_across_generations():
    class Driver(_FallbackDriver):
        async def capture_deadline_frame(self, deadline):
            image = self.images[min(self.calls, len(self.images) - 1)]
            self.calls += 1
            generation = self.calls
            return (
                {
                    "class": "Root", "package": "com.example",
                    "text": "current", "bounds": "[0,0][32,64]",
                    "children": [],
                },
                image,
                {
                    "provider": "adb_fallback", "generation": generation,
                    "pixel_provider": "adb_screencap",
                    "pixel_monotonic_ms": time.monotonic() * 1000.0,
                    "coherence_status": "adb_post_tree",
                    "provider_state": self.observation_diagnostics(),
                    "fallback_edges": [], "coordinate_compatible": True, "complete": True,
                },
            )

    old = _package("red")
    old.captured_monotonic_ms = 0
    driver = Driver([_png("green"), _png("blue")])
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(),
        artifacts=None,
        baseline_package=old,
    )
    result = await handler(
        {"mode": "temporal", "frames": 2, "duration_ms": 1},
        ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )
    assert result.data["status"] == "degraded_current"
    assert result.data["frame_count"] == 1


@pytest.mark.asyncio
async def test_current_timeout_can_retry_without_cross_call_suppression():
    class Driver:
        serial = "S1"

        def __init__(self):
            self.calls = 0
            self.generation = 1

        def observation_diagnostics(self, mode=None):
            return {
                "generation": self.generation, "healthy": False,
                "status": "stale", "selected_mode": None,
            }

        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "com.example", "activity": ".Main",
                "component": "com.example/.Main", "conflict": False,
            }

        async def capture_deadline_frame(self, deadline):
            self.calls += 1
            raise ObservationStageError("screencap", "hung", timed_out=True)

    driver = Driver()
    baseline = _package()
    baseline.captured_monotonic_ms = 0
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(),
        artifacts=None, baseline_package=baseline,
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i")
    first = await handler({"mode": "current"}, context)
    first_call_count = driver.calls
    second = await handler({"mode": "current"}, context)
    assert first.status == ToolStatus.TIMEOUT
    assert second.status == ToolStatus.TIMEOUT
    assert "retry_suppressed" not in second.data
    assert first_call_count > 0
    assert driver.calls > first_call_count
    second_call_count = driver.calls
    driver.generation = 2
    third = await handler({"mode": "current"}, context)
    assert third.status == ToolStatus.TIMEOUT
    assert driver.calls > second_call_count


@pytest.mark.asyncio
async def test_identical_current_retry_can_recover_after_transient_provider_failure():
    class Driver:
        serial = "S1"

        def __init__(self):
            self.fail = True
            self.calls = 0

        def observation_diagnostics(self, mode=None):
            del mode
            return {
                "generation": 1,
                "healthy": True,
                "status": "ready",
                "selected_mode": "adb",
            }

        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "com.example", "activity": ".Main",
                "component": "com.example/.Main", "conflict": False,
            }

        async def capture_deadline_frame(self, deadline):
            del deadline
            self.calls += 1
            if self.fail:
                raise RuntimeError("transient capture failure")
            return (
                {
                    "class": "Root",
                    "package": "com.example",
                    "text": "recovered",
                    "bounds": "[0,0][32,64]",
                    "children": [],
                },
                _png("green"),
                {
                    "provider": "adb_fallback",
                    "pixel_provider": "adb_screencap",
                    "pixel_monotonic_ms": time.monotonic() * 1000.0,
                    "coherence_status": "adb_post_tree",
                    "coordinate_compatible": True,
                    "complete": True,
                },
            )

    driver = Driver()
    handler = make_observe_screen_handler(
        driver=driver,
        builder=ObservationBuilder(),
        artifacts=None,
        baseline_package=_package(),
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i")

    first = await handler({"mode": "current"}, context)
    driver.fail = False
    second = await handler({"mode": "current"}, context)

    assert first.status == ToolStatus.FAILED
    assert second.status == ToolStatus.SUCCEEDED
    assert second.data["attempt"] == 2
    assert driver.calls == 2


@pytest.mark.asyncio
async def test_failed_observe_attempt_remains_visible_without_local_cap(monkeypatch, tmp_path: Path):
    root = tmp_path / "skills"
    (root / "generic").mkdir(parents=True)
    session = AgentSession("executor", "m", library=SkillLibrary(root))
    session.reset_lifecycle("task:t")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    rounds = 0
    handler_calls = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds <= 2:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls", latency_ms=1,
                tool_calls=[ToolCall(
                    id=f"o{rounds}", name="observe_screen", arguments='{"mode":"current"}',
                )],
            )
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls", latency_ms=1,
            tool_calls=[ToolCall(
                id="submit", name="submit_executor_step",
                arguments=(
                    '{"decision":"act",'
                    '"summary":"wait for the observation provider",'
                    '"action":{"type":"sleep","duration_ms":1}'
                    '}'
                ),
            )],
        )

    async def observe(_args, _context):
        nonlocal handler_calls
        handler_calls += 1
        return AgentToolResult(status=ToolStatus.TIMEOUT, summary="timeout", error="timeout")

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}], handlers={"observe_screen": observe},
    )
    assert handler_calls == 2
    assert result.tool_calls[0].status == ToolStatus.TIMEOUT
    assert result.tool_calls[1].status == ToolStatus.TIMEOUT
