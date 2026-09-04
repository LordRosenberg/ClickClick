"""`get_driver()` factory transport selection."""

import pytest

from driver.android import AndroidDriver
from driver.factory import get_driver
from driver.fixture import FixtureDriver
from agent.driver_client import DriverClient
from shared.config import Settings


def _settings(**overrides) -> Settings:
    base = {"driver_url": "", "platform": "android", "use_fixture_driver": False}
    base.update(overrides)
    return Settings(**base)


def test_get_driver_no_url_returns_android_driver(monkeypatch):
    # Avoid touching real ADB during construction.
    monkeypatch.setattr(
        "driver.factory._build_android_driver",
        lambda _settings=None, *, serial=None: AndroidDriver(serial=serial),
    )
    drv = get_driver(_settings())
    assert isinstance(drv, AndroidDriver)


def test_get_driver_with_url_returns_driver_client():
    drv = get_driver(_settings(driver_url="http://driver.example:8765"))
    assert isinstance(drv, DriverClient)


def test_get_driver_with_url_preserves_requested_serial():
    drv = get_driver(
        _settings(driver_url="http://driver.example:8765"),
        serial="device-123",
    )
    assert isinstance(drv, DriverClient)
    assert drv.serial == "device-123"


def test_get_driver_fixture_overrides_url():
    # Fixture mode wins even when a remote URL is configured.
    drv = get_driver(
        _settings(driver_url="http://driver.example:8765", use_fixture_driver=True)
    )
    assert isinstance(drv, FixtureDriver)


def test_get_driver_fixture_no_url():
    drv = get_driver(_settings(use_fixture_driver=True))
    assert isinstance(drv, FixtureDriver)


def test_get_driver_unknown_platform_raises(monkeypatch):
    with pytest.raises(SystemExit):
        get_driver(_settings(platform="solaris"))
