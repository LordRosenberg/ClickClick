"""Thin host client and strict DTOs for the Android accessibility collector."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import struct
import time
from dataclasses import dataclass, field, replace
from typing import Any

from driver import adb
from driver.adb import AdbError


CAPTURE_META_KEY = "_capture"
COLLECTOR_SCHEMA_VERSION = 1
DEFAULT_AUTHORITY = "ai.clickclick.collector"
SNAPSHOT_PROTOCOL_VERSION = 1
SNAPSHOT_MAX_FRAME_BYTES = 8 * 1024 * 1024
SNAPSHOT_REQUEST_TIMEOUT_S = 2.5
SNAPSHOT_CONNECT_TIMEOUT_S = 1.0
# The Android collector keeps idle clients for 90 s. Closing at 60 s avoids a
# half-closed 30–60 s window while preserving persistent reuse across role calls.
SNAPSHOT_IDLE_CLOSE_S = 60.0
_TRANSPORT_FAILURE_CLASSES = frozenset({
    "connection_reset",
    "header_eof",
    "malformed_frame",
    "malformed_json",
    "malformed_response",
    "malformed_snapshot",
    "not_connected",
    "payload_eof",
    "protocol_mismatch",
    "remote_error",
    "request_id_mismatch",
    "timeout",
    "transport_io",
    "transport_unavailable",
})


@dataclass(frozen=True)
class AccessibilityWindow:
    window_id: int
    type: int
    layer: int
    bounds: list[int]
    active: bool
    focused: bool
    display_id: int
    root: dict[str, Any] | None
    complete: bool = True
    reason: str = ""
    traversal_elapsed_ms: float = 0.0


@dataclass(frozen=True)
class AccessibilitySnapshot:
    generation: int
    captured_monotonic_ms: float
    complete: bool
    reasons: list[str]
    windows: list[AccessibilityWindow] = field(default_factory=list)
    schema_version: int = COLLECTOR_SCHEMA_VERSION
    provider: str = "accessibility_collector"
    transport_elapsed_ms: float = 0.0
    serialization_elapsed_ms: float = 0.0
    decode_elapsed_ms: float = 0.0
    connection_generation: int = 0
    collector_exchanges: list[dict[str, Any]] = field(default_factory=list)
    fallback_edges: list[dict[str, str]] = field(default_factory=list)

    def to_raw_tree(self, *, elapsed_ms: float = 0.0) -> dict[str, Any]:
        """Return the one hierarchy shape consumed by the existing normalizer."""
        children: list[dict[str, Any]] = []
        for window in sorted(self.windows, key=lambda item: item.layer, reverse=True):
            wrapper: dict[str, Any] = {
                "class": "android.view.accessibility.AccessibilityWindow",
                "role": "Window",
                "window_wrapper": True,
                "window_id": window.window_id,
                "window_type": window.type,
                "window_layer": window.layer,
                "display_id": window.display_id,
                "active": window.active,
                "focused": window.focused,
                "bounds": window.bounds,
                "clickable": False,
                "children": [window.root] if isinstance(window.root, dict) else [],
            }
            if not window.complete:
                wrapper["capture_reason"] = window.reason or "window_incomplete"
            children.append(wrapper)
        return {
            "class": "android.view.accessibility.WindowSet",
            "role": "WindowSet",
            "window_wrapper": True,
            "children": children,
            CAPTURE_META_KEY: {
                "provider": self.provider,
                "complete": self.complete,
                "reasons": list(self.reasons),
                "generation": self.generation,
                "captured_monotonic_ms": self.captured_monotonic_ms,
                "window_count": len(self.windows),
                "collector_elapsed_ms": round(max(0.0, elapsed_ms), 3),
                "collector_transport_ms": round(max(0.0, self.transport_elapsed_ms), 3),
                "collector_serialization_ms": round(
                    max(0.0, self.serialization_elapsed_ms), 3
                ),
                "collector_decode_ms": round(max(0.0, self.decode_elapsed_ms), 3),
                "collector_connection_generation": self.connection_generation,
                "collector_exchanges": [dict(item) for item in self.collector_exchanges],
                "fallback_edges": list(self.fallback_edges),
                "window_traversal_ms": [
                    round(max(0.0, window.traversal_elapsed_ms), 3)
                    for window in self.windows
                ],
            },
        }


def decode_snapshot(payload: str | bytes | dict[str, Any]) -> AccessibilitySnapshot:
    """Strictly decode a collector response; malformed data never becomes a tree."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "strict")
    if isinstance(payload, str):
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AdbError(f"collector returned invalid JSON: {exc}") from exc
    else:
        raw = payload
    if not isinstance(raw, dict):
        raise AdbError("collector snapshot must be an object")
    version = _required_int(raw, "schema_version")
    if version != COLLECTOR_SCHEMA_VERSION:
        raise AdbError(f"unsupported collector schema_version={version}")
    raw_windows = raw.get("windows")
    if not isinstance(raw_windows, list):
        raise AdbError("collector snapshot windows must be a list")
    windows: list[AccessibilityWindow] = []
    window_ids: set[int] = set()
    for index, item in enumerate(raw_windows):
        if not isinstance(item, dict):
            raise AdbError(f"collector window[{index}] must be an object")
        bounds = item.get("bounds")
        if (
            not isinstance(bounds, list)
            or len(bounds) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
        ):
            raise AdbError(f"collector window[{index}].bounds must be four integers")
        root = item.get("root")
        if root is not None and not isinstance(root, dict):
            raise AdbError(f"collector window[{index}].root must be object or null")
        window_id = _required_int(item, "window_id")
        if window_id < 0:
            raise AdbError(f"collector window[{index}].window_id must be non-negative")
        if window_id in window_ids:
            raise AdbError(f"collector snapshot has duplicate window_id={window_id}")
        window_ids.add(window_id)
        complete = (
            _required_bool(item, "complete")
            if "complete" in item else root is not None
        )
        windows.append(AccessibilityWindow(
            window_id=window_id,
            type=_required_int(item, "type"),
            layer=_required_int(item, "layer"),
            bounds=list(bounds),
            active=_required_bool(item, "active"),
            focused=_required_bool(item, "focused"),
            display_id=_required_int(item, "display_id"),
            root=root,
            complete=complete,
            reason=_bounded_reason(item.get("reason")),
            traversal_elapsed_ms=_optional_number(item, "traversal_elapsed_ms"),
        ))
    reasons = raw.get("reasons", [])
    if not isinstance(reasons, list):
        raise AdbError("collector snapshot reasons must be a list")
    return AccessibilitySnapshot(
        schema_version=version,
        generation=_required_int(raw, "generation"),
        captured_monotonic_ms=_required_number(raw, "captured_monotonic_ms"),
        complete=_required_bool(raw, "complete"),
        reasons=[_bounded_reason(reason) for reason in reasons if _bounded_reason(reason)],
        windows=windows,
        serialization_elapsed_ms=_optional_number(raw, "serialization_elapsed_ms"),
    )


