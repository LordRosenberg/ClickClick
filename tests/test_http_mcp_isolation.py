import asyncio
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from desktop.files import read_json, write_json
from desktop.supervisor import ServiceGroup, ensure_ports
from tests.test_assistant_mcp import fixture_env, free_port


def test_service_group_recovers_failed_child_without_stopping_sibling():
    now = [0]
    launched = []
    class Child:
        returncode = None
        def poll(self):
            return self.returncode
    def launch(name):
        child = Child()
        launched.append((name, child))
        return child
    group = ServiceGroup(launch, clock=lambda: now[0])
    group.tick()
    backend = group.children["backend"]
    group.children["http-mcp"].returncode = 1
    group.tick()
    assert group.children["backend"] is backend
    assert "http-mcp" not in group.children
    now[0] = 1
    group.tick()
    assert len(launched) == 3 and group.children["backend"] is backend
    # Repeated MCP failures must never terminate the healthy phone worker.
    for _ in range(6):
        group.children["http-mcp"].returncode = 1
        group.tick()
        now[0] += 30
        group.tick()
        assert group.children["backend"] is backend and backend.poll() is None


def test_child_launch_failure_does_not_end_healthy_backend():
    now = [0]
    child = SimpleNamespace(poll=lambda: None)
    def launch(name):
        if name == "http-mcp":
            raise OSError("resource temporarily unavailable")
        return child
    group = ServiceGroup(launch, clock=lambda: now[0])
    group.tick()
    assert group.children == {"backend": child}
    now[0] += 30
    group.tick()
    assert group.children == {"backend": child}


def test_fresh_install_and_missing_token_are_not_running(tmp_path):
    from desktop import service
    from desktop.install import install_payload
    from tests.test_desktop_onboarding import sample_payload
    home = tmp_path / "new"
    assert service.http_status(home) == {"running": False}
    install_payload(sample_payload(tmp_path), home, platform_check=False)
    (home / "versions/1.0.0/app/desktop/supervisor.py").write_text("# new service")
    assert service.http_status(home) == {"running": False}


def test_windows_stop_reaps_orphans_only_after_verifying_ownership(tmp_path, monkeypatch):
    from desktop import service
    events = []
    class Gone(Exception):
        pass
    class Timeout(Exception):
        pass
    class Process:
        def __init__(self, pid):
            self.pid, self.live = pid, True
        def exe(self):
            return str(tmp_path / "versions/1/runtime/python.exe") if self.pid != 9 else str(tmp_path / "foreign.exe")
        def cmdline(self):
            return ["python", "-I", "-m", "desktop.main", "--home", str(tmp_path), "http-mcp"]
        def wait(self, timeout):
            if self.live:
                raise Timeout()
        def terminate(self):
            events.append(("terminate", self.pid))
            self.live = False
        def is_running(self):
            return self.live
    processes = {7: Process(7), 9: Process(9)}
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=processes.__getitem__,
        TimeoutExpired=Timeout, NoSuchProcess=Gone, AccessDenied=PermissionError))
    write_json(tmp_path / "data/service-state.json", {"pid": 9, "children": {"http-mcp": 7}})
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    def native_stop(command, **kwargs):
        assert kwargs["creationflags"] == 0x08000000
        events.append("native-stop")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service.subprocess, "run", native_stop)
    service.native("stop", tmp_path)
    assert events == ["native-stop", ("terminate", 7)]
    assert processes[9].live


