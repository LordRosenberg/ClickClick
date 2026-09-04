"""Single `get_driver()` factory that selects the `DeviceDriver` transport.

This is the only place that decides which transport the Agent uses:
- fixture mode enabled            -> `FixtureDriver` (tests / offline)
- remote driver URL configured    -> `DriverClient` HTTP RPC adapter (remote lab)
- otherwise                       -> in-process platform driver (local ADB-direct)

Both entrypoints (`agent.main`, `control_api.main`) share this factory so the
selection logic lives in exactly one place. The standalone `clickclick-driver`
process reuses `_build_android_driver()` for its own `build_driver`.

Multi-device Control API prefers ``driver.pool.DriverPool`` over a process-wide
singleton; ``get_driver`` remains for CLI one-shots and tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from driver.android import AndroidDriver
from driver.environment import AndroidProvisioningOptions
from driver.fixture import FixtureDriver
from driver.stubs import IOSDriver, WindowsDriver

if TYPE_CHECKING:
    from shared.app_resolver import NameResolver
    from shared.config import Settings
    from shared.protocol import DeviceDriver


def _build_android_driver(
    settings: "Settings | None" = None,
    *,
    serial: str | None = None,
) -> AndroidDriver:
    """Construct an `AndroidDriver` and best-effort connect to a device.

    When ``serial`` is provided, connect to that ADB device. Otherwise connect
    only succeeds when exactly one device is online (CLI convenience).
    A connect failure is non-fatal: the driver still returns and surfaces the
    error on the next device I/O call (e.g. `health()` / `get_frame()`). When
    A `NameResolver` is injected for deterministic alias/cache resolution and
    installed-package discovery; it never starts an LLM call.
    """
    resolver: NameResolver | None = None
    if settings is not None:
        from shared.app_resolver import build_default_resolver

        try:
            resolver = build_default_resolver(settings)
        except Exception as exc:  # noqa: BLE001
            print(f"[driver] app-resolver build warning: {exc}")
            resolver = None
    resolved_serial = serial
    if settings is not None and resolved_serial is None:
        # CLI one-shots legitimately omit a serial when exactly one device is
        # online. Resolve it before provider construction so this convenience
        # path does not silently lose the normal scrcpy-first pixel source.
        try:
            from driver import adb

            resolved_serial = adb.sole_online_device()
        except Exception:  # noqa: BLE001
            resolved_serial = None
    stream_provider = None
    if settings is not None and resolved_serial:
        # Provider order is a runtime invariant: construct the shared scrcpy
        # consumer and let health determine automatic ADB fallback.
        from driver.scrcpy_mirror import REGISTRY
        from driver.scrcpy_observation import ScrcpyObservationProvider
        stream_provider = ScrcpyObservationProvider(REGISTRY, resolved_serial)
    provisioning = AndroidProvisioningOptions(
        collector_apk_path=settings.accessibility_collector_apk_path if settings else "",
        ime_apk_path=settings.ime_apk_path if settings else "",
        stay_awake_while_plugged=(
            settings.device_stay_awake_while_plugged if settings else False
        ),
    )
    drv = AndroidDriver(
        serial=resolved_serial,
        resolver=resolver,
        stream_provider=stream_provider,
        ime_auto_setup=settings.ime_auto_setup if settings is not None else True,
        collector_enabled=(
            settings.accessibility_collector_enabled if settings is not None else True
        ),
        provisioning=provisioning,
    )
    try:
        drv.connect(resolved_serial)
    except Exception as exc:  # noqa: BLE001
        print(f"[driver] ADB connect warning: {exc}")
    return drv


def get_driver(settings: "Settings", *, serial: str | None = None) -> "DeviceDriver":
    """Return a `DeviceDriver` selected by configuration.

    Selection order: fixture override > remote URL > in-process platform driver.
    """
    if settings.use_fixture_driver:
        return FixtureDriver()
    if settings.driver_url:
        # Lazy import keeps the standalone driver process from pulling in the
        # agent package (and httpx client) when it only needs a local driver.
        from agent.driver_client import DriverClient

        return DriverClient(settings.driver_url, serial=serial)
    platform = (settings.platform or "android").lower()
    if platform == "android":
        return _build_android_driver(settings, serial=serial)
    if platform == "windows":
        return WindowsDriver()
    if platform == "ios":
        return IOSDriver()
    raise SystemExit(f"unknown platform: {platform}")
