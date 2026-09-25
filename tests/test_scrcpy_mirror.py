"""web-scrcpy-live-decode: StreamSource registry + remote key routing."""

from __future__ import annotations

import asyncio
import io
import subprocess
import struct
import time
from typing import AsyncIterator

import pytest

from driver.scrcpy_mirror import (
    AnnexBParser,
    LocalStreamSource,
    MirrorSession,
    MirrorRegistry,
    MirrorShutdownError,
    MirrorUnavailableError,
    RemoteStreamSource,
    _new_scid,
    _scrcpy_socket_name,
    is_mirror_server_available,
    server_jar_path,
)
from driver.scrcpy_observation import FrameGeometry, FrameHandle, FrameRing


class _FakeSource:
    def __init__(self, key: str) -> None:
        self.key = key
        self.started = 0
        self.stopped = 0
        self._alive = False
        self.codec_string = "avc1.42E01E"

    async def start(self) -> None:
        self.started += 1
        self._alive = True

    async def stop(self) -> None:
        self.stopped += 1
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        yield b"\x00\x00\x00\x01\x67"  # pretend SPS start
        return


class _PushSource(_FakeSource):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def stop(self) -> None:
        await super().stop()
        await self.queue.put(None)

    async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        del chunk_size
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item


def test_vendored_jar_present():
    assert server_jar_path().is_file()
    assert is_mirror_server_available()


def test_scrcpy_sessions_use_isolated_31_bit_socket_names():
    first = _new_scid()

    assert 0 <= first <= 0x7FFF_FFFF
    assert _scrcpy_socket_name(first) == f"scrcpy_{first:08x}"


@pytest.mark.asyncio
async def test_registry_keyed_by_device_key_shares_session():
    sources: dict[str, _FakeSource] = {}

    def factory(key: str) -> _FakeSource:
        src = _FakeSource(key)
        sources[key] = src
        return src

    registry = MirrorRegistry()
    registry.set_source_factory(factory)

    a = await registry.start("lab-a/S1")
    b = await registry.start("lab-a/S1")
    assert a is b
    assert sources["lab-a/S1"].started == 1

    await registry.stop("lab-a/S1")
    assert sources["lab-a/S1"].stopped == 0  # still one consumer
    await registry.stop("lab-a/S1")
    assert sources["lab-a/S1"].stopped == 1


@pytest.mark.asyncio
async def test_registry_distinct_keys_collide_on_serial():
    sources: dict[str, _FakeSource] = {}

    def factory(key: str) -> _FakeSource:
        src = _FakeSource(key)
        sources[key] = src
        return src

    registry = MirrorRegistry()
    registry.set_source_factory(factory)
    await registry.start("lab-a/emulator-5554")
    await registry.start("lab-b/emulator-5554")
    assert set(sources) == {"lab-a/emulator-5554", "lab-b/emulator-5554"}
    assert sources["lab-a/emulator-5554"].started == 1
    assert sources["lab-b/emulator-5554"].started == 1
    await registry.shutdown()
    assert sources["lab-a/emulator-5554"].stopped == 1
    assert sources["lab-b/emulator-5554"].stopped == 1


@pytest.mark.asyncio
async def test_registry_shutdown_cancels_replaced_idle_tasks():
    source = _PushSource("S1")
    registry = MirrorRegistry(idle_shutdown_seconds=30.0)
    registry.set_source_factory(lambda _key: source)
    session, first = await registry.acquire("S1", "agent")
    await registry.release("S1", first)
    _, second = await registry.acquire("S1", "agent")
    await registry.release("S1", second)

    await registry.shutdown()

    assert session._background_tasks == set()
    assert registry._sessions == {}
    assert source.stopped == 1


@pytest.mark.asyncio
async def test_registry_shutdown_failure_preserves_session_for_retry():
    class FailingStopSource(_PushSource):
        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    source = FailingStopSource("S1")
    registry = MirrorRegistry()
    registry.set_source_factory(lambda _key: source)
    session, _lease = await registry.acquire("S1", "agent")

    with pytest.raises(MirrorShutdownError, match="stop failed"):
        await registry.shutdown()

    assert registry._sessions["S1"] is session


