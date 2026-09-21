from types import SimpleNamespace
import asyncio
import time

import pytest

from driver.observation_deadline import ObservationDeadline
from driver.scrcpy_observation import FrameGeometry, FrameHandle, ScrcpyObservationProvider


def test_deadline_never_grants_more_than_original_budget_on_coarse_clock():
    deadline = ObservationDeadline("current", 800, clock=lambda: 1023531.843)
    assert deadline.remaining_seconds("tree") == 0.8


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_ms,delay_ms,completed", [(100, 25, True), (10, 50, False)])
async def test_deadline_wait_rechecks_early_wakeup_without_extending_budget(monkeypatch, budget_ms, delay_ms, completed):
    now = [0.0]
    sleeps = []
    deadline = ObservationDeadline("sample", budget_ms, clock=lambda: now[0])

    async def wake_early(seconds):
        assert seconds <= deadline.remaining_ms / 1000.0
        sleeps.append(seconds)
        now[0] += min(seconds, 0.005)

    monkeypatch.setattr("driver.observation_deadline.asyncio.sleep", wake_early)
    assert await deadline.wait(delay_ms) is completed
    assert len(sleeps) > 1
    assert now[0] * 1000 >= min(delay_ms, budget_ms)
    assert now[0] * 1000 <= budget_ms


@pytest.mark.asyncio
async def test_missing_decoder_never_starts_transport_or_waits_for_frames(monkeypatch):
    provider = ScrcpyObservationProvider(SimpleNamespace(), "device")
    monkeypatch.setattr(provider, "decoder_available", lambda: False)
    assert await provider.start() is False
    assert provider.current().detail == "decoder_unavailable"
    assert await provider.await_fresh_frame(after_id=0, timeout_s=10) is None


@pytest.mark.asyncio
async def test_cold_capture_uses_first_new_frame_without_reset():
    reset = asyncio.Event()
    provider = ScrcpyObservationProvider(SimpleNamespace(), "device")

    async def reset_video():
        assert provider.ring.latest(generation=1) is not None
        reset.set()
        return True

    provider._session = SimpleNamespace(generation=1, source=SimpleNamespace(reset_video=reset_video))
    geometry = FrameGeometry(32, 64, 32, 64)

    async def publish():
        await asyncio.sleep(0)
        assert not reset.is_set()
        provider.ring.append(FrameHandle(b"initial", time.monotonic(), 1, geometry))

    publisher = asyncio.create_task(publish())
    frame = await provider.await_fresh_frame(after_id=0, timeout_s=1)
    await publisher
    assert frame is not None and frame.data == b"initial"
    assert not reset.is_set()


@pytest.mark.asyncio
async def test_unready_capture_times_out_without_resetting_server():
    async def reset_video():
        raise AssertionError("do not reset an uninitialized capture")

    provider = ScrcpyObservationProvider(SimpleNamespace(), "device")
    provider._session = SimpleNamespace(generation=1, source=SimpleNamespace(reset_video=reset_video))
    assert await provider.await_fresh_frame(after_id=0, timeout_s=0.02) is None


@pytest.mark.asyncio
async def test_start_waits_for_decoded_frame_within_startup_budget(monkeypatch):
    provider = ScrcpyObservationProvider(SimpleNamespace(), "device")
    provider._session = SimpleNamespace(generation=1)
    ready = asyncio.Event()
    geometry = FrameGeometry(32, 64, 32, 64)

    async def start_decoder():
        ready.set()
        return True

    monkeypatch.setattr(provider, "_start_decoder", start_decoder)
    task = asyncio.create_task(provider.start())
    await ready.wait()
    await asyncio.sleep(0)
    assert not task.done(), "Socket setup alone must not complete cold startup"
    provider.ring.append(FrameHandle(b"decoded", time.monotonic(), 1, geometry))
    assert await task is True


@pytest.mark.asyncio
async def test_start_readiness_timeout_does_not_start_refresh(monkeypatch):
    provider = ScrcpyObservationProvider(SimpleNamespace(), "device")
    provider._session = SimpleNamespace(generation=1)

    async def start_decoder():
        return True

    async def no_frame(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_start_decoder", start_decoder)
    monkeypatch.setattr(provider, "_wait_for_frame", no_frame)
    monkeypatch.setattr("driver.scrcpy_observation.SCRCPY_START_ATTEMPT_TIMEOUT_MS", 20)
    # The initial frame wait is bounded even for a connected but silent source.
    with pytest.raises(TimeoutError):
        await provider.start()
