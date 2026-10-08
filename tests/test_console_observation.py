"""Console viewing cannot create, reset or borrow task video resources."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from driver.fixture import FixtureDriver
from driver.rpc_server import create_driver_app
from driver.scrcpy_stream import ScrcpyRegistry
from tests.test_scrcpy_stream import _PushSource
from tests.test_task_pause import app  # noqa: F401 - isolated API fixture


async def test_removed_routes_leave_active_observation_lease_untouched(app, monkeypatch):
    source = _PushSource("fixture")
    registry = ScrcpyRegistry()
    registry.set_source_factory(lambda _: source)
    session, lease = await registry.acquire("fixture", "agent")
    await asyncio.sleep(0)
    before = session.metrics()
    monkeypatch.setattr("control_api.main.OBSERVATION_STREAMS", registry)
    monkeypatch.setattr("driver.rpc_server.OBSERVATION_STREAMS", registry)

    def forbidden(*args, **kwargs):
        pytest.fail("Console touched the task stream registry")

    monkeypatch.setattr(registry, "get", forbidden)
    monkeypatch.setattr(registry, "acquire", forbidden)
    hub = create_driver_app(FixtureDriver())
    try:
        for application, paths, ws_path in (
            (app, ["/api/device/scrcpy", "/api/device/mirror/stream"], "/api/device/mirror/stream"),
            (hub, ["/mirror/stream"], "/mirror/stream"),
        ):
            assert not any(getattr(route, "path", None) in paths for route in application.routes)
            assert not any(getattr(route, "path", None) == ws_path for route in application.routes)
            async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as client:
                for path in paths:
                    for _ in range(2):
                        response = await client.get(path, params={"device_key": "fixture"})
                        assert response.status_code == 404

        task = app.state.db.create_task("saved image", device_serial="fixture")
        image = b"\x89PNG\r\n\x1a\nsaved-task-image"
        ref = app.state.artifacts.save_bytes("som", image, ".png")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/tasks/{task.id}/artifacts/{ref}")
            assert response.status_code == 200
            assert response.content == image
        assert session.metrics() == before
        assert source.started == 1 and source.stopped == 0
    finally:
        await registry.release("fixture", lease)
        await registry.shutdown()
