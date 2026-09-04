"""DeviceDriver protocol and Agent↔Driver RPC wire contracts."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from shared.schemas import Action, ActionResult, CanonicalUI


class UnsupportedPlatformError(Exception):
    """Raised when a non-Android driver is requested in the MVP."""


@runtime_checkable
class DeviceDriver(Protocol):
    """Platform-agnostic device control surface used by the Agent process."""

    async def get_ui_state(self) -> CanonicalUI:
        """Return normalized UI state for the foreground app."""
        ...

    async def screenshot(self) -> bytes:
        """Capture a PNG/JPEG screenshot of the device screen."""
        ...

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        """Return a tree/image pair with explicit mechanical capture evidence.

        The tree must contain typed ``_capture.complete`` and
        ``_capture.coordinate_compatible`` facts.
        Missing evidence is treated as an incomplete capture; callers never
        infer success merely because a driver returned a dictionary.
        """
        ...

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Return exact resumed-application identity from platform facts."""
        ...

    async def act(self, action: Action) -> ActionResult:
        """Execute a single action on the device."""
        ...

    async def wake_and_unlock(self) -> None:
        """Wake the screen and dismiss any non-secure lock so the first tap
        reaches its target instead of just lighting the screen. Best-effort:
        implementations MUST NOT raise on failure (a task should still
        proceed when the device is already awake)."""
        ...

    async def health(self) -> dict[str, Any]:
        """Return driver/process health information."""
        ...

    async def initialize_environment(self) -> dict[str, Any]:
        """Idempotently provision an explicitly bound device."""
        ...

    async def readiness(self) -> dict[str, Any]:
        """Return bounded per-task readiness without installing software."""
        ...


# --- JSON-RPC style wire messages (HTTP POST /rpc) ---


class RpcRequest(dict):
    """Lightweight helper documenting the RPC request shape.

    Expected keys:
      method: str  # get_ui_state | screenshot | get_frame | act | health
      params: dict
      id: str | int | None
    """


RPC_METHODS = frozenset({
    "get_ui_state", "screenshot", "get_frame", "capture_deadline_frame",
    "act", "health",
    "wake_and_unlock", "current_activity", "current_foreground_identity",
    "get_input_diagnostics",
    "resolve_installed_app", "search_installed_apps", "validate_installed_app",
    "record_installed_app_selection",
    "initialize_environment", "readiness",
    # multi-device-concurrent: inventory behind CLICKCLICK_DRIVER_URL
    "list_devices",
})
