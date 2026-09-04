"""`DeviceDriver` Protocol runtime checks: every transport implements it."""

from driver.android import AndroidDriver
from driver.fixture import FixtureDriver
from driver.stubs import IOSDriver, WindowsDriver
from agent.driver_client import DriverClient
from shared.protocol import DeviceDriver


def test_android_driver_is_device_driver():
    assert isinstance(AndroidDriver(), DeviceDriver)


def test_fixture_driver_is_device_driver():
    assert isinstance(FixtureDriver(), DeviceDriver)


def test_driver_client_is_device_driver():
    assert isinstance(DriverClient("http://driver.example"), DeviceDriver)


def test_stub_drivers_are_device_driver():
    # Stubs implement the Protocol surface even though their methods raise.
    assert isinstance(WindowsDriver(), DeviceDriver)
    assert isinstance(IOSDriver(), DeviceDriver)
