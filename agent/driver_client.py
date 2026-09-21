"""`DeviceDriver` remote HTTP RPC adapter.

This module implements the `DeviceDriver` Protocol over an HTTP JSON-RPC
boundary. It is the transport-specific adapter used when a remote driver URL
is configured (e.g. a lab in another city controlling phones/PCs). In-process
deployments do not use this client — they hold an `AndroidDriver` (or
`FixtureDriver`) directly. See `driver/factory.get_driver()` for selection.

Multi-device: construct one ``DriverClient`` per ADB serial (``serial=...``);
every RPC includes that serial so the remote hub routes to the right phone.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from driver.observation_deadline import ObservationDeadline, ObservationStageError
from shared.schemas import Action, ActionResult, CanonicalUI


class DriverClient:
    """Remote HTTP adapter implementing the `DeviceDriver` Protocol.

    Speaks only to a Driver `/rpc` endpoint; never touches ADB directly.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        *,
        serial: str | None = None,
        transport: "httpx.AsyncBaseTransport | None" = None,
    ) -> None:
        if not base_url:
            raise ValueError("DriverClient requires a non-empty base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.serial = serial
        self._transport = transport

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        if self.serial:
            payload.setdefault("serial", self.serial)
        # The Driver URL names the device-control plane explicitly. Inheriting
        # unrelated HTTP(S)/SOCKS proxy variables can prevent even localhost
        # RPCs from being constructed and must not change device routing.
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            trust_env=False,
        ) as client:
            r = await client.post(
                f"{self.base_url}/rpc",
                json={"method": method, "params": payload, "id": "1"},
            )
            r.raise_for_status()
            body = r.json()
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        return body.get("result")

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return online devices from the remote driver hub."""
        # Unbound client (no serial) for inventory.
        result = await self._rpc("list_devices")
        if isinstance(result, dict):
            devices = result.get("devices")
            if isinstance(devices, list):
                return devices
        return []

    async def get_ui_state(self) -> CanonicalUI:
        """Fetch CanonicalUI via Driver RPC."""
        result = await self._rpc("get_ui_state")
        return CanonicalUI.model_validate(result)

    async def screenshot(self) -> bytes:
        """Fetch screenshot PNG bytes via Driver RPC."""
        result = await self._rpc("screenshot")
        return base64.b64decode(result["png_base64"])

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        """Fetch aligned raw tree dict + screenshot PNG bytes in one RPC."""
        result = await self._rpc("get_frame")
        shot = base64.b64decode(result["png_base64"])
        tree = result["tree"]
        if not isinstance(tree, dict):
            raise RuntimeError("get_frame returned non-dict tree")
        return tree, shot

    async def capture_deadline_frame(
        self, deadline: ObservationDeadline
    ) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
        """Run one atomic remote capture under the caller's remaining budget."""
        result = await self._rpc("capture_deadline_frame", {
            "mode": deadline.mode,
            "budget_ms": deadline.remaining_ms,
        })
        if not isinstance(result, dict):
            raise RuntimeError("capture_deadline_frame returned invalid result")
        failure = result.get("failure")
        if isinstance(failure, dict):
            attempts = [
                row for row in failure.get("provider_attempts") or []
                if isinstance(row, dict)
            ]
            for row in attempts:
                deadline.record_attempt(row)
            for edge in failure.get("fallback_edges") or []:
                if isinstance(edge, dict):
                    deadline.fallback_edges.append(dict(edge))
            deadline.cancelled_tasks.extend(
                str(name) for name in failure.get("cancelled_tasks") or []
            )
            raise ObservationStageError(
                str(failure.get("stage") or "remote_capture"),
                str(failure.get("reason") or "remote_capture_failed"),
                elapsed_ms=float(failure.get("elapsed_ms") or 0.0),
                budget_ms=float(failure.get("budget_ms") or 0.0),
                timed_out=bool(failure.get("timed_out")),
                fallback_edges=list(deadline.fallback_edges),
                provider_attempts=list(deadline.provider_attempts),
                cancelled_tasks=list(deadline.cancelled_tasks),
            )
        tree = result.get("tree")
        metadata = result.get("metadata")
        encoded = result.get("png_base64")
        if not isinstance(tree, dict) or not isinstance(metadata, dict):
            raise RuntimeError("capture_deadline_frame returned malformed package")
        if not isinstance(encoded, str):
            raise RuntimeError("capture_deadline_frame returned missing pixels")
        for row in metadata.get("provider_attempts") or []:
            if isinstance(row, dict):
                deadline.record_attempt(row)
        for edge in metadata.get("fallback_edges") or []:
            if isinstance(edge, dict):
                deadline.fallback_edges.append(dict(edge))
        deadline.cancelled_tasks.extend(
            str(name) for name in metadata.get("cancelled_capture_tasks") or []
        )
        return tree, base64.b64decode(encoded), metadata

    async def act(self, action: Action) -> ActionResult:
        """Execute an action via Driver RPC."""
        result = await self._rpc("act", {"action": action.model_dump()})
        return ActionResult.model_validate(result)

    async def wake_and_unlock(self) -> None:
        """Wake + unlock the device via Driver RPC (best-effort, never raises)."""
        try:
            await self._rpc("wake_and_unlock")
        except Exception:  # noqa: BLE001
            # Best-effort: a remote wake failure must not abort the task.
            return

    async def begin_task_session(self, task_id: str) -> dict[str, Any]:
        result = await self._rpc("begin_task_session", {"task_id": task_id})
        return result if isinstance(result, dict) else {
            "status": "failed", "reason": "invalid driver response",
        }

    async def end_task_session(self, task_id: str = "") -> dict[str, Any]:
        try:
            result = await self._rpc("end_task_session", {"task_id": task_id})
            return result if isinstance(result, dict) else {"status": "invalid_response"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "release_deferred_to_ttl", "reason": str(exc)[:200]}

    async def current_activity(self) -> str:
        """Return the foreground activity component via Driver RPC."""
        try:
            result = await self._rpc("current_activity")
            return str(result.get("activity", "")) if isinstance(result, dict) else ""
        except Exception:  # noqa: BLE001
            return ""

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return exact resumed-App facts via Driver RPC."""
        try:
            params = ({"timeout_s": max(0.05, timeout_s)}
                      if timeout_s is not None else None)
            result = await self._rpc("current_foreground_identity", params)
            return result if isinstance(result, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def get_input_diagnostics(self, *, timeout_s: float = 0.35) -> dict[str, Any]:
        result = await self._rpc("get_input_diagnostics", {"timeout_s": timeout_s})
        return result if isinstance(result, dict) else {}

    async def search_installed_apps(self, query: str, *, limit: int = 12) -> list[dict[str, str]]:
        result = await self._rpc("search_installed_apps", {"query": query, "limit": limit})
        candidates = result.get("candidates") if isinstance(result, dict) else None
        return candidates if isinstance(candidates, list) else []

    async def resolve_installed_app(self, app: str) -> dict[str, Any]:
        result = await self._rpc("resolve_installed_app", {"app": app})
        return result if isinstance(result, dict) else {}

    async def skill_profile_ids(self) -> list[str]:
        result = await self._rpc("skill_profile_ids", {})
        return result.get("profiles", []) if isinstance(result, dict) else []

    async def validate_installed_app(self, package: str) -> bool:
        result = await self._rpc("validate_installed_app", {"package": package})
        return bool(result.get("installed")) if isinstance(result, dict) else False

    async def record_installed_app_selection(self, display_name: str, package: str) -> bool:
        result = await self._rpc("record_installed_app_selection", {
            "display_name": display_name, "package": package,
        })
        return bool(result.get("recorded")) if isinstance(result, dict) else False

    async def health(self) -> dict[str, Any]:
        """Proxy driver health (per-serial when ``self.serial`` is set)."""
        return await self._rpc("health")

    async def initialize_environment(self) -> dict[str, Any]:
        result = await self._rpc("initialize_environment")
        return result if isinstance(result, dict) else {"status": "failed"}

    async def reconcile_environment(self) -> dict[str, Any]:
        result = await self._rpc("reconcile_environment")
        return result if isinstance(result, dict) else {"status": "failed"}

    async def readiness(self) -> dict[str, Any]:
        result = await self._rpc("readiness")
        return result if isinstance(result, dict) else {"status": "degraded"}
