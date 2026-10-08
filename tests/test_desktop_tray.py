"""Installed entry, task admission, safe shutdown and native manager contracts."""

import asyncio
import json
from pathlib import Path
import plistlib
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import httpx
import pytest
from PIL import Image

from desktop import entries, service, shutdown, updates
from desktop.files import read_json, write_json
from desktop.install import install_payload
from desktop.lifecycle import DesktopController
from desktop.tray import TrayManager
from shared.schemas import TaskStatus
from tests.test_desktop_onboarding import sample_payload
from tests.test_task_pause import app


@pytest.fixture
def home(tmp_path):
    location = tmp_path / "installed space 中文"
    install_payload(sample_payload(tmp_path), location, platform_check=False)
    (location / "data/mcp-token").write_text("t" * 43, encoding="utf-8")
    return location


def test_native_tray_definitions_and_login_preference(home):
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    enabled = ET.fromstring(service.windows_definition(home, tray=True))
    assert " tray" in enabled.find("t:Actions/t:Exec/t:Arguments", namespace).text
    assert enabled.find("t:Triggers/t:LogonTrigger", namespace) is not None
    write_json(home / "installation.json", {"port": 18080, "autostart": False})
    for tray in (False, True):
        disabled = ET.fromstring(service.windows_definition(home, tray=tray))
        assert disabled.find("t:Triggers/t:LogonTrigger", namespace) is None
    mac = plistlib.loads(service.mac_definition(home, tray=True))
    assert mac["ProgramArguments"][-1] == "tray"
    assert mac["Label"].endswith("-tray") and mac["KeepAlive"] is False


def test_autostart_reregisters_both_without_restarting(home, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "native", lambda action, root, **kwargs: calls.append((action, kwargs)))
    service.configure_autostart(home, False)
    assert read_json(home / "installation.json")["autostart"] is False
    assert calls == [("install", {}), ("install", {"tray": True})]
    service.configure_autostart(home, True)
    assert service.autostart_enabled(home)


def test_autostart_failure_restores_preference(home, monkeypatch):
    previous = read_json(home / "installation.json")
    monkeypatch.setattr(service, "native", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("denied")))
    with pytest.raises(RuntimeError, match="denied"):
        service.configure_autostart(home, False)
    assert read_json(home / "installation.json") == previous


