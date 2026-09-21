"""Idempotent Android device provisioning with explicit degraded outcomes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Literal

from driver import adb
from driver.accessibility import AccessibilityCollectorClient
from driver.adb import AdbError
from driver.collector_release import (
    COLLECTOR_VERSION,
    collector_cache_path,
    download_collector_release,
    file_sha256,
)


StepStatus = Literal["ready", "degraded", "operator_action_required", "failed"]

COLLECTOR_PACKAGE = "ai.clickclick.collector"
COLLECTOR_COMPONENT = "ai.clickclick.collector/.CollectorService"
COLLECTOR_AUTHORITY = "ai.clickclick.collector"


def resolve_collector_apk_path(
    configured: str = "", *, root: Path | None = None,
) -> str:
    """Resolve explicit path, matching local build, then pinned Release cache.

    Local development builds precede Release assets, including same-version
    rebuilds. Downloading is deferred until a device actually needs installation.
    """
    if configured.strip():
        return configured.strip()
    root = root or Path(__file__).resolve().parent.parent
    output_dir = (
        root / "android" / "accessibility-collector" / "app" / "build" /
        "outputs" / "apk" / "debug"
    )
    built = output_dir / "app-debug.apk"
    metadata = output_dir / "output-metadata.json"
    try:
        raw = json.loads(metadata.read_text(encoding="utf-8"))
        versions = {
            str(item.get("versionName") or "")
            for item in raw.get("elements", [])
            if isinstance(item, dict)
        }
    except (OSError, json.JSONDecodeError, TypeError):
        versions = set()
    if built.is_file() and COLLECTOR_VERSION in versions:
        return str(built)
    return str(collector_cache_path(root=root))


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
    path = Path(collector_apk_path) if collector_apk_path else None
    release_path = collector_cache_path()
    release_selected = path is not None and path.resolve() == release_path.resolve()
    if not needs_install and path is not None and path.is_file() and not release_selected:
        # Same versionName does not mean the same development build.
        installed_digest = await adb.package_apk_sha256_async(serial, collector_package)
        needs_install = installed_digest != file_sha256(path)
    if needs_install:
        download_error = ""
        if release_selected:
            try:
                path = await download_collector_release(release_path)
            except Exception as exc:
                download_error = f"{type(exc).__name__}: {exc}"
        if download_error or path is None or not path.is_file():
            steps["collector_apk"] = _step(
                "degraded", download_error or "collector APK is missing; build locally or configure collector_apk_path",
                installed_version=installed,
            )
        else:
            try:
                signature_reinstalled = False
                try:
                    await adb.install_apk_async(serial, path)
                except AdbError as exc:
                    if "INSTALL_FAILED_UPDATE_INCOMPATIBLE" not in str(exc):
                        raise
                    # Collector is intentionally stateless. A debug signing-key
                    # change cannot be upgraded in place, so remove only this
                    # exact package and immediately install the required build.
                    await adb.uninstall_package_async(serial, collector_package)
                    await adb.install_apk_async(serial, path)
                    signature_reinstalled = True
                installed = await adb.package_version_async(serial, collector_package)
                if collector_version and installed not in {collector_version, "installed"}:
                    steps["collector_apk"] = _step(
                        "failed",
                        "installed collector version does not match required version",
                        installed_version=installed,
                        required_version=collector_version,
                    )
                else:
                    steps["collector_apk"] = _step(
                        "ready",
                        installed_version=installed or "installed",
                        signature_reinstalled=signature_reinstalled,
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
