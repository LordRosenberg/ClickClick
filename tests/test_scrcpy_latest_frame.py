from fractions import Fraction
from io import BytesIO
from types import SimpleNamespace
import asyncio

import pytest
from PIL import Image

from driver.android import AndroidDriver
from driver.observation_deadline import ObservationDeadline
from driver.scrcpy_mirror import AnnexBParser
from driver.scrcpy_observation import FrameGeometry, FrameHandle, ScrcpyObservationProvider


def _provider():
    provider = ScrcpyObservationProvider(
        SimpleNamespace(), "device", lambda: FrameGeometry(64, 64, 64, 64), max_fps=1,
    )
    provider._session = SimpleNamespace(generation=3, is_alive=lambda: True)
    provider._task = SimpleNamespace(done=lambda: False)
    provider.status = "healthy"
    return provider


@pytest.mark.asyncio
async def test_decoder_reopen_discards_previous_pixels_even_in_same_generation(monkeypatch):
    provider = _provider()
    provider._device_size = (64, 64)
    provider._task = None
    provider.ring.append(FrameHandle(b"old", 1000, 3, FrameGeometry(64, 64, 64, 64)))

    async def acquire(*_):
        return SimpleNamespace(generation=3), object()

    async def consume():
        await asyncio.Event().wait()

    provider.registry = SimpleNamespace(acquire=acquire)
    monkeypatch.setattr(provider, "decoder_available", lambda: True)
    monkeypatch.setattr(provider, "_consume", consume)
    await provider._start_decoder()
    try:
        assert not provider.ring._frames
    finally:
        provider._task.cancel()
        await asyncio.gather(provider._task, return_exceptions=True)


def _feed_fast_colors(provider, monkeypatch):
    av = pytest.importorskip("av")
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = encoder.height = 64
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "0"}
    packets = []
    for color in ("red", "blue"):
        packets.extend(bytes(packet) for packet in encoder.encode(
            av.VideoFrame.from_image(Image.new("RGB", (64, 64), color)),
        ))
    packets.extend(bytes(packet) for packet in encoder.encode(None))
    assert len(packets) == 2
    clock = [1000.0]
    monkeypatch.setattr("driver.scrcpy_observation.time.monotonic", lambda: clock[0])
    parser = AnnexBParser()
    for packet in packets:
        for unit, _ in parser.feed(packet, packet_complete=True):
            provider.decoder.feed(unit, 3)
        clock[0] += 0.01
    return clock


def test_current_keeps_final_static_frame_inside_fps_cap(monkeypatch):
    provider = _provider()
    clock = _feed_fast_colors(provider, monkeypatch)
    assert len(provider.ring._frames) == 1  # Background PNG sampling remains capped.
    clock[0] = 1004.0  # No third packet arrives on a static screen.
    current = provider.current()
    pixel = Image.open(BytesIO(current.frame.data)).getpixel((32, 32))
    assert pixel[2] > 240 and pixel[0] < 10
    assert current.frame.timestamp == pytest.approx(1000.01)  # Never retimestamp old pixels.
    assert provider.current().frame.frame_id == current.frame.frame_id


def test_action_boundary_includes_a_preexisting_throttled_frame(monkeypatch):
    provider = _provider()
    _feed_fast_colors(provider, monkeypatch)
    boundary = provider.frame_boundary()
    assert boundary == (3, 2)
    provider.mark_action_complete(boundary)
    assert provider.current().detail == "post_action_frame_missing"


def test_decoder_reset_discards_throttled_frame(monkeypatch):
    provider = _provider()
    _feed_fast_colors(provider, monkeypatch)
    provider.decoder.reset()
    provider.ring.clear()
    assert provider.current().frame is None


@pytest.mark.asyncio
@pytest.mark.parametrize("later_generation,later_time,use_later", [(3, 1001, True), (4, 1001, False), (3, 999, False)])
async def test_slow_tree_uses_latest_same_generation_pixels_without_recapture(
    monkeypatch, later_generation, later_time, use_later,
):
    def frame(color, timestamp, generation, frame_id):
        output = BytesIO()
        Image.new("RGB", (64, 64), color).save(output, format="PNG")
        return FrameHandle(output.getvalue(), timestamp, generation,
                           FrameGeometry(64, 64, 64, 64, generation=generation), frame_id)

    early = frame("red", 1000, 3, 1)
    later = frame("blue", later_time, later_generation, 2)
    pixels_ready = asyncio.Event()

    class Provider:
        action_frame_boundary = None
        latest = early
        refreshes = 0

        async def await_fresh_frame(self, **kwargs):
            self.refreshes += 1
            pixels_ready.set()
            return early

        def current(self):
            return SimpleNamespace(status="healthy", frame=self.latest)

    provider = Provider()
    driver = AndroidDriver(stream_provider=provider)

    async def tree(_deadline):
        await pixels_ready.wait()
        provider.latest = later
        return {"class": "Root", "text": "ready", "children": [], "_capture": {"complete": True}}

    async def unexpected_adb(_deadline):
        raise AssertionError("Do not add a capture or fallback to select an already decoded frame")

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", unexpected_adb)
    _, pixels, metadata = await driver.capture_deadline_frame(ObservationDeadline("current", 3000))
    assert pixels == (later.data if use_later else early.data)
    assert metadata["pixel_monotonic_ms"] == (later_time * 1000 if use_later else 1000000)
    assert provider.refreshes == 1


@pytest.mark.asyncio
async def test_final_pixel_selection_keeps_a_successful_adb_fallback(monkeypatch):
    fallback_done = asyncio.Event()
    output = BytesIO()
    Image.new("RGB", (64, 64), "green").save(output, format="PNG")
    adb_pixels = output.getvalue()

    class Provider:
        latest = None

        async def await_fresh_frame(self, **kwargs):
            return None

        def current(self):
            return SimpleNamespace(status="healthy" if self.latest else "unavailable", frame=self.latest)

    provider = Provider()
    driver = AndroidDriver(stream_provider=provider)

    async def adb_capture(_deadline):
        fallback_done.set()
        return adb_pixels

    async def tree(_deadline):
        await fallback_done.wait()
        provider.latest = FrameHandle(b"unrelated stream pixels", 1001, 3, FrameGeometry(64, 64, 64, 64))
        return {"class": "Root", "text": "ready", "children": [], "_capture": {"complete": True}}

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", adb_capture)
    _, pixels, metadata = await driver.capture_deadline_frame(ObservationDeadline("current", 3000))
    assert pixels == adb_pixels
    assert metadata["pixel_provider"] == "adb_screencap"
