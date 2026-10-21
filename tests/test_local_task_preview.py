"""Preview must be passive, task-bound and optional to all original business I/O."""
import asyncio
import io
import json
from types import SimpleNamespace
import time

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from control_api.preview import FRAME_HEADER, PreviewBudget, PreviewImageResponse
from driver.android import AndroidDriver
from driver.scrcpy_observation import FrameGeometry, FrameHandle, ScrcpyObservationProvider
from shared.schemas import TaskStatus
from tests.test_task_pause import app  # noqa: F401

ORIGIN = "http://127.0.0.1:8080"
HEADERS = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(autouse=True)
def local_origin(monkeypatch):
    # Do not inherit the developer's externally bound backend configuration.
    monkeypatch.setenv("CLICKCLICK_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKCLICK_API_PORT", "8080")


def png(color="blue"):
    out = io.BytesIO()
    Image.new("RGB", (32, 64), color).save(out, "PNG")
    return out.getvalue()


def provider():
    result = ScrcpyObservationProvider(None, "S")
    result._session = SimpleNamespace(generation=1, is_alive=lambda: True)
    result._task = SimpleNamespace(done=lambda: False)
    result.decoder.status = "healthy"
    return result


def publish(source, color="blue"):
    stamp = time.monotonic()
    geometry = FrameGeometry(32, 64, 64, 128, timestamp=stamp, generation=source.generation)
    return source.ring.append(FrameHandle(png(color), stamp, source.generation, geometry))


def forbidden(*args, **kwargs):
    raise AssertionError("preview crossed the observation/device boundary")


def test_passive_read_cannot_flush_pending_or_change_observation(monkeypatch):
    source = provider()
    frame = publish(source)
    source.decoder._pending = object()
    original = (source.ring._bytes, tuple(source.ring._frames), dict(source.metrics))
    for method in ("current", "_publish_pending", "start", "frame_boundary", "await_fresh_frame"):
        monkeypatch.setattr(source, method, forbidden)
    monkeypatch.setattr(source.decoder, "publish_pending", forbidden)
    for _ in range(10):
        assert source.peek_published_frame().frame is frame
        assert source.published_cursor() == (1, frame.frame_id)
    assert source.decoder._pending is not None
    assert (source.ring._bytes, tuple(source.ring._frames), source.metrics) == original
    source.mark_action_complete((1, frame.frame_id))
    assert source.peek_published_frame().detail == "post_action_frame_pending"
    newer = publish(source, "red")
    assert source.peek_published_frame().frame is newer
    source._session.generation = 2
    assert source.peek_published_frame().frame is None


async def test_task_binding_drops_previous_run_and_failure_does_not_block_cleanup(monkeypatch):
    source = provider()
    publish(source)
    driver = AndroidDriver(serial="S", stream_provider=source)
    assert (await driver.begin_task_session("A"))["status"] == "disabled"
    assert driver.peek_task_preview("A")["frame"] is None
    first = publish(source)
    old_epoch = driver.peek_task_preview("A")["run_epoch"]
    assert driver.peek_task_preview("A")["frame"] is first
    assert driver.peek_task_preview("B")["frame"] is None
    calls = []
    async def end():
        calls.append("end")
        return {"status": "released"}
    monkeypatch.setattr(driver._task_power_lease, "end", end)
    monkeypatch.setattr(driver, "_clear_task_preview", forbidden)
    assert (await driver.end_task_session("A"))["status"] == "released"
    assert calls == ["end"] and driver.peek_task_preview("A")["frame"] is None
    await driver.begin_task_session("A")
    assert driver.peek_task_preview("A")["frame"] is None
    publish(source)
    assert driver.peek_task_preview("A")["run_epoch"] != old_epoch
    monkeypatch.setattr(driver, "_bind_task_preview", forbidden)
    assert (await driver.begin_task_session("B"))["status"] == "disabled"
    assert driver.peek_task_preview("A")["frame"] is None
    assert driver.peek_task_preview("B")["frame"] is None


