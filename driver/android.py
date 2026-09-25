"""Android device control with causal tree/pixel capture and ADB fallback.

Action set (D9): tap / tap_xy / type / replace_text / swipe / long_press /
scroll / drag / key / launch / back / home / sleep. `launch` resolves a display name to a
package via a two-stage resolver (Stage 1: local curated seed + learned
cache; misses are exposed through the Executor's installed-app read tool);
on no match it returns a failure so the Planner can replan. Resolution lives
in the driver (device capability, not an agent tool).

Action dispatch deliberately does not guess when an effect is complete. The
agent-side ActionObservationTransaction owns bounded effect confirmation and
post-action stability using this driver's fresh capture metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import hashlib
import io
import time
from typing import Any, Callable

from driver import adb
from driver.accessibility import (
    CAPTURE_META_KEY,
    AccessibilityCollectorClient,
    AccessibilityTransportError,
    CollectorPowerLeaseSession,
    mark_dump_fallback,
)
from driver.adb import AdbError
from driver.environment import AndroidProvisioningOptions
from driver.observation_deadline import (
    CURRENT_DEADLINE_MS,
    SCREENCAP_MAX_BUDGET_MS,
    SCRCPY_ROUND_REFRESH_TIMEOUT_MS,
    SCRCPY_START_ATTEMPT_TIMEOUT_MS,
    TREE_PRIMARY_ATTEMPT_TIMEOUT_MS,
    ObservationDeadline,
    ObservationStageError,
)
from perception.filters import DetailedFilter
from perception.normalizer import (
    normalize_a11y_tree,
    tree_ownership_candidates,
    tree_ownership_source_complete,
)
from perception.observation import has_model_visible_tree_content
from perception.uiautomator import extract_package, parse_uiautomator_xml
from shared.app_resolver import NameResolver
from shared.schemas import Action, ActionResult, CanonicalUI

# Keep in sync with perception.observation.FRAME_GATE_DEGRADED_KEY.
FRAME_GATE_DEGRADED_KEY = "_frame_gate_degraded"
_EMPTY_HIERARCHY_XML = '<?xml version="1.0"?><hierarchy rotation="0"/>'
_SCRCPY_CAPABILITY_FAILURES = frozenset({
    "provider_unavailable",
    "scrcpy_start_timeout",
    "scrcpy_start_error",
    "source_dead",
    "consumer_dead",
    "no_frame",
    "generation_mismatch",
    "decoder_error",
    "geometry_unavailable",
    "action_boundary_generation_changed",
    "post_action_frame_missing",
    "live_frame_stale",
    "capture_boundary_frame_missing",
    "fresh_frame_timeout",
    "post_action_frame_timeout",
    "scrcpy_frame_unavailable",
    "scrcpy_frame_error",
})


def _scrcpy_capability_failure(reason: Any, *, default: str) -> str:
    """Project provider diagnostics onto the finite fallback protocol."""
    candidate = str(reason or "").strip()
    return candidate if candidate in _SCRCPY_CAPABILITY_FAILURES else default


# Map Action.key names to Android keycodes.
_KEYCODES = {
    "back": 4,
    "home": 3,
    "enter": 66,
    "menu": 82,
    "delete": 67,
    "search": 84,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "media_pause": 127,
}

_DEFAULT_CAPTURE_BUDGET_MS = CURRENT_DEADLINE_MS
# ADBKeyBoard IME defaults. ADBKeyBoard is a small open-source IME whose only
# job is to receive typed text via an `am broadcast -a ADB_INPUT_TEXT
# --es msg '...'` intent and forward it to the focused EditText. We use it
# to dodge `InputShellCommand.sendText:408` NPEs on non-ASCII payloads.
_DEFAULT_ADBKEYBOARD_IME_ID = "com.android.adbkeyboard/.AdbIME"
# Android may report the new default IME before its input connection is ready
# to receive broadcasts.  Task readiness switches early, and this short wait
# also protects the lazy input-time fallback when readiness was skipped.
_DEFAULT_IME_SWITCH_SETTLE_MS = 350

@dataclass(frozen=True)
class _PixelCapture:
    data: bytes
    completed_monotonic_ms: float


def _tree_has_model_visible_content(
    tree: dict[str, Any],
    *,
    screen: tuple[int, int] | None = None,
) -> bool:
    """Reject empty/off-screen providers using exact geometry when available."""
    tree_filter = DetailedFilter(screen=screen) if screen is not None else None
    ui = normalize_a11y_tree(
        tree,
        allow_tree_app_fallback=False,
        tree_filter=tree_filter,
    )
    if not has_model_visible_tree_content(ui):
        return False
    windowed = bool(
        isinstance(tree, dict)
        and any(
            isinstance(child, dict) and child.get("window_wrapper")
            for child in tree.get("children") or []
        )
    )
    if not windowed:
        return True
    candidates = tree_ownership_candidates(tree, tree_filter=tree_filter)
    return bool(
        tree_ownership_source_complete(tree, candidates)
        and any(
            int(row["retained_model_visible_node_count"]) > 0
            and bool(row["packages"])
            for row in candidates
        )
    )


class AndroidDriver:
    """Android driver: collector semantics, scrcpy/ADB pixels, ADB actions."""

    # Private native references can only cross an explicitly supporting transport.
    # Legacy HTTP drivers retain coordinate resolution instead of losing a binding.
    supports_native_node_click = True

    def __init__(
        self,
        *,
        serial: str | None = None,
        timeout: float = 30.0,
        resolver: NameResolver | None = None,
        capture_budget_ms: int = _DEFAULT_CAPTURE_BUDGET_MS,
        stream_provider: Any | None = None,
        ime_id: str = _DEFAULT_ADBKEYBOARD_IME_ID,
        ime_auto_setup: bool = True,
        collector_enabled: bool = False,
        collector_authority: str = "ai.clickclick.collector",
        collector_timeout_ms: int = TREE_PRIMARY_ATTEMPT_TIMEOUT_MS,
        provisioning: AndroidProvisioningOptions | None = None,
    ) -> None:
        self._serial: str | None = serial
        self.timeout = timeout
        self._last_ui: CanonicalUI | None = None
        # Two-stage app-name resolver (injected; defaults lazily on first use).
        self._resolver: NameResolver | None = resolver
        self._capture_budget_ms = max(1, int(capture_budget_ms))
        # Optional provider is deliberately injected: this driver layer has no
        # dependency on AgentSession/model schemas and can always fall back to
        # one causal ADB tree/pixel capture.
        self._stream_provider = stream_provider
        self._last_frame_geometry: Any | None = None
        # ADBKeyBoard IME channel for non-ASCII `type` actions. The package
        # must already be installed on the device (`adb install
        # adbkeyboard.apk`); per-task readiness selects it before any actions,
        # while input-time readiness remains as a defensive fallback.
        # `ime_auto_setup=False` short-circuits that setup so an operator can
        # disable the whole IME path without code changes.
        self._ime_id: str = ime_id
        self._ime_auto_setup: bool = bool(ime_auto_setup)
        self._collector_enabled = bool(collector_enabled)
        self._collector_authority = collector_authority
        self._collector_timeout_ms = max(1, int(collector_timeout_ms))
        self._provisioning = provisioning or AndroidProvisioningOptions()
        self._collector = AccessibilityCollectorClient(
            serial, authority=collector_authority
        )
        self._task_power_lease = CollectorPowerLeaseSession(self._collector)

    def set_last_ui(self, ui: CanonicalUI) -> None:
        """Inject the current frame's normalized UI so driver-side index/scroll
        resolution (`_resolve_xy`, `_scroll_coords`) operates on the same frame
        the Orchestrator observed, rather than a stale `_last_ui` from an
        earlier `get_ui_state()` call. The Orchestrator calls this after
        `get_frame()` + `ObservationBuilder.build()` to keep the cache current.
        """
        self._last_ui = ui

    @property
    def last_frame_geometry(self) -> Any | None:
        """Geometry for the exact image most recently returned by get_frame."""
        return self._last_frame_geometry

    async def interaction_event_cursor(self, *, timeout_s: float = 0.5) -> int | None:
        """Return a collector action boundary without exposing event content."""
        if not self._collector_enabled:
            return None
        return await self._collector.event_cursor(timeout=timeout_s)

    async def interaction_events_after(
        self, after_sequence: int, *, limit: int = 64, timeout_s: float = 0.5,
    ) -> Any | None:
        """Return one bounded safe-metadata batch for transaction correlation."""
        if not self._collector_enabled:
            return None
        return await self._collector.events_after(
            after_sequence, limit=limit, timeout=timeout_s,
        )

    @staticmethod
    def _image_dimensions(image: bytes) -> tuple[int, int]:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(image)) as parsed:
                return int(parsed.width), int(parsed.height)
        except Exception:  # noqa: BLE001
            return (0, 0)

    @classmethod
    def _healthy_frame(cls, result: Any) -> Any | None:
        """Return a mechanically valid frame from one healthy provider read."""
        frame = getattr(result, "frame", None)
        geometry = getattr(frame, "geometry", None)
        geometry_valid = bool(
            geometry is not None
            and min(
                int(getattr(geometry, "stream_width", 0)),
                int(getattr(geometry, "stream_height", 0)),
                int(getattr(geometry, "device_width", 0)),
                int(getattr(geometry, "device_height", 0)),
            ) > 0
        )
        bytes_valid = bool(
            frame is not None
            and all(dimension > 0 for dimension in cls._image_dimensions(frame.data))
        )
        if not (
            getattr(result, "status", "") == "healthy"
            and frame is not None
            and geometry_valid
            and bytes_valid
        ):
            return None
        return frame

    @classmethod
    def _geometry_metadata(
        cls, image: bytes, geometry: Any | None = None
    ) -> dict[str, Any]:
        image_size = cls._image_dimensions(image)
        device_size = image_size
        rotation = 0
        crop_box = None
        if geometry is not None:
            candidate = (
                int(getattr(geometry, "device_width", 0) or 0),
                int(getattr(geometry, "device_height", 0) or 0),
            )
            if candidate[0] > 0 and candidate[1] > 0:
                device_size = candidate
            rotation = int(getattr(geometry, "rotation", 0) or 0)
            crop_width = getattr(geometry, "crop_width", None)
            crop_height = getattr(geometry, "crop_height", None)
            if crop_width and crop_height:
                crop_box = [
                    int(getattr(geometry, "crop_left", 0) or 0),
                    int(getattr(geometry, "crop_top", 0) or 0),
                    int(getattr(geometry, "crop_left", 0) or 0) + int(crop_width),
                    int(getattr(geometry, "crop_top", 0) or 0) + int(crop_height),
                ]
        return {
            "image_geometry": list(image_size),
            "frame_geometry": list(device_size),
            "rotation_degrees": rotation,
            "crop_box": crop_box,
        }

    @classmethod
    def _stamp_geometry(
        cls, tree: dict[str, Any], image: bytes, geometry: Any | None = None
    ) -> dict[str, Any]:
        metadata = cls._geometry_metadata(image, geometry)
        capture = tree.setdefault(CAPTURE_META_KEY, {})
        if isinstance(capture, dict):
            capture.update(metadata)
        return metadata

    def _remember_frame_geometry(self, metadata: dict[str, Any]) -> None:
        geometry = metadata.get("frame_geometry")
        if isinstance(geometry, (list, tuple)) and len(geometry) == 2:
            width, height = int(geometry[0]), int(geometry[1])
            if width > 0 and height > 0:
                self._last_frame_geometry = (width, height)

    @staticmethod
    def _screen_from_geometry(metadata: dict[str, Any]) -> tuple[int, int] | None:
        geometry = metadata.get("frame_geometry")
        if not isinstance(geometry, (list, tuple)) or len(geometry) != 2:
            return None
        width, height = int(geometry[0]), int(geometry[1])
        return (width, height) if width > 0 and height > 0 else None

    def query_stream_temporal(self, start: float, end: float, count: int) -> Any | None:
        """Driver-layer provider query; it deliberately imports no agent code."""
        provider = self._stream_provider
        if provider is None:
            return None
        return provider.temporal(start, end, count)

    async def _current_stream_frame(self) -> Any | None:
        """Return one eligible scrcpy frame, or None for automatic ADB fallback."""
        provider = self._stream_provider
        if provider is None:
            return None
        try:
            starter = getattr(provider, "start", None)
            if callable(starter):
                await starter()
            result = provider.current()
            frame = getattr(result, "frame", None)
            if getattr(result, "status", "") == "healthy" and frame is not None:
                self._last_frame_geometry = frame.geometry
                return frame
        except Exception:  # noqa: BLE001 - provider failure is a soft fallback edge
            pass
        return None

    async def warm_observation_provider(self) -> bool:
        provider = self._stream_provider
        starter = getattr(provider, "start", None)
        if provider is None or not callable(starter):
            return False
        return bool(await starter())

    async def close_observation_provider(self) -> None:
        await self._task_power_lease.end()
        await self._collector.close()
        closer = getattr(self._stream_provider, "close", None)
        if callable(closer):
            await closer()

    def observation_diagnostics(self, mode: str | None = None) -> dict[str, Any]:
        provider = self._stream_provider
        diagnostics = getattr(provider, "diagnostics", None)
        if callable(diagnostics):
            return diagnostics(selected_mode=mode)
        return {
            "device": self._serial or "",
            "available": False,
            "dependencies": {"decoder": False, "stream_source": False},
            "healthy": False,
            "selected": False,
            "selected_mode": None,
            "generation": 0,
            "status": "unavailable",
            "ring": {"frame_count": 0, "freshness_ms": None, "fresh": False},
        }

    async def _collector_tree(
        self,
        *,
        timeout_s: float,
    ) -> dict[str, Any]:
        if not self._collector_enabled:
            raise AdbError("collector disabled")
        self._collector.serial = self._serial
        started = time.monotonic()
        snapshot = await self._collector.fetch_primary(timeout=timeout_s)
        tree = snapshot.to_raw_tree(elapsed_ms=(time.monotonic() - started) * 1000.0)
        capture = tree.setdefault(CAPTURE_META_KEY, {})
        if isinstance(capture, dict):
            capture["tree_ready_monotonic_ms"] = time.monotonic() * 1000.0
        return tree

    async def _deadline_tree(
        self,
        deadline: ObservationDeadline,
        *,
        screen: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """Acquire one Tree from the configured route without nested fallback."""
        capture_ordinal = int(getattr(deadline, "capture_ordinal", 1))
        attempts: list[dict[str, Any]] = []

        def record_attempt(row: dict[str, Any]) -> None:
            row.setdefault("capture_ordinal", capture_ordinal)
            attempts.append(row)
            deadline.record_attempt(row)

        tree: dict[str, Any] | None = None
        collector_ready = bool(
            self._collector_enabled
            and (
                capture_ordinal == 1
                or self._collector.diagnostics().get("ready")
            )
        )
        if collector_ready:
            stage = "tree_collector"
            timeout_s = deadline.remaining_seconds(stage)
            started = time.monotonic()
            capture = {}
            try:
                tree = await self._collector_tree(timeout_s=timeout_s)
                capture = tree.get(CAPTURE_META_KEY) or {}
                reasons = [str(value) for value in capture.get("reasons") or []]
                if "generation_changed" in reasons:
                    if capture.get("window_generation") is not None:
                        raise ObservationStageError("alignment", "window_transition")
                    raise ObservationStageError("accessibility_tree", "generation_changed")
                if capture.get("complete") is not True:
                    raise AdbError("collector returned incomplete snapshot: " + ",".join(reasons))
                if not _tree_has_model_visible_content(tree, screen=screen):
                    raise AdbError("collector returned model-empty snapshot")
            except asyncio.CancelledError:
                deadline.record(stage, started, budget_s=timeout_s, outcome="cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                reason = str(getattr(exc, "reason", exc))[:200]
                timed_out = bool(
                    isinstance(exc, AccessibilityTransportError)
                    and exc.failure_class in {"deadline_exhausted", "timeout"}
                )
                deadline.record(
                    stage, started, budget_s=timeout_s,
                    outcome="timeout" if timed_out else "error",
                    timed_out=timed_out,
                )
                record_attempt({
                    "provider": "accessibility_collector_primary",
                    "status": "timeout" if timed_out else "error",
                    "budget_ms": round(timeout_s * 1000.0, 3),
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "error": reason,
                    "window_generation": capture.get("window_generation"),
                    "window_quiet_ms": capture.get("window_quiet_ms"),
                    "failed_capture": {
                        key: value[:200] if isinstance(value, str) else value for key in (
                            "provider", "complete", "generation", "window_generation",
                            "window_quiet_ms", "content_changed_during_capture",
                            "window_count", "collector_snapshot_ms", "collector_elapsed_ms",
                            "collector_cache_cleared", "collector_capture_attempts",
                        )
                        if isinstance((value := capture.get(key)), (bool, int, float, str))
                    },
                    "failed_capture_reasons": [str(item)[:100] for item in (capture.get("reasons") or [])[:8]],
                })
                if capture_ordinal == 1 or isinstance(exc, ObservationStageError):
                    raise ObservationStageError(
                        "accessibility_tree",
                        reason or "collector_failed",
                        elapsed_ms=deadline.elapsed_ms,
                        budget_ms=deadline.budget_ms,
                        timed_out=timed_out,
                        provider_attempts=attempts,
                    ) from exc
                tree = None
            else:
                deadline.record(stage, started, budget_s=timeout_s)
                record_attempt({
                    "provider": "accessibility_collector_primary",
                    "status": "ok",
                    "budget_ms": round(timeout_s * 1000.0, 3),
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "error": "",
                })
        if tree is None:
            started = time.monotonic()
            try:
                tree = await self._deadline_dump(deadline)
                if not _tree_has_model_visible_content(tree, screen=screen):
                    raise AdbError("uiautomator dump returned model-empty snapshot")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reason = str(getattr(exc, "reason", exc))[:200]
                timed_out = bool(
                    getattr(exc, "timed_out", False)
                    or "timed out" in reason.casefold()
                )
                record_attempt({
                    "provider": "uiautomator_dump",
                    "status": "timeout" if timed_out else "error",
                    "budget_ms": float(getattr(exc, "budget_ms", 0.0)),
                    "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                    "error": reason,
                })
                raise ObservationStageError(
                    "accessibility_tree",
                    reason or "uiautomator_dump_failed",
                    elapsed_ms=deadline.elapsed_ms,
                    budget_ms=deadline.budget_ms,
                    timed_out=timed_out,
                    provider_attempts=attempts,
                ) from exc
            record_attempt({
                "provider": "uiautomator_dump",
                "status": "ok",
                "budget_ms": round(deadline.budget_ms, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "error": "",
            })

        assert tree is not None
        capture = tree.setdefault(CAPTURE_META_KEY, {})
        if isinstance(capture, dict):
            capture["tree_provider_attempts"] = list(attempts)
            capture["tree_providers_exhausted"] = False
        return tree

    async def _deadline_dump(self, deadline: ObservationDeadline) -> dict[str, Any]:
        remaining_ms = deadline.remaining_ms
        if remaining_ms <= 0:
            raise ObservationStageError(
                "ui_dump",
                "outer_deadline_exhausted",
                elapsed_ms=deadline.elapsed_ms,
                budget_ms=deadline.budget_ms,
                timed_out=True,
            )
        timeout_s = max(0.001, remaining_ms / 1000.0)
        started = time.monotonic()
        try:
            xml = await adb.uiautomator_dump_async(
                self._serial, max_attempts=1, timeout=timeout_s,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            timed_out = "timed out" in str(exc).casefold()
            deadline.record(
                "ui_dump",
                started,
                budget_s=timeout_s,
                outcome="timeout" if timed_out else "error",
                timed_out=timed_out,
            )
            raise ObservationStageError(
                "ui_dump",
                str(exc),
                elapsed_ms=elapsed,
                budget_ms=timeout_s * 1000.0,
                timed_out=timed_out,
            ) from exc
        deadline.record("ui_dump", started, budget_s=timeout_s)
        tree = parse_uiautomator_xml(xml)
        capture = tree.setdefault(CAPTURE_META_KEY, {})
        if isinstance(capture, dict):
            capture.update({
                "provider": "uiautomator_dump",
                "complete": True,
                "tree_ready_monotonic_ms": time.monotonic() * 1000.0,
            })
        return tree

    async def _deadline_screencap(
        self, deadline: ObservationDeadline,
    ) -> _PixelCapture:
        remaining_ms = deadline.remaining_ms
        if remaining_ms <= 0:
            raise ObservationStageError(
                "screencap",
                "outer_deadline_exhausted",
                elapsed_ms=deadline.elapsed_ms,
                budget_ms=deadline.budget_ms,
                timed_out=True,
            )
        timeout_s = max(
            0.001,
            min(remaining_ms, float(SCREENCAP_MAX_BUDGET_MS)) / 1000.0,
        )
        started = time.monotonic()
        try:
            shot = await adb.screencap_async(self._serial, timeout=timeout_s)
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            timed_out = "timed out" in str(exc).casefold()
            deadline.record(
                "screencap",
                started,
                budget_s=timeout_s,
                outcome="timeout" if timed_out else "error",
                timed_out=timed_out,
            )
            raise ObservationStageError(
                "screencap",
                str(exc),
                elapsed_ms=elapsed,
                budget_ms=timeout_s * 1000.0,
                timed_out=timed_out,
            ) from exc
        deadline.record("screencap", started, budget_s=timeout_s)
        return _PixelCapture(shot, time.monotonic() * 1000.0)

    @staticmethod
    def _stamp_capture_timings(
        tree: dict[str, Any], deadline: ObservationDeadline
    ) -> None:
        capture = tree.setdefault(CAPTURE_META_KEY, {})
        if isinstance(capture, dict):
            capture["stage_timings"] = list(deadline.stage_timings)

    async def capture_native_frame(self, deadline: ObservationDeadline):
        """Fresh native pixels and tree under the same existing capture fences."""
        return await self.capture_deadline_frame(deadline, prefer_stream=False)

    async def capture_deadline_frame(
        self, deadline: ObservationDeadline, *, prefer_stream: bool = True
    ) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
        """Capture a frame and always reap work abandoned by an early exit."""
        capture_tasks: set[asyncio.Task[Any]] = set()
        failure: BaseException | None = None
        try:
            return await self._capture_simple_frame(
                deadline,
                prefer_stream=prefer_stream,
                capture_tasks=capture_tasks,
            )
        except BaseException as exc:
            failure = exc
            raise
        finally:
            pending = [task for task in capture_tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            cancelled_names = [
                task.get_name()
                for task in capture_tasks
                if task.cancelled() or task in pending
            ]
            deadline.cancelled_tasks = list(dict.fromkeys(
                deadline.cancelled_tasks + cancelled_names
            ))
            if isinstance(failure, ObservationStageError):
                failure.cancelled_tasks = list(dict.fromkeys(
                    list(failure.cancelled_tasks)
                    + deadline.cancelled_tasks
                ))

    @staticmethod
    def _scrcpy_frame_after_action(
        frame: Any,
        boundary: tuple[int, int] | None,
    ) -> bool:
        if frame is None:
            return False
        if not isinstance(boundary, tuple) or len(boundary) != 2:
            return True
        return (
            int(getattr(frame, "generation", -1)) == int(boundary[0])
            and int(getattr(frame, "frame_id", 0)) > int(boundary[1])
        )

    async def _capture_simple_frame(
        self,
        deadline: ObservationDeadline,
        *,
        prefer_stream: bool,
        capture_tasks: set[asyncio.Task[Any]],
    ) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
        """Capture one tree and one sequentially selected pixel source.

        The transaction above this Driver owns the only optional full resample.
        This method deliberately does not chase a tree/image fixed point.
        """
        capture_started_ms = time.monotonic() * 1000.0
        capture_ordinal = int(getattr(deadline, "capture_ordinal", 1))
        fallback_edges: list[dict[str, str]] = []
        entry_window_state = {}
        if self._collector_enabled and self._collector.diagnostics().get("ready"):
            try:
                entry_window_state = await self._collector.window_state(
                    timeout=min(0.3, deadline.remaining_seconds("window_fence")),
                )
                quiet_ms = entry_window_state.get("window_quiet_ms")
                if quiet_ms is not None and float(quiet_ms) < 300:
                    await deadline.wait(300 - max(0, float(quiet_ms)))
            except Exception:
                # A failed optional entry read is not a reason to discard pixels.
                entry_window_state = {}

        async def capture_pixels() -> tuple[bytes, str, float, Any | None, str]:
            provider = self._stream_provider if prefer_stream else None
            failure_reason = "provider_unavailable"
            if provider is not None:
                started = time.monotonic()
                try:
                    starter = getattr(provider, "start", None)
                    if callable(starter):
                        remaining_s = deadline.remaining_ms / 1000.0
                        if remaining_s <= 0:
                            raise ObservationStageError(
                                "pixel_capture", "outer_deadline_exhausted",
                                timed_out=True,
                            )
                        await asyncio.wait_for(
                            starter(),
                            timeout=min(
                                remaining_s,
                                SCRCPY_START_ATTEMPT_TIMEOUT_MS / 1000.0,
                            ),
                        )
                    frame = await self._refresh_scrcpy_before_pixel_capture(
                        provider, deadline,
                    )
                    if frame is None and not callable(getattr(provider, "await_fresh_frame", None)):
                        result = provider.current()
                        frame = self._healthy_frame(result)
                    else:
                        result = None
                    boundary = getattr(provider, "action_frame_boundary", None)
                    after_action = self._scrcpy_frame_after_action(frame, boundary)
                    if frame is not None and after_action:
                        deadline.record_attempt({
                            "provider": "scrcpy",
                            "status": "ok",
                            "elapsed_ms": round(
                                (time.monotonic() - started) * 1000.0, 3,
                            ),
                            "generation": int(frame.generation),
                            "frame_id": int(frame.frame_id),
                            "capture_ordinal": capture_ordinal,
                        })
                        self._last_frame_geometry = frame.geometry
                        return (
                            frame.data,
                            "scrcpy",
                            float(frame.timestamp) * 1000.0,
                            frame.geometry,
                            "",
                        )
                    failure_reason = (
                        "post_action_frame_missing"
                        if frame is not None else
                        _scrcpy_capability_failure(
                            getattr(result, "detail", "") if result is not None else "",
                            default="scrcpy_frame_unavailable",
                        )
                    )
                except asyncio.CancelledError:
                    deadline.cancelled_tasks.append("current-observation-pixels")
                    raise
                except Exception as exc:  # noqa: BLE001
                    failure_reason = _scrcpy_capability_failure(
                        getattr(exc, "reason", str(exc)),
                        default="scrcpy_frame_error",
                    )
                deadline.record_attempt({
                    "provider": "scrcpy",
                    "status": "error",
                    "elapsed_ms": round(
                        (time.monotonic() - started) * 1000.0, 3,
                    ),
                    "failure_class": failure_reason,
                    "error": failure_reason,
                    "capture_ordinal": capture_ordinal,
                })

            fallback_edges.append({
                "from": "scrcpy" if provider is not None else "direct_capture",
                "to": "adb_screencap",
                "reason": failure_reason,
            })
            deadline.fallback_edges.append(dict(fallback_edges[-1]))
            adb_started = time.monotonic()
            try:
                captured = await self._deadline_screencap(deadline)
                if isinstance(captured, _PixelCapture):
                    captured_bytes = captured.data
                    captured_ms = captured.completed_monotonic_ms
                else:
                    captured_bytes = bytes(captured)
                    captured_ms = time.monotonic() * 1000.0
                deadline.record_attempt({
                    "provider": "adb_screencap",
                    "status": "ok",
                    "elapsed_ms": round(
                        (time.monotonic() - adb_started) * 1000.0, 3,
                    ),
                    "capture_ordinal": capture_ordinal,
                })
                return (
                    captured_bytes,
                    "adb_screencap",
                    captured_ms,
                    None,
                    failure_reason,
                )
            except asyncio.CancelledError:
                deadline.cancelled_tasks.append("current-observation-pixels")
                raise
            except Exception as exc:  # noqa: BLE001
                reason = str(getattr(exc, "reason", exc))[:200]
                deadline.record_attempt({
                    "provider": "adb_screencap",
                    "status": "error",
                    "elapsed_ms": float(getattr(exc, "elapsed_ms", 0.0)),
                    "failure_class": reason,
                    "error": reason,
                    "capture_ordinal": capture_ordinal,
                })
                return b"", "unavailable", 0.0, None, reason

        async def capture_tree() -> dict[str, Any]:
            # Tree acquisition is optional when current pixels are available.
            # Bound its entire route (including dump fallback), leaving time
            # for pixel fallback and the transaction's foreground checks.
            budget_ms = min(TREE_PRIMARY_ATTEMPT_TIMEOUT_MS, deadline.remaining_ms / 2)
            tree_deadline = ObservationDeadline(deadline.mode, budget_ms)
            tree_deadline.capture_ordinal = capture_ordinal
            tree_deadline.provider_attempts = deadline.provider_attempts
            tree_deadline.stage_timings = deadline.stage_timings
            tree_deadline.fallback_edges = deadline.fallback_edges
            started = time.monotonic()
            try:
                return await asyncio.wait_for(
                    self._deadline_tree(tree_deadline), timeout=budget_ms / 1000.0,
                )
            except asyncio.TimeoutError as exc:
                deadline.cancelled_tasks.append("current-observation-tree")
                deadline.record_attempt({
                    "provider": "tree_capture", "status": "timeout",
                    "budget_ms": budget_ms,
                    "elapsed_ms": (time.monotonic() - started) * 1000.0,
                    "error": "tree_budget_exhausted", "capture_ordinal": capture_ordinal,
                    "collector_diagnostics": self._collector.diagnostics(),
                })
                raise ObservationStageError(
                    "accessibility_tree", "tree_budget_exhausted",
                    budget_ms=budget_ms, timed_out=True,
                    provider_attempts=list(deadline.provider_attempts),
                ) from exc

        tree_task = asyncio.create_task(capture_tree(), name="current-observation-tree")
        pixel_task = asyncio.create_task(
            capture_pixels(), name="current-observation-pixels",
        )
        capture_tasks.update({tree_task, pixel_task})
        tree_result, pixel_result = await asyncio.gather(
            tree_task, pixel_task, return_exceptions=True,
        )

        tree_failure = (
            tree_result if isinstance(tree_result, BaseException) else None
        )
        if isinstance(tree_failure, ObservationStageError) and tree_failure.reason == "window_transition":
            raise tree_failure
        if tree_failure is None:
            tree = tree_result
        else:
            tree = parse_uiautomator_xml(_EMPTY_HIERARCHY_XML)
            tree[FRAME_GATE_DEGRADED_KEY] = True
            failure_capture = tree.setdefault(CAPTURE_META_KEY, {})
            if isinstance(failure_capture, dict):
                attempts = list(getattr(tree_failure, "provider_attempts", []) or [])
                dump_attempted = any(row.get("provider") == "uiautomator_dump" for row in attempts)
                failure_capture.update({
                    "provider": "unavailable",
                    "complete": False,
                    "reasons": [str(getattr(tree_failure, "reason", tree_failure))[:200]],
                    "tree_providers_exhausted": dump_attempted,
                    "dump_attempted": dump_attempted,
                    "tree_provider_attempts": attempts,
                })
                for attempt in getattr(tree_failure, "provider_attempts", []) or []:
                    if attempt.get("window_generation") is not None:
                        failure_capture.update({key: attempt[key] for key in ("window_generation", "window_quiet_ms") if key in attempt})

        if isinstance(pixel_result, BaseException):
            if isinstance(pixel_result, asyncio.CancelledError):
                raise pixel_result
            shot, pixel_provider, pixel_ms, pixel_geometry, pixel_failure = (
                b"", "unavailable", 0.0, None, str(pixel_result)[:200]
            )
        else:
            shot, pixel_provider, pixel_ms, pixel_geometry, pixel_failure = (
                pixel_result
            )

        if pixel_provider == "scrcpy" and self._stream_provider is not None:
            # Tree traversal may outlast the first refreshed image. Use an
            # already decoded newer frame from that same stream, without a
            # second RESET, capture, wait, or semantic comparison.
            try:
                latest = self._healthy_frame(self._stream_provider.current())
                if (
                    latest is not None
                    and latest.generation == getattr(pixel_geometry, "generation", None)
                    and latest.timestamp * 1000.0 >= pixel_ms
                    and self._scrcpy_frame_after_action(
                        latest, getattr(self._stream_provider, "action_frame_boundary", None),
                    )
                ):
                    shot = latest.data
                    pixel_ms = latest.timestamp * 1000.0
                    pixel_geometry = latest.geometry
                    self._last_frame_geometry = latest.geometry
            except Exception as exc:  # A failed optional read must not discard captured pixels.
                deadline.record_attempt({
                    "provider": "scrcpy", "phase": "final_pixel_selection", "status": "error",
                    "error": str(exc)[:200], "capture_ordinal": capture_ordinal,
                })

        self._stamp_capture_timings(tree, deadline)
        geometry_meta = self._stamp_geometry(tree, shot, pixel_geometry)
        self._remember_frame_geometry(geometry_meta)
        screen = self._screen_from_geometry(geometry_meta)
        coordinate_compatible = bool(
            tree_failure is None
            and (
                not shot
                or screen is not None
                and _tree_has_model_visible_content(tree, screen=screen)
            )
        )
        tree_capture = tree.setdefault(CAPTURE_META_KEY, {})
        # A readable/current image is not evidence that a window transition
        # has ended. Only the new collector exposes this mechanical fence;
        # missing trees on legacy/unsupported surfaces still permit image-only.
        window_generation = tree_capture.get("window_generation")
        if window_generation is None:
            window_generation = entry_window_state.get("window_generation")
        if window_generation is not None:
            try:
                # A cancelled tree exchange closes its socket. A cold health
                # read then includes ADB bootstrap/forwarding; the warm 300 ms
                # budget would systematically fail before checking the window.
                fence_timeout = 0.3 if self._collector.diagnostics().get("ready") else 1.5
                window_state = await self._collector.window_state(
                    timeout=min(fence_timeout, deadline.remaining_seconds("window_fence")),
                )
            except Exception as exc:
                raise ObservationStageError("alignment", "window_fence_unavailable") from exc
            if (
                window_state.get("window_generation") != window_generation
                or (entry_window_state.get("window_generation") is not None
                    and entry_window_state["window_generation"] != window_generation)
                or float(window_state.get("window_quiet_ms", 0)) < 300
                or (tree_capture.get("window_quiet_ms") is not None
                    and float(tree_capture["window_quiet_ms"]) < 300)
            ):
                raise ObservationStageError("alignment", "window_transition")
            tree_capture["window_fence"] = "unchanged"
        tree_ready_ms = float(
            tree_capture.get("tree_ready_monotonic_ms")
            or time.monotonic() * 1000.0
        )
        diagnostics = self.observation_diagnostics(None)
        metadata = {
            "provider": (
                "scrcpy" if pixel_provider == "scrcpy" else
                "adb_fallback" if pixel_provider == "adb_screencap" else
                "unavailable"
            ),
            "generation": int(diagnostics.get("generation") or 0),
            "tree_provider": tree_capture.get("provider", "unknown"),
            "tree_generation": int(tree_capture.get("generation") or 0),
            "pixel_provider": pixel_provider,
            "provider_state": diagnostics,
            "fallback_edges": fallback_edges,
            "provider_attempts": list(deadline.provider_attempts),
            "coordinate_compatible": coordinate_compatible,
            "tree_ready_monotonic_ms": tree_ready_ms,
            "capture_started_monotonic_ms": capture_started_ms,
            "pixel_monotonic_ms": pixel_ms,
            "coherence_status": (
                "mechanically_compatible"
                if coordinate_compatible else "component_degraded"
            ),
            "pixel_failure": pixel_failure,
            "complete": bool(
                tree_failure is None
                and tree_capture.get("complete") is True
            ),
            "active_capture_ordinal": capture_ordinal,
            **geometry_meta,
        }
        tree_capture.update(metadata)
        return tree, shot, metadata

    async def _refresh_scrcpy_before_pixel_capture(
        self,
        provider: Any,
        deadline: ObservationDeadline,
    ) -> Any | None:
        """RESET_VIDEO + wait for a newer decoded frame before pixel capture."""
        await_fresh = getattr(provider, "await_fresh_frame", None)
        if not callable(await_fresh):
            return None
        boundary_getter = getattr(provider, "frame_boundary", None)
        baseline = boundary_getter() if callable(boundary_getter) else None
        after_id = int(baseline[1]) if baseline is not None else 0
        wait_s = min(
            max(0.0, deadline.remaining_ms / 1000.0),
            SCRCPY_ROUND_REFRESH_TIMEOUT_MS / 1000.0,
        )
        if wait_s <= 0:
            return None
        started = time.monotonic()
        try:
            frame = await await_fresh(after_id=after_id, timeout_s=wait_s)
        except Exception as exc:  # noqa: BLE001
            deadline.record_attempt({
                "provider": "scrcpy",
                "status": "error",
                "phase": "pixel_refresh",
                "elapsed_ms": round(
                    (time.monotonic() - started) * 1000.0, 3,
                ),
                "fresh_after_id": after_id,
                "failure_class": _scrcpy_capability_failure(
                    getattr(exc, "reason", str(exc)),
                    default="scrcpy_pixel_refresh_error",
                ),
                "error": str(exc)[:200],
            })
            return None
        deadline.record_attempt({
            "provider": "scrcpy",
            "status": "ok" if frame is not None else "error",
            "phase": "pixel_refresh",
            "elapsed_ms": round(
                (time.monotonic() - started) * 1000.0, 3,
            ),
            "fresh_after_id": after_id,
            "generation": int(frame.generation) if frame is not None else None,
            "frame_id": int(frame.frame_id) if frame is not None else None,
        })
        return frame

    async def refresh_stream_frame(
        self,
        deadline: ObservationDeadline | None = None,
        *,
        timeout_s: float | None = None,
    ) -> bool:
        """Restart scrcpy encoding before an explicit pixel capture."""
        provider = self._stream_provider
        if provider is None:
            return False
        if deadline is None:
            deadline = ObservationDeadline(
                "pixel_refresh",
                SCRCPY_ROUND_REFRESH_TIMEOUT_MS,
            )
        try:
            starter = getattr(provider, "start", None)
            if callable(starter):
                remaining_s = (
                    timeout_s
                    if timeout_s is not None else
                    min(
                        max(0.0, deadline.remaining_ms / 1000.0),
                        SCRCPY_ROUND_REFRESH_TIMEOUT_MS / 1000.0,
                    )
                )
                if remaining_s > 0:
                    await asyncio.wait_for(
                        starter(),
                        timeout=min(
                            remaining_s,
                            SCRCPY_START_ATTEMPT_TIMEOUT_MS / 1000.0,
                        ),
                    )
            frame = await self._refresh_scrcpy_before_pixel_capture(
                provider, deadline,
            )
            return frame is not None
        except Exception:
            return False

    async def sample_stream_temporal(
        self, start: float, end: float, count: int, deadline: ObservationDeadline
    ) -> Any | None:
        if self._stream_provider is None:
            return None
        remaining_ms = deadline.remaining_ms
        if remaining_ms <= 0:
            raise ObservationStageError(
                "provider_wait", "outer_deadline_exhausted", timed_out=True,
            )
        wait_s = max(
            0.001,
            min(remaining_ms, float(SCRCPY_START_ATTEMPT_TIMEOUT_MS)) / 1000.0,
        )
        started = time.monotonic()
        try:
            await asyncio.wait_for(self._stream_provider.start(), timeout=wait_s)
        except Exception as exc:
            deadline.record("provider_wait", started, budget_s=wait_s, outcome="error")
            raise ObservationStageError("provider_wait", str(exc), budget_ms=wait_s * 1000) from exc
        deadline.record("provider_wait", started, budget_s=wait_s)
        while (sample_delay_s := end - time.monotonic()) > 0:
            sample_started = time.monotonic()
            sample_budget_s = deadline.remaining_seconds("temporal_sample")
            if sample_delay_s > sample_budget_s:
                raise ObservationStageError(
                    "temporal_sample", "outer_deadline_exhausted", timed_out=True,
                    budget_ms=sample_budget_s * 1000,
                )
            # Windows 3.12 may resume sleep before the coarse monotonic clock
            # reaches the requested boundary. Check the boundary again, using
            # the same clock and the original outer deadline.
            await asyncio.sleep(min(sample_budget_s, max(
                sample_delay_s, time.get_clock_info("monotonic").resolution,
            )))
            deadline.record(
                "temporal_sample", sample_started,
                budget_s=sample_budget_s,
            )
        return self._stream_provider.temporal(start, end, count)

    async def capture_deadline_tree(self, deadline: ObservationDeadline) -> dict[str, Any]:
        """Acquire the single ending UI tree used by ring-backed temporal evidence."""
        return await self._deadline_tree(deadline)

    def connect(self, serial: str | None = None) -> str:
        """Bind this driver to an explicit ADB serial (or the sole online device).

        Multi-device labs MUST pass ``serial``. Omitting it only succeeds when
        exactly one authorized device is online.
        """
        if serial:
            online = adb.list_device_serials()
            if serial not in online:
                raise AdbError(f"device serial not online: {serial}")
            self._serial = serial
            self._collector.serial = serial
            return self._serial
        if self._serial:
            return self._serial
        self._serial = adb.sole_online_device()
        self._collector.serial = self._serial
        return self._serial

    @property
    def serial(self) -> str | None:
        return self._serial

    async def current_device_date(self) -> str:
        """Return the Android device-local date as ISO ``YYYY-MM-DD``."""

        raw = await adb.shell_async(
            self._serial, ["date", "+%Y-%m-%d"], timeout=3.0,
        )
        value = raw.decode("utf-8", "replace").strip()
        datetime.date.fromisoformat(value)
        return value

    async def health(self) -> dict[str, Any]:
        """Report driver reachability and ADB device presence."""
        serial = self._serial
        try:
            if serial is None:
                serial = await adb.sole_online_device_async()
                self._serial = serial
            else:
                online = await adb.list_device_serials_async()
                if serial not in online:
                    return {
                        "ok": False,
                        "platform": "android",
                        "serial": serial,
                        "error": f"device serial not online: {serial}",
                    }
        except AdbError as exc:
            return {"ok": False, "platform": "android", "error": str(exc)}
        collector = (
            await self._collector.warm(
                timeout=self._collector_timeout_ms / 1000
            )
            if self._collector_enabled else {"ready": False, "reason": "disabled"}
        )
        return {
            "ok": True,
            "platform": "android",
            "serial": serial,
            "backend": "adb-direct",
            "observation_provider": self.observation_diagnostics(),
            "accessibility_collector": collector,
            "accessibility_channel": self._collector.diagnostics(),
        }

    async def initialize_environment(self) -> dict[str, Any]:
        """Idempotently provision this explicit device for ClickClick."""
        if not self._serial:
            return {"serial": "", "status": "failed", "steps": {
                "adb": {"status": "failed", "reason": "driver has no bound serial"}
            }}
        from driver.environment import initialize_android_device, resolve_collector_apk_path

        options = self._provisioning
        return await initialize_android_device(
            self._serial,
            collector_authority=self._collector_authority,
            collector_apk_path=resolve_collector_apk_path(options.collector_apk_path),
            ime_id=self._ime_id if self._ime_auto_setup else "",
            ime_apk_path=options.ime_apk_path,
            stay_awake_while_plugged=options.stay_awake_while_plugged,
        )

    async def reconcile_environment(self) -> dict[str, Any]:
        """Automatic Collector install/upgrade, honoring the disable switch."""
        if not self._collector_enabled:
            return {"status": "disabled", "reason": "collector_disabled"}
        return await self.initialize_environment()

    async def readiness(self) -> dict[str, Any]:
        """Run independent bounded task-start probes concurrently."""
        readiness_started = time.monotonic()

        async def collector_probe() -> dict[str, Any]:
            if not self._collector_enabled:
                return {"ready": False, "reason": "disabled"}
            try:
                return await self._collector.warm(
                    timeout=self._collector_timeout_ms / 1000
                )
            except Exception as exc:  # noqa: BLE001
                return {"ready": False, "reason": str(exc)}

        async def ime_probe() -> dict[str, Any]:
            if not self._ime_auto_setup:
                return {"ready": False, "reason": "disabled"}
            try:
                previous_ime = await adb.default_ime_async(self._serial)
                ime_ready = await self._ensure_adbkeyboard()
                return {
                    "ready": ime_ready,
                    "ime_id": self._ime_id,
                    "previous_default": previous_ime,
                    "switched": bool(ime_ready and previous_ime != self._ime_id),
                    "reason": "" if ime_ready else "IME selection could not be verified",
                }
            except AdbError as exc:
                return {
                    "ready": False,
                    "ime_id": self._ime_id,
                    "reason": str(exc),
                }

        async def interactive_probe() -> bool | None:
            try:
                return await adb.screen_interactive_async(self._serial)
            except AdbError:
                return None

        async def timed(coro: Any) -> tuple[Any, float]:
            started = time.monotonic()
            result = await coro
            return result, round((time.monotonic() - started) * 1000.0, 3)

        collector_result, ime_result, interactive_result = await asyncio.gather(
            timed(collector_probe()),
            timed(ime_probe()),
            timed(interactive_probe()),
        )
        collector, collector_ms = collector_result
        test_ime, ime_ms = ime_result
        interactive, interactive_ms = interactive_result
        ready = bool(collector.get("ready")) and (
            not self._ime_auto_setup or bool(test_ime.get("ready"))
        )
        return {
            "status": "ready" if ready else "degraded",
            "collector": collector,
            "collector_channel": self._collector.diagnostics(),
            "test_ime": test_ime,
            "screen_interactive": interactive,
            "fallback": None if collector.get("ready") else "uiautomator_dump",
            "timing_ms": {
                "collector": collector_ms,
                "test_ime": ime_ms,
                "screen_interactive": interactive_ms,
                "total": round((time.monotonic() - readiness_started) * 1000.0, 3),
            },
        }

    async def get_ui_state(self) -> CanonicalUI:
        """Fetch a current multi-window tree, with fresh dump fallback."""
        app_id = ""
        try:
            tree = await self._collector_tree(
                timeout_s=self._collector_timeout_ms / 1000.0
            )
        except Exception as exc:  # noqa: BLE001
            xml = await adb.uiautomator_dump_async(self._serial)
            tree = mark_dump_fallback(parse_uiautomator_xml(xml), reason=str(exc))
            app_id = extract_package(xml) or ""
        activity = await adb.current_activity_async(self._serial)
        ui = normalize_a11y_tree(tree, app_id=app_id, activity=activity)
        self._last_ui = ui
        return ui

    async def screenshot(self) -> bytes:
        """Capture current pixels from scrcpy, with automatic ADB fallback."""
        frame = await self._current_stream_frame()
        if frame is not None:
            return frame.data
        shot = await adb.screencap_async(self._serial)
        self._remember_frame_geometry(self._geometry_metadata(shot))
        return shot

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        """Capture one causally ordered tree/pixel pair under one deadline."""
        tree, shot, _metadata = await self.capture_deadline_frame(
            ObservationDeadline("current", self._capture_budget_ms)
        )
        return tree, shot

    async def current_activity(self) -> str:
        """Return the foreground activity component (e.g. ``.MainActivity``)."""
        return await adb.current_activity_async(self._serial)

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return exact resumed-App facts independently from accessibility."""
        # The enclosing observation transaction supplies its remaining time.
        # The subprocess is cancellable, so transaction cancellation also
        # terminates and reaps ADB instead of leaving a worker-thread orphan.
        return await adb.current_activity_identity_async(
            self._serial,
            timeout=10.0 if timeout_s is None else timeout_s,
        )

    async def search_installed_apps(self, query: str, *, limit: int = 12) -> list[dict[str, str]]:
        if self._resolver is None:
            return []
        return await self._resolver.search_installed_apps(query, self._serial, limit=limit)

    async def resolve_installed_app(self, app: str) -> dict[str, Any]:
        """Return a typed deterministic resolution result for Harness preflight."""
        if self._resolver is None:
            from shared.app_resolver import normalize_app_query
            from shared.schemas import AppResolutionResult, AppResolutionStatus

            return AppResolutionResult(
                requested_name=app,
                normalized_query=normalize_app_query(app),
                status=AppResolutionStatus.MISS,
                resolver_generation="unavailable",
            ).model_dump(mode="json")
        return (await self._resolver.resolve_with_status(app, self._serial)).model_dump(mode="json")

    async def skill_profile_ids(self) -> list[str]:
        """Use the same verified device identity as the app alias resolver."""
        if self._resolver is None:
            return []
        return await self._resolver.selected_profile_ids(self._serial)

    async def validate_installed_app(self, package: str) -> bool:
        if self._resolver is None:
            return package in set(await adb.list_packages_async(self._serial))
        return await self._resolver.validate_installed(package, self._serial)

    async def record_installed_app_selection(self, display_name: str, package: str) -> bool:
        if self._resolver is None:
            return False
        return await self._resolver.record_explicit_selection(display_name, package, self._serial)

    async def get_input_diagnostics(self, *, timeout_s: float = 0.35) -> dict[str, Any]:
        """Optional bounded IME signal. Callers soft-degrade all failures."""
        return await adb.input_method_diagnostics_async(self._serial, timeout=timeout_s)

    async def act(self, action: Action) -> ActionResult:
        """Dispatch one action; effect confirmation belongs to the transaction."""
        try:
            boundary = None
            boundary_snapshotted = False
            boundary_getter = getattr(self._stream_provider, "frame_boundary", None)

            def before_dispatch() -> None:
                nonlocal boundary, boundary_snapshotted
                if not boundary_snapshotted and callable(boundary_getter):
                    boundary = boundary_getter()
                    boundary_snapshotted = True

            result = await self._act(action, before_dispatch=before_dispatch)
            if result.success and action.type != "sleep":
                marker = getattr(self._stream_provider, "mark_action_complete", None)
                if callable(marker):
                    marker(boundary)
            return result
        except AdbError as exc:
            return ActionResult(success=False, message=str(exc))

    async def wake_and_unlock(self) -> None:
        """Wake a confirmed sleeping screen and dismiss keyguard (best-effort).

        The operation is intentionally inert when the display is already
        interactive or its state cannot be determined.  Unlocking uses the
        window-manager command instead of a swipe so it cannot scroll or open
        something in the foreground app.
        """
        # A live task lease already wakes/brightens the display atomically.
        # Avoid injecting KEYCODE_WAKEUP, whose check-then-send race can turn
        # off or otherwise disturb a display brightened by the environment.
        if self._task_power_lease.active:
            return
        try:
            interactive = await adb.screen_interactive_async(self._serial)
        except AdbError:
            interactive = None
        if interactive is not False:
            return

        try:
            await adb.input_keyevent_async(self._serial, 224)  # KEYCODE_WAKEUP
        except AdbError:
            pass
        # Give the display controller a moment to light up.
        await asyncio.sleep(0.4)
        try:
            await adb.dismiss_keyguard_async(self._serial)
        except AdbError:
            pass

    async def begin_task_session(self, task_id: str) -> dict[str, Any]:
        """Acquire the renewable display lease for one task execution."""
        if not self._collector_enabled:
            return {"status": "disabled", "reason": "collector_disabled"}
        try:
            return await self._task_power_lease.begin(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "reason": str(exc)[:200]}

    async def end_task_session(self, _task_id: str = "") -> dict[str, Any]:
        """Release immediately; TTL remains the fallback after transport loss."""
        return await self._task_power_lease.end()

    async def _act(
        self,
        action: Action,
        *,
        before_dispatch: Callable[[], None] = lambda: None,
    ) -> ActionResult:
        atype = action.type
        if atype == "tap" and action._node_handle:
            before_dispatch()
            try:
                response = await self._collector.click_node(action._node_handle)
            except asyncio.CancelledError:
                raise
            except Exception:  # A request may already have reached Android.
                return ActionResult(
                    success=False, message="node_click outcome unknown; observe before any further action",
                    detail={"node_click_status": "outcome_unknown", "device_dispatch": "unknown"},
                )
            status = response["node_click_status"]
            return ActionResult(
                success=status == "performed", message=f"node_click:{status}",
                detail={
                    "node_click_status": status,
                    "native_action_performed": response.get("performed"),
                    "device_dispatch": "attempted" if response["action_attempted"] else "not_dispatched",
                    "node_click_elapsed_ms": response.get("elapsed_ms"),
                    "node_click_bounds": response.get("bounds"),
                },
            )
        if atype == "tap":
            x, y = self._resolve_xy(action)
            if x is None or y is None:
                return ActionResult(success=False, message=f"unresolved tap target index={action.index}")
            before_dispatch()
            await adb.input_tap_async(self._serial, x, y)
            return ActionResult(success=True, message="tap", detail={"x": x, "y": y})
        if atype == "tap_xy":
            if action.x is None or action.y is None:
                return ActionResult(success=False, message="tap_xy missing x/y")
            before_dispatch()
            await adb.input_tap_async(self._serial, action.x, action.y)
            return ActionResult(success=True, message="tap_xy", detail={"x": action.x, "y": action.y})
        if atype == "double_tap":
            if action.x is None or action.y is None:
                return ActionResult(success=False, message="double_tap missing x/y")
            before_dispatch()
            await adb.input_double_tap_async(self._serial, action.x, action.y)
            return ActionResult(success=True, message="double_tap dispatched; zoom depends on app", detail={"x": action.x, "y": action.y})
        if atype == "long_press":
            x, y = self._resolve_xy(action)
            if x is None or y is None:
                return ActionResult(success=False, message=f"unresolved long_press target index={action.index}")
            dur = action.duration_ms if action.duration_ms is not None else 1000
            # `adb shell input swipe` with the same start/end point + duration emulates a long press.
            before_dispatch()
            await adb.input_swipe_async(self._serial, x, y, x, y, dur)
            return ActionResult(success=True, message="long_press", detail={"x": x, "y": y, "duration_ms": dur})
        if atype == "scroll":
            if not action.direction:
                return ActionResult(success=False, message="scroll missing direction")
            coordinates = self._scroll_coords(action.direction)
            if coordinates is None:
                return ActionResult(
                    success=False,
                    message="scroll viewport geometry unavailable; acquire a fresh observation",
                )
            x1, y1, x2, y2 = coordinates
            dur = action.duration_ms if action.duration_ms is not None else 300
            before_dispatch()
            await adb.input_swipe_async(self._serial, x1, y1, x2, y2, dur)
            return ActionResult(success=True, message=f"scroll:{action.direction}",
                                  detail={"from": (x1, y1), "to": (x2, y2)})
        if atype == "swipe":
            if None in (action.x, action.y, action.x2, action.y2):
                return ActionResult(success=False, message="swipe missing coordinates")
            dur = action.duration_ms if action.duration_ms is not None else 300
            before_dispatch()
            await adb.input_swipe_async(self._serial, action.x, action.y, action.x2, action.y2, dur)
            return ActionResult(success=True, message="swipe")
        if atype == "drag":
            if None in (action.x, action.y, action.x2, action.y2):
                return ActionResult(success=False, message="drag missing coordinates")
            dur = action.duration_ms if action.duration_ms is not None else 500
            before_dispatch()
            await adb.input_swipe_async(self._serial, action.x, action.y, action.x2, action.y2, dur)
            return ActionResult(success=True, message="drag",
                                  detail={"from": (action.x, action.y), "to": (action.x2, action.y2), "duration_ms": dur})
        if atype == "type":
            if not action.text:
                return ActionResult(success=False, message="type missing text")
            text = action.text
            if _is_ascii_text(text):
                before_dispatch()
                await adb.input_text_async(self._serial, text)
                return ActionResult(
                    success=True, message="type",
                    detail={"channel": "input_text", "text_chars": len(text)},
                )
            # Non-ASCII: route through ADBKeyBoard's broadcast intent so we
            # avoid `InputShellCommand.sendText:408` NPE. The IME must already
            # be enabled+selected; on failure we surface a typed error
            # (`detail.channel == "ime"`) instead of silently falling back to
            # the broken `adb input text` path.
            if not await self._ensure_adbkeyboard():
                return ActionResult(
                    success=False, message="type: adbkeyboard_unavailable",
                    detail={
                        "channel": "ime",
                        "ime_id": self._ime_id,
                        "text_chars": len(text),
                        "reason": "ime_setup_failed",
                    },
                )
            try:
                before_dispatch()
                await adb.input_text_via_broadcast_async(self._serial, text)
                return ActionResult(
                    success=True, message="type",
                    detail={"channel": "ime", "text_chars": len(text)},
                )
            except AdbError as exc:
                return ActionResult(
                    success=False, message="type: ime_broadcast_failed",
                    detail={"channel": "ime", "error": str(exc),
                            "text_chars": len(text)},
                )
        if atype == "replace_text":
            if action.index is not None:
                return ActionResult(success=False, message="targeted_input_requires_harness",
                                    detail={"device_dispatch": "not_dispatched"})
            if not action.text:
                return ActionResult(success=False, message="replace_text missing text")
            text = action.text
            if not await self._ensure_adbkeyboard():
                return ActionResult(
                    success=False,
                    message="replace_text: adbkeyboard_unavailable",
                    detail={
                        "channel": "ime",
                        "operation": "replace_text",
                        "stage": "prepare_ime",
                        "ime_id": self._ime_id,
                        "text_chars": len(text),
                        "reason": "ime_setup_failed",
                    },
                )
            before_dispatch()
            try:
                await adb.clear_text_via_broadcast_async(self._serial)
            except AdbError as exc:
                return ActionResult(
                    success=False,
                    message="replace_text: ime_clear_failed",
                    detail={
                        "channel": "ime",
                        "operation": "replace_text",
                        "stage": "clear",
                        "error": str(exc),
                        "text_chars": len(text),
                    },
                )
            try:
                await adb.input_text_via_broadcast_async(self._serial, text)
            except AdbError as exc:
                return ActionResult(
                    success=False,
                    message="replace_text: ime_broadcast_failed",
                    detail={
                        "channel": "ime",
                        "operation": "replace_text",
                        "stage": "input",
                        "error": str(exc),
                        "text_chars": len(text),
                    },
                )
            return ActionResult(
                success=True,
                message="replace_text",
                detail={
                    "channel": "ime",
                    "operation": "replace_text",
                    "stages": ["clear", "input"],
                    "text_chars": len(text),
                },
            )
        if atype in ("key", "back", "home"):
            key = action.key or atype
            keycode = _KEYCODES.get(key, key)
            # Long-press intent: duration_ms >= 500ms triggers `--longpress`
            # keyevent (e.g., long-press delete to clear text). A normal key
            # event is not a semantic substitute and must not be dispatched
            # after an unsupported or uncertain long-press result.
            if action.duration_ms is not None and action.duration_ms >= 500:
                try:
                    before_dispatch()
                    await adb.input_keyevent_longpress_async(self._serial, keycode)
                    return ActionResult(
                        success=True, message=f"key:{key}:longpress",
                        detail={"duration_ms": action.duration_ms},
                    )
                except Exception as exc:  # noqa: BLE001
                    return ActionResult(
                        success=False,
                        message=f"key:{key}:longpress_failed",
                        detail={
                            "longpress_unsupported": True,
                            "dispatch_uncertain": True,
                            "error": str(exc),
                        },
                    )
            before_dispatch()
            await adb.input_keyevent_async(self._serial, keycode)
            return ActionResult(success=True, message=f"key:{key}")
        if atype == "launch":
            if not action.app:
                return ActionResult(success=False, message="launch missing app")
            pkg = await self._resolve_app_name(action.app)
            if pkg is None:
                return ActionResult(success=False, message=f"launch: unresolved app name '{action.app}'")
            before_dispatch()
            await adb.am_start_async(self._serial, pkg)
            return ActionResult(
                success=True,
                message=f"launch:{pkg}",
                detail={"resolved_package": pkg},
            )
        if atype == "sleep":
            await adb.wait_ms_async(action.duration_ms if action.duration_ms is not None else 200)
            return ActionResult(success=True, message="sleep")
        return ActionResult(success=False, message=f"unknown action type: {atype}")

    def _resolve_xy(self, action: Action) -> tuple[float | None, float | None]:
        """Resolve a tap/long_press action to screen coordinates by index or explicit x/y."""
        if action.x is not None and action.y is not None:
            return action.x, action.y
        if action.index is None:
            return None, None
        ui = self._last_ui
        if ui is None:
            return None, None
        el = next((e for e in ui.elements if e.index == action.index), None)
        if not el or len(el.bounds) != 4:
            return None, None
        x1, y1, x2, y2 = el.bounds
        return (x1 + x2) / 2, (y1 + y2) / 2

    def _scroll_viewport(self) -> tuple[int, int] | None:
        geometry = self._last_frame_geometry
        if geometry is not None:
            width = int(getattr(geometry, "device_width", 0) or 0)
            height = int(getattr(geometry, "device_height", 0) or 0)
            if width > 0 and height > 0:
                return width, height
            if isinstance(geometry, (list, tuple)) and len(geometry) == 2:
                width, height = int(geometry[0]), int(geometry[1])
                if width > 0 and height > 0:
                    return width, height

        candidates: list[tuple[int, int, int]] = []
        if self._last_ui is not None:
            for element in self._last_ui.semantic_tree:
                if element.depth > 1 and not element.window_wrapper:
                    continue
                if len(element.bounds) != 4:
                    continue
                x1, y1, x2, y2 = (int(value) for value in element.bounds)
                width, height = x2 - x1, y2 - y1
                if width > 0 and height > 0:
                    candidates.append((width * height, width, height))
        if not candidates:
            return None
        _area, width, height = max(candidates)
        return width, height

    def _scroll_coords(
        self, direction: str,
    ) -> tuple[float, float, float, float] | None:
        """Compute screen-relative swipe endpoints for a discrete scroll.

        `direction` is content/viewport destination (down reveals content
        below), translated to the opposite finger gesture. Exact capture
        geometry is authoritative. A positive application/window root is a
        fallback; unknown geometry never produces a blind swipe.
        """
        viewport = self._scroll_viewport()
        if viewport is None:
            return None
        w, h = viewport
        cx, cy = w / 2, h / 2
        delta = min(w, h) * 0.4  # 40% of the short side
        if direction == "up":
            # Reveal content above → finger down.
            return cx, cy - delta / 2, cx, cy + delta / 2
        if direction == "down":
            # Reveal content below → finger up.
            return cx, cy + delta / 2, cx, cy - delta / 2
        if direction == "left":
            # Reveal content to the left → finger right.
            return cx - delta / 2, cy, cx + delta / 2, cy
        # right: reveal content to the right → finger left.
        return cx + delta / 2, cy, cx - delta / 2, cy

    async def _resolve_app_name(self, app: str) -> str | None:
        """Resolve a launch target to a package name via the two-stage resolver.

        Delegates to the injected `NameResolver` for local curated aliases and
        learned-cache hits. Misses are handled visibly by the Executor's
        `search_installed_apps` tool. When no resolver is wired (fixture/offline),
        only package-name passthrough is honored and
        display names return None — the caller surfaces a failure `ActionResult`
        and the Planner can replan.
        """
        # Package-name passthrough remains deterministic, but final launch
        # membership is validated when an installed-package provider exists.
        if "." in app and len(app.split(".")) >= 2:
            if self._resolver is None:
                return app
            validate = getattr(self._resolver, "validate_installed", None)
            if not callable(validate):
                return app
            return app if await validate(app, self._serial) else None
        resolver = self._resolver
        if resolver is None:
            return None
        try:
            resolved = await resolver.resolve(app, self._serial)
            if resolved is None:
                return None
            validate = getattr(resolver, "validate_installed", None)
            if not callable(validate):
                return resolved
            return resolved if await validate(resolved, self._serial) else None
        except Exception as exc:  # noqa: BLE001
            return None

    async def _ensure_adbkeyboard(self) -> bool:
        """Ensure ADBKeyBoard is the currently selected foreground IME.

        Deployment provisioning enables the configured IME without selecting
        it; per-task readiness selects it before actions begin. Enabled
        membership is not readiness for broadcast input: Android silently
        drops the broadcast unless this IME is selected. Re-check selection
        before every non-ASCII input so an external IME switch cannot leave a
        stale in-process success cache.
        """
        if not self._ime_auto_setup:
            return False
        try:
            if await adb.default_ime_async(self._serial) == self._ime_id:
                return True
        except AdbError as exc:
            print(f"[driver] adbkeyboard setup: default IME query failed: {exc}")
            return False
        try:
            await adb.ime_set_async(self._serial, self._ime_id)
            selected = await adb.default_ime_async(self._serial)
        except AdbError as exc:
            print(
                f"[driver] adbkeyboard setup: ime enable/set failed for "
                f"{self._ime_id!r}: {exc}. Install ADBKeyBoard APK first; "
                "see https://github.com/senzhk/ADBKeyBoard"
            )
            return False
        if selected != self._ime_id:
            print(
                f"[driver] adbkeyboard setup: platform kept {selected!r} "
                f"instead of {self._ime_id!r}"
            )
            return False
        # `ime set` can update secure settings before the newly selected IME
        # has an active input connection.  Broadcasting in that gap exits 0
        # but silently drops the text, so wait only when a switch occurred.
        if _DEFAULT_IME_SWITCH_SETTLE_MS > 0:
            await asyncio.sleep(_DEFAULT_IME_SWITCH_SETTLE_MS / 1000.0)
        return True


def _is_ascii_text(text: str) -> bool:
    """True iff `text` is pure 7-bit ASCII and free of NUL / control characters.

    The bound 0x20–0x7E is what `adb shell input text` reliably feeds into
    `InputShellCommand.sendText` without NPE. CJK (0x4E00–0x9FFF range and
    beyond), emoji, and control bytes all trip the same NPE; the broadcast
    path through ADBKeyBoard handles arbitrary UTF-16 safely.
    """
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return False
    # Even ASCII control chars (\\x00, \\x01, …) crash InputShellCommand.
    return not any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)