def test_windows_stop_requests_coordinated_exit_before_native_end(tmp_path, monkeypatch):
    from desktop import service
    events = []
    process = SimpleNamespace(pid=7, wait=lambda timeout: events.append("supervisor-exit"), is_running=lambda: False)
    monkeypatch.setattr(service, "service_processes", lambda _: [("serve", process)])
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(TimeoutExpired=TimeoutError))
    monkeypatch.setattr(service, "finish_service_exit", lambda _: events.append("verified-exit"))
    monkeypatch.setattr(service.sys, "platform", "win32")
    monkeypatch.setattr(service.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    def native_stop(command, **kwargs):
        assert kwargs["creationflags"] == 0x08000000
        assert read_json(tmp_path / "data/service-stop.request") == {"pid": 7}
        events.append("native-stop")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(service.subprocess, "run", native_stop)
    service.native("stop", tmp_path)
    assert events == ["supervisor-exit", "native-stop", "verified-exit"]


def test_port_allocation_persists_and_does_not_replace_registered_port(tmp_path):
    home = tmp_path / "home"
    port = free_port()
    write_json(home / "installation.json", {"port": port, "autostart": False})
    chosen = ensure_ports(home)
    assert chosen != port and 1 <= chosen <= 65535
    assert read_json(home / "installation.json") == {"port": port, "mcp_port": chosen, "autostart": False}
    assert ensure_ports(home) == chosen
    write_json(home / "installation.json", {"port": port, "mcp_port": port})
    with pytest.raises(ValueError, match="Invalid"):
        ensure_ports(home)


def test_gateway_does_not_construct_harness_or_task_database(tmp_path):
    env = fixture_env(tmp_path, free_port())
    script = '''
import sys
from control_api.mcp_http import create_http_app
from shared.config import Settings
app = create_http_app(Settings())
assert not any(name == "agent" or name.startswith("agent.") for name in sys.modules)
assert "shared.db" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")


async def test_protocol_responsive_while_backend_blocked(tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    backend_port, gateway_port = free_port(), free_port()
    while gateway_port == backend_port:
        gateway_port = free_port()
    marker = tmp_path / "blocked"
    env = {**fixture_env(tmp_path, backend_port), "CLICKCLICK_MCP_HTTP_PORT": str(gateway_port),
           "CLICKCLICK_TEST_BLOCK_FILE": str(marker)}
    root = Path(__file__).resolve().parents[1]
    processes = []
    try:
        for module in ("tests.http_blocking_backend", "control_api.mcp_http"):
            with (tmp_path / (module + ".log")).open("wb") as log:
                processes.append(subprocess.Popen([sys.executable, "-m", module], cwd=root,
                                                  env=env, stdout=log, stderr=log))
        async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
            token_path = tmp_path / "data/mcp-token"
            for _ in range(200):
                assert all(p.poll() is None for p in processes)
                if token_path.exists():
                    headers = {"Authorization": "Bearer " + token_path.read_text().strip()}
                    try:
                        identity = await http.get(f"http://127.0.0.1:{gateway_port}/mcp/identity", headers=headers)
                        backend = await http.get(f"http://127.0.0.1:{backend_port}/api/assistant/identity")
                        if identity.is_success and backend.is_success:
                            break
                    except httpx.RequestError:
                        pass
                await asyncio.sleep(.05)
            else:
                pytest.fail("Isolated services did not start")
            assert identity.json()["pid"] != backend.json()["pid"]
            url = f"http://127.0.0.1:{gateway_port}/mcp/"
            async with httpx.AsyncClient(headers=headers, trust_env=False) as mcp_http:
                async with streamable_http_client(url, http_client=mcp_http) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                        await session.initialize()
                        block = asyncio.create_task(http.get(f"http://127.0.0.1:{backend_port}/block"))
                        try:
                            for _ in range(100):
                                if marker.exists():
                                    break
                                await asyncio.sleep(.02)
                            assert marker.exists() and not block.done()
                            pending_tool = asyncio.create_task(session.call_tool("get_status"))
                            try:
                                await asyncio.wait_for(session.send_ping(), timeout=1.5)
                                assert len((await asyncio.wait_for(session.list_tools(), timeout=1.5)).tools) == 8
                                async with streamable_http_client(url, http_client=mcp_http) as (fresh_read, fresh_write, _):
                                    async with ClientSession(fresh_read, fresh_write) as fresh:
                                        await asyncio.wait_for(fresh.initialize(), timeout=1.5)
                                        assert len((await asyncio.wait_for(fresh.list_tools(), timeout=1.5)).tools) == 8
                                assert not block.done() and not pending_tool.done()
                                assert (await block).is_success
                                assert not (await pending_tool).isError
                            finally:
                                if not pending_tool.done():
                                    pending_tool.cancel()
                                await asyncio.gather(pending_tool, return_exceptions=True)
                        finally:
                            await asyncio.gather(block, return_exceptions=True)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


async def test_wrong_backend_identity_blocks_tool_forwarding(tmp_path):
    from control_api.mcp import create_server
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"service": "other", "data_dir": str(tmp_path), "workspace": str(Path.cwd())})
    server = create_server("http://127.0.0.1:8080", expected_backend={"data_dir": tmp_path},
        client_factory=lambda: httpx.AsyncClient(base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handler)))
    result = await server.call_tool("start_task", {"instruction": "do not execute", "request_key": "identity"})
    assert result.isError and calls == ["/api/assistant/identity"]
