"""Real HTTP lifecycle compatibility, independent of devices and Agent models."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route
import uvicorn

from control_api.preview import PreviewBoundary, PreviewBudget, PreviewImageResponse


@pytest.mark.parametrize("version", ["1.0", "1.1", "2"])
@pytest.mark.parametrize("allowed", [False, True])
async def test_connection_policy_is_preview_http1_only(version, allowed):
    async def app(scope, receive, send):
        await Response(b"ok")(scope, receive, send)
    boundary = PreviewBoundary(app, host="127.0.0.1", port=8080, scheme="http")
    for path in ("/api/tasks/T/preview/state", "/api/tasks/T/preview/frame", "/api/tasks/T"):
        messages = []
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "path": path, "method": "GET", "http_version": version,
                 "headers": [(b"host", b"127.0.0.1:8080")] + (
                     [(b"sec-fetch-site", b"same-origin")] if allowed else [])}
        await boundary(scope, None, send)
        start = messages[0]
        headers = dict(start["headers"])
        assert headers.get(b"connection") == (
            b"close" if "/preview/" in path and version != "2" else None)
        assert start["status"] == (403 if "/preview/" in path and not allowed else 200)


@pytest.mark.parametrize("protocol", ["h11", "httptools"])
async def test_real_disconnects_release_connections_and_preserve_business_loop(tmp_path, protocol):
    pytest.importorskip(protocol)
    if os.name == "nt":
        assert isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop)
    budget = PreviewBudget()
    async def image(request):
        admission = budget.admit("T", "frame")
        if admission is None:
            return Response(status_code=429)
        return PreviewImageResponse(b"x"*524288, "{}", admission)
    async def business(request):
        return Response(b"ok")
    app = Starlette(routes=[Route("/api/tasks/T/preview/frame", image), Route("/business", business)])
    # Port is selected by the OS, so fill the boundary after listener startup.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, http=protocol,
        lifespan="off", log_level="error", timeout_graceful_shutdown=2))
    handle = asyncio.create_task(server.serve())
    proc = None
    stop = tmp_path / "stop"
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if handle.done():
                    await handle
                await asyncio.sleep(.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        server.config.loaded_app = PreviewBoundary(server.config.loaded_app, host="127.0.0.1",
                                                   port=port, scheme="http")
        proc = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).resolve().parents[1]/"scripts/preview_http_client.py"),
            "--url", f"http://127.0.0.1:{port}", "--path", "/api/tasks/T/preview/frame",
            "--stop-file", str(stop), "--viewers", "10", "--slow", "--interval", ".02",
            "--read-delay", ".07", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        ready = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        assert ready["kind"] == "client_ready"
        # Normal async subprocess work remains available while preview peers run.
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('normal-task')",
                                                     stdout=asyncio.subprocess.PIPE)
        output, _ = await asyncio.wait_for(child.communicate(), 5)
        assert child.returncode == 0 and output.strip() == b"normal-task"
        # Two requests reuse one ordinary business connection.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            for _ in range(2):
                writer.write(f"GET /business HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
                await writer.drain()
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
                assert b"200 OK" in headers and b"connection: close" not in headers.lower()
                assert await reader.readexactly(2) == b"ok"
        finally:
            writer.close()
            await writer.wait_closed()
        await asyncio.sleep(.5)
        stop.touch()
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
        assert proc.returncode == 0 and not stderr
        cleanup = json.loads(stdout)
        assert cleanup["complete"] and cleanup["http_counts"]["frame_200"] > 0
        await asyncio.sleep(.1)
        assert all(entry.inflight == 0 for entry in budget.entries.values())
        assert all(s._active_count == 0 for s in server.servers)
    finally:
        stop.touch()
        if proc and proc.returncode is None:
            await asyncio.wait_for(proc.communicate(), 10)
        server.should_exit = True
        await asyncio.wait_for(handle, 5)
    assert all(s._active_count == 0 for s in server.servers)