@pytest.mark.asyncio
async def test_registry_unavailable_source_raises():
    class Boom:
        codec_string = None

        async def start(self) -> None:
            raise MirrorUnavailableError("jar missing")

        async def stop(self) -> None:
            return

        def is_alive(self) -> bool:
            return False

        async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
            if False:
                yield b""

    registry = MirrorRegistry()
    registry.set_source_factory(lambda _k: Boom())
    with pytest.raises(MirrorUnavailableError):
        await registry.start("S1")


@pytest.mark.asyncio
async def test_cancelled_first_acquire_cleans_half_started_source():
    entered = asyncio.Event()

    class SlowSource(_FakeSource):
        async def start(self) -> None:
            self.started += 1
            self._alive = True
            entered.set()
            await asyncio.Event().wait()

    source = SlowSource("S1")
    session = MirrorSession(device_key="S1", source=source)
    acquire = asyncio.create_task(session.acquire("agent"))
    await entered.wait()
    acquire.cancel()

    with pytest.raises(asyncio.CancelledError):
        await acquire

    assert source.stopped == 1
    assert source.is_alive() is False
    assert session.consumer_count == 0


@pytest.mark.asyncio
async def test_cancel_after_source_start_before_lease_commit_stops_source():
    source = _FakeSource("S1")
    session = MirrorSession(device_key="S1", source=source)
    await session._lock.acquire()
    acquire = asyncio.create_task(session.acquire("agent"))
    try:
        while source.started == 0:
            await asyncio.sleep(0)
        acquire.cancel()
        with pytest.raises(asyncio.CancelledError):
            await acquire
    finally:
        session._lock.release()

    assert source.stopped == 1
    assert source.is_alive() is False
    assert session.consumer_count == 0


@pytest.mark.asyncio
async def test_start_cleanup_reaps_process_after_initial_wait_timeout():
    class Proc:
        stdout = None

        def __init__(self):
            self.killed = False
            self.wait_calls: list[float | None] = []

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if timeout is not None:
                raise subprocess.TimeoutExpired("scrcpy", timeout)
            return -9

    source = LocalStreamSource("S1")
    proc = Proc()
    source._proc = proc  # type: ignore[assignment]

    await source._cleanup_start_failure()

    assert proc.killed is True
    assert proc.wait_calls == [0.5, None]
    assert source._proc is None


@pytest.mark.asyncio
async def test_remote_stop_bounds_stuck_websocket_close():
    class WebSocket:
        async def close(self):
            await asyncio.Future()

    source = RemoteStreamSource("http://hub", "S1")
    source._ws = WebSocket()
    source._alive = True

    await asyncio.wait_for(source.stop(), timeout=1.2)

    assert source.is_alive() is False
    assert source._ws is None


@pytest.mark.asyncio
async def test_local_cold_start_has_no_fixed_wait_and_orders_server_before_forward(
    monkeypatch,
):
    order: list[str] = []
    processes: list[object] = []

    class Proc:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.returncode = None
            self.waited = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, _timeout=None):
            self.waited = True
            return self.returncode

    class Reader:
        async def read(self, _size):
            return b"\x00"

    class Writer:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    async def push(*_args, **_kwargs):
        order.append("push")

    def popen(*_args, **_kwargs):
        assert _kwargs["stdout"] == subprocess.PIPE
        assert _kwargs["stderr"] == subprocess.STDOUT
        order.append("server")
        process = Proc()
        processes.append(process)
        return process

    async def forward(*_args, **_kwargs):
        order.append("forward")
        return 43210

    async def connect(*_args, **_kwargs):
        order.append("connect")
        return Reader(), Writer()

    async def cleanup(*_args, **_kwargs):
        return None

    monkeypatch.setattr("driver.adb.adb_bin", lambda: "adb")
    monkeypatch.setattr("driver.adb.push_file_async", push)
    monkeypatch.setattr("driver.adb.forward_localabstract_async", forward)
    monkeypatch.setattr("driver.adb.remove_forward_async", cleanup)
    monkeypatch.setattr("driver.adb.shell_async", cleanup)
    monkeypatch.setattr("driver.scrcpy_mirror.subprocess.Popen", popen)
    monkeypatch.setattr("driver.scrcpy_mirror.asyncio.open_connection", connect)

    source = LocalStreamSource("S1")
    started = time.monotonic()
    await source.start()

    assert time.monotonic() - started < 0.2
    assert order == ["push", "server", "forward", "connect", "connect"]
    assert source.is_alive() is True
    await source._cleanup_start_failure()
    assert source._proc is None
    assert processes[0].waited is True