@pytest.fixture
async def live(app, monkeypatch):
    settings, pool = app.state.settings, app.state.driver_pool
    settings.use_fixture_driver = False
    settings.driver_url = ""
    settings.driver_urls_json = "[]"
    settings.platform = "android"
    source = provider()
    driver = AndroidDriver(serial="S", stream_provider=source)
    task = app.state.db.create_task("preview test", device_serial="S")
    app.state.db.update_task(task.id, status=TaskStatus.RUNNING)
    await driver.begin_task_session(task.id)
    publish(source)
    pool._drivers["S"] = driver
    worker = asyncio.create_task(asyncio.Event().wait())
    app.state.running_tasks[task.id] = worker
    monkeypatch.setattr(pool, "get", forbidden)
    for name in ("screenshot", "get_frame", "get_ui_state", "act"):
        monkeypatch.setattr(driver, name, forbidden)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN, headers=HEADERS) as client:
        yield SimpleNamespace(app=app, client=client, source=source, driver=driver,
                              task=task, root=f"/api/tasks/{task.id}/preview")
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    app.state.running_tasks.pop(task.id, None)


async def test_atomic_latest_frame_progresses_instead_of_repeated_409(live):
    v = live
    last = -1
    for color in ("red", "green", "blue"):
        result = await v.client.get(v.root + "/state")
        assert result.status_code == 200, result.text
        state = result.json()
        advertised = state["frame"]
        new = publish(v.source, color)
        response = await v.client.get(v.root + "/frame", params={
            "run_epoch": advertised["run_epoch"], "generation": advertised["provider_generation"],
            "after_frame_id": last,
        })
        assert response.status_code == 200
        actual = json.loads(response.headers[FRAME_HEADER])
        assert response.content == new.data
        assert actual["frame_id"] == new.frame_id > advertised["frame_id"]
        assert actual["geometry"] == json.loads(json.dumps(new.geometry.__dict__))
        assert response.headers["cache-control"] == "no-store"
        last = actual["frame_id"]
    response = await v.client.get(v.root + "/frame", params={
        "run_epoch": advertised["run_epoch"], "generation": 1, "after_frame_id": last,
    })
    assert response.status_code == 204 and not response.content
    assert v.app.state.task_preview.budget.entries[v.task.id].inflight == 0


@pytest.mark.parametrize("status", list(TaskStatus))
async def test_only_running_task_can_preview(live, status):
    live.app.state.db.update_task(live.task.id, status=status)
    result = (await live.client.get(live.root + "/state")).json()
    assert (result["preview_state"] == "available") == (status == TaskStatus.RUNNING)
    if status != TaskStatus.RUNNING:
        assert result["frame"] is None


async def test_old_task_epoch_generation_and_action_boundary_cannot_leak(live):
    v = live
    state = (await v.client.get(v.root + "/state")).json()["frame"]
    args = {"run_epoch": state["run_epoch"], "generation": 1}
    v.source.mark_action_complete((1, state["frame_id"]))
    assert (await v.client.get(v.root + "/frame", params=args)).status_code == 409
    publish(v.source)
    assert (await v.client.get(v.root + "/frame", params=args)).status_code == 200
    await v.driver.begin_task_session("different-task")
    publish(v.source, "red")
    assert (await v.client.get(v.root + "/frame", params=args)).status_code == 409
    await v.driver.begin_task_session(v.task.id)
    publish(v.source)
    assert (await v.client.get(v.root + "/frame", params=args)).status_code == 409


@pytest.mark.parametrize("metadata", [None, "cross-site", "same-site", "none"])
async def test_browser_origin_checks_do_not_depend_on_http_mcp(live, metadata):
    headers = {"Sec-Fetch-Site": metadata} if metadata is not None else {}
    async with AsyncClient(transport=ASGITransport(app=live.app), base_url=ORIGIN) as client:
        assert (await client.get(live.root + "/state", headers=headers)).status_code == 403
    assert (await live.client.get(live.root + "/state", headers={"Origin": "https://evil.test"})).status_code == 403
    assert (await live.client.get(live.root + "/state", headers=[("Host", "127.0.0.1:8080"), ("Host", "evil.test")])).status_code == 403