def parse_content_value(output: bytes | str, column: str) -> str:
    """Extract one ContentProvider cursor value from ``adb shell content``."""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else str(output)
    marker = f"{column}="
    position = text.find(marker)
    if position < 0:
        raise AdbError(f"collector response missing {column}")
    value = text[position + len(marker):].strip()
    if not value or value == "NULL":
        raise AdbError(f"collector response has empty {column}")
    return value


class AccessibilityCollectorClient:
    """One persistent collector client with an explicit owned-session close."""

    def __init__(self, serial: str | None, *, authority: str = DEFAULT_AUTHORITY) -> None:
        self.serial = serial
        self.authority = authority
        self._channel = CHANNEL_REGISTRY.get(serial, authority) if serial else None

    async def fetch_primary(self, *, timeout: float) -> AccessibilitySnapshot:
        """Run one complete cancellable persistent-channel attempt."""
        if not self.serial:
            raise AdbError("persistent channel unavailable")
        budget_s = float(timeout)
        if budget_s <= 0:
            raise AccessibilityTransportError(
                "persistent channel deadline exhausted before request",
                failure_class="deadline_exhausted",
                stage="admission",
            )
        self._channel = self._channel or CHANNEL_REGISTRY.get(
            self.serial, self.authority
        )
        return await self._channel.snapshot(timeout=budget_s)

    async def warm(self, *, timeout: float = SNAPSHOT_REQUEST_TIMEOUT_S) -> dict[str, Any]:
        if not self.serial:
            return {"ready": False, "reason": "serial is not bound"}
        self._channel = self._channel or CHANNEL_REGISTRY.get(
            self.serial, self.authority
        )
        started = time.monotonic()
        try:
            snapshot = await self.fetch_primary(timeout=timeout)
            return {
                "ready": snapshot.complete,
                "provider": snapshot.provider,
                "connection_generation": snapshot.connection_generation,
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "reason": str(exc)[:200]}

    async def close(self) -> None:
        """Close this client's current persistent channel session."""
        channel, self._channel = self._channel, None
        if channel is not None:
            await channel.close()

    def diagnostics(self) -> dict[str, Any]:
        return self._channel.diagnostics() if self._channel else {
            "ready": False, "status": "unbound"
        }

    async def health(self, *, timeout: float = 0.5) -> dict[str, Any]:
        try:
            output = await adb.content_query_async(
                self.serial, f"content://{self.authority}/health", timeout=timeout
            )
            encoded = parse_content_value(output, "health")
            payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
            return payload if isinstance(payload, dict) else {"ready": False}
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "reason": str(exc)[:200]}


