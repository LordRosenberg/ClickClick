"""Task observation video transport, owned by the driver process.

One per-device scrcpy source supplies codec-safe H.264 units to observation
decoders. Explicit leases, startup bootstrap and idle cleanup also support
task pause/resume. Console image viewing reads saved artifacts separately.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol
from uuid import uuid4

from driver.processes import background_process_kwargs

logger = logging.getLogger(__name__)

SCRCPY_OBSERVATION_MAX_SIZE = 1440
SCRCPY_VIDEO_BIT_RATE = 8_000_000

# Pin must match the vendored jar filename and Server <ver> argv.
SCRCPY_SERVER_VERSION = "3.3.1"
SCRCPY_SERVER_JAR_NAME = f"scrcpy-server-v{SCRCPY_SERVER_VERSION}.jar"
# scrcpy control channel: TYPE_RESET_VIDEO restarts capture/encoding (1 byte).
SCRCPY_CONTROL_RESET_VIDEO = 17
VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
DEVICE_SERVER_PATH = "/data/local/tmp/clickclick-scrcpy-server.jar"

OBSERVATION_IDLE_GRACE_SECONDS: float = 30.0


@dataclass(frozen=True)
class ConsumerLease:
    """A single task consumer's ownership claim on a scrcpy source."""
    token: str
    consumer: str
    generation: int


@dataclass
class _Subscriber:
    queue: asyncio.Queue[bytes | None]
    bootstrap: bool = True


class AnnexBParser:
    """Incremental Annex-B parser that emits complete NAL units.

    We retain SPS/PPS and use IDR
    NALs as safe bootstrap boundaries; arbitrary TCP chunks are never exposed
    to a recovering consumer.
    """
    def __init__(self) -> None:
        self._buffer = b""
        self.config: list[bytes] = []

    @staticmethod
    def _starts(data: bytes) -> list[int]:
        starts: list[int] = []
        i = 0
        while i + 3 < len(data):
            if data[i:i + 3] == b"\x00\x00\x01":
                starts.append(i); i += 3
            elif data[i:i + 4] == b"\x00\x00\x00\x01":
                starts.append(i); i += 4
            else: i += 1
        return starts

    def feed(self, data: bytes, *, packet_complete: bool = False) -> list[tuple[bytes, bool]]:
        self._buffer += data
        starts = self._starts(self._buffer)
        if packet_complete and starts:
            starts.append(len(self._buffer))
        if len(starts) < 2:
            return []
        out: list[tuple[bytes, bool]] = []
        for a, b in zip(starts, starts[1:]):
            nal = self._buffer[a:b]
            offset = 4 if nal.startswith(b"\x00\x00\x00\x01") else 3
            kind = nal[offset] & 0x1F if len(nal) > offset else 0
            if kind in (7, 8):
                self.config = [x for x in self.config if (x[(4 if x.startswith(b"\x00\x00\x00\x01") else 3)] & 0x1F) != kind] + [nal]
            out.append((nal, kind == 5))
        self._buffer = self._buffer[starts[-1]:]
        return out

    def bootstrap(self, idr: bytes) -> bytes:
        return b"".join(self.config + [idr])


def server_jar_path() -> Path:
    return VENDOR_DIR / SCRCPY_SERVER_JAR_NAME


def is_scrcpy_server_available() -> bool:
    """True when the vendored scrcpy-server jar is present on this host."""
    return server_jar_path().is_file()


class ScrcpyUnavailableError(RuntimeError):
    """Raised when the scrcpy server jar is missing or a session cannot start."""


class ScrcpyShutdownError(RuntimeError):
    """Raised when a process-owned scrcpy session cannot be stopped."""