async def test_preview_read_and_serializer_failure_are_bounded(live, monkeypatch):
    state = (await live.client.get(live.root + "/state")).json()["frame"]
    monkeypatch.setattr(live.app.state.task_preview, "encode_descriptor", forbidden)
    response = await live.client.get(live.root + "/state")
    assert response.status_code == 200 and response.json()["reason"] == "preview_internal_error"
    response = await live.client.get(live.root + "/frame", params={"run_epoch": state["run_epoch"], "generation": 1})
    assert response.status_code == 503 and FRAME_HEADER not in response.headers
    assert live.app.state.task_preview.budget.entries[live.task.id].inflight == 0
    assert live.app.state.db.get_task(live.task.id).status == TaskStatus.RUNNING
    assert not live.app.state.running_tasks[live.task.id].done()


def test_budgets_bound_slow_viewers_records_and_request_rate():
    clock = [0.0]
    budget = PreviewBudget(clock=lambda: clock[0], capacity=2, idle_seconds=3)
    a = budget.admit("A", "frame")
    assert a is not None and budget.admit("A", "frame") is a
    assert budget.admit("A", "frame") is None
    b = budget.admit("B", "frame")
    assert b is not None and budget.admit("C", "state") is None
    a.inflight = b.inflight = 0
    for _ in range(8):
        assert budget.admit("A", "state") is not None
    assert budget.admit("A", "state") is None
    clock[0] = 5
    assert budget.admit("C", "state") is not None
    assert list(budget.entries) == ["C"]


@pytest.mark.parametrize("disconnect", [False, True])
async def test_slow_send_or_disconnect_releases_only_http_resources(disconnect):
    budget = PreviewBudget().admit("A", "frame")
    response = PreviewImageResponse(b"png", "{}", budget)
    response.send_timeout = .01
    gate = asyncio.Event()
    async def receive():
        if disconnect:
            return {"type": "http.disconnect"}
        await gate.wait()
    async def send(_message):
        await gate.wait()
    await response({"type": "http"}, receive, send)
    assert budget.inflight == 0 and response.body == b""


async def test_cursor_errors_and_snapshot_survives_later_publication(live, monkeypatch):
    v = live
    f = (await v.client.get(v.root + "/state")).json()["frame"]
    args = {"run_epoch": f["run_epoch"], "generation": 1}
    assert (await v.client.get(v.root + "/frame")).status_code == 400
    assert (await v.client.get(v.root + "/frame", params={**args, "generation": "x"})).status_code == 400
    assert (await v.client.get(v.root + "/frame", params={**args, "after_frame_id": 9999})).status_code == 409
    assert (await v.client.get(v.root + "/frame", params={**args, "generation": 2})).status_code == 409
    v.app.state.task_preview.budget.clear()
    assert (await v.client.get("/api/tasks/missing/preview/state")).status_code == 404
    assert (await v.client.get("/api/tasks/missing/preview/frame", params=args)).status_code == 404
    snapshot = v.app.state.task_preview.snapshot
    selected = v.source.peek_published_frame().frame
    def advancing(task_id):
        result = snapshot(task_id)
        publish(v.source, "red")
        return result
    monkeypatch.setattr(v.app.state.task_preview, "snapshot", advancing)
    response = await v.client.get(v.root + "/frame", params=args)
    assert response.status_code == 200 and response.content == selected.data
    assert json.loads(response.headers[FRAME_HEADER])["frame_id"] == selected.frame_id
    assert v.source.peek_published_frame().frame.frame_id > selected.frame_id


@pytest.mark.parametrize("mode,reason", [("fixture", "unsupported_fixture"),
    ("remote", "unsupported_remote"), ("ios", "unsupported_platform")])
async def test_unsupported_transport_never_inspects_driver(live, monkeypatch, mode, reason):
    monkeypatch.setattr(type(live.app.state.driver_pool), "mode", property(lambda _: mode))
    monkeypatch.setattr(live.app.state.driver_pool, "peek", forbidden)
    assert (await live.client.get(live.root + "/state")).json()["reason"] == reason