@pytest.mark.asyncio
async def test_unknown_scrcpy_forward_port_reconciles_unique_socket(monkeypatch):
    calls: list[tuple[str, str, float]] = []

    async def reconcile(serial, socket_name, *, timeout):
        calls.append((serial, socket_name, timeout))
        return [43210]

    monkeypatch.setattr(
        "driver.adb.remove_forwards_to_localabstract_async", reconcile,
    )
    source = LocalStreamSource("S1")
    source._socket_name = "scrcpy_1234abcd"

    await source._cleanup_forward(timeout=0.5)

    assert calls == [("S1", "scrcpy_1234abcd", 0.5)]


@pytest.mark.asyncio
async def test_registry_idempotent_stop():
    registry = MirrorRegistry()
    await registry.stop("never-seen")


@pytest.mark.asyncio
async def test_explicit_leases_are_idempotent_and_restart_generations():
    source = _FakeSource("S1")
    registry = MirrorRegistry()
    registry.set_source_factory(lambda _key: source)
    session, console = await registry.acquire("S1", "console")
    _, agent = await registry.acquire("S1", "agent")
    assert source.started == 1
    assert session.generation == console.generation == agent.generation
    await registry.release("S1", console)
    await registry.release("S1", console)  # stale release cannot stop agent
    assert source.stopped == 0
    await registry.release("S1", agent)
    assert source.stopped == 1
    _, next_lease = await registry.acquire("S1", "agent")
    assert next_lease.generation > agent.generation


def test_annex_b_parser_retains_config_and_bootstraps_at_idr():
    p = AnnexBParser()
    # Deliberately split TCP data in the middle of a start code.
    assert p.feed(b"\0\0\0\x01\x67sps\0\0") == []
    units = p.feed(b"\x01\x68pps\0\0\0\x01\x65idr\0\0\0\x01\x41next")
    idr = next(nal for nal, is_idr in units if is_idr)
    assert b"\x67sps" in p.bootstrap(idr)
    assert b"\x68pps" in p.bootstrap(idr)


@pytest.mark.asyncio
async def test_local_packet_delivers_static_final_nal_without_next_frame():
    reader = asyncio.StreamReader()
    source = LocalStreamSource("S1")
    source._reader = reader
    packet = b"\0\0\0\x01\x67sps\0\0\0\x01\x68pps\0\0\0\x01\x65idr"
    framed = struct.pack(">QI", 1 << 62, len(packet)) + packet
    stream = source.frames()
    waiting = asyncio.create_task(anext(stream))
    reader.feed_data(framed[:15])
    await asyncio.sleep(0)
    assert not waiting.done()
    reader.feed_data(framed[15:])
    received = await asyncio.wait_for(waiting, 0.2)
    parser = AnnexBParser()
    units = parser.feed(received, packet_complete=source.packet_complete)
    assert units[-1] == (b"\0\0\0\x01\x65idr", True)
    assert parser._buffer == b""
    assert b"\x68pps" in parser.bootstrap(units[-1][0])
    await stream.aclose()


@pytest.mark.asyncio
async def test_local_packet_rejects_unbounded_size_before_reading_body():
    reader = asyncio.StreamReader()
    source = LocalStreamSource("S1")
    source._reader = reader
    reader.feed_data(struct.pack(">QI", 0, 0xFFFFFFFF))
    stream = source.frames()
    with pytest.raises(MirrorUnavailableError, match="packet size"):
        await anext(stream)


