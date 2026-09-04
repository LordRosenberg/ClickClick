from __future__ import annotations

import pytest

from driver.environment import initialize_android_device, merge_component
from driver.adb import AdbError


def test_merge_component_is_idempotent_and_preserves_existing_order():
    existing = "a/.One:b/.Two"
    assert merge_component(existing, "c/.Collector") == "a/.One:b/.Two:c/.Collector"
    assert merge_component(existing + ":c/.Collector", "c/.Collector") == (
        "a/.One:b/.Two:c/.Collector"
    )


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
        return "0.2.0" if package == "ai.clickclick.collector" else "0.1.0"
    return None


async def _health(*_args, **_kwargs):
    return {"ready": True, "generation": 1}