async def test_orphan_missing_cached_driver_decoder_and_remote_identity(live):
    v = live
    handle = v.app.state.running_tasks.pop(v.task.id)
    assert (await v.client.get(v.root + "/state")).json()["reason"] == "owner_unavailable"
    v.app.state.running_tasks[v.task.id] = handle
    driver = v.app.state.driver_pool._drivers.pop("S")
    assert (await v.client.get(v.root + "/state")).json()["frame"] is None
    assert not v.app.state.driver_pool._drivers
    v.app.state.driver_pool._drivers["S"] = driver
    v.source.decoder.status = "unavailable"
    assert (await v.client.get(v.root + "/state")).json()["reason"] == "decoder_unavailable"
    v.source.decoder.status = "healthy"
    remote = v.app.state.db.create_task("remote identity", device_serial="hub/S")
    path = f"/api/tasks/{remote.id}/preview/state"
    assert (await v.client.get(path)).json()["reason"] == "unsupported_remote"


@pytest.mark.parametrize("enabled", [False, True])
async def test_boundary_with_actual_http_mcp_configuration(tmp_path, monkeypatch, enabled):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_MCP_HTTP_ENABLED", str(enabled).lower())
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            path = "/api/tasks/missing/preview/state"
            assert (await client.get(path)).status_code == 403
            assert (await client.get(path, headers=HEADERS)).status_code == 404
            for name in ("Origin", "Sec-Fetch-Site", "Authorization"):
                headers = [("Sec-Fetch-Site", "same-origin"), (name, "x"), (name, "x")]
                assert (await client.get(path, headers=headers)).status_code == 403
            assert (await client.get(path, headers={**HEADERS, "Origin": "http://localhost:8080"})).status_code == 403
            assert (await client.get(path, headers={**HEADERS, "Host": "localhost:8080"})).status_code == 403
            page = await client.get("/tasks/missing/preview")
            if page.status_code == 200:
                assert page.headers["x-frame-options"] == "DENY"
                assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
                assert page.headers["cache-control"] == "no-store"
    finally:
        app.state.db.close()


