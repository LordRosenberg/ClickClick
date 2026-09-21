from __future__ import annotations

import pytest

from driver.environment import (
    initialize_android_device,
    merge_component,
    resolve_collector_apk_path,
)
from driver.adb import AdbError


def test_merge_component_is_idempotent_and_preserves_existing_order():
    existing = "a/.One:b/.Two"
    assert merge_component(existing, "c/.Collector") == "a/.One:b/.Two:c/.Collector"
    assert merge_component(existing + ":c/.Collector", "c/.Collector") == (
        "a/.One:b/.Two:c/.Collector"
    )


def test_collector_apk_resolution_rejects_stale_debug_build(tmp_path):
    output = (
        tmp_path / "android/accessibility-collector/app/build/outputs/apk/debug"
    )
    output.mkdir(parents=True)
    (output / "app-debug.apk").write_bytes(b"old")
    (output / "output-metadata.json").write_text(
        '{"elements":[{"versionName":"0.2.0"}]}', encoding="utf-8",
    )

    resolved = resolve_collector_apk_path(root=tmp_path)

    assert resolved == str(tmp_path / "data/device-apks/clickclick-collector-0.4.5-debug.apk")


def test_collector_apk_resolution_accepts_matching_debug_build(tmp_path):
    output = (
        tmp_path / "android/accessibility-collector/app/build/outputs/apk/debug"
    )
    output.mkdir(parents=True)
    built = output / "app-debug.apk"
    built.write_bytes(b"new")
    (output / "output-metadata.json").write_text(
        '{"elements":[{"versionName":"0.4.5"}]}', encoding="utf-8",
    )

    assert resolve_collector_apk_path(root=tmp_path) == str(built)


@pytest.mark.asyncio
async def test_initialization_preserves_services_and_does_not_select_ime(monkeypatch):
    settings = {
        ("secure", "enabled_accessibility_services"): "a/.One:b/.Two",
        ("secure", "default_input_method"): "system/.Ime",
    }
    writes: list[tuple[str, str, str]] = []
    ime_enabled: list[str] = []

    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", _package_version)

    async def get(_serial, namespace, key, *, timeout=5.0):
        return settings.get((namespace, key), "")

    async def put(_serial, namespace, key, value, *, timeout=5.0):
        writes.append((namespace, key, value))
        settings[(namespace, key)] = value

    async def enable(_serial, ime_id):
        ime_enabled.append(ime_id)

    monkeypatch.setattr("driver.environment.adb.setting_get_async", get)
    monkeypatch.setattr("driver.environment.adb.setting_put_async", put)
    monkeypatch.setattr("driver.environment.adb.default_ime_async", _return("system/.Ime"))
    monkeypatch.setattr("driver.environment.adb.ime_enable_async", enable)
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("allow"))
    monkeypatch.setattr("driver.environment.AccessibilityCollectorClient.health", _health)
    monkeypatch.setattr(
        "driver.environment.AccessibilityCollectorClient.warm",
        _health,
    )

    result = await initialize_android_device(
        "S",
        collector_package="ai.clickclick.collector",
        collector_component="ai.clickclick.collector/.CollectorService",
        collector_authority="ai.clickclick.collector",
        ime_id="com.android.adbkeyboard/.AdbIME",
    )
    assert result["status"] == "ready"
    merged = next(value for ns, key, value in writes if key == "enabled_accessibility_services")
    assert merged == "a/.One:b/.Two:ai.clickclick.collector/.CollectorService"
    assert ime_enabled == ["com.android.adbkeyboard/.AdbIME"]
    assert result["steps"]["test_ime"]["previous_default"] == "system/.Ime"
    assert result["steps"]["test_ime"]["selected"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("old_version, digest, expected_installs", [("0.2.0", None, 1), ("0.4.5", None, 1), ("0.4.5", "different", 1), ("0.4.5", "same", 0)])
async def test_initialization_upgrades_mismatched_collector_apk(tmp_path, monkeypatch, old_version, digest, expected_installs):
    apk = tmp_path / "collector.apk"
    apk.write_bytes(b"apk")
    versions = iter([old_version, "0.4.5"])
    from driver.collector_release import file_sha256
    monkeypatch.setattr("driver.environment.adb.package_apk_sha256_async", _return(file_sha256(apk) if digest == "same" else digest))
    installed = []
    settings = {("secure", "enabled_accessibility_services"): ""}

    async def package_version(_serial, package):
        return next(versions) if package == "ai.clickclick.collector" else None

    async def install(_serial, path):
        installed.append(path)

    async def get(_serial, namespace, key, *, timeout=5.0):
        return settings.get((namespace, key), "")

    async def put(_serial, namespace, key, value, *, timeout=5.0):
        settings[(namespace, key)] = value

    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", package_version)
    monkeypatch.setattr("driver.environment.adb.install_apk_async", install)
    monkeypatch.setattr("driver.environment.adb.setting_get_async", get)
    monkeypatch.setattr("driver.environment.adb.setting_put_async", put)
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("allow"))
    monkeypatch.setattr("driver.environment.AccessibilityCollectorClient.health", _health)
    monkeypatch.setattr("driver.environment.AccessibilityCollectorClient.warm", _health)

    result = await initialize_android_device("S", collector_apk_path=str(apk))

    assert installed == [apk] * expected_installs
    assert result["steps"]["collector_apk"]["status"] == "ready"
    assert result["steps"]["collector_apk"]["installed_version"] == "0.4.5"


