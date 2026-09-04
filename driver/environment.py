"""Idempotent Android device provisioning with explicit degraded outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from driver import adb
from driver.accessibility import AccessibilityCollectorClient
from driver.adb import AdbError


StepStatus = Literal["ready", "degraded", "operator_action_required", "failed"]

COLLECTOR_PACKAGE = "ai.clickclick.collector"
COLLECTOR_COMPONENT = "ai.clickclick.collector/.CollectorService"
COLLECTOR_AUTHORITY = "ai.clickclick.collector"
COLLECTOR_VERSION = "0.2.0"


@dataclass(frozen=True)
class AndroidProvisioningOptions:
    """Deployment-specific inputs for optional device provisioning."""

    collector_apk_path: str = ""
    ime_apk_path: str = ""
    stay_awake_while_plugged: bool = False


def _step(status: StepStatus, reason: str = "", **details: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason[:240], **details}


def merge_component(existing: str, component: str) -> str:
    """Append one service component while preserving unrelated service order."""
    values = [item.strip() for item in (existing or "").split(":") if item.strip()]
    if component not in values:
        values.append(component)
    return ":".join(values)


async def initialize_android_device(
    serial: str,
    *,
    collector_package: str = COLLECTOR_PACKAGE,
    collector_component: str = COLLECTOR_COMPONENT,
    collector_authority: str = COLLECTOR_AUTHORITY,
    collector_apk_path: str = "",
    collector_version: str = COLLECTOR_VERSION,
    ime_id: str = "",
    ime_apk_path: str = "",
    stay_awake_while_plugged: bool = False,
    health_timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Provision a controlled test device without hiding security boundaries."""
    steps: dict[str, dict[str, Any]] = {}
    try:
        online = await adb.list_device_serials_async()
        if serial not in online:
            return {
                "serial": serial,
                "status": "failed",
                "steps": {"adb": _step("failed", "device is offline or unauthorized")},
            }
        steps["adb"] = _step("ready")
    except Exception as exc:  # noqa: BLE001
        return {
            "serial": serial,
            "status": "failed",
            "steps": {"adb": _step("failed", str(exc))},
        }

    installed = await adb.package_version_async(serial, collector_package)
    needs_install = installed is None or bool(
        collector_version and installed not in {collector_version, "installed"}
    )
    if needs_install:
        path = Path(collector_apk_path) if collector_apk_path else None
        if path is None or not path.is_file():
            steps["collector_apk"] = _step(
                "degraded", "collector APK is missing; build or configure collector_apk_path",
                installed_version=installed,
            )
        else:
            try:
                await adb.install_apk_async(serial, path)
                installed = await adb.package_version_async(serial, collector_package)
                steps["collector_apk"] = _step(
                    "ready", installed_version=installed or "installed"
                )
            except AdbError as exc:
                steps["collector_apk"] = _step("failed", str(exc))
    else:
        steps["collector_apk"] = _step("ready", installed_version=installed)

    try:
        existing = await adb.setting_get_async(
            serial, "secure", "enabled_accessibility_services"
        )
        merged = merge_component(existing, collector_component)
        if merged != existing:
            await adb.setting_put_async(
                serial, "secure", "enabled_accessibility_services", merged
            )
        await adb.setting_put_async(serial, "secure", "accessibility_enabled", "1")
        confirmed = await adb.setting_get_async(
            serial, "secure", "enabled_accessibility_services"
        )
        if collector_component not in confirmed.split(":"):
            raise AdbError("platform did not retain the collector service setting")
        restricted_mode = await adb.appop_mode_async(
            serial, collector_package, "ACCESS_RESTRICTED_SETTINGS"
        )
        if restricted_mode and restricted_mode != "allow":
            raise AdbError(
                f"restricted settings are not allowed (mode={restricted_mode})"
            )
        steps["accessibility_service"] = _step(
            "ready", preserved_services=[
                value for value in existing.split(":") if value and value != collector_component
            ]
        )
    except AdbError as exc:
        steps["accessibility_service"] = _step(
            "operator_action_required",
            str(exc),
            guidance=(
                "Open Android Settings > Accessibility and enable "
                "ClickClick Accessibility Collector. In the collector app "
                "details, also choose Allow restricted settings when present."
            ),
        )

    client = AccessibilityCollectorClient(serial, authority=collector_authority)
    health: dict[str, Any] = {"ready": False, "reason": "not_checked"}
    if steps["accessibility_service"]["status"] == "ready":
        deadline = asyncio.get_running_loop().time() + max(0.1, health_timeout_s)
        while asyncio.get_running_loop().time() < deadline:
            health = await client.health(timeout=min(2.0, health_timeout_s))
            if health.get("ready"):
                break
            await asyncio.sleep(0.1)
    steps["collector_health"] = (
        _step("ready", **health)
        if health.get("ready")
        else _step("degraded", str(health.get("reason") or "collector not connected"))
    )
    channel = (
        await client.warm(timeout=health_timeout_s)
        if health.get("ready")
        else {"ready": False, "reason": "collector health is unavailable"}
    )
    steps["collector_channel"] = (
        _step("ready", **channel)
        if channel.get("ready")
        else _step("degraded", str(channel.get("reason") or "channel warm failed"))
    )

    if ime_id:
        try:
            previous = await adb.default_ime_async(serial)
            ime_package = ime_id.split("/", 1)[0]
            ime_installed = await adb.package_version_async(serial, ime_package)
            if ime_installed is None and ime_apk_path:
                await adb.install_apk_async(serial, ime_apk_path)
                ime_installed = await adb.package_version_async(serial, ime_package)
            if ime_installed is None:
                steps["test_ime"] = _step(
                    "degraded", "test IME APK is not installed", previous_default=previous
                )
            else:
                await adb.ime_enable_async(serial, ime_id)
                steps["test_ime"] = _step(
                    "ready", previous_default=previous, selected=False
                )
        except AdbError as exc:
            steps["test_ime"] = _step("degraded", str(exc))
    else:
        steps["test_ime"] = _step("degraded", "test IME is not configured")

    if stay_awake_while_plugged:
        try:
            await adb.stay_awake_while_plugged_async(serial, True)
            steps["stay_awake"] = _step("ready", plugged_only=True)
        except AdbError as exc:
            steps["stay_awake"] = _step("degraded", str(exc), plugged_only=True)
    else:
        steps["stay_awake"] = _step("ready", "not requested", plugged_only=True)

    statuses = {value["status"] for value in steps.values()}
    overall: StepStatus
    if "failed" in statuses:
        overall = "failed"
    elif "operator_action_required" in statuses:
        overall = "operator_action_required"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ready"
    return {"serial": serial, "status": overall, "steps": steps}