@dataclass(frozen=True)
class AccessibilityChannelBootstrap:
    socket_name: str
    token: str
    protocol_version: int
    bootstrap_snapshot_generation: int


class AccessibilityTransportError(AdbError):
    """Typed failure for one persistent collector exchange lifecycle."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: str,
        stage: str,
        exchanges: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.stage = stage
        self.exchanges = [dict(item) for item in exchanges or []]


class AccessibilitySnapshotChannel:
    """One serial-scoped persistent collector connection and visible attempt."""

    def __init__(self, serial: str, authority: str) -> None:
        self.serial = serial
        self.authority = authority
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._port: int | None = None
        self._bootstrap: AccessibilityChannelBootstrap | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0
        self.connection_generation = 0
        self.last_transport_ms = 0.0
        self.last_error = ""
        self._last_exchanges: list[dict[str, Any]] = []
        self._idle_task: asyncio.Task[None] | None = None
        self._socket_cleanup_task: asyncio.Task[Any] | None = None
        self._forward_cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_failed = False

    async def snapshot(
        self, *, timeout: float = SNAPSHOT_REQUEST_TIMEOUT_S,
    ) -> AccessibilitySnapshot:
        """Run one persistent exchange; outer capture owns any retry."""
        budget_s = max(0.0, float(timeout))
        deadline = time.monotonic() + budget_s
        async with self._lock_before_deadline(deadline):
            if budget_s <= 0:
                raise AccessibilityTransportError(
                    "persistent collector deadline exhausted before exchange",
                    failure_class="deadline_exhausted",
                    stage="admission",
                )

            started = time.monotonic()
            remaining_before_s = max(0.0, deadline - started)
            connection_before = self.connection_generation
            stage = "connect"
            try:
                if remaining_before_s <= 0:
                    raise AccessibilityTransportError(
                        "persistent collector deadline exhausted before connect",
                        failure_class="deadline_exhausted",
                        stage=stage,
                    )
                await asyncio.wait_for(
                    self._ensure_connected(timeout=remaining_before_s),
                    timeout=remaining_before_s,
                )
                stage = "snapshot_exchange"
                remaining_s = deadline - time.monotonic()
                if remaining_s <= 0:
                    raise AccessibilityTransportError(
                        "persistent collector deadline exhausted before snapshot",
                        failure_class="deadline_exhausted",
                        stage=stage,
                    )
                response = await asyncio.wait_for(
                    self._exchange("snapshot"), timeout=remaining_s,
                )
                stage = "snapshot_decode"
                decode_started = time.monotonic()
                try:
                    snapshot = decode_snapshot(response)
                except Exception as exc:  # noqa: BLE001
                    raise AccessibilityTransportError(
                        f"collector returned malformed snapshot: {exc}",
                        failure_class="malformed_snapshot",
                        stage=stage,
                    ) from exc
                finished = time.monotonic()
                elapsed_ms = (finished - started) * 1000.0
                self.last_transport_ms = elapsed_ms
                exchanges = [self._exchange_telemetry(
                    ordinal=1,
                    lifecycle="initial",
                    status="succeeded",
                    started=started,
                    finished=finished,
                    deadline=deadline,
                    remaining_before_s=remaining_before_s,
                    original_budget_s=budget_s,
                    connection_before=connection_before,
                    bootstrap_snapshot_generation=(
                        self._bootstrap.bootstrap_snapshot_generation
                        if self._bootstrap is not None else None
                    ),
                    failure=None,
                    close_outcome="not_needed",
                    socket_close_outcome="not_needed",
                    forward_remove_outcome="not_needed",
                    final_route="primary",
                )]
                self._last_exchanges = [dict(item) for item in exchanges]
                self._schedule_idle_close()
                return replace(
                    snapshot,
                    provider="accessibility_collector_channel",
                    transport_elapsed_ms=elapsed_ms,
                    decode_elapsed_ms=(
                        time.monotonic() - decode_started
                    ) * 1000.0,
                    connection_generation=self.connection_generation,
                    collector_exchanges=[dict(item) for item in exchanges],
                )
            except asyncio.CancelledError:
                failure = AccessibilityTransportError(
                    "persistent collector exchange cancelled",
                    failure_class="cancelled",
                    stage=stage,
                )
                cleanup = await self._close_locked_shielded(deadline=deadline)
                finished = time.monotonic()
                self._last_exchanges = [self._exchange_telemetry(
                    ordinal=1,
                    lifecycle="initial",
                    status="cancelled",
                    started=started,
                    finished=finished,
                    deadline=deadline,
                    remaining_before_s=remaining_before_s,
                    original_budget_s=budget_s,
                    connection_before=connection_before,
                    bootstrap_snapshot_generation=(
                        self._bootstrap.bootstrap_snapshot_generation
                        if self._bootstrap is not None else None
                    ),
                    failure=failure,
                    close_outcome=cleanup["outcome"],
                    socket_close_outcome=cleanup["socket_outcome"],
                    forward_remove_outcome=cleanup["forward_outcome"],
                    final_route="cancelled",
                )]
                self.last_error = str(failure)[:200]
                raise
            except Exception as exc:  # noqa: BLE001
                failure = _coerce_transport_error(exc, stage=stage)
                bootstrap_generation = (
                    self._bootstrap.bootstrap_snapshot_generation
                    if self._bootstrap is not None else None
                )
                cleanup = await self._close_locked_shielded(deadline=deadline)
                finished = time.monotonic()
                exchange = self._exchange_telemetry(
                    ordinal=1,
                    lifecycle="initial",
                    status="failed",
                    started=started,
                    finished=finished,
                    deadline=deadline,
                    remaining_before_s=remaining_before_s,
                    original_budget_s=budget_s,
                    connection_before=connection_before,
                    bootstrap_snapshot_generation=bootstrap_generation,
                    failure=failure,
                    close_outcome=cleanup["outcome"],
                    socket_close_outcome=cleanup["socket_outcome"],
                    forward_remove_outcome=cleanup["forward_outcome"],
                    final_route="persistent_failed",
                )
                self._last_exchanges = [dict(exchange)]
                self.last_error = str(failure)[:200]
                cleanup_failed = cleanup["outcome"] not in {
                    "closed", "not_owned",
                }
                raise AccessibilityTransportError(
                    f"persistent collector failed: {failure}",
                    failure_class=(
                        cleanup["outcome"]
                        if cleanup_failed else failure.failure_class
                    ),
                    stage="cleanup" if cleanup_failed else failure.stage,
                    exchanges=[exchange],
                ) from exc

    @asynccontextmanager
    async def _lock_before_deadline(self, deadline: float):
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise AccessibilityTransportError(
                "persistent collector deadline exhausted before channel lock",
                failure_class="deadline_exhausted",
                stage="channel_lock_admission",
            )
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=remaining_s)
        except asyncio.TimeoutError as exc:
            raise AccessibilityTransportError(
                "persistent collector deadline exhausted waiting for channel lock",
                failure_class="deadline_exhausted",
                stage="channel_lock_admission",
            ) from exc
        try:
            yield
        finally:
            self._lock.release()

    async def close(self) -> None:
        idle_task, self._idle_task = self._idle_task, None
        current = asyncio.current_task()
        if idle_task is not None and idle_task is not current:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
        async with self._lock:
            cleanup = await self._close_locked(
                deadline=time.monotonic() + SNAPSHOT_REQUEST_TIMEOUT_S
            )
            if cleanup["outcome"] == "cleanup_pending":
                pending = [
                    task for task in (
                        self._socket_cleanup_task,
                        self._forward_cleanup_task,
                    )
                    if task is not None and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.sleep(0)
                cleanup = await self._close_locked(deadline=time.monotonic())
            if cleanup["outcome"] not in {"closed", "not_owned"}:
                raise AccessibilityTransportError(
                    "collector transport cleanup did not settle",
                    failure_class=cleanup["outcome"],
                    stage="final_cleanup",
                    exchanges=[cleanup],
                )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": self._writer is not None and not self._writer.is_closing(),
            "status": "connected" if self._writer is not None else "disconnected",
            "protocol_version": (
                self._bootstrap.protocol_version if self._bootstrap else None
            ),
            "bootstrap_snapshot_generation": (
                self._bootstrap.bootstrap_snapshot_generation
                if self._bootstrap else None
            ),
            "connection_generation": self.connection_generation,
            "last_transport_ms": round(self.last_transport_ms, 3),
            "last_error": self.last_error,
            "cleanup_failed": self._cleanup_failed,
            "collector_exchanges": [dict(item) for item in self._last_exchanges],
        }

    def _exchange_telemetry(
        self,
        *,
        ordinal: int,
        lifecycle: str,
        status: str,
        started: float,
        finished: float,
        deadline: float,
        remaining_before_s: float,
        original_budget_s: float,
        connection_before: int,
        bootstrap_snapshot_generation: int | None,
        failure: AccessibilityTransportError | None,
        close_outcome: str,
        socket_close_outcome: str,
        forward_remove_outcome: str,
        final_route: str,
    ) -> dict[str, Any]:
        family = ""
        if failure is not None:
            family = (
                "lifecycle"
                if failure.failure_class in {"cancelled", "deadline_exhausted"}
                else "transport_break"
                if failure.failure_class in _TRANSPORT_FAILURE_CLASSES
                else "cleanup"
                if failure.failure_class.startswith("cleanup_")
                else "protocol"
            )
        return {
            "exchange_ordinal": ordinal,
            "lifecycle": lifecycle,
            "status": status,
            "failure_family": family,
            "failure_class": failure.failure_class if failure else "",
            "failure_stage": failure.stage if failure else "",
            "failure_reason": str(failure)[:200] if failure else "",
            "connection_generation_before": connection_before,
            "connection_generation": self.connection_generation,
            "bootstrap_snapshot_generation": bootstrap_snapshot_generation,
            "elapsed_ms": round(max(0.0, finished - started) * 1000.0, 3),
            "original_budget_ms": round(max(0.0, original_budget_s) * 1000.0, 3),
            "remaining_before_ms": round(max(0.0, remaining_before_s) * 1000.0, 3),
            "remaining_after_ms": round(max(0.0, deadline - finished) * 1000.0, 3),
            "close_outcome": close_outcome,
            "socket_close_outcome": socket_close_outcome,
            "forward_remove_outcome": forward_remove_outcome,
            "final_route": final_route,
        }

    def _schedule_idle_close(self) -> None:
        previous_task = self._idle_task
        if previous_task is not None:
            previous_task.cancel()

        async def close_after_idle() -> None:
            try:
                await asyncio.sleep(SNAPSHOT_IDLE_CLOSE_S)
                await self.close()
            except asyncio.CancelledError:
                return
            finally:
                current = asyncio.current_task()
                if current is self._idle_task:
                    self._idle_task = None

        self._idle_task = asyncio.create_task(
            close_after_idle(), name=f"a11y-channel-idle-{self.serial}"
        )

    async def _ensure_connected(self, *, timeout: float) -> None:
        self._refresh_cleanup_state()
        if self._writer is not None and not self._writer.is_closing():
            return
        if (
            self._port is not None
            or self._socket_cleanup_task is not None
            or self._forward_cleanup_task is not None
            or self._cleanup_failed
        ):
            raise AccessibilityTransportError(
                "collector channel still owns unsettled cleanup state",
                failure_class="cleanup_required",
                stage="connect_admission",
            )
        bootstrap = await self._load_bootstrap(timeout=timeout)
        self._bootstrap = bootstrap
        port = await adb.forward_localabstract_async(
            self.serial, bootstrap.socket_name
        )
        self._port = port
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=SNAPSHOT_CONNECT_TIMEOUT_S,
        )
        self._reader = reader
        self._writer = writer
        self.connection_generation += 1
        response = await asyncio.wait_for(
            self._exchange("health"), timeout=SNAPSHOT_CONNECT_TIMEOUT_S
        )
        if not bool(response.get("ready")):
            raise AdbError("collector channel health handshake failed")

    async def _load_bootstrap(self, *, timeout: float) -> AccessibilityChannelBootstrap:
        output = await adb.content_query_async(
            self.serial,
            f"content://{self.authority}/health",
            timeout=float(timeout),
        )
        encoded = parse_content_value(output, "health")
        try:
            raw = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise AdbError(f"invalid collector bootstrap: {exc}") from exc
        socket_name = str(raw.get("socket_name") or "")
        token = str(raw.get("token") or "")
        version = int(raw.get("protocol_version") or 0)
        if not socket_name or not token or version != SNAPSHOT_PROTOCOL_VERSION:
            raise AdbError("collector bootstrap is missing compatible channel metadata")
        return AccessibilityChannelBootstrap(
            socket_name=socket_name,
            token=token,
            protocol_version=version,
            bootstrap_snapshot_generation=int(raw.get("generation") or 0),
        )

    async def _exchange(self, operation: str) -> dict[str, Any]:
        reader, writer, bootstrap = self._reader, self._writer, self._bootstrap
        if reader is None or writer is None or bootstrap is None:
            raise AccessibilityTransportError(
                "collector channel is not connected",
                failure_class="not_connected",
                stage="request_admission",
            )
        self._request_id += 1
        request_id = self._request_id
        payload = json.dumps({
            "version": SNAPSHOT_PROTOCOL_VERSION,
            "request_id": request_id,
            "operation": operation,
            "token": bootstrap.token,
        }, separators=(",", ":")).encode("utf-8")
        if not 0 < len(payload) <= SNAPSHOT_MAX_FRAME_BYTES:
            raise AccessibilityTransportError(
                "collector request exceeds frame bound",
                failure_class="malformed_request",
                stage="request_encode",
            )
        try:
            writer.write(struct.pack(">I", len(payload)) + payload)
            await writer.drain()
        except (ConnectionError, OSError) as exc:
            raise AccessibilityTransportError(
                f"collector request write failed: {exc}",
                failure_class="connection_reset",
                stage="request_write",
            ) from exc
        try:
            header = await reader.readexactly(4)
        except asyncio.IncompleteReadError as exc:
            raise AccessibilityTransportError(
                "collector response ended during frame header",
                failure_class="header_eof",
                stage="response_header",
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise AccessibilityTransportError(
                f"collector response header failed: {exc}",
                failure_class="connection_reset",
                stage="response_header",
            ) from exc
        size = struct.unpack(">I", header)[0]
        if not 0 < size <= SNAPSHOT_MAX_FRAME_BYTES:
            raise AccessibilityTransportError(
                f"collector response has invalid frame size: {size}",
                failure_class="malformed_frame",
                stage="response_header",
            )
        try:
            body = await reader.readexactly(size)
        except asyncio.IncompleteReadError as exc:
            raise AccessibilityTransportError(
                "collector response ended during frame payload",
                failure_class="payload_eof",
                stage="response_payload",
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise AccessibilityTransportError(
                f"collector response payload failed: {exc}",
                failure_class="connection_reset",
                stage="response_payload",
            ) from exc
        try:
            response = json.loads(body.decode("utf-8", "strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AccessibilityTransportError(
                f"collector response is malformed JSON: {exc}",
                failure_class="malformed_json",
                stage="response_decode",
            ) from exc
        if not isinstance(response, dict):
            raise AccessibilityTransportError(
                "collector response must be an object",
                failure_class="malformed_response",
                stage="response_decode",
            )
        if int(response.get("version") or 0) != SNAPSHOT_PROTOCOL_VERSION:
            raise AccessibilityTransportError(
                "collector response protocol mismatch",
                failure_class="protocol_mismatch",
                stage="response_validation",
            )
        if int(response.get("request_id") or -1) != request_id:
            raise AccessibilityTransportError(
                "collector response request_id mismatch",
                failure_class="request_id_mismatch",
                stage="response_validation",
            )
        if response.get("status") == "error":
            raise AccessibilityTransportError(
                str(response.get("error") or "collector error"),
                failure_class="remote_error",
                stage="response_validation",
            )
        return response

    async def _close_locked_shielded(
        self, *, deadline: float,
    ) -> dict[str, Any]:
        cleanup = asyncio.create_task(self._close_locked(deadline=deadline))
        try:
            return await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _close_locked(self, *, deadline: float) -> dict[str, Any]:
        self._refresh_cleanup_state()
        writer, port = self._writer, self._port
        self._reader = None
        self._writer = None
        self._bootstrap = None
        had_socket = writer is not None or self._socket_cleanup_task is not None
        had_forward = port is not None or self._forward_cleanup_task is not None
        if writer is not None:
            writer.close()
        if (
            writer is not None
            and self._socket_cleanup_task is None
            and not self._cleanup_failed
        ):
            self._socket_cleanup_task = asyncio.create_task(
                writer.wait_closed(),
                name=f"a11y-socket-close-{self.serial}",
            )
        if (
            port is not None
            and self._forward_cleanup_task is None
            and not self._cleanup_failed
        ):
            self._forward_cleanup_task = asyncio.create_task(
                adb.remove_forward_async(self.serial, port),
                name=f"a11y-forward-remove-{self.serial}-{port}",
            )

        cleanup_tasks = {
            task for task in (
                self._socket_cleanup_task, self._forward_cleanup_task,
            )
            if task is not None and not task.done()
        }
        remaining_s = max(0.0, deadline - time.monotonic())
        if cleanup_tasks and remaining_s > 0:
            await asyncio.wait(cleanup_tasks, timeout=remaining_s)

        socket_outcome = "not_owned"
        socket_task = self._socket_cleanup_task
        if socket_task is not None:
            if not socket_task.done():
                socket_outcome = "close_pending"
            elif socket_task.cancelled() or socket_task.exception() is not None:
                socket_outcome = "close_failed"
                self._socket_cleanup_task = None
                self._cleanup_failed = True
            else:
                socket_outcome = "closed"
                self._socket_cleanup_task = None

        forward_outcome = "not_owned"
        forward_task = self._forward_cleanup_task
        if forward_task is not None:
            if not forward_task.done():
                forward_outcome = "remove_pending"
            elif forward_task.cancelled() or forward_task.exception() is not None:
                forward_outcome = "remove_failed"
                self._forward_cleanup_task = None
                self._cleanup_failed = True
            else:
                forward_outcome = "removed"
                self._forward_cleanup_task = None
                if self._port == port:
                    self._port = None

        cleanup_pending = (
            socket_outcome == "close_pending"
            or forward_outcome == "remove_pending"
        )
        cleanup_failed = (
            self._cleanup_failed
            or (
                socket_outcome == "close_failed"
                or forward_outcome == "remove_failed"
            )
        )
        had_transport = had_socket or had_forward
        return {
            "outcome": (
                "cleanup_pending" if cleanup_pending else
                "cleanup_failed" if cleanup_failed else
                "closed" if had_transport else
                "not_owned"
            ),
            "socket_outcome": socket_outcome,
            "forward_outcome": forward_outcome,
        }

    def _refresh_cleanup_state(self) -> None:
        """Harvest completed cleanup without waiting or losing ownership."""
        socket_task = self._socket_cleanup_task
        if socket_task is not None and socket_task.done():
            if socket_task.cancelled() or socket_task.exception() is not None:
                self._cleanup_failed = True
            self._socket_cleanup_task = None
        forward_task = self._forward_cleanup_task
        if forward_task is not None and forward_task.done():
            if forward_task.cancelled() or forward_task.exception() is not None:
                self._cleanup_failed = True
            elif self._port is not None:
                self._port = None
            self._forward_cleanup_task = None


class AccessibilityChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[tuple[str, str], AccessibilitySnapshotChannel] = {}

    def get(self, serial: str, authority: str) -> AccessibilitySnapshotChannel:
        key = (serial, authority)
        channel = self._channels.get(key)
        if channel is None:
            channel = AccessibilitySnapshotChannel(serial, authority)
            self._channels[key] = channel
        return channel

    async def shutdown(self) -> None:
        failures: list[BaseException] = []
        for key, channel in list(self._channels.items()):
            try:
                await channel.close()
            except Exception as exc:  # ownership must remain registered
                failures.append(exc)
            else:
                if self._channels.get(key) is channel:
                    self._channels.pop(key, None)
        if failures:
            first = failures[0]
            raise AccessibilityTransportError(
                f"collector registry cleanup failed: {first}",
                failure_class="cleanup_failed",
                stage="registry_shutdown",
            ) from first


CHANNEL_REGISTRY = AccessibilityChannelRegistry()


def mark_dump_fallback(tree: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Record a provider fallback without downgrading a successful dump.

    Falling back from the optional collector says where the tree came from;
    it does not make a complete uiautomator tree incomplete. Existing capture
    timings are preserved so the pixel stage can form a causal pair.
    """
    existing = tree.get(CAPTURE_META_KEY)
    metadata = dict(existing) if isinstance(existing, dict) else {}
    metadata.update({
        "provider": "uiautomator_dump",
        "complete": bool(metadata.get("complete", True)),
        "reasons": ["collector_fallback", _bounded_reason(reason)],
        "generation": int(metadata.get("generation") or 0),
        "captured_monotonic_ms": float(metadata.get("captured_monotonic_ms") or 0),
        "window_count": int(metadata.get("window_count") or 1),
        "fallback_edges": [{
            "from": "accessibility_collector",
            "to": "uiautomator_dump",
            "reason": _bounded_reason(reason),
        }],
    })
    tree[CAPTURE_META_KEY] = metadata
    return tree


def _required_int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdbError(f"collector field {key} must be an integer")
    return value


def _required_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw.get(key)
    if not isinstance(value, bool):
        raise AdbError(f"collector field {key} must be a boolean")
    return value


def _required_number(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdbError(f"collector field {key} must be numeric")
    return float(value)


def _optional_number(raw: dict[str, Any], key: str) -> float:
    value = raw.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdbError(f"collector field {key} must be numeric")
    return float(value)


def _bounded_reason(value: Any) -> str:
    return str(value or "").strip()[:120]


def _coerce_transport_error(
    error: BaseException, *, stage: str
) -> AccessibilityTransportError:
    if isinstance(error, AccessibilityTransportError):
        return error
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        failure_class = "timeout"
    elif isinstance(error, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        failure_class = "connection_reset"
    elif isinstance(error, (ConnectionError, OSError)):
        failure_class = "transport_io"
    elif isinstance(error, AdbError):
        failure_class = "transport_unavailable"
    else:
        raise error
    return AccessibilityTransportError(
        str(error) or error.__class__.__name__,
        failure_class=failure_class,
        stage=stage,
    )
