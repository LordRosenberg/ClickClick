"""Paired replay through the real shared transport, PyAV and Agent publication API."""
import asyncio
from collections import Counter
from dataclasses import asdict
from fractions import Fraction
import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from control_api.preview import PreviewBudget, PreviewImageResponse, TaskPreview
from driver.android import AndroidDriver
from driver.scrcpy_observation import FrameGeometry, ScrcpyObservationProvider
from driver.scrcpy_stream import ScrcpyRegistry
from shared.schemas import TaskStatus
from tests.test_scrcpy_stream import _PushSource


def packets():
    av = pytest.importorskip("av")
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = encoder.height = 64
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "0"}
    out = []
    for color in ("red", "blue", "green", "white"):
        out.extend(bytes(p) for p in encoder.encode(av.VideoFrame.from_image(Image.new("RGB", (64, 64), color))))
    out.extend(bytes(p) for p in encoder.encode(None))
    return out


@pytest.mark.parametrize("load", ["normal", "two_windows", "slow", "reconnect"])
async def test_preview_does_not_change_agent_pixels_boundaries_or_capture_resources(monkeypatch, load):
    import driver.scrcpy_observation as observation
    stream = packets()
    async def replay(enabled):
        clock = [1000.0]
        monkeypatch.setattr(observation, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        counts = Counter(decoders=1, tree=0, screenshot=0)
        source = _PushSource("S")
        source.packet_complete = True
        registry = ScrcpyRegistry()
        registry.set_source_factory(lambda _: source)
        provider = ScrcpyObservationProvider(registry, "S", lambda: FrameGeometry(64, 64, 64, 64), max_fps=1)
        provider._device_size = (64, 64)
        driver = AndroidDriver(serial="S", stream_provider=provider, collector_enabled=False)
        for obj, name, label in ((provider, "_publish_pending", "forced"),
                                  (provider.decoder, "feed", "feed")):
            original = getattr(obj, name)
            def wrapped(*args, _original=original, _label=label, **kwargs):
                counts[_label] += 1
                return _original(*args, **kwargs)
            monkeypatch.setattr(obj, name, wrapped)
        for name in ("acquire", "release"):
            original = getattr(registry, name)
            async def wrapped(*args, _original=original, _label=name, **kwargs):
                counts[_label] += 1
                return await _original(*args, **kwargs)
            monkeypatch.setattr(registry, name, wrapped)
        async def reset():
            counts["reset"] += 1
        source.reset_video = reset
        worker = asyncio.create_task(asyncio.Event().wait())
        task = {"id": "T", "device_serial": "S", "status": TaskStatus.RUNNING.value}
        service = TaskPreview(SimpleNamespace(state=SimpleNamespace(running_tasks={"T": worker})),
                              SimpleNamespace(task_preview_identity=lambda _: task),
                              SimpleNamespace(mode="android", peek=lambda _: driver))
        async def viewers():
            if not enabled:
                return
            for _ in range(2 if load == "two_windows" else 1):
                state, frame = service.snapshot("T")
                if frame is None:
                    continue
                budget = PreviewBudget().admit("T", "frame")
                response = PreviewImageResponse(frame.data, service.encode_descriptor(state["frame"]), budget)
                response.send_timeout = .001
                gate = asyncio.Event()
                async def receive():
                    if load == "reconnect":
                        return {"type": "http.disconnect"}
                    await gate.wait()
                async def send(_message):
                    if load == "slow":
                        await gate.wait()
                await response({"type": "http"}, receive, send)
                assert budget.inflight == 0 and response.body == b""
        async def feed(index):
            before = counts["feed"]
            await source.queue.put(stream[index])
            for _ in range(100):
                await asyncio.sleep(0)
                if counts["feed"] > before:
                    return
            raise AssertionError("controlled source did not decode")
        def selected():
            current = provider.current()
            frame = current.frame
            return (current.status, current.detail, provider.action_frame_boundary,
                    None if frame is None else (hashlib.sha256(frame.data).hexdigest(),
                        asdict(frame.geometry), frame.generation, frame.frame_id))
        result = []
        await driver.begin_task_session("T")
        try:
            await provider._start_decoder()
            for _ in range(3):
                await asyncio.sleep(0)
            await feed(0)
            result.append(selected())
            clock[0] += .01
            await feed(1)  # Blue remains pending inside the PNG publication cap.
            await viewers()
            assert provider.decoder._pending is not None
            result.append(selected())  # Only the original Agent flushes blue.
            boundary = provider.frame_boundary()
            provider.mark_action_complete(boundary)
            await viewers()
            result.append(selected())  # Must still report the same post-action gap.
            clock[0] += .01
            await feed(2)
            await viewers()
            result.append(selected())
            clock[0] += 2
            await feed(3)
            await viewers()
            result.append(selected())
            # Original screenshot fallback remains callable only by the Agent.
            provider.decoder.status = "unavailable"
            fallback = io.BytesIO()
            Image.new("RGB", (64, 64), "yellow").save(fallback, "PNG")
            async def screencap(*_args, **_kwargs):
                counts["screenshot"] += 1
                return fallback.getvalue()
            monkeypatch.setattr("driver.adb.screencap_async", screencap)
            await viewers()
            result.append(("fallback", hashlib.sha256(await driver.screenshot()).hexdigest()))
            session = provider._session
            counts["encoded_subscribers"] = session.metrics()["fanout_subscribers"]
            counts["leases"] = session.metrics()["leases"]
        finally:
            await driver.close_observation_provider()
            await registry.shutdown()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        counts["source_start"], counts["source_stop"] = source.started, source.stopped
        return result, dict(counts)
    assert await replay(False) == await replay(True)