async def test_preview_exception_cleanup_does_not_skip_provider_or_power_lease(monkeypatch):
    driver = AndroidDriver(serial="S", stream_provider=provider())
    calls = []
    async def record(label):
        calls.append(label)
    monkeypatch.setattr(driver, "_clear_task_preview", forbidden)
    monkeypatch.setattr(driver._task_power_lease, "end", lambda: record("power"))
    monkeypatch.setattr(driver._collector, "close", lambda: record("collector"))
    monkeypatch.setattr(driver._stream_provider, "close", lambda: record("provider"))
    await driver.close_observation_provider()
    assert calls == ["power", "collector", "provider"]
    async def cancelled():
        raise asyncio.CancelledError
    monkeypatch.setattr(driver._task_power_lease, "end", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await driver.close_observation_provider()


@pytest.mark.parametrize("phase", ["mount", "boundary"])
async def test_initialization_failure_rolls_back_only_preview_routes(tmp_path, monkeypatch, phase):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    from control_api.main import create_app
    def broken(app, **kwargs):
        app.add_api_route("/api/preview-partial", lambda: {})
        app.add_middleware(type("BrokenMiddleware", (), {}))
        raise RuntimeError("optional preview failed")
    if phase == "mount":
        monkeypatch.setattr("control_api.preview.mount_preview_api", broken)
    else:
        monkeypatch.setattr("control_api.preview.PreviewBoundary", forbidden)
    app = create_app(include_temp_runs=False)
    try:
        assert app.state.task_preview is None
        task = app.state.db.create_task("original business", device_serial="S")
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            assert (await client.get("/api/preview-partial")).status_code == 404
            result = (await client.get(f"/api/assistant/tasks/{task.id}")).json()
            assert result["task_id"] == task.id and result["status"] == "queued"
            assert result["preview_url"] is None
            assert result["preview_support"]["reason"] == "preview_unavailable"
    finally:
        app.state.db.close()


@pytest.mark.parametrize("fault", ["compute", "serialize"])
async def test_preview_projection_failure_preserves_submission_replay_and_business_errors(app, monkeypatch, fault):
    from control_api import preview
    gate, starts = asyncio.Event(), []
    async def run(tid):
        starts.append(tid)
        await gate.wait()
    app.state.orchestrator.run_task = run
    app.state.settings.models_json = '{"test":{"provider":"openai","api_key":"secret-test-only"}}'
    app.state.settings.default_model = "test"
    app.state.settings.manager_model = app.state.settings.executor_model = ""
    original = preview.preview_projection
    from control_api.mcp import create_server
    server = create_server(ORIGIN, client_factory=lambda: AsyncClient(
        transport=ASGITransport(app=app), base_url=ORIGIN))
    original_tools = {tool.name for tool in await server.list_tools()}
    assert {"get_status", "start_task", "get_task", "list_tasks", "get_task_snapshot",
            "cancel_task", "pause_task", "resume_task"} <= original_tools
    if fault == "compute":
        monkeypatch.setattr(preview, "preview_projection", forbidden)
    else:
        monkeypatch.setattr(preview, "preview_projection", lambda *a, **k: {"preview_url": object()})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
            body = {"instruction": "goal", "request_key": "preview-fault", "device_ids": ["fixture"]}
            response = await client.post("/api/assistant/tasks", json=body)
            assert response.status_code == 200
            task = response.json()["tasks"][0]
            tid = task["task_id"]
            assert task["preview_support"]["reason"] == "preview_metadata_error"
            assert task["preview_url"] is None and "secret-test-only" not in response.text
            replay = await client.post("/api/assistant/tasks", json=body)
            assert replay.json()["tasks"][0]["task_id"] == tid
            await asyncio.sleep(0)
            assert starts == [tid]
            assert (await client.get(f"/api/assistant/tasks/{tid}")).json()["task_id"] == tid
            assert (await client.get("/api/assistant/tasks")).json()["tasks"][0]["task_id"] == tid
            assert (await client.post("/api/assistant/tasks", json={**body, "instruction": "different"})).status_code == 409
            assert (await client.post("/api/assistant/tasks", json={**body, "instruction": ""})).status_code == 422
            assert (await client.get("/api/assistant/tasks/missing")).status_code == 404
            assert (await client.get("/api/setup/settings")).status_code == 401
            # A mixed batch isolates enrichment failure to its own member.
            healthy = app.state.db.create_task("healthy", device_serial="S")
            monkeypatch.setattr(preview, "preview_projection", lambda t, **kw:
                forbidden() if t.id == tid else original(t, **kw))
            mixed = (await server.call_tool("get_task", {"task_ids": [tid, healthy.id, "missing"]})).structuredContent
            assert mixed["tasks"][0]["preview_support"]["reason"] == "preview_metadata_error"
            assert mixed["tasks"][1]["preview_support"]["reason"] != "preview_metadata_error"
            assert mixed["tasks"][2]["error"]["code"] == 404
            # Controls keep their original result when their extra projection fails.
            for operation in ("pause_task", "resume_task", "cancel_task"):
                monkeypatch.setattr(preview, "preview_projection", forbidden)
                output = await server.call_tool(operation, {"task_ids": healthy.id})
                member = output.structuredContent["tasks"][0]
                assert "control" in member
                assert member["preview_support"]["reason"] == "preview_metadata_error"
            assert {t.name for t in await server.list_tools()} == original_tools
            from shared.schemas import AgentState
            expired_state = AgentState(instruction="expired")
            expired_state.revisable.checkpoint_version = 1
            expired_state.revisable.limits.deadline_at = time.time()-1
            expired = app.state.db.create_task("expired", expired_state, device_serial="expired-device")
            app.state.db.update_task(expired.id, status=TaskStatus.PAUSED)
            result = await server.call_tool("resume_task", {"task_ids": expired.id})
            member = result.structuredContent["tasks"][0]
            # Compare the current business path with preview working, so an
            # independent change to budget recovery is not frozen by this test.
            monkeypatch.setattr(preview, "preview_projection", original)
            baseline = app.state.db.create_task("expired baseline", expired_state.model_copy(deep=True),
                                                device_serial="expired-baseline-device")
            app.state.db.update_task(baseline.id, status=TaskStatus.PAUSED)
            baseline_output = await server.call_tool("resume_task", {"task_ids": baseline.id})
            baseline_member = baseline_output.structuredContent["tasks"][0]
            for field in ("status", "failure_reason", "stop_reason", "budget_kind"):
                assert member.get(field) == baseline_member.get(field)
            assert {k: v for k, v in member["control"].items() if k != "task_id"} == {
                k: v for k, v in baseline_member["control"].items() if k != "task_id"}
            monkeypatch.setattr(preview, "preview_projection", forbidden)
            assert member["preview_support"]["reason"] == "preview_metadata_error"
            from fastapi import HTTPException
            def denied(*args, **kwargs):
                raise HTTPException(409, "original admission failure")
            monkeypatch.setattr("desktop.updates.require_admission", denied)
            rejected = await client.post("/api/assistant/tasks", json={**body, "request_key": "denied"})
            assert rejected.status_code == 409 and "device busy" in rejected.json()["detail"]
            gate.set()
            await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)
            app.state.db.update_task(tid, status=TaskStatus.SUCCEEDED)
            rejected = await client.post("/api/assistant/tasks", json={**body, "request_key": "denied"})
            assert rejected.status_code == 409 and rejected.json()["detail"] == "original admission failure"
    finally:
        gate.set()
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


