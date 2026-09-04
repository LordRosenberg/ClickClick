"""Provider-neutral primitives for scrcpy-backed observations.

This module deliberately contains no agent/model types.  It is usable by the
Console transport and by a driver-side observation provider alike.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
import time
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from driver.scrcpy_mirror import ConsumerLease, MirrorRegistry, MirrorSession


@dataclass(frozen=True)
class FrameGeometry:
    """Immutable mapping between decoded stream pixels and driver coordinates."""
    stream_width: int
    stream_height: int
    device_width: int
    device_height: int
    rotation: int = 0
    crop_left: int = 0
    crop_top: int = 0
    crop_width: int | None = None
    crop_height: int | None = None
    timestamp: float = 0.0
    generation: int = 0

    def frame_to_device(self, x: float, y: float) -> tuple[float, float]:
        w = self.crop_width or self.stream_width
        h = self.crop_height or self.stream_height
        x, y = x + self.crop_left, y + self.crop_top
        r = self.rotation % 360
        if r == 90:
            x, y, w, h = self.stream_height - y, x, h, w
        elif r == 180:
            x, y = self.stream_width - x, self.stream_height - y
        elif r == 270:
            x, y, w, h = y, self.stream_width - x, h, w
        return x * self.device_width / w, y * self.device_height / h

    def device_to_frame(self, x: float, y: float) -> tuple[float, float]:
        # Invert affine transform by testing the rotated logical dimensions.
        r = self.rotation % 360
        w = self.crop_width or self.stream_width
        h = self.crop_height or self.stream_height
        if r in (90, 270):
            x, y = x * h / self.device_width, y * w / self.device_height
        else:
            x, y = x * w / self.device_width, y * h / self.device_height
        if r == 90:
            x, y = y, self.stream_height - x
        elif r == 180:
            x, y = self.stream_width - x, self.stream_height - y
        elif r == 270:
            x, y = self.stream_width - y, x
        return x - self.crop_left, y - self.crop_top


@dataclass(frozen=True)
class FrameHandle:
    data: bytes
    timestamp: float
    generation: int
    geometry: FrameGeometry
    frame_id: int = 0


@dataclass(frozen=True)
class TemporalResult:
    status: str
    frames: tuple[FrameHandle, ...] = ()
    available_from: float | None = None
    available_to: float | None = None
    detail: str = ""


@dataclass
class FrameRing:
    retention_seconds: float = 5.0
    max_bytes: int = 16 * 1024 * 1024
    _frames: deque[FrameHandle] = field(default_factory=deque, init=False)
    _bytes: int = field(default=0, init=False)
    _next_ids: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def append(self, frame: FrameHandle) -> FrameHandle:
        next_id = self._next_ids.get(frame.generation, 0) + 1
        self._next_ids[frame.generation] = next_id
        frame = replace(frame, frame_id=next_id)
        self._frames.append(frame)
        self._bytes += len(frame.data)
        self._trim(frame.timestamp)
        return frame

    def _trim(self, now: float) -> None:
        while self._frames and (self._bytes > self.max_bytes or now - self._frames[0].timestamp > self.retention_seconds):
            self._bytes -= len(self._frames.popleft().data)

    @property
    def bounds(self) -> tuple[float | None, float | None]:
        return (self._frames[0].timestamp, self._frames[-1].timestamp) if self._frames else (None, None)

    def latest(self, *, generation: int, after_id: int = -1) -> FrameHandle | None:
        return next(
            (
                frame
                for frame in reversed(self._frames)
                if frame.generation == generation and frame.frame_id > after_id
            ),
            None,
        )

    def query(self, start: float, end: float, count: int = 3, *, generation: int | None = None) -> TemporalResult:
        lo, hi = self.bounds
        if not self._frames:
            return TemporalResult("unavailable", available_from=lo, available_to=hi, detail="decoder has no frames")
        selected = [f for f in self._frames if start <= f.timestamp <= end and (generation is None or f.generation == generation)]
        generations = {f.generation for f in selected}
        if len(generations) > 1:
            return TemporalResult("partial", tuple(selected[:count]), lo, hi, "generation discontinuity")
        if len(selected) < count:
            return TemporalResult("partial", tuple(selected), lo, hi, "history expired or insufficient samples")
        if count == 1:
            selected = [selected[-1]]
        else:
            # evenly select ordered samples without duplicate indices
            selected = [selected[round(i * (len(selected)-1) / (count-1))] for i in range(count)]
        return TemporalResult("ok", tuple(selected), lo, hi)


class PyAVDecoder:
    """Optional single-consumer H.264 decoder feeding a :class:`FrameRing`.

    Importing this class never requires PyAV; callers can expose the typed
    unavailable state and retain the existing screencap path on hosts without
    the optional wheel.
    """
    def __init__(self, ring: FrameRing, geometry: Callable[[], FrameGeometry], max_fps: float = 6.0) -> None:
        self.ring, self.geometry, self.max_fps = ring, geometry, max(0.1, max_fps)
        self._codec = None
        self._last_emit = 0.0
        self.status = "unavailable"
        self.decode_errors = 0
        self.last_error: str | None = None

    def reset(self) -> None:
        self._codec = None
        self._last_emit = 0.0
        self.last_error = None
        self.status = "reset"

    def feed(self, annex_b: bytes, generation: int) -> int:
        now = time.monotonic()
        try:
            import av  # type: ignore[import-not-found]
            if self._codec is None:
                self._codec = av.CodecContext.create("h264", "r")
            decoded = self._codec.decode(av.Packet(annex_b))
        except ImportError:
            self.status = "unavailable"
            return 0
        except Exception as exc:
            # A dropped/interrupted predictive NAL may make libavcodec reject
            # one packet.  Discarding the entire codec context here makes the
            # decoder unable to recover until another SPS/PPS+IDR bootstrap,
            # while the subscriber itself remains mid-stream.  Keep the
            # context: H.264 decoders can conceal the loss and resume on a
            # subsequent packet or IDR.
            self.status = "corrupt_stream"
            self.decode_errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return 0
        emitted = 0
        for frame in decoded:
            if now - self._last_emit < 1.0 / self.max_fps:
                continue
            try:
                image = frame.to_image()
                from io import BytesIO
                out = BytesIO()
                image.save(out, format="PNG", compress_level=1)
                template = self.geometry()
                device_width = template.device_width
                device_height = template.device_height
                stream_is_portrait = image.height >= image.width
                device_is_portrait = device_height >= device_width
                if stream_is_portrait != device_is_portrait:
                    device_width, device_height = device_height, device_width
                g = replace(
                    template,
                    stream_width=image.width,
                    stream_height=image.height,
                    device_width=device_width,
                    device_height=device_height,
                    timestamp=now,
                    generation=generation,
                )
                self.ring.append(FrameHandle(out.getvalue(), now, generation, g))
                self._last_emit = now
                emitted += 1
                bypass_throttle = False
            except Exception:
                self.status = "decode_error"
                continue
        self.status = "healthy"
        self.last_error = None
        return emitted


@dataclass(frozen=True)
class CurrentFrameResult:
    status: str
    frame: FrameHandle | None = None
    detail: str = ""
    validation: str = ""
    visual_age_ms: float | None = None
    frame_after_boundary: bool | None = None
    source_healthy: bool = False
    source_alive: bool = False
    consumer_alive: bool = False
    decoder_healthy: bool = False


class ScrcpyObservationProvider:
    """One decoder consumer attached to a shared :class:`MirrorSession`."""
    def __init__(
        self,
        registry: "MirrorRegistry",
        device_key: str,
        geometry: Callable[[], FrameGeometry] | None = None,
        *,
        retention_seconds: float = 5.0,
        max_bytes: int = 16 * 1024 * 1024,
        max_fps: float = 6.0,
        frame_max_age_ms: int = 1250,
    ) -> None:
        self.registry, self.device_key = registry, device_key
        self.geometry = geometry
        self._device_size: tuple[int, int] | None = None
        self.ring = FrameRing(retention_seconds, max_bytes)
        self.decoder = PyAVDecoder(self.ring, self._geometry_template, max_fps)
        self._session: "MirrorSession | None" = None
        self._lease: "ConsumerLease | None" = None
        self._task = None
        self.status = "stopped"
        self.metrics: dict[str, float | int | str] = {"decoded_frames": 0, "decode_errors": 0}
        self.frame_max_age_ms = max(1, int(frame_max_age_ms))
        self._action_frame_boundary: tuple[int, int] | None = None

    async def start(self) -> bool:
        if self._task and not self._task.done():
            return True
        if self._device_size is None:
            try:
                from driver import adb

                self._device_size = await adb.display_size_async(self.device_key)
            except Exception:
                self._device_size = None
        if self._lease is not None:
            await self.registry.release(self.device_key, self._lease)
            self._lease = None
            self._session = None
        self._session, self._lease = await self.registry.acquire(self.device_key, "agent-decoder")
        # A newly acquired source generation begins at its own codec bootstrap;
        # never carry prediction state across generations.
        self.decoder.reset()
        self._task = __import__("asyncio").create_task(self._consume(), name=f"scrcpy-decode-{self.device_key}")
        self.status = "starting"
        return True

    def _geometry_template(self) -> FrameGeometry:
        if self.geometry is not None:
            template = self.geometry()
        else:
            template = FrameGeometry(0, 0, 0, 0)
        if self._device_size is None:
            return template
        return replace(
            template,
            device_width=self._device_size[0],
            device_height=self._device_size[1],
        )

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try: await self._task
            except BaseException: pass
            self._task = None
        if self._lease:
            await self.registry.release(self.device_key, self._lease)
            self._lease = None
        self._session = None
        self._action_frame_boundary = None
        self.status = "stopped"

    async def _consume(self) -> None:
        assert self._session is not None
        try:
            async for unit in self._session.frames():
                count = self.decoder.feed(unit, self._session.generation)
                self.metrics["decoded_frames"] = int(self.metrics["decoded_frames"]) + count
                self.metrics["decode_errors"] = self.decoder.decode_errors
                self.status = self.decoder.status
        except Exception:
            self.status = "unavailable"
            self.metrics["decode_errors"] = int(self.metrics["decode_errors"]) + 1
        finally:
            if self.status not in {"disabled", "stopped"}:
                self.status = "unavailable"

    def current(self) -> CurrentFrameResult:
        source_alive = self._session is not None and self._session.is_alive()
        if not source_alive:
            return CurrentFrameResult("unavailable", detail="source_dead")
        if self._task is None or self._task.done():
            return CurrentFrameResult("unavailable", detail="consumer_dead")
        if not self.ring._frames:
            # A live source/consumer may still be waiting for its first
            # decoded frame. Let the caller perform one bounded frame wait
            # before declaring scrcpy unavailable and falling back to ADB.
            return CurrentFrameResult(
                "unavailable", detail="no_frame", source_healthy=True,
            )
        frame = self.ring._frames[-1]
        if frame.generation != self.generation:
            return CurrentFrameResult("unavailable", detail="generation_mismatch")
        if self.decoder.status != "healthy":
            return CurrentFrameResult("unavailable", detail="decoder_error")
        if min(
            frame.geometry.stream_width,
            frame.geometry.stream_height,
            frame.geometry.device_width,
            frame.geometry.device_height,
        ) <= 0:
            return CurrentFrameResult("unavailable", detail="geometry_unavailable")

        age_ms = max(0.0, (time.monotonic() - frame.timestamp) * 1000.0)
        boundary = self._action_frame_boundary
        if boundary is not None and boundary[0] != self.generation:
            return CurrentFrameResult(
                "unavailable",
                detail="action_boundary_generation_changed",
                validation="action_boundary_generation_changed",
                visual_age_ms=age_ms,
                frame_after_boundary=False,
                source_healthy=False,
            )
        after_boundary = (
            frame.frame_id > boundary[1] if boundary is not None else None
        )
        if after_boundary is False:
            return CurrentFrameResult(
                "unavailable",
                detail="post_action_frame_missing",
                validation="post_action_frame_missing",
                visual_age_ms=age_ms,
                frame_after_boundary=False,
                source_healthy=True,
            )
        if after_boundary is True:
            validation = "post_action_frame"
        elif age_ms > self.frame_max_age_ms:
            validation = "live_static_reuse"
        else:
            validation = "live_frame"
        return CurrentFrameResult(
            "healthy",
            frame,
            detail=validation,
            validation=validation,
            visual_age_ms=age_ms,
            frame_after_boundary=after_boundary,
            source_healthy=True,
            source_alive=True,
            consumer_alive=True,
            decoder_healthy=True,
        )

    def frame_boundary(self) -> tuple[int, int] | None:
        """Snapshot the latest decoded-frame position before action dispatch."""
        if self._session is None or not self._session.is_alive():
            return None
        generation = self.generation
        latest = self.ring.latest(generation=generation)
        return generation, latest.frame_id if latest is not None else 0

    async def await_fresh_frame(
        self,
        *,
        after_id: int,
        timeout_s: float,
    ) -> FrameHandle | None:
        """Restart encoding when possible and wait for a later decoded frame id."""
        if timeout_s <= 0:
            return None
        source = self._session.source if self._session is not None else None
        reset = getattr(source, "reset_video", None)
        if callable(reset):
            await reset()
        deadline = time.monotonic() + timeout_s
        generation = self.generation
        while time.monotonic() < deadline:
            frame = self.ring.latest(generation=generation, after_id=after_id)
            if frame is not None:
                return frame
            await asyncio.sleep(0.01)
        return None

    @property
    def action_frame_boundary(self) -> tuple[int, int] | None:
        return self._action_frame_boundary

    def mark_action_complete(self, boundary: tuple[int, int] | None = None) -> None:
        """Commit a pre-dispatch boundary only after action dispatch succeeds."""
        if isinstance(boundary, tuple) and len(boundary) == 2:
            self._action_frame_boundary = (int(boundary[0]), int(boundary[1]))
        else:
            self._action_frame_boundary = None

    def temporal(self, start: float, end: float, count: int) -> TemporalResult:
        if self.decoder.status != "healthy":
            lo, hi = self.ring.bounds
            return TemporalResult("unavailable", available_from=lo, available_to=hi, detail=self.decoder.status)
        return self.ring.query(start, end, count, generation=self.generation)

    @staticmethod
    def decoder_available() -> bool:
        try:
            import av  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def generation(self) -> int:
        if self._session is not None:
            return self._session.generation
        return 0

    def diagnostics(self, *, selected_mode: str | None = None) -> dict[str, Any]:
        lo, hi = self.ring.bounds
        now = time.monotonic()
        freshness_ms = None if hi is None else max(0.0, (now - hi) * 1000.0)
        decode_available = self.decoder_available()
        source_available = self._session.available if self._session is not None else True
        available = decode_available and source_available
        current = self.current()
        source_alive = self._session is not None and self._session.is_alive()
        consumer_alive = self._task is not None and not self._task.done()
        healthy = current.source_healthy
        selected = current.status == "healthy" and selected_mode in {"current", "temporal", "screenshot"}
        return {
            "device": self.device_key,
            "available": available,
            "dependencies": {
                "decoder": decode_available,
                "stream_source": source_available,
            },
            "healthy": healthy,
            "source_alive": source_alive,
            "consumer_alive": consumer_alive,
            "selected": selected,
            "selected_mode": selected_mode if selected else None,
            "generation": self.generation,
            "status": self.status,
            "validation": current.validation or current.detail,
            "action_boundary_generation": (
                self._action_frame_boundary[0]
                if self._action_frame_boundary is not None else None
            ),
            "action_frame_boundary_id": (
                self._action_frame_boundary[1]
                if self._action_frame_boundary is not None else None
            ),
            "latest_frame_id": (
                self.ring._frames[-1].frame_id if self.ring._frames else None
            ),
            "frame_after_boundary": current.frame_after_boundary,
            "fallback_reason": None if healthy else current.detail,
            "ring": {
                "frame_count": len(self.ring._frames),
                "available_from": lo,
                "available_to": hi,
                "freshness_ms": freshness_ms,
                "visual_age_ms": freshness_ms,
                "fresh": freshness_ms is not None and freshness_ms <= self.frame_max_age_ms,
            },
            "metrics": {
                **self.metrics,
                "last_decode_error": self.decoder.last_error,
            },
        }