def test_windows_entry_passes_quoted_paths_as_args(home, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(entries.sys, "platform", "win32")
    monkeypatch.setattr(entries.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(entries.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    result = entries.install_entries(home, user_dir=tmp_path / "user 中文")
    args, kwargs = calls[0]
    values = json.loads(kwargs["input"])
    assert values["command"].endswith("pythonw.exe")
    assert '"' in values["arguments"] and "installed space 中文" in values["arguments"]
    assert len(args) == args.index("-File") + 2
    assert kwargs["creationflags"] == entries.subprocess.CREATE_NO_WINDOW
    assert result["launcher"].endswith("ClickClick.lnk")
    assert (home / "bin/ClickClick.ico").is_file()
    with Image.open(home / "bin/ClickClick.ico") as icon:
        assert (256, 256) in icon.ico.sizes()
        assert icon.convert("RGBA").getpixel((0, 0))[3] == 0


def test_mac_app_entry_is_background_and_uses_private_runtime(home, tmp_path, monkeypatch):
    monkeypatch.setattr(entries.sys, "platform", "darwin")
    result = entries.install_entries(home, user_dir=tmp_path / "user")
    bundle = Path(result["launcher"])
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    assert info["LSUIElement"] is True
    assert info["CFBundleIconFile"] == "ClickClick.icns"
    with Image.open(bundle / "Contents/Resources/ClickClick.icns") as icon:
        assert icon.format == "ICNS"
    script = (bundle / "Contents/MacOS/ClickClick").read_text(encoding="utf-8")
    assert "installed space 中文" in script and " tray" in script


def test_mac_disabled_autostart_moves_both_login_definitions(home, tmp_path, monkeypatch):
    monkeypatch.setattr(entries.sys, "platform", "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    for tray in (False, True):
        result = service.native("install", home, tray=tray)
        assert "LaunchAgents" in result["path"]
    write_json(home / "installation.json", {"autostart": False})
    for tray in (False, True):
        result = service.native("install", home, tray=tray)
        assert Path(result["path"]).parent == home
    assert not list((tmp_path / "user/Library/LaunchAgents").glob("*.plist"))


def test_mac_tray_replacement_unloads_previous_launch_agent(home, monkeypatch):
    monkeypatch.setattr(entries.sys, "platform", "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 1000, raising=False)
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(service, "native", lambda action, root, **kwargs: calls.append((action, kwargs)))
    service.stop_tray(home)
    assert calls == [("stop", {"tray": True})]


def test_shutdown_lease_ownership_expiry_and_update_conflict(tmp_path, monkeypatch):
    shutdown.prepare(tmp_path, "a" * 32)
    assert shutdown.active(tmp_path)
    with pytest.raises(ValueError, match="关闭操作"):
        shutdown.prepare(tmp_path, "b" * 32)
    shutdown.release(tmp_path, "b" * 32)
    assert shutdown.active(tmp_path)
    with pytest.raises(Exception) as error:
        updates.require_admission(tmp_path)
    assert error.value.status_code == 409
    monkeypatch.setattr(shutdown.time, "time", lambda: float("inf"))
    assert not shutdown.active(tmp_path)
    updates.require_admission(tmp_path)
    write_json(tmp_path / "update-maintenance.json", {"job": "u"})
    with pytest.raises(ValueError, match="升级"):
        shutdown.prepare(tmp_path, "b" * 32)


async def test_shutdown_api_requires_local_auth_and_recovers_stale_gate(home, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(home / "data"))
    monkeypatch.setenv("CLICKCLICK_MCP_HTTP_ENABLED", "true")
    monkeypatch.setenv("CLICKCLICK_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_INSTALL_HOME", str(home))
    from control_api.main import create_app
    from shared.config import get_settings
    api = create_app(include_temp_runs=False)
    try:
        port = get_settings().api_port
        token = (home / "data/mcp-token").read_text(encoding="utf-8")
        headers = {"Authorization": "Bearer " + token}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url=f"http://127.0.0.1:{port}") as http:
            route = "/api/setup/shutdown/prepare"
            assert (await http.post(route, json={"job": "a" * 32})).status_code == 401
            assert (await http.post(route, headers={**headers, "Origin": "https://evil.example"}, json={"job": "a" * 32})).status_code == 403
            assert (await http.post(route, headers=headers, json={"job": "a" * 32})).status_code == 200
            assert shutdown.active(home / "data")
            await http.post("/api/setup/shutdown/release", headers=headers, json={"job": "b" * 32})
            assert shutdown.active(home / "data")
            await http.post("/api/setup/shutdown/release", headers=headers, json={"job": "a" * 32})
            assert not shutdown.active(home / "data")
        shutdown.prepare(home / "data", "a" * 32)
        await next(handler for handler in api.router.on_startup if handler.__name__ == "clear_abandoned_shutdown")()
        assert not shutdown.active(home / "data")
    finally:
        api.state.db.close()


async def test_shutdown_blocks_inflight_create_and_resume(app):
    pool = app.state.driver_pool
    original = pool.inventory
    entered, release = asyncio.Event(), asyncio.Event()
    async def inventory():
        entered.set()
        await release.wait()
        return await original()
    pool.inventory = inventory
    data = app.state.db.path.parent
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = asyncio.create_task(client.post("/api/tasks", json={"instruction": "goal", "device_serials": ["fixture"]}))
        await entered.wait()
        shutdown.prepare(data, "a" * 32)
        release.set()
        assert (await request).status_code == 409
        assert not app.state.db.assistant_task_page(status=None, limit=10)
        task = app.state.db.create_task("paused", device_serial="fixture")
        app.state.db.update_task(task.id, status=TaskStatus.PAUSED)
        assert (await client.post(f"/api/tasks/{task.id}/resume")).status_code == 409
        shutdown.release(data, "a" * 32)
        assert not shutdown.active(data)


@pytest.fixture
def controller(home, monkeypatch):
    controller = DesktopController(home)
    state = {"running": True, "tasks": [{"task_id": "T1", "instruction": "read settings", "status": "running"}], "calls": [], "complete": True}
    def handler(request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path.endswith("/shutdown/prepare"):
            shutdown.prepare(home / "data", json.loads(request.content)["job"])
            return httpx.Response(200, json={"prepared": True})
        if request.url.path == "/api/assistant/tasks":
            status = request.url.params["status"]
            return httpx.Response(200, json={"tasks": [t for t in state["tasks"] if t["status"] == status], "next_cursor": None})
        if request.url.path.endswith(("/pause", "/cancel")):
            if state["complete"]:
                state["tasks"][0]["status"] = "paused" if request.url.path.endswith("/pause") else "cancelled"
            return httpx.Response(200, json={})
        return httpx.Response(404)
    monkeypatch.setattr(controller, "client", lambda: httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(service, "backend_status", lambda root: {"running": state["running"]})
    def stop(action, root):
        assert action == "stop" and not any(t["status"] in {"running", "pausing", "queued"} for t in state["tasks"])
        assert shutdown.active(home / "data")
        state["running"] = False
    monkeypatch.setattr(service, "native", stop)
    monkeypatch.setattr(service, "require_idle", lambda root: None)
    monkeypatch.setattr(service, "wait_stopped", lambda root: None)
    return controller, state


@pytest.mark.parametrize("choice", ["pause", "cancel", "back"])
def test_shutdown_choice_preserves_active_task_safety(controller, choice):
    control, state = controller
    def choose(tasks):
        assert [t["task_id"] for t in tasks] == ["T1"]
        assert shutdown.active(control.home / "data")
        return choice
    assert control.stop(choose) is (choice != "back")
    assert state["running"] is (choice == "back")
    assert not shutdown.active(control.home / "data")
    assert not any("devices" in url for _, url in state["calls"])


def test_shutdown_timeout_keeps_backend_and_releases_gate(controller):
    control, state = controller
    state["complete"] = False
    with pytest.raises(RuntimeError, match="后台继续运行"):
        control.stop(lambda tasks: "pause", timeout=0)
    assert state["running"] and not shutdown.active(control.home / "data")


def test_wrong_backend_identity_cannot_be_stopped(home, monkeypatch):
    monkeypatch.setattr(service, "backend_status", lambda root: (_ for _ in ()).throw(RuntimeError("another backend")))
    with pytest.raises(RuntimeError, match="another backend"):
        DesktopController(home).stop(lambda tasks: "cancel")
    assert not shutdown.active(home / "data")


def test_status_refresh_queries_tasks_without_phone_inventory(controller):
    control, state = controller
    manager = TrayManager(control.home, control)
    manager.refresh()
    assert manager.state == "busy" and "1 个" in manager.status_text
    assert all(path == "/api/assistant/tasks" for _, path in state["calls"])


def test_exit_back_does_not_close_tray(controller, monkeypatch):
    control, state = controller
    manager = TrayManager(control.home, control)
    manager.icon = SimpleNamespace(stop=lambda: pytest.fail("must keep tray"), update_menu=lambda: None)
    monkeypatch.setattr("desktop.dialogs.choose_shutdown", lambda tasks: "back")
    manager.perform("exit")
    assert manager._actions.acquire(timeout=3)
    manager._actions.release()
    assert state["running"] and not manager._closed.is_set()