def test_static_projection_is_cached_only_and_never_creates_driver():
    from control_api.preview import safe_preview_projection
    settings = SimpleNamespace(api_host="127.0.0.1")
    pool = SimpleNamespace(mode="android", get=forbidden, inventory=forbidden, peek=forbidden)
    task = SimpleNamespace(id="queued-id", device_serial="S")
    value = safe_preview_projection(task, settings=settings, pool=pool, enabled=True, base_url=ORIGIN)
    assert value["preview_url"] == ORIGIN + "/tasks/queued-id/preview"
    task.device_serial = "hub/S"
    assert safe_preview_projection(task, settings=settings, pool=pool, enabled=True,
                                   base_url=ORIGIN)["preview_url"] is None


def test_pool_peek_only_returns_exact_cached_local_key():
    from driver.pool import DriverPool
    from shared.config import Settings
    pool = DriverPool(Settings(_env_file=None, use_fixture_driver=False,
                               driver_url="", driver_urls_json=""))
    assert pool.peek("S") is None and not pool._drivers
    existing = object()
    pool._drivers["local/S"] = existing
    assert pool.peek("local/S") is existing
    assert pool.peek("S") is None and pool.peek("hub/S") is None


async def test_cancelled_viewer_does_not_cancel_task_or_keep_http_reference(live):
    before = live.source.peek_published_frame().frame
    budget = PreviewBudget().admit("T", "frame")
    response = PreviewImageResponse(before.data, "{}", budget)
    gate = asyncio.Event()
    async def wait(*_args):
        await gate.wait()
    sender = asyncio.create_task(response({"type": "http"}, wait, wait))
    await asyncio.sleep(0)
    sender.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sender
    assert budget.inflight == 0 and response.body == b""
    assert not live.app.state.running_tasks[live.task.id].done()
    assert live.source.peek_published_frame().frame is before


