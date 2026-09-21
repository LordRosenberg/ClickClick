"""Per-device DeviceDriver pool for multi-device / multi-lab concurrent execution.

In-process Android: lazy-create one ``AndroidDriver`` per ADB serial.
Fixture transport: a single logical ``fixture`` device.
Remote hub(s): one or more ``CLICKCLICK_DRIVER_URL`` / ``DRIVER_URLS_JSON``
hosts; inventory is aggregated and each binding key routes to the right hub.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from driver.fixture import FixtureDriver

if TYPE_CHECKING:
    from shared.config import Settings
    from shared.protocol import DeviceDriver

logger = logging.getLogger(__name__)

FIXTURE_SERIAL = "fixture"
LOCAL_DRIVER_ID = "local"
FIXTURE_DRIVER_ID = "fixture"


def device_key(driver_id: str, serial: str) -> str:
    """Stable binding key for inventory / task.device_serial / pool routing."""
    if driver_id in (LOCAL_DRIVER_ID, "") or not driver_id:
        return serial
    if driver_id == FIXTURE_DRIVER_ID:
        return FIXTURE_SERIAL
    return f"{driver_id}/{serial}"


def parse_device_key(key: str) -> tuple[str, str]:
    """Return ``(driver_id, adb_serial)`` for a binding key.

    - ``fixture`` → (fixture, fixture)
    - ``lab-a/SERIAL`` → (lab-a, SERIAL)
    - bare ``SERIAL`` → (local, SERIAL)  (in-process ADB / legacy rows)
    """
    key = (key or "").strip()
    if not key:
        raise ValueError("empty device key")
    if key == FIXTURE_SERIAL:
        return FIXTURE_DRIVER_ID, FIXTURE_SERIAL
    if "/" in key:
        driver_id, serial = key.split("/", 1)
        if not driver_id or not serial:
            raise ValueError(f"invalid device key: {key!r}")
        return driver_id, serial
    return LOCAL_DRIVER_ID, key


def adb_serial_from_key(key: str | None) -> str:
    """ADB serial portion of a binding key (for Live mirror hello, etc.)."""
    if not key:
        return ""
    try:
        return parse_device_key(key)[1]
    except ValueError:
        return key


class DriverPool:
    """Lazy per-key driver sessions + asyncio locks for mutual exclusion."""

    def __init__(
        self,
        settings: "Settings",
        *,
        force_local: bool = False,
    ) -> None:
        self._settings = settings
        # When True (clickclick-driver process), never treat driver_url as
        # remote — that env var only configures the listen address.
        self._force_local = force_local
        self._drivers: dict[str, "DeviceDriver"] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._environment_results: dict[str, dict[str, Any]] = {}
        self._online_keys: set[str] = set()

    def hubs(self) -> list[dict[str, str]]:
        """Remote hubs visible to this pool (empty when force_local / fixture)."""
        if self._force_local or self._settings.use_fixture_driver:
            return []
        return self._settings.driver_hubs()

    @property
    def mode(self) -> str:
        if self._settings.use_fixture_driver:
            return "fixture"
        if self.hubs():
            return "remote"
        platform = (self._settings.platform or "android").lower()
        return platform if platform in ("android", "windows", "ios") else "android"

    def lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def inventory(self) -> list[dict[str, Any]]:
        """Online devices without busy flags (caller merges busy from DB).

        Each entry includes ``key`` (binding id for create/busy), ``driver_id``,
        and ADB ``serial`` (plus model / market_name when available).
        """
        mode = self.mode
        if mode == "fixture":
            return [
                {
                    "key": FIXTURE_SERIAL,
                    "driver_id": FIXTURE_DRIVER_ID,
                    "serial": FIXTURE_SERIAL,
                    "state": "device",
                    "model": "FixtureDriver",
                    "market_name": "fixture",
                }
            ]
        if mode == "remote":
            return await self._inventory_remote()
        if mode != "android":
            return []
        from driver import adb

        out: list[dict[str, Any]] = []
        for d in await adb.describe_devices_async():
            serial = d["serial"]
            out.append(
                {
                    **d,
                    "key": device_key(LOCAL_DRIVER_ID, serial),
                    "driver_id": LOCAL_DRIVER_ID,
                }
            )
        return out

    async def _inventory_remote(self) -> list[dict[str, Any]]:
        from agent.driver_client import DriverClient

        out: list[dict[str, Any]] = []
        for hub in self.hubs():
            client = DriverClient(hub["url"])
            try:
                devices = await client.list_devices()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "remote list_devices failed hub=%s url=%s: %s",
                    hub["id"],
                    hub["url"],
                    exc,
                )
                continue
            for d in devices:
                serial = d.get("serial") or ""
                if not serial:
                    continue
                out.append(
                    {
                        **d,
                        "key": device_key(hub["id"], serial),
                        "driver_id": hub["id"],
                        "serial": serial,
                    }
                )
        return out

    def get(self, key: str) -> "DeviceDriver":
        """Return (creating if needed) the driver bound to ``key``."""
        mode = self.mode
        if mode == "fixture":
            if key != FIXTURE_SERIAL:
                raise KeyError(f"fixture pool only serves key={FIXTURE_SERIAL!r}")
            drv = self._drivers.get(FIXTURE_SERIAL)
            if drv is None:
                drv = FixtureDriver()
                self._drivers[FIXTURE_SERIAL] = drv
            return drv

        if mode == "remote":
            driver_id, serial = parse_device_key(key)
            # Allow bare serial when exactly one hub (legacy create payloads).
            hubs = self.hubs()
            hub = next((h for h in hubs if h["id"] == driver_id), None)
            if hub is None and driver_id == LOCAL_DRIVER_ID and len(hubs) == 1:
                hub = hubs[0]
                driver_id = hub["id"]
                key = device_key(driver_id, serial)
            if hub is None:
                raise KeyError(f"unknown driver hub id={driver_id!r} for key={key!r}")
            cached = self._drivers.get(key)
            if cached is not None:
                return cached
            from agent.driver_client import DriverClient

            client = DriverClient(hub["url"], serial=serial)
            self._drivers[key] = client
            return client

        _, serial = parse_device_key(key)
        cached = self._drivers.get(key)
        if cached is not None:
            return cached
        from driver.factory import _build_android_driver

        drv = _build_android_driver(self._settings, serial=serial)
        self._drivers[key] = drv
        return drv

    async def ensure_environment(
        self, key: str, *, force: bool = False,
    ) -> dict[str, Any]:
        """Idempotently install/upgrade one online device environment."""
        if not self._settings.accessibility_collector_enabled:
            return self.record_environment_result(key, {
                "status": "disabled",
                "reason": "accessibility_collector_disabled",
            })
        cached = self._environment_results.get(key)
        if not force and cached is not None and cached.get("collector_ready"):
            return dict(cached)
        async with self.lock_for(key):
            cached = self._environment_results.get(key)
            if not force and cached is not None and cached.get("collector_ready"):
                return dict(cached)
            driver = self.get(key)
            initialize = getattr(driver, "reconcile_environment", None)
            if not callable(initialize):
                result: dict[str, Any] = {
                    "status": "unsupported",
                    "reason": "driver does not support environment initialization",
                }
            else:
                try:
                    raw = await initialize()
                    result = raw if isinstance(raw, dict) else {
                        "status": "failed", "reason": "invalid initialization response",
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    result = {"status": "failed", "reason": str(exc)[:240]}
            return self.record_environment_result(key, result)

    async def reconcile_environments(
        self, *, skip_keys: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Initialize newly-online or previously-failed devices in parallel."""
        inventory = await self.inventory()
        online = {str(row.get("key") or row.get("serial") or "") for row in inventory}
        online.discard("")
        for offline in self._online_keys - online:
            self._environment_results.pop(offline, None)
        self._online_keys = online
        skipped = set(skip_keys or ())
        keys = [
            key for key in sorted(online)
            if key not in skipped and not (
                self._environment_results.get(key, {}).get("collector_ready")
            )
        ]
        if keys:
            values = await asyncio.gather(
                *(self.ensure_environment(key) for key in keys),
                return_exceptions=True,
            )
            for key, value in zip(keys, values):
                if isinstance(value, BaseException):
                    self._environment_results[key] = {
                        "status": "failed",
                        "reason": str(value)[:240],
                        "collector_ready": False,
                        "checked_monotonic": time.monotonic(),
                    }
        return {
            key: dict(value) for key, value in self._environment_results.items()
            if key in online
        }

    def environment_status(self, key: str) -> dict[str, Any] | None:
        value = self._environment_results.get(key)
        return dict(value) if value is not None else None

    def record_environment_result(
        self, key: str, result: dict[str, Any],
    ) -> dict[str, Any]:
        recorded = {
            **result,
            "collector_ready": self._collector_ready(result),
            "checked_monotonic": time.monotonic(),
        }
        self._environment_results[key] = recorded
        return dict(recorded)

    @staticmethod
    def _collector_ready(result: dict[str, Any]) -> bool:
        if result.get("status") == "disabled":
            return True
        if result.get("status") == "ready" and not result.get("steps"):
            return True  # fixture and drivers with one aggregate readiness bit
        steps = result.get("steps")
        if not isinstance(steps, dict):
            return False
        required = (
            "collector_apk",
            "accessibility_service",
            "collector_health",
            "collector_channel",
        )
        return all(
            isinstance(steps.get(name), dict)
            and steps[name].get("status") == "ready"
            for name in required
        )