@pytest.mark.asyncio
async def test_initialization_recovers_from_collector_signature_mismatch(
    tmp_path, monkeypatch,
):
    apk = tmp_path / "collector.apk"
    apk.write_bytes(b"apk")
    versions = iter(["0.2.0", "0.4.5"])
    installs: list[object] = []
    uninstalls: list[str] = []
    settings = {("secure", "enabled_accessibility_services"): ""}

    async def package_version(_serial, package):
        return next(versions) if package == "ai.clickclick.collector" else None

    async def install(_serial, path):
        installs.append(path)
        if len(installs) == 1:
            raise AdbError("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]")

    async def uninstall(_serial, package):
        uninstalls.append(package)

    async def get(_serial, namespace, key, *, timeout=5.0):
        return settings.get((namespace, key), "")

    async def put(_serial, namespace, key, value, *, timeout=5.0):
        settings[(namespace, key)] = value

    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", package_version)
    monkeypatch.setattr("driver.environment.adb.install_apk_async", install)
    monkeypatch.setattr("driver.environment.adb.uninstall_package_async", uninstall)
    monkeypatch.setattr("driver.environment.adb.setting_get_async", get)
    monkeypatch.setattr("driver.environment.adb.setting_put_async", put)
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("allow"))
    monkeypatch.setattr("driver.environment.AccessibilityCollectorClient.health", _health)
    monkeypatch.setattr("driver.environment.AccessibilityCollectorClient.warm", _health)

    result = await initialize_android_device("S", collector_apk_path=str(apk))

    assert installs == [apk, apk]
    assert uninstalls == ["ai.clickclick.collector"]
    assert result["steps"]["collector_apk"] == {
        "status": "ready",
        "reason": "",
        "installed_version": "0.4.5",
        "signature_reinstalled": True,
    }


@pytest.mark.asyncio
async def test_initialization_does_not_uninstall_for_unrelated_install_failure(
    tmp_path, monkeypatch,
):
    apk = tmp_path / "collector.apk"
    apk.write_bytes(b"apk")
    uninstalls: list[str] = []

    async def install(_serial, _path):
        raise AdbError("device offline")

    async def uninstall(_serial, package):
        uninstalls.append(package)

    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", _return("0.2.0"))
    monkeypatch.setattr("driver.environment.adb.install_apk_async", install)
    monkeypatch.setattr("driver.environment.adb.uninstall_package_async", uninstall)
    monkeypatch.setattr("driver.environment.adb.setting_get_async", _return(""))
    monkeypatch.setattr("driver.environment.adb.setting_put_async", _return(None))
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("allow"))

    result = await initialize_android_device("S", collector_apk_path=str(apk))

    assert uninstalls == []
    assert result["steps"]["collector_apk"]["status"] == "failed"


@pytest.mark.asyncio
async def test_blocked_accessibility_setting_requires_operator(monkeypatch):
    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", _package_version)
    monkeypatch.setattr(
        "driver.environment.adb.setting_get_async", _return("a/.One")
    )
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("default"))

    async def blocked(*_args, **_kwargs):
        raise AdbError("restricted setting")

    monkeypatch.setattr("driver.environment.adb.setting_put_async", blocked)
    result = await initialize_android_device(
        "S",
        collector_package="ai.clickclick.collector",
        collector_component="ai.clickclick.collector/.CollectorService",
        collector_authority="ai.clickclick.collector",
    )
    assert result["status"] == "operator_action_required"
    assert result["steps"]["accessibility_service"]["status"] == "operator_action_required"


@pytest.mark.asyncio
async def test_restricted_settings_appop_requires_operator(monkeypatch):
    settings = {("secure", "enabled_accessibility_services"): "a/.One"}
    monkeypatch.setattr("driver.environment.adb.list_device_serials_async", _return(["S"]))
    monkeypatch.setattr("driver.environment.adb.package_version_async", _package_version)

    async def get(_serial, namespace, key, *, timeout=5.0):
        return settings.get((namespace, key), "")

    async def put(_serial, namespace, key, value, *, timeout=5.0):
        settings[(namespace, key)] = value

    monkeypatch.setattr("driver.environment.adb.setting_get_async", get)
    monkeypatch.setattr("driver.environment.adb.setting_put_async", put)
    monkeypatch.setattr("driver.environment.adb.appop_mode_async", _return("default"))

    result = await initialize_android_device(
        "S",
        collector_package="ai.clickclick.collector",
        collector_component="ai.clickclick.collector/.CollectorService",
        collector_authority="ai.clickclick.collector",
    )

    step = result["steps"]["accessibility_service"]
    assert step["status"] == "operator_action_required"
    assert "restricted settings" in step["reason"]
    assert "Allow restricted settings" in step["guidance"]


def _return(value):
    async def inner(*_args, **_kwargs):
        return value
    return inner


async def _package_version(_serial, package):
    if package in {"ai.clickclick.collector", "com.android.adbkeyboard"}:
        return "0.4.5" if package == "ai.clickclick.collector" else "0.1.0"
    return None


async def _health(*_args, **_kwargs):
    return {"ready": True, "generation": 1}