async def test_preview_budget_cleanup_failure_preserves_task_finalization(app, monkeypatch):
    starts = []
    async def run(tid):
        starts.append(tid)
        app.state.db.update_task(tid, status=TaskStatus.SUCCEEDED)
    app.state.orchestrator.run_task = run
    monkeypatch.setattr(app.state.task_preview.budget, "finished", forbidden)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.post("/api/tasks", json={"instruction": "finish",
            "device_serials": ["fixture"], "request_key": "cleanup-error"})
        tid = result.json()["tasks"][0]["id"]
        await asyncio.sleep(0)
        assert starts == [tid] and tid not in app.state.running_tasks
        assert app.state.db.get_task(tid).status == TaskStatus.SUCCEEDED
    calls = []
    async def shutdown():
        calls.append("source_shutdown")
    monkeypatch.setattr(app.state.task_preview.budget, "clear", forbidden)
    monkeypatch.setattr("control_api.main.OBSERVATION_STREAMS.shutdown", shutdown)
    handler = next(fn for fn in app.router.on_shutdown if fn.__name__ == "_shutdown_observation_streams")
    await handler()
    assert calls == ["source_shutdown"]


async def test_state_and_frame_http_throttle_return_retry_after(live):
    v = live
    for _ in range(8):
        assert (await v.client.get(v.root + "/state")).status_code == 200
    result = await v.client.get(v.root + "/state")
    assert result.status_code == 429 and result.headers["retry-after"] == "1"
    assert result.headers["cache-control"] == "no-store"
    v.app.state.task_preview.budget.clear()
    frame = (await v.client.get(v.root + "/state")).json()["frame"]
    query = {"run_epoch": frame["run_epoch"], "generation": frame["provider_generation"],
             "after_frame_id": frame["frame_id"]}
    for _ in range(4):
        assert (await v.client.get(v.root + "/frame", params=query)).status_code == 204
    result = await v.client.get(v.root + "/frame", params=query)
    assert result.status_code == 429 and result.headers["retry-after"] == "1"
    assert not v.app.state.running_tasks[v.task.id].done()


async def test_preview_polling_does_not_deserialize_agent_state(live, monkeypatch):
    monkeypatch.setattr(live.app.state.db, "get_task", forbidden)
    monkeypatch.setattr(live.app.state.db, "get_task_summary", forbidden)
    from shared.schemas import AgentState
    monkeypatch.setattr(AgentState, "model_validate", forbidden)
    result = await live.client.get(live.root + "/state")
    assert result.status_code == 200 and result.json()["frame"]


def test_healthy_old_static_frame_does_not_need_a_new_publication_or_dependency_probe(monkeypatch):
    source = provider()
    stamp = time.monotonic()-3600
    geometry = FrameGeometry(32, 64, 64, 128, timestamp=stamp, generation=1)
    old = source.ring.append(FrameHandle(png(), stamp, 1, geometry))
    monkeypatch.setattr(source, "decoder_available", forbidden)
    monkeypatch.setattr(source, "_publish_pending", forbidden)
    value = source.peek_published_frame()
    assert value.frame is old and value.visual_age_ms >= 3600000
    source.decoder.status = "unavailable"
    assert source.peek_published_frame().detail == "decoder_unavailable"
    source._session.is_alive = lambda: False
    assert source.peek_published_frame().detail == "source_unavailable"


async def test_new_provider_generation_rejects_old_descriptor_without_retagging(live):
    v = live
    old = (await v.client.get(v.root+"/state")).json()["frame"]
    v.source._session.generation = 2
    assert (await v.client.get(v.root+"/state")).json()["frame"] is None
    current = publish(v.source, "red")
    state = (await v.client.get(v.root+"/state")).json()["frame"]
    assert state["provider_generation"] == 2 and state["frame_id"] == current.frame_id
    assert (await v.client.get(v.root+"/frame", params={
        "run_epoch": old["run_epoch"], "generation": 1})).status_code == 409
    result = await v.client.get(v.root+"/frame", params={
        "run_epoch": state["run_epoch"], "generation": 2})
    assert result.status_code == 200 and result.content == current.data


async def test_mismatched_cached_driver_or_source_cannot_relabel_another_device(live):
    live.driver._serial = "different-phone"
    result = (await live.client.get(live.root+"/state")).json()
    assert result["frame"] is None and result["reason"] == "owner_unavailable"
    live.driver._serial = "S"
    live.source.device_key = "different-phone"
    result = (await live.client.get(live.root+"/state")).json()
    assert result["frame"] is None and result["reason"] == "owner_unavailable"