def test_static_screen_change_decodes_without_a_following_packet(monkeypatch):
    from fractions import Fraction
    from PIL import Image
    from driver.scrcpy_observation import PyAVDecoder

    av = pytest.importorskip("av")
    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = encoder.height = 64
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.options = {"preset": "ultrafast", "tune": "zerolatency", "crf": "0"}
    packets = []
    for color in ("red", "blue"):
        frame = av.VideoFrame.from_image(Image.new("RGB", (64, 64), color))
        packets.extend(bytes(packet) for packet in encoder.encode(frame))
    packets.extend(bytes(packet) for packet in encoder.encode(None))
    ticks = iter(range(1000, 1100))
    monkeypatch.setattr("driver.scrcpy_observation.time.monotonic", lambda: next(ticks))
    ring = FrameRing()
    decoder = PyAVDecoder(ring, lambda: FrameGeometry(64, 64, 64, 64))
    parser = AnnexBParser()
    for packet in packets:
        for unit, _ in parser.feed(packet, packet_complete=True):
            decoder.feed(unit, 1)
    pixel = Image.open(io.BytesIO(ring._frames[-1].data)).getpixel((32, 32))
    assert pixel[2] > 240 and pixel[0] < 10


@pytest.mark.asyncio
async def test_late_subscriber_skips_cached_idr_and_waits_for_new_bootstrap():
    source = _PushSource("S1")
    session = MirrorSession(device_key="S1", source=source)
    lease = await session.acquire("console")
    console_frames = session.frames()
    first_console = asyncio.create_task(anext(console_frames))
    await asyncio.sleep(0)
    await source.queue.put(
        b"\0\0\0\x01\x67sps"
        b"\0\0\0\x01\x68pps"
        b"\0\0\0\x01\x65idr"
        b"\0\0\0\x01\x41p1"
        b"\0\0\0\x01\x41p2"
    )
    bootstrap = await asyncio.wait_for(first_console, timeout=0.2)
    assert b"\x67sps" in bootstrap
    assert b"\x68pps" in bootstrap
    assert b"\x65idr" in bootstrap

    late_frames = session.frames()
    reset_calls = []

    async def reset_video():
        reset_calls.append(True)
        return True

    source.reset_video = reset_video
    pending = asyncio.create_task(anext(late_frames))
    await asyncio.sleep(0)
    assert reset_calls == [True]
    await source.queue.put(b"\0\0\0\x01\x41gap-p\0\0\0\x01\x41next-p")
    await asyncio.sleep(0.02)
    assert not pending.done()  # Neither stale IDR nor an undecodable live P frame.
    await source.queue.put(b"\0\0\0\x01\x65fresh-idr\0\0\0\x01\x41after")
    late_bootstrap = await asyncio.wait_for(pending, timeout=0.2)
    assert b"\x65fresh-idr" in late_bootstrap
    assert b"\x67sps" in late_bootstrap and b"\x68pps" in late_bootstrap
    assert late_bootstrap != bootstrap
    assert session.metrics()["bootstrap_cached"] is True

    await late_frames.aclose()
    await console_frames.aclose()
    await session.release(lease)
    assert session.metrics()["bootstrap_cached"] is False


def test_geometry_round_trip_and_generation_bounded_ring():
    geometry = FrameGeometry(540, 960, 1080, 1920)
    x, y = geometry.frame_to_device(123, 456)
    assert geometry.device_to_frame(x, y) == pytest.approx((123, 456))
    ring = FrameRing(retention_seconds=5, max_bytes=100)
    ring.append(FrameHandle(b"a", 1.0, 1, geometry))
    ring.append(FrameHandle(b"b", 2.0, 2, geometry))
    result = ring.query(0, 3, 2)
    assert result.status == "partial"


def test_ring_reports_expired_history():
    geometry = FrameGeometry(10, 10, 10, 10)
    ring = FrameRing(retention_seconds=1, max_bytes=100)
    ring.append(FrameHandle(b"a", 10.0, 1, geometry))
    ring.append(FrameHandle(b"b", 12.0, 1, geometry))
    assert ring.bounds == (12.0, 12.0)
    assert ring.query(1.0, 2.0, 2).status == "partial"
    assert len(ring._frames) == 1


def test_ring_clear_discards_pixels_without_reusing_frame_ids():
    geometry = FrameGeometry(10, 10, 10, 10)
    ring = FrameRing(retention_seconds=5, max_bytes=100)
    first = ring.append(FrameHandle(b"a", 10.0, 1, geometry))

    ring.clear()
    second = ring.append(FrameHandle(b"b", 11.0, 1, geometry))

    assert ring.bounds == (11.0, 11.0)
    assert first.frame_id == 1
    assert second.frame_id == 2
