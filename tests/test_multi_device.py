"""multi-device-concurrent: inventory, fan-out create, busy exclusion, pool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from driver.android import AndroidDriver
from driver.pool import FIXTURE_SERIAL, DriverPool
from shared.config import Settings
from shared.db import Database
from shared.schemas import Action, AgentState, ExecutorStepSubmit, TaskStatus
from tests.fake_agents import FakeExecutor


def _settings(**overrides) -> Settings:
    base = {"driver_url": "", "platform": "android", "use_fixture_driver": True}
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_driver_pool_fixture_inventory_and_get():
    pool = DriverPool(_settings())
    inv = await pool.inventory()
    assert len(inv) == 1
    assert inv[0]["serial"] == FIXTURE_SERIAL
    assert inv[0]["key"] == FIXTURE_SERIAL
    assert inv[0]["driver_id"] == "fixture"
    a = pool.get(FIXTURE_SERIAL)
    b = pool.get(FIXTURE_SERIAL)
    assert a is b


@pytest.mark.asyncio
async def test_environment_reconciliation_initializes_once_per_online_period():
    pool = DriverPool(_settings())
    driver = pool.get(FIXTURE_SERIAL)

    first = await pool.reconcile_environments()
    second = await pool.reconcile_environments()

    assert first[FIXTURE_SERIAL]["collector_ready"] is True
    assert second[FIXTURE_SERIAL]["collector_ready"] is True
    assert driver.environment_initializations == 1

    original_inventory = pool.inventory

    async def offline():
        return []

    pool.inventory = offline  # type: ignore[method-assign]
    await pool.reconcile_environments()
    pool.inventory = original_inventory  # type: ignore[method-assign]
    await pool.reconcile_environments()

    assert driver.environment_initializations == 2


@pytest.mark.asyncio
async def test_forced_environment_check_bypasses_success_cache():
    pool = DriverPool(_settings())
    driver = pool.get(FIXTURE_SERIAL)

    await pool.ensure_environment(FIXTURE_SERIAL)
    await pool.ensure_environment(FIXTURE_SERIAL, force=True)

    assert driver.environment_initializations == 2


def test_driver_hubs_merge_urls_json_and_legacy():
    s = _settings(
        use_fixture_driver=False,
        driver_url="http://legacy:8765",
        driver_urls_json='[{"id":"lab-a","url":"http://10.0.0.1:8765"}]',
    )
    hubs = s.driver_hubs()
    assert hubs == [
        {"id": "lab-a", "url": "http://10.0.0.1:8765"},
        {"id": "default", "url": "http://legacy:8765"},
    ]
    # Duplicate URL from legacy is ignored.
    s2 = _settings(
        use_fixture_driver=False,
        driver_url="http://10.0.0.1:8765",
        driver_urls_json='[{"id":"lab-a","url":"http://10.0.0.1:8765"}]',
    )
    assert s2.driver_hubs() == [{"id": "lab-a", "url": "http://10.0.0.1:8765"}]


@pytest.mark.asyncio
async def test_multi_hub_inventory_colliding_serials(monkeypatch: pytest.MonkeyPatch):
    from agent.driver_client import DriverClient

    async def fake_list(self):
        # Same adb serial on two labs — keys must stay distinct.
        if "1111" in self.base_url:
            return [
                {
                    "serial": "emulator-5554",
                    "state": "device",
                    "model": "A",
                    "market_name": "",
                }
            ]
        return [
            {
                "serial": "emulator-5554",
                "state": "device",
                "model": "B",
                "market_name": "",
            }
        ]

    monkeypatch.setattr(DriverClient, "list_devices", fake_list)
    pool = DriverPool(
        _settings(
            use_fixture_driver=False,
            driver_url="",
            driver_urls_json=(
                '[{"id":"lab-a","url":"http://10.0.0.1:1111"},'
                '{"id":"lab-b","url":"http://10.0.0.2:2222"}]'
            ),
        )
    )
    inv = await pool.inventory()
    assert {d["key"] for d in inv} == {"lab-a/emulator-5554", "lab-b/emulator-5554"}
    c_a = pool.get("lab-a/emulator-5554")
    c_b = pool.get("lab-b/emulator-5554")
    assert c_a is not c_b
    assert c_a.serial == "emulator-5554"
    assert "1111" in c_a.base_url
    assert "2222" in c_b.base_url


def test_driver_pool_distinct_android_serials(monkeypatch: pytest.MonkeyPatch):
    created: list[str] = []

    def fake_build(settings, *, serial=None):
        created.append(serial or "")
        drv = AndroidDriver(serial=serial)
        drv._serial = serial  # noqa: SLF001
        return drv

    monkeypatch.setattr("driver.factory._build_android_driver", fake_build)
    pool = DriverPool(_settings(use_fixture_driver=False, driver_url=""))
    d1 = pool.get("S1")
    d2 = pool.get("S2")
    assert d1 is not d2
    assert created == ["S1", "S2"]


def test_db_device_serial_and_busy(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    t1 = db.create_task("a", AgentState(instruction="a"), device_serial="S1")
    assert t1.device_serial == "S1"
    busy = db.busy_serials()
    assert busy["S1"] == t1.id
    db.update_task(t1.id, status=TaskStatus.SUCCEEDED)
    assert "S1" not in db.busy_serials()
    db.close()


def test_db_cancelled_releases_busy_serial(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    t1 = db.create_task("a", AgentState(instruction="a"), device_serial="S1")
    db.update_task(t1.id, status=TaskStatus.RUNNING)
    assert db.busy_serials()["S1"] == t1.id
    db.update_task(t1.id, status=TaskStatus.CANCELLED, failure_reason="cancelled_by_operator")
    assert "S1" not in db.busy_serials()
    db.close()


@pytest.mark.asyncio
async def test_remote_hub_list_devices_and_serial_routing():
    """Driver RPC hub with force_local fixture exposes list_devices + serial RPC."""
    from driver.pool import DriverPool
    from driver.rpc_server import create_driver_app
    from httpx import ASGITransport, AsyncClient as HttpxAsyncClient
    from agent.driver_client import DriverClient

    pool = DriverPool(_settings(use_fixture_driver=True), force_local=True)
    app = create_driver_app(pool=pool)
    transport = ASGITransport(app=app)
    async with HttpxAsyncClient(transport=transport, base_url="http://driver") as http:
        # Wire DriverClient through the same ASGI transport.
        client = DriverClient("http://driver", transport=transport)
        devices = await client.list_devices()
        assert len(devices) == 1
        assert devices[0]["serial"] == FIXTURE_SERIAL

        bound = DriverClient("http://driver", serial=FIXTURE_SERIAL, transport=transport)
        health = await bound.health()
        assert health.get("ok") is True
        assert health.get("serial") == FIXTURE_SERIAL

        frame = await bound.get_frame()
        assert isinstance(frame[0], dict)
        assert isinstance(frame[1], (bytes, bytearray))

        from driver.observation_deadline import ObservationDeadline

        tree, pixels, metadata = await bound.capture_deadline_frame(
            ObservationDeadline("current", 500)
        )
        assert isinstance(tree, dict)
        assert isinstance(pixels, (bytes, bytearray))
        assert metadata["provider"] == "remote_get_frame"

        identity = await bound.current_foreground_identity(timeout_s=0.5)
        assert identity["package"] == "com.example.demo"
