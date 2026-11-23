"""Installed Android package build evidence and optional Driver RPC."""

import httpx
import pytest

from agent.driver_client import DriverClient
from driver import adb
from driver.android import AndroidDriver
from driver.fixture import FixtureDriver
from driver.rpc_server import create_driver_app


APP = "com.example.demo"


@pytest.mark.asyncio
async def test_package_build_reports_active_version_code_through_rpc(monkeypatch):
    async def shell(serial, args, *, timeout):
        assert serial == "device-1"
        assert args == ["dumpsys", "package", APP]
        assert timeout == 2.0
        return b"""Packages:
  Package [com.example.demo] (5e7ac9c):
    versionCode=0002147483700 minSdk=23 targetSdk=34
    versionName=4.3 beta
Hidden system packages:
  Package [com.example.demo] (1efac29):
    versionCode=7 minSdk=23 targetSdk=34
    versionName=old
"""

    monkeypatch.setattr(adb, "shell_async", shell)
    app = create_driver_app(AndroidDriver(serial="device-1"))
    client = DriverClient("http://driver.test", transport=httpx.ASGITransport(app=app))

    assert await client.read_app_build(APP) == {
        "package": APP,
        "version_code": "2147483700",
        "version_name": "4.3 beta",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dump",
    [
        b"Unable to find package: com.example.demo\n",
        b"Packages:\n  Package [com.example.other]:\n    versionCode=22\n",
        b"Packages:\n  Package [com.example.demo]:\n    versionName=old\n",
        b"Packages:\n  Package [com.example.demo]:\n    versionCode=unknown\n",
    ],
)
async def test_missing_or_malformed_package_build_is_unknown(monkeypatch, dump):
    async def shell(*args, **kwargs):
        return dump

    monkeypatch.setattr(adb, "shell_async", shell)
    assert await adb.read_app_build("device-1", APP) is None


@pytest.mark.asyncio
async def test_missing_version_name_keeps_known_build(monkeypatch):
    async def shell(*args, **kwargs):
        return b"Packages:\n  Package [com.example.demo]:\n    versionCode=22\n    versionName=null\n"

    monkeypatch.setattr(adb, "shell_async", shell)
    assert await adb.read_app_build("device-1", APP) == {
        "package": APP,
        "version_code": "22",
        "version_name": None,
    }


@pytest.mark.asyncio
async def test_unbound_or_invalid_build_query_never_calls_adb(monkeypatch):
    async def unexpected_shell(*args, **kwargs):
        pytest.fail("ADB must be scoped to one bound device and exact package")

    monkeypatch.setattr(adb, "shell_async", unexpected_shell)
    assert await AndroidDriver().read_app_build(APP) is None
    assert await AndroidDriver(serial="device-1").read_app_build("--help") is None
    assert await AndroidDriver(serial="device-1").read_app_build("com.example.demo;") is None


@pytest.mark.asyncio
async def test_adb_timeout_is_unknown(monkeypatch):
    async def shell(*args, **kwargs):
        raise adb.AdbTimeoutError("device stalled")

    monkeypatch.setattr(adb, "shell_async", shell)
    assert await adb.read_app_build("device-1", APP) is None


@pytest.mark.asyncio
async def test_optional_rpc_supports_fixture_and_older_servers():
    app = create_driver_app(FixtureDriver())
    client = DriverClient("http://driver.test", transport=httpx.ASGITransport(app=app))
    assert await client.read_app_build(APP) is None

    older = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"id": "1", "error": "unknown method"},
    ))
    client = DriverClient("http://driver.test", transport=older)
    assert await client.read_app_build(APP) is None
