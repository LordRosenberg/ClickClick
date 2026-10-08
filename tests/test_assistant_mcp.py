"""Compact assistant API, SDK tools and launcher ownership boundaries."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Timeout
from PIL import Image

from shared.schemas import TaskStatus
from tests.test_task_pause import app


async def test_compact_api_pagination_wait_and_snapshot(app):
    db = app.state.db
    tasks = [db.create_task("goal" * 1000, device_serial=f"device-{i}") for i in range(4)]
    db.update_task(tasks[0].id, status=TaskStatus.SUCCEEDED)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/api/assistant/status")).json()
        assert status["models_configured"] is False
        assert status["credentials_validated"] is False
        assert "console_url" in status and status["guidance"]
        assert {"physical_phone", "emulator", "collector", "input_method", "local_driver"} <= status["device_setup"].keys()
        assert status["setup_guides"]
        assert all(Path(path).is_absolute() and Path(path).is_file() for path in status["setup_guides"].values())
        page = (await client.get("/api/assistant/tasks?limit=2")).json()
        assert len(page["tasks"]) == 2
        assert "state" not in page["tasks"][0]
        assert len(page["tasks"][0]["instruction"]) == 2000
        next_page = (await client.get("/api/assistant/tasks", params={"limit": 2, "cursor": page["next_cursor"]})).json()
        assert len(next_page["tasks"]) == 2
        assert not {t["task_id"] for t in page["tasks"]} & {t["task_id"] for t in next_page["tasks"]}
        assert next_page["next_cursor"] is None
        assert (await client.get("/api/assistant/tasks", params={"cursor": "invalid"})).status_code == 400
        assert (await client.get("/api/assistant/tasks?limit=51")).status_code == 422
        assert (await client.get(f"/api/assistant/tasks/{tasks[1].id}?wait_seconds=31")).status_code == 422
        waiting = asyncio.create_task(client.get(f"/api/assistant/tasks/{tasks[1].id}?wait_seconds=1"))
        await asyncio.sleep(.05)
        db.update_task(tasks[1].id, status=TaskStatus.PAUSED)
        assert (await waiting).json()["status"] == "paused"
        snapshot = (await client.get(f"/api/assistant/tasks/{tasks[1].id}/snapshot")).json()
        assert snapshot["image_available"] is False
        image = io.BytesIO()
        Image.new("RGB", (16, 16), "blue").save(image, "PNG")
        ref = app.state.artifacts.save_bytes("history_images", image.getvalue())
        db.append_agent_record(tasks[1].id, "observation", "obs", {
            "image_ref": ref, "step": 3, "recorded_at": 123.0})
        snapshot = (await client.get(f"/api/assistant/tasks/{tasks[1].id}/snapshot")).json()
        assert snapshot["image_available"] and snapshot["historical"]
        assert snapshot["recorded_at"] == 123 and snapshot["step"] == 3
        assert "console_url" in snapshot


async def test_sdk_tool_surface_batch_errors_images_and_no_host_context(app):
    pytest.importorskip("mcp")
    from control_api.mcp import create_server
    from mcp.types import CallToolResult
    db = app.state.db
    task = db.create_task("goal", device_serial="fixture")
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(buffer, "PNG")
    ref = app.state.artifacts.save_bytes("history_images", buffer.getvalue())
    db.append_agent_record(task.id, "observation", "obs", {"image_ref": ref, "step": 0, "recorded_at": 1})
    server = create_server("http://127.0.0.1:8080", client_factory=lambda: AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080"))
    tools = await server.list_tools()
    assert {t.name for t in tools} == {"get_status", "start_task", "get_task", "list_tasks",
                                     "get_task_snapshot", "cancel_task", "pause_task", "resume_task"}
    start_schema = next(t.inputSchema for t in tools if t.name == "start_task")
    assert set(start_schema["properties"]) == {"instruction", "device_ids", "request_key"}
    output = await server.call_tool("get_task", {"task_ids": [task.id, "missing"]})
    assert isinstance(output, CallToolResult)
    assert not output.isError
    assert output.structuredContent["tasks"][1]["error"]["code"] == 404
    snapshot = await server.call_tool("get_task_snapshot", {"task_id": task.id})
    assert snapshot.content[1].type == "image"
    paused = await server.call_tool("pause_task", {"task_ids": task.id})
    assert paused.structuredContent["tasks"][0]["status"] == "paused"
    cancelled = await server.call_tool("cancel_task", {"task_ids": [task.id, "missing"]})
    assert cancelled.structuredContent["tasks"][0]["status"] == "cancelled"
    invalid = await server.call_tool("get_task", {"task_ids": []})
    assert invalid.isError


def test_launcher_configuration_and_loopback_restriction(tmp_path):
    from control_api.mcp_launcher import client_config, local_url
    config = client_config(tmp_path, "http://127.0.0.1:8080", tmp_path / "data")
    entry = config["mcpServers"]["clickclick"]
    assert Path(entry["command"]).is_absolute()
    assert entry["args"][entry["args"].index("--workspace") + 1] == str(tmp_path.resolve())
    for invalid in ["https://example.org", "http://127.0.0.1/private", "http://user:pw@localhost", "http://localhost?token=x"]:
        with pytest.raises(ValueError):
            local_url(invalid)


async def test_assistant_api_submission_defaults_and_replay_without_device(app):
    gate = asyncio.Event()
    calls = []
    async def run(tid):
        calls.append(tid)
        await gate.wait()
    app.state.orchestrator.run_task = run
    app.state.settings.models_json = '{"test":{"provider":"openai","api_key":"test-secret"}}'
    app.state.settings.default_model = "test"
    app.state.settings.manager_model = ""
    app.state.settings.executor_model = ""
    app.state.driver_pool.environment_status = lambda key: {
        "status": "operator_action_required", "steps": {
            "accessibility_service": {"status": "operator_action_required", "guidance": "Enable Collector"}}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/api/assistant/status")).json()
        assert status["models_configured"] is True
        assert "test-secret" not in json.dumps(status)
        assert status["devices"][0]["environment"]["steps"]["accessibility_service"]["guidance"] == "Enable Collector"
        assert any("operator_action_required" in message for message in status["guidance"])
        body = {"instruction": "goal", "request_key": "auto"}
        concurrent = await asyncio.gather(*(client.post("/api/assistant/tasks", json=body) for _ in range(2)))
        assert all(response.status_code == 200 for response in concurrent)
        assert concurrent[0].json()["tasks"][0]["task_id"] == concurrent[1].json()["tasks"][0]["task_id"]
        created = concurrent[0]
        assert created.status_code == 200
        tid = created.json()["tasks"][0]["task_id"]
        async def offline():
            return []
        app.state.driver_pool.inventory = offline
        offline_status = (await client.get("/api/assistant/status")).json()
        assert offline_status["devices"] == []
        assert offline_status["device_setup"]["physical_phone"]
        assert offline_status["device_setup"]["emulator"]
        assert any("ADB" in message for message in offline_status["guidance"])
        replay = await client.post("/api/assistant/tasks", json=body)
        assert replay.json()["tasks"][0]["task_id"] == tid
        assert replay.json()["replayed"] is True
        assert (await client.post("/api/assistant/tasks", json={**body, "instruction": "changed"})).status_code == 409
        assert (await client.post("/api/assistant/tasks", json={**body, "system_prompt": "host"})).status_code == 422
        await asyncio.sleep(0)
        assert calls == [tid]
        gate.set()
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fixture_env(tmp_path, port):
    return {**os.environ,
            "CLICKCLICK_DATA_DIR": str(tmp_path / "data"),
            "CLICKCLICK_API_PORT": str(port), "CLICKCLICK_API_HOST": "127.0.0.1",
            "CLICKCLICK_API_SSL_CERTFILE": "", "CLICKCLICK_API_SSL_KEYFILE": "",
            "CLICKCLICK_USE_FIXTURE_DRIVER": "true", "CLICKCLICK_DRIVER_URL": "",
            "CLICKCLICK_DRIVER_URLS_JSON": "", "CLICKCLICK_DEFAULT_MODEL": "test",
            "CLICKCLICK_MANAGER_MODEL": "", "CLICKCLICK_EXECUTOR_MODEL": "",
            "CLICKCLICK_MODELS_JSON": '{"test":{"provider":"openai","api_key":"fake"}}',
            "CLICKCLICK_TEST_RELEASE_FILE": str(tmp_path / "release")}


async def test_real_stdio_disconnect_does_not_cancel_running_harness(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from control_api.mcp_launcher import ensure_backend
    root = Path(__file__).resolve().parents[1]
    port = free_port()
    env = fixture_env(tmp_path, port)
    with (tmp_path / "fixture-backend.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "tests.mcp_fixture_backend"],
                                   cwd=root, env=env, stdout=log, stderr=log)
    identity = None
    try:
        base = f"http://127.0.0.1:{port}"
        async with AsyncClient(base_url=base, trust_env=False, timeout=Timeout(10, connect=2)) as client:
            for _ in range(150):
                try:
                    response = await client.get("/api/assistant/identity")
                    if response.is_success:
                        break
                except Exception:
                    pass
                assert process.poll() is None
                await asyncio.sleep(.1)
            else:
                pytest.fail("fixture backend startup timed out")
            # Reuses the same identity rather than spawning another backend.
            identity = ensure_backend(base, root, tmp_path / "data", auto_start=False)
            assert identity["pid"] == response.json()["pid"]
            params = StdioServerParameters(command=sys.executable,
                args=["-m", "control_api.mcp", "--workspace", str(root), "--data-dir", str(tmp_path / "data"),
                      "--backend-url", base, "--no-start"], env=env, cwd=str(root))
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.instructions
                    discovered = (await session.list_tools()).tools
                    assert len(discovered) == 8
                    assert all(tool.description for tool in discovered)
                    status = await session.call_tool("get_status")
                    assert status.structuredContent["models_configured"]
                    created = await session.call_tool("start_task", {"instruction": "fixture task", "request_key": "stdio"})
                    assert not created.isError
                    tid = created.structuredContent["tasks"][0]["task_id"]
            assert process.poll() is None
            current = (await client.get(f"/api/assistant/tasks/{tid}")).json()
            assert current["status"] in {"queued", "running"}
            (tmp_path / "release").touch()
            for _ in range(100):
                current = (await client.get(f"/api/assistant/tasks/{tid}")).json()
                if current["status"] == "succeeded":
                    break
                await asyncio.sleep(.05)
            assert current["status"] == "succeeded"
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tasks = await session.call_tool("list_tasks")
                    assert tasks.structuredContent["tasks"][0]["task_id"] == tid
                    replay = await session.call_tool("start_task", {"instruction": "fixture task", "request_key": "stdio"})
                    assert replay.structuredContent["replayed"]
    finally:
        if identity and identity["pid"] != process.pid:
            import signal
            os.kill(identity["pid"], signal.SIGTERM)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_launcher_serializes_concurrent_auto_start_and_checks_identity(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from control_api.mcp_launcher import ensure_backend, INDEPENDENT_BACKEND_ERROR
    from shared.schemas import TaskStatus
    port = free_port()
    for key, value in fixture_env(tmp_path, port).items():
        if key.startswith("CLICKCLICK_"):
            monkeypatch.setenv(key, value)
    base = f"http://127.0.0.1:{port}"
    root = Path(__file__).resolve().parents[1]
    identity = None
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(ensure_backend, base, root, tmp_path / "data") for _ in range(2)]
            outcomes = [future.exception(timeout=45) for future in futures]
            if any(outcomes):
                # A containing Windows Job Object can prohibit independence
                # even when the immediate parent permits breakaway. Both
                # serialized callers must fail safely, leaving no backend.
                assert os.name == "nt"
                assert all(isinstance(exc, RuntimeError) and str(exc) == INDEPENDENT_BACKEND_ERROR
                           for exc in outcomes)
                import httpx
                with httpx.Client(base_url=base, trust_env=False, timeout=.5) as client:
                    with pytest.raises(httpx.ConnectError):
                        client.get("/api/assistant/identity")
                return
            identities = [future.result() for future in futures]
        identity = identities[0]
        assert identities[0]["pid"] == identities[1]["pid"]
        with pytest.raises(RuntimeError, match="mismatch"):
            ensure_backend(base, root, tmp_path / "other-data", auto_start=False)
    finally:
        if identity:
            # Only terminate the test-owned process, never an existing user backend.
            import signal
            os.kill(identity["pid"], signal.SIGTERM)


async def test_stdio_auto_start_has_independent_lifetime_or_fails_without_orphan(tmp_path):
    """A Windows client's kill-on-close Job Object must never own the backend."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    root = Path(__file__).resolve().parents[1]
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "control_api.mcp", "--workspace", str(root),
              "--data-dir", str(tmp_path / "data"), "--backend-url", base],
        env=fixture_env(tmp_path, port), cwd=str(root))
    identity = None
    startup_error = None
    try:
        with (tmp_path / "stdio-error.log").open("w+") as errors:
            try:
                async with stdio_client(params, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        assert not (await session.call_tool("get_status")).isError
                        async with AsyncClient(base_url=base, trust_env=False) as client:
                            identity = (await client.get("/api/assistant/identity")).json()
            except Exception as exc:
                startup_error = exc
            errors.seek(0)
            stderr = errors.read()
        async with AsyncClient(base_url=base, trust_env=False, timeout=.5) as client:
            if startup_error is not None:
                # Official SDK 1.x restricts breakaway on Windows. Give a safe
                # setup instruction instead of silently accepting doomed tasks.
                assert os.name == "nt", stderr
                assert "prohibits an independent backend process" in stderr
                assert "--no-start" in stderr
                with pytest.raises(Exception):
                    await client.get("/api/assistant/identity")
            else:
                response = await client.get("/api/assistant/identity")
                assert response.is_success
                assert response.json()["pid"] == identity["pid"]
    finally:
        if identity:
            import signal
            try:
                os.kill(identity["pid"], signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API")
def test_backend_guard_accepts_no_job_and_rejects_unexpected_query_error(monkeypatch):
    """pywin32's API exception is not an OSError; an unconfined process is valid."""
    import pywintypes
    import win32job
    import control_api.main
    from control_api.mcp_backend import main
    calls = []
    monkeypatch.setattr(control_api.main, "main", lambda: calls.append("started"))
    def no_job(*args):
        raise pywintypes.error(5, "QueryInformationJobObject", "Access denied")
    monkeypatch.setattr(win32job, "QueryInformationJobObject", no_job)
    main()
    assert calls == ["started"]
    def unexpected(*args):
        raise pywintypes.error(87, "QueryInformationJobObject", "Invalid parameter")
    monkeypatch.setattr(win32job, "QueryInformationJobObject", unexpected)
    with pytest.raises(pywintypes.error):
        main()
    assert calls == ["started"]