class StreamSource(Protocol):
    """Yields raw H.264 bytes for one device session."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def is_alive(self) -> bool: ...
    def frames(self) -> AsyncIterator[bytes]: ...


def _new_scid() -> int:
    """Return the 31-bit session id required for an isolated scrcpy socket."""
    return uuid4().int & 0x7FFF_FFFF


def _scrcpy_socket_name(scid: int) -> str:
    return f"scrcpy_{int(scid) & 0x7FFF_FFFF:08x}"


@dataclass
class LocalStreamSource:
    """Read complete H.264 packets from a local scrcpy-server over ADB."""

    serial: str
    max_size: int = SCRCPY_OBSERVATION_MAX_SIZE
    bit_rate: int = SCRCPY_VIDEO_BIT_RATE

    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _writer: asyncio.StreamWriter | None = field(default=None, init=False, repr=False)
    _control_reader: asyncio.StreamReader | None = field(
        default=None, init=False, repr=False,
    )
    _control_writer: asyncio.StreamWriter | None = field(
        default=None, init=False, repr=False,
    )
    _port: int | None = field(default=None, init=False, repr=False)
    _output_thread: threading.Thread | None = field(default=None, init=False, repr=False)
    packet_complete: bool = field(default=True, init=False)
    _scid: int | None = field(default=None, init=False, repr=False)
    _socket_name: str = field(default="", init=False, repr=False)
    _output_lines: list[str] = field(default_factory=list, init=False, repr=False)

    async def start(self) -> None:
        if self._proc is not None and self.is_alive():
            return
        if not is_scrcpy_server_available():
            raise ScrcpyUnavailableError(
                f"vendored scrcpy-server jar missing: {server_jar_path()}"
            )
        jar = server_jar_path()
        scid = _new_scid()
        socket_name = _scrcpy_socket_name(scid)
        self._scid = scid
        self._socket_name = socket_name
        self._output_lines.clear()

        try:
            await self._start_transport(
                jar=jar, scid=scid, socket_name=socket_name,
            )
        except BaseException as exc:  # noqa: BLE001
            # Startup failure is not a graceful shutdown path. Reap the exact
            # local/remote session immediately so an outer observation fuse is
            # never held by hidden blocking setup work.
            await asyncio.shield(self._cleanup_start_failure())
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, ScrcpyUnavailableError):
                raise
            raise ScrcpyUnavailableError(f"failed to start scrcpy-server: {exc}") from exc

    async def _start_transport(
        self, *, jar: Path, scid: int, socket_name: str
    ) -> None:
        """Start one isolated transport with cancellable ADB setup work."""
        from driver import adb

        await adb.push_file_async(
            self.serial, jar, DEVICE_SERVER_PATH, timeout=3.0,
        )
        argv = [
            adb.adb_bin(),
            "-s",
            self.serial,
            "shell",
            f"CLASSPATH={DEVICE_SERVER_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            SCRCPY_SERVER_VERSION,
            f"scid={scid:08x}",
            "tunnel_forward=true",
            "audio=false",
            "control=true",
            "clipboard_autosync=false",
            "cleanup=false",
            "send_device_meta=false",
            "send_codec_meta=false",
            "send_frame_meta=true",
            "send_dummy_byte=true",
            f"max_size={self.max_size}",
            f"video_bit_rate={self.bit_rate}",
        ]
        logger.info("scrcpy-server local start serial=%s", self.serial)
        self._proc = subprocess.Popen(
            argv,
            # scrcpy writes startup and controller failures to stdout too.
            # Drain both channels into one bounded diagnostic tail.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            **background_process_kwargs(),
        )

        self._output_thread = threading.Thread(
            target=_drain_output,
            args=(self._proc, self.serial, self._output_lines),
            name=f"scrcpy-server-output-{self.serial}",
            daemon=True,
        )
        self._output_thread.start()

        try:
            # The server process exists before the forward. Connection retry
            # below handles the short interval before its abstract socket is
            # bound, avoiding a fixed one-second blind wait.
            self._port = await adb.forward_localabstract_async(
                self.serial, socket_name, timeout=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            raise ScrcpyUnavailableError(f"adb forward failed: {exc}") from exc

        port = self._port
        if port is None:
            raise ScrcpyUnavailableError("adb forward returned no local port")

        last_err: Exception | None = None
        for _ in range(40):
            await asyncio.sleep(0.05)
            if self._proc.poll() is not None:
                raise ScrcpyUnavailableError(
                    self._early_exit_reason()
                )
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                # Reject the empty "adb accepted but server not ready" sockets:
                # peek with a short wait; if EOF with no bytes, reconnect.
                try:
                    first = await asyncio.wait_for(reader.read(1), timeout=1.0)
                except asyncio.TimeoutError:
                    writer.close()
                    await writer.wait_closed()
                    last_err = ScrcpyUnavailableError("scrcpy handshake timeout")
                    continue
                if not first:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:  # noqa: BLE001
                        pass
                    last_err = ScrcpyUnavailableError("empty forward socket")
                    continue
                if first != b"\x00":
                    writer.close()
                    await writer.wait_closed()
                    raise ScrcpyUnavailableError("invalid scrcpy handshake")
                await self._attach_video_connection(reader, writer)
                return
            except OSError as exc:
                last_err = exc
                continue
        raise ScrcpyUnavailableError(
            f"could not connect to scrcpy-server on port {port}: {last_err}"
        )

    async def _attach_video_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        if self._port is not None:
            await self._connect_control_socket(self._port)

    async def _connect_control_socket(self, port: int) -> None:
        """Second tunnel_forward accept is the scrcpy control channel."""
        await self._close_control_socket()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=2.0,
            )
            self._control_reader = reader
            self._control_writer = writer
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scrcpy control socket unavailable serial=%s: %s",
                self.serial,
                exc,
            )

    async def reset_video(self) -> bool:
        """Ask scrcpy-server to restart video capture and emit a new IDR."""
        writer = self._control_writer
        if writer is None:
            return False
        try:
            writer.write(bytes([SCRCPY_CONTROL_RESET_VIDEO]))
            await writer.drain()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("scrcpy reset_video failed serial=%s: %s", self.serial, exc)
            return False

    async def _close_control_socket(self) -> None:
        writer = self._control_writer
        self._control_writer = None
        self._control_reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def _cleanup_start_failure(self) -> None:
        """Best-effort bounded cleanup for a cancelled or failed cold start."""
        await self._close_control_socket()
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await asyncio.wait_for(wait_closed(), timeout=0.2)
                except Exception:  # noqa: BLE001
                    pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            wait = getattr(proc, "wait", None)
            if callable(wait):
                try:
                    await asyncio.to_thread(wait, 0.5)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    # SIGKILL cannot be ignored; complete the OS reap instead
                    # of declaring cleanup finished with a possible zombie.
                    await asyncio.to_thread(wait)
        self._proc = None
        await asyncio.gather(
            self._stop_remote_server(timeout=0.75),
            self._cleanup_forward(timeout=0.75),
            return_exceptions=True,
        )
        thread, self._output_thread = self._output_thread, None
        self._close_output(proc)
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 0.2)
        self._scid = None
        self._socket_name = ""

    async def stop(self) -> None:
        await self._close_control_socket()
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                try:
                    # A connected server normally exits when the video socket
                    # closes. Give that clean path a chance before signalling
                    # the local adb client.
                    await asyncio.to_thread(proc.wait, 0.75)
                except subprocess.TimeoutExpired:
                    await self._stop_remote_server()
                    try:
                        await asyncio.to_thread(proc.wait, 0.75)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            await asyncio.to_thread(proc.wait, 1.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            await asyncio.to_thread(proc.wait)
            except Exception as exc:  # noqa: BLE001
                logger.debug("scrcpy-server stop error serial=%s: %s", self.serial, exc)
        thread, self._output_thread = self._output_thread, None
        self._close_output(proc)
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 0.2)
        await self._cleanup_forward()
        self._scid = None
        self._socket_name = ""

    def terminate_host_process(self) -> None:
        """Best-effort interpreter-exit fallback; normal cleanup uses stop()."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:  # noqa: BLE001
                pass

    def _close_output(self, proc: subprocess.Popen[bytes] | None) -> None:
        stream = proc.stdout if proc is not None else None
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _early_exit_reason(self) -> str:
        detail = "; ".join(self._output_lines[-3:])
        suffix = f": {detail}" if detail else ""
        return f"scrcpy-server exited early for serial={self.serial}{suffix}"

    async def _stop_remote_server(self, *, timeout: float = 2.0) -> None:
        scid = self._scid
        if scid is None:
            return
        try:
            from driver import adb

            await adb.shell_async(
                self.serial,
                ["pkill", "-f", f"scid={scid:08x}"],
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("scrcpy-server remote stop error serial=%s: %s", self.serial, exc)

    async def _cleanup_forward(self, *, timeout: float = 2.0) -> None:
        port = self._port
        self._port = None
        if port is None:
            if self._socket_name:
                try:
                    from driver import adb

                    await adb.remove_forwards_to_localabstract_async(
                        self.serial, self._socket_name, timeout=timeout,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "adb forward reconcile failed serial=%s: %s",
                        self.serial, exc,
                    )
            return
        try:
            from driver import adb

            await adb.remove_forward_async(
                self.serial, port, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("adb forward remove failed serial=%s: %s", self.serial, exc)

    def is_alive(self) -> bool:
        if self._proc is None or self._reader is None:
            return False
        return self._proc.poll() is None

    async def frames(self) -> AsyncIterator[bytes]:
        reader = self._reader
        if reader is None:
            return
        try:
            while True:
                # scrcpy's 12-byte header preserves MediaCodec packet bounds.
                # Raw TCP chunk boundaries cannot delimit a static final NAL.
                header = await reader.readexactly(12)
                _pts_and_flags, size = struct.unpack(">QI", header)
                if not 0 < size <= 16 * 1024 * 1024:
                    raise ScrcpyUnavailableError("invalid scrcpy packet size")
                yield await reader.readexactly(size)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, OSError):
            return


@dataclass
class ScrcpySession:
    """One StreamSource bound to a device key, with shared consumer fan-out.

    A single background pump reads the source TCP once and copies chunks
    into per-consumer queues. Observation consumers MUST NOT each read ``source.frames()`` directly;
    one pump owns that reader across task lease and decoder lifecycles.
    """

    device_key: str
    source: StreamSource
    available: bool = field(default_factory=is_scrcpy_server_available)
    idle_shutdown_seconds: float = 0.0

    _last_activity: float = field(default=0.0, init=False, repr=False)
    _consumer_count: int = field(default=0, init=False, repr=False)
    _leases: dict[str, ConsumerLease] = field(default_factory=dict, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _subscribers: list[_Subscriber] = field(default_factory=list, init=False, repr=False)
    _parser: AnnexBParser = field(default_factory=AnnexBParser, init=False, repr=False)
    _latest_bootstrap: bytes | None = field(default=None, init=False, repr=False)
    _pump_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _idle_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _background_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False,
    )

    def _track_background_task(
        self, task: asyncio.Task[Any],
    ) -> None:
        self._background_tasks.add(task)

        def settle(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if done is self._idle_task:
                self._idle_task = None
            if done is self._pump_task:
                self._pump_task = None

        task.add_done_callback(settle)

    async def acquire(self, consumer: str = "agent") -> ConsumerLease:
        """Acquire an explicit lease; only first acquisition starts the source."""
        async with self._lifecycle:
            idle_task = self._idle_task
            self._idle_task = None
            if idle_task is not None:
                idle_task.cancel()
            if not (self._started and self.source.is_alive()):
                try:
                    await self.source.start()
                    async with self._lock:
                        self._latest_bootstrap = None
                    self._started = True
                    self._generation += 1
                    self._parser = AnnexBParser()
                    if self._pump_task is None or self._pump_task.done():
                        self._pump_task = asyncio.create_task(
                            self._pump(),
                            name=f"scrcpy-pump-{self.device_key}",
                        )
                        self._track_background_task(self._pump_task)
                    logger.info("scrcpy: started session key=%s", self.device_key)
                except BaseException:
                    # Start, state commit, and lease publication form one
                    # transaction; cancellation before publication owns cleanup.
                    self._started = False
                    await asyncio.shield(self.source.stop())
                    raise
            lease = ConsumerLease(uuid4().hex, consumer, self._generation)
            self._leases[lease.token] = lease
            self._consumer_count = len(self._leases)
            self._last_activity = time.monotonic()
            logger.info(
                "scrcpy: consumer+ key=%s consumers=%d",
                self.device_key,
                self._consumer_count,
            )
            return lease

    async def release(self, lease: ConsumerLease | str) -> None:
        """Idempotently release one lease; stale tokens are harmless."""
        token = lease.token if isinstance(lease, ConsumerLease) else lease
        pump: asyncio.Task[None] | None = None
        should_stop_source = False
        async with self._lifecycle:
            if self._leases.pop(token, None) is None:
                return
            self._consumer_count = len(self._leases)
            logger.info(
                "scrcpy: consumer- key=%s consumers=%d",
                self.device_key,
                self._consumer_count,
            )
            if self._consumer_count > 0:
                self._last_activity = time.monotonic()
                return
            self._last_activity = time.monotonic()
            if self.idle_shutdown_seconds > 0:
                generation = self._generation
                self._idle_task = asyncio.create_task(
                    self._stop_after_idle(generation),
                    name=f"scrcpy-idle-{self.device_key}",
                )
                self._track_background_task(self._idle_task)
                return
            pump, should_stop_source = await self._detach_for_stop()
        if pump is not None:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if should_stop_source:
            await self.source.stop()

    async def _detach_for_stop(self) -> tuple[asyncio.Task[None] | None, bool]:
        self._started = False
        pump = self._pump_task
        self._pump_task = None
        async with self._lock:
            self._latest_bootstrap = None
            for sub in self._subscribers:
                try:
                    sub.queue.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass
            self._subscribers.clear()
        return pump, True

    async def _stop_after_idle(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.idle_shutdown_seconds)
            async with self._lifecycle:
                if self._consumer_count or generation != self._generation:
                    return
                self._idle_task = None
                pump, should_stop_source = await self._detach_for_stop()
            if pump is not None:
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if should_stop_source:
                await self.source.stop()
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        """Force-close this process-owned session during registry shutdown."""
        async with self._lifecycle:
            self._idle_task = None
            self._leases.clear()
            self._consumer_count = 0
            await self._detach_for_stop()
            background = list(self._background_tasks)
            for task in background:
                task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        try:
            await self.source.stop()
        except BaseException as exc:
            raise ScrcpyShutdownError(
                f"scrcpy source cleanup failed for {self.device_key}: {exc}",
            ) from exc

    def is_alive(self) -> bool:
        if not self.source.is_alive():
            return False
        if self._consumer_count > 0:
            return True
        return (time.monotonic() - self._last_activity) <= OBSERVATION_IDLE_GRACE_SECONDS

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    async def _pump(self) -> None:
        try:
            async for chunk in self.source.frames():
                self.touch()
                units = self._parser.feed(
                    chunk, packet_complete=bool(getattr(self.source, "packet_complete", False)),
                )
                for unit, is_idr in units:
                    bootstrap_payload = self._parser.bootstrap(unit) if is_idr else None
                    async with self._lock:
                        if bootstrap_payload is not None:
                            self._latest_bootstrap = bootstrap_payload
                        subscribers = list(self._subscribers)
                    for sub in subscribers:
                        # A late or overflowed subscriber starts only on a
                        # configuration+IDR bootstrap, never a dropped chunk.
                        if sub.bootstrap:
                            if bootstrap_payload is None:
                                continue
                            payload = bootstrap_payload
                        else:
                            payload = unit
                        try:
                            sub.queue.put_nowait(payload)
                            sub.bootstrap = False
                        except asyncio.QueueFull:
                            while not sub.queue.empty():
                                try: sub.queue.get_nowait()
                                except asyncio.QueueEmpty: break
                            sub.bootstrap = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("scrcpy pump ended key=%s: %s", self.device_key, exc)
        finally:
            async with self._lock:
                subscribers = list(self._subscribers)
            for sub in subscribers:
                try:
                    sub.queue.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass

    async def frames(self) -> AsyncIterator[bytes]:
        sub = _Subscriber(asyncio.Queue(maxsize=32))
        async with self._lock:
            self._subscribers.append(sub)
            capture_started = self._latest_bootstrap is not None
        try:
            # A cached IDR without every intervening predictive frame is not a
            # valid starting point for the live stream. Wait for a new bootstrap
            # instead; request one when the source supports capture reset.
            # Register first so the resulting IDR cannot race past this consumer.
            reset = getattr(self.source, "reset_video", None)
            if capture_started and callable(reset):
                await reset()
            while True:
                item = await sub.queue.get()
                if item is None:
                    return
                yield item
        finally:
            async with self._lock:
                if sub in self._subscribers:
                    self._subscribers.remove(sub)

    @property
    def consumer_count(self) -> int:
        return self._consumer_count

    @property
    def generation(self) -> int:
        return self._generation

    def metrics(self) -> dict[str, Any]:
        return {
            "source_alive": self.source.is_alive(),
            "fanout_subscribers": len(self._subscribers),
            "leases": self._consumer_count,
            "generation": self._generation,
            "bootstrap_cached": self._latest_bootstrap is not None,
            "idle_shutdown_pending": self._idle_task is not None,
        }


class ScrcpyRegistry:
    """Process-wide registry of ScrcpySession instances keyed by device key."""

    def __init__(self, idle_shutdown_seconds: float = 0.0) -> None:
        self._sessions: dict[str, ScrcpySession] = {}
        self._lock = asyncio.Lock()
        self._source_factory: Callable[[str], StreamSource] | None = None
        self.idle_shutdown_seconds = max(0.0, float(idle_shutdown_seconds))

    def set_source_factory(self, factory: Callable[[str], StreamSource]) -> None:
        """Inject ``(device_key) -> StreamSource`` for deterministic transport tests."""
        self._source_factory = factory

    def _default_source(self, device_key: str) -> StreamSource:
        from driver.pool import LOCAL_DRIVER_ID, parse_device_key

        driver_id, serial = parse_device_key(device_key)
        if driver_id in (LOCAL_DRIVER_ID, "fixture"):
            return LocalStreamSource(serial=serial)
        # Observation sources belong to the local driver process; tests may inject a factory.
        raise ScrcpyUnavailableError(
            f"no StreamSource factory for remote key={device_key!r}"
        )

    def get(self, device_key: str) -> ScrcpySession:
        session = self._sessions.get(device_key)
        if session is None:
            factory = self._source_factory or self._default_source
            source = factory(device_key)
            session = ScrcpySession(
                device_key=device_key,
                source=source,
                idle_shutdown_seconds=self.idle_shutdown_seconds,
            )
            self._sessions[device_key] = session
        return session

    async def acquire(self, device_key: str, consumer: str = "agent") -> tuple[ScrcpySession, ConsumerLease]:
        session = self.get(device_key)
        return session, await session.acquire(consumer)

    async def release(self, device_key: str, lease: ConsumerLease | str) -> None:
        session = self._sessions.get(device_key)
        if session is None:
            return
        await session.release(lease)

    def terminate_host_processes(self) -> None:
        """Fallback owned here, without exposing subprocess internals to APIs."""
        for session in list(self._sessions.values()):
            if isinstance(session.source, LocalStreamSource):
                session.source.terminate_host_process()

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.items())
        failures: list[BaseException] = []
        for key, session in sessions:
            try:
                await session.shutdown()
            except Exception as exc:  # noqa: BLE001
                failures.append(exc)
            else:
                async with self._lock:
                    if self._sessions.get(key) is session:
                        self._sessions.pop(key, None)
        if failures:
            first = failures[0]
            raise ScrcpyShutdownError(
                f"scrcpy registry cleanup failed: {first}",
            ) from first


def _drain_output(
    proc: subprocess.Popen[bytes],
    serial: str,
    sink: list[str] | None = None,
) -> None:
    stream = proc.stdout
    if stream is None:
        return
    try:
        while True:
            line = stream.readline()
            if not line:
                return
            decoded = line.decode("utf-8", "replace").rstrip()
            if sink is not None:
                sink.append(decoded)
                del sink[:-20]
            logger.debug("scrcpy-server[%s] %s", serial, decoded)
    except Exception as exc:  # noqa: BLE001
        logger.debug("scrcpy-server[%s] output drain ended: %s", serial, exc)


REGISTRY: ScrcpyRegistry = ScrcpyRegistry(OBSERVATION_IDLE_GRACE_SECONDS)
atexit.register(REGISTRY.terminate_host_processes)
