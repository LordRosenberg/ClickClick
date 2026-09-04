"""HTTP JSON-RPC surface for the Driver process (consumed by Agent).

Supports either a single ``DeviceDriver`` (tests / legacy) or a local
``DriverPool`` hub so one driver URL can serve many ADB serials.

Also exposes operator-side ``/mirror/stream`` (WebSocket) for Console Live
relay — independent of ``/rpc`` agent traffic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from driver.observation_deadline import (
    CURRENT_DEADLINE_MS,
    ObservationDeadline,
    ObservationStageError,
)
from driver.scrcpy_mirror import (
    LocalStreamSource,
    MirrorRegistry,
    MirrorUnavailableError,
    is_mirror_server_available,
)
from shared.protocol import RPC_METHODS, UnsupportedPlatformError
from shared.schemas import Action

logger = logging.getLogger(__name__)

_WS_CLOSE_TRY_AGAIN = 1013
_WS_CLOSE_INTERNAL = 1011


class RpcBody(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: str | int | None = None


def create_driver_app(
    driver: Any = None,
    *,
    pool: Any | None = None,
) -> FastAPI:
    """Build a FastAPI app exposing /health and /rpc.

    Pass either ``driver`` (single session) or ``pool`` (per-serial hub).
    """
    if driver is None and pool is None:
        raise ValueError("create_driver_app requires driver= or pool=")

    app = FastAPI(title="ClickClick Driver", version="0.1.0")
    # Hub-local Live sessions (scrcpy-server on this host's adb). Separate
    # from agent /rpc so Live does not share the DriverPool action lock.
    mirror_registry = MirrorRegistry()
    mirror_registry.set_source_factory(
        lambda key: LocalStreamSource(serial=key)
    )

    async def _inventory() -> list[dict[str, Any]]:
        if pool is not None:
            return await pool.inventory()
        # Single-driver compat: synthesize one inventory row from health.
        try:
            h = await driver.health()
        except Exception as exc:  # noqa: BLE001
            return []
        serial = h.get("serial") if isinstance(h, dict) else None
        if not isinstance(serial, str) or not serial:
            serial = "default"
        return [
            {
                "serial": serial,
                "state": "device",
                "model": str(h.get("model", "")) if isinstance(h, dict) else "",
                "market_name": str(h.get("market_name", "")) if isinstance(h, dict) else "",
            }
        ]

    async def _resolve(params: dict[str, Any]) -> Any:
        if pool is None:
            return driver
        serial = params.get("serial")
        if isinstance(serial, str):
            serial = serial.strip() or None
        else:
            serial = None
        if not serial:
            inv = await pool.inventory()
            if len(inv) == 1:
                serial = inv[0]["serial"]
            else:
                raise ValueError(
                    "params.serial is required when multiple devices are online"
                )
        return pool.get(serial)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        devices = await _inventory()
        return {
            "ok": bool(devices),
            "device_count": len(devices),
            "devices": devices,
            "mode": getattr(pool, "mode", "single") if pool is not None else "single",
        }

    @app.post("/rpc")
    async def rpc(body: RpcBody) -> dict[str, Any]:
        if body.method not in RPC_METHODS:
            return {"id": body.id, "error": f"unknown method {body.method}"}
        try:
            if body.method == "list_devices":
                result = {"devices": await _inventory()}
                return {"id": body.id, "result": result}

            if body.method == "health":
                # Aggregate when no serial; per-device when serial provided.
                serial = body.params.get("serial")
                if pool is not None and not serial:
                    devices = await _inventory()
                    result = {
                        "ok": bool(devices),
                        "device_count": len(devices),
                        "devices": devices,
                        "mode": pool.mode,
                    }
                else:
                    drv = await _resolve(body.params)
                    result = await drv.health()
                return {"id": body.id, "result": result}

            drv = await _resolve(body.params)
            if body.method == "get_ui_state":
                ui = await drv.get_ui_state()
                result = ui.model_dump()
            elif body.method == "screenshot":
                data = await drv.screenshot()
                result = {"png_base64": base64.b64encode(data).decode("ascii")}
            elif body.method == "get_frame":
                tree, shot = await drv.get_frame()
                result = {
                    "tree": tree,
                    "png_base64": base64.b64encode(shot).decode("ascii"),
                }
            elif body.method == "capture_deadline_frame":
                requested_budget_ms = float(body.params.get("budget_ms") or 1.0)
                if not math.isfinite(requested_budget_ms):
                    requested_budget_ms = 1.0
                server_budget_ms = min(
                    float(CURRENT_DEADLINE_MS),
                    max(1.0, requested_budget_ms),
                )
                deadline = ObservationDeadline(
                    str(body.params.get("mode") or "current"),
                    server_budget_ms,
                )
                capture = getattr(drv, "capture_deadline_frame", None)
                try:
                    async def run_capture():
                        if callable(capture):
                            return await capture(deadline)
                        tree, shot = await drv.get_frame()
                        return tree, shot, {
                            "provider": "remote_get_frame",
                            "provider_attempts": [],
                            "fallback_edges": [],
                        }

                    tree, shot, metadata = await asyncio.wait_for(
                        run_capture(),
                        timeout=max(0.001, deadline.remaining_ms / 1000.0),
                    )
                    result = {
                        "tree": tree,
                        "png_base64": base64.b64encode(shot).decode("ascii"),
                        "metadata": metadata,
                    }
                except asyncio.TimeoutError:
                    result = {"failure": {
                        "stage": "remote_capture",
                        "reason": "server_deadline_exhausted",
                        "elapsed_ms": deadline.elapsed_ms,
                        "budget_ms": deadline.budget_ms,
                        "timed_out": True,
                        "fallback_edges": list(deadline.fallback_edges),
                        "provider_attempts": list(deadline.provider_attempts),
                        "cancelled_tasks": list(deadline.cancelled_tasks),
                    }}
                except ObservationStageError as exc:
                    result = {"failure": {
                        "stage": exc.stage,
                        "reason": exc.reason,
                        "elapsed_ms": exc.elapsed_ms,
                        "budget_ms": exc.budget_ms,
                        "timed_out": exc.timed_out,
                        "fallback_edges": list(exc.fallback_edges),
                        "provider_attempts": list(exc.provider_attempts),
                        "cancelled_tasks": list(exc.cancelled_tasks),
                    }}
            elif body.method == "act":
                action = Action.model_validate(body.params.get("action") or body.params)
                result = (await drv.act(action)).model_dump()
            elif body.method == "wake_and_unlock":
                await drv.wake_and_unlock()
                result = {"ok": True}
            elif body.method == "current_activity":
                result = {"activity": await drv.current_activity()}
            elif body.method == "current_foreground_identity":
                timeout_value = body.params.get("timeout_s")
                timeout_s = None
                if timeout_value is not None:
                    requested_timeout_s = float(timeout_value)
                    if math.isfinite(requested_timeout_s):
                        timeout_s = min(
                            CURRENT_DEADLINE_MS / 1000.0,
                            max(0.05, requested_timeout_s),
                        )
                result = await drv.current_foreground_identity(
                    timeout_s=timeout_s
                )
            elif body.method == "get_input_diagnostics":
                timeout_s = float(body.params.get("timeout_s", 0.35))
                fn = getattr(drv, "get_input_diagnostics", None)
                result = await fn(timeout_s=timeout_s) if callable(fn) else {}
            elif body.method == "search_installed_apps":
                fn = getattr(drv, "search_installed_apps", None)
                candidates = await fn(
                    str(body.params.get("query") or ""),
                    limit=int(body.params.get("limit", 12)),
                ) if callable(fn) else []
                result = {"candidates": candidates}
            elif body.method == "resolve_installed_app":
                fn = getattr(drv, "resolve_installed_app", None)
                result = await fn(str(body.params.get("app") or "")) if callable(fn) else {}
            elif body.method == "validate_installed_app":
                fn = getattr(drv, "validate_installed_app", None)
                installed = await fn(str(body.params.get("package") or "")) if callable(fn) else False
                result = {"installed": bool(installed)}
            elif body.method == "record_installed_app_selection":
                fn = getattr(drv, "record_installed_app_selection", None)
                recorded = await fn(
                    str(body.params.get("display_name") or ""),
                    str(body.params.get("package") or ""),
                ) if callable(fn) else False
                result = {"recorded": bool(recorded)}
            elif body.method == "initialize_environment":
                fn = getattr(drv, "initialize_environment", None)
                result = await fn() if callable(fn) else {
                    "status": "failed", "reason": "driver does not support initialization"
                }
            elif body.method == "readiness":
                fn = getattr(drv, "readiness", None)
                result = await fn() if callable(fn) else {"status": "degraded"}
            else:  # pragma: no cover
                result = {}
            return {"id": body.id, "result": result}
        except UnsupportedPlatformError as exc:
            return {"id": body.id, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"id": body.id, "error": str(exc)}

    @app.websocket("/mirror/stream")
    async def hub_mirror_stream(websocket: WebSocket) -> None:
        """Raw H.264 relay for Control API RemoteStreamSource (operator-only)."""
        await websocket.accept()
        serial: str | None = None
        try:
            if not is_mirror_server_available():
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "reason": "mirror_unavailable",
                            "detail": "vendored scrcpy-server jar missing on hub",
                        }
                    )
                )
                await websocket.close(code=_WS_CLOSE_TRY_AGAIN)
                return
            try:
                hello = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL,
                    reason=json.dumps({"reason": "hello_timeout"}),
                )
                return
            try:
                payload = json.loads(hello)
            except json.JSONDecodeError:
                payload = {}
            serial = payload.get("serial") if isinstance(payload, dict) else None
            if not isinstance(serial, str) or not serial.strip():
                await websocket.send_text(
                    json.dumps({"type": "error", "reason": "missing_serial"})
                )
                await websocket.close(code=_WS_CLOSE_INTERNAL)
                return
            serial = serial.strip()
            try:
                session = await mirror_registry.start(serial)
            except MirrorUnavailableError as exc:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "reason": "mirror_unavailable",
                            "detail": str(exc),
                        }
                    )
                )
                await websocket.close(code=_WS_CLOSE_TRY_AGAIN)
                return
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "serial": serial,
                        "codec": "h264",
                        "codec_string": session.codec_string or "avc1.42E01E",
                    }
                )
            )
            async for chunk in session.frames():
                if not chunk:
                    break
                await websocket.send_bytes(chunk)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("hub mirror stream error serial=%s: %s", serial, exc)
            try:
                await websocket.close(code=_WS_CLOSE_INTERNAL, reason=str(exc)[:120])
            except Exception:  # noqa: BLE001
                pass
        finally:
            if serial is not None:
                try:
                    await mirror_registry.stop(serial)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("hub mirror stop error: %s", exc)

    @app.on_event("shutdown")
    async def _shutdown_hub_mirror() -> None:
        from driver.accessibility import CHANNEL_REGISTRY

        await CHANNEL_REGISTRY.shutdown()
        await mirror_registry.shutdown()

    return app
