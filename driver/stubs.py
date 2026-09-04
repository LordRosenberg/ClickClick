"""Windows / iOS driver stubs for MVP."""

from __future__ import annotations

from typing import Any

from shared.protocol import UnsupportedPlatformError
from shared.schemas import Action, ActionResult, CanonicalUI


class WindowsDriver:
    """Placeholder Windows driver — not implemented in Android MVP."""

    async def get_ui_state(self) -> CanonicalUI:
        raise UnsupportedPlatformError("Windows driver is not available in Android MVP")

    async def screenshot(self) -> bytes:
        raise UnsupportedPlatformError("Windows driver is not available in Android MVP")

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        raise UnsupportedPlatformError("Windows driver is not available in Android MVP")

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        raise UnsupportedPlatformError("Windows foreground identity is not available")

    async def act(self, action: Action) -> ActionResult:
        raise UnsupportedPlatformError("Windows driver is not available in Android MVP")

    async def wake_and_unlock(self) -> None:
        return None

    async def initialize_environment(self) -> dict[str, Any]:
        raise UnsupportedPlatformError("Windows initialization is not implemented")

    async def readiness(self) -> dict[str, Any]:
        return {"status": "degraded", "reason": "unsupported platform"}

    async def health(self) -> dict[str, Any]:
        return {"ok": False, "platform": "windows", "error": "unsupported"}


class IOSDriver:
    """Placeholder iOS driver — not implemented in Android MVP."""

    async def get_ui_state(self) -> CanonicalUI:
        raise UnsupportedPlatformError("iOS driver is not available in Android MVP")

    async def screenshot(self) -> bytes:
        raise UnsupportedPlatformError("iOS driver is not available in Android MVP")

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        raise UnsupportedPlatformError("iOS driver is not available in Android MVP")

    async def current_foreground_identity(
        self, *, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        raise UnsupportedPlatformError("iOS foreground identity is not available")

    async def act(self, action: Action) -> ActionResult:
        raise UnsupportedPlatformError("iOS driver is not available in Android MVP")

    async def wake_and_unlock(self) -> None:
        return None

    async def initialize_environment(self) -> dict[str, Any]:
        raise UnsupportedPlatformError("iOS initialization is not implemented")

    async def readiness(self) -> dict[str, Any]:
        return {"status": "degraded", "reason": "unsupported platform"}

    async def health(self) -> dict[str, Any]:
        return {"ok": False, "platform": "ios", "error": "unsupported"}
