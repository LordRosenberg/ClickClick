import asyncio
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from zipfile import ZipFile

import httpx
import pytest

from desktop.configuration import model_environment, model_summary, save_model
from desktop.devices import endpoint, inventory, pair, connect
from desktop.install import install_payload, installed_command
from desktop.registration import register, server_entry
from desktop.service import mac_definition, windows_definition
from tests.test_assistant_mcp import fixture_env, free_port


def sample_payload(tmp_path, *, version="1.0.0", corrupt=False, extra=None):
    files = {"runtime/python.exe": b"private python", "app/desktop/main.py": b"# runtime",
             "app/web/dist/index.html": b"console", "app/driver/vendor/scrcpy-server-v3.3.1.jar": b"mirror",
             "tools/platform-tools/adb.exe": b"adb", "tools/collector.apk": b"collector",
             "tools/ADBKeyboard.apk": b"keyboard", "licenses/ADBKeyboard-source.zip": b"source",
             "app/skills/generic/example/SKILL.md": b"bundled skill"}
    manifest = {"version": version, "platform": "windows", "architecture": "x86_64", "python": "runtime/python.exe",
                "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
    archive = tmp_path / (version + ".zip")
    with ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            z.writestr(name, data + (b"bad" if corrupt else b""))
        if extra:
            z.writestr(extra, b"escape")
    return archive


def test_verified_install_preserves_config_and_rejects_corruption(tmp_path):
    home = tmp_path / "installed space"
    archive = sample_payload(tmp_path)
    result = install_payload(archive, home, platform_check=False)
    assert result["version"] == "1.0.0"
    secret = home / "data/api-model.json"
    secret.write_text('{"keep":"private"}')
    (home / "data/clickclick.db").write_bytes(b"task history")
    (home / "data/skills/generic/example/SKILL.md").write_bytes(b"user edit")
    install_payload(archive, home, platform_check=False)
    install_payload(sample_payload(tmp_path, version="2.0.0"), home, platform_check=False)
    assert secret.read_text() == '{"keep":"private"}'
    assert (home / "data/clickclick.db").read_bytes() == b"task history"
    assert (home / "data/skills/generic/example/SKILL.md").read_bytes() == b"user edit"
    with pytest.raises(ValueError, match="integrity"):
        install_payload(sample_payload(tmp_path, version="3.0.0", corrupt=True), home, platform_check=False)
    assert json.loads((home / "current.json").read_text())["version"] == "2.0.0"
    with pytest.raises(ValueError, match="Unsafe"):
        install_payload(sample_payload(tmp_path, version="4.0.0", extra="../escape"), home, platform_check=False)
    assert not (tmp_path / "escape").exists()
    (home / "versions/2.0.0/app/desktop/main.py").write_bytes(b"modified")
    with pytest.raises(ValueError, match="damaged"):
        install_payload(tmp_path / "2.0.0.zip", home, platform_check=False)


@pytest.mark.parametrize("client", ["codex", "claude-code", "claude-desktop", "kimi-code"])
def test_registration_preserves_others_and_detects_conflicts(tmp_path, client):
    from desktop.registration import client_path
    path = client_path(client, user_dir=tmp_path, system="win32")
    path.parent.mkdir(parents=True, exist_ok=True)
    original = '# preserved comment\nmodel = "custom"\n[mcp_servers.other]\ncommand = "other"\n' if client == "codex" else '{"theme":"dark","mcpServers":{"other":{"command":"other"}}}'
    path.write_text(original, encoding="utf-8")
    transport = "stdio" if client == "claude-desktop" else "http"
    entry = server_entry(client=client, transport=transport, base_url="http://127.0.0.1:18080",
        token="t" * 43, command=str(tmp_path / "runtime space/python.exe"), args=["-m", "desktop.main", "mcp"])
    result = register(client, entry, user_dir=tmp_path, system="win32")
    assert Path(result["backup"]).read_text() == original
    assert not result["connection_verified"]
    assert "other" in path.read_text()
    if client == "codex":
        assert "# preserved comment" in path.read_text()
    assert not register(client, entry, user_dir=tmp_path, system="win32")["changed"]
    with pytest.raises(ValueError, match="conflicts"):
        register(client, {"command": "different"}, user_dir=tmp_path, system="win32")
    assert register(client, {"command": "different"}, user_dir=tmp_path, system="win32", replace=True)["changed"]


def test_saved_desktop_api_url_reaches_model_request(tmp_path):
    from shared.config import Settings
    from shared.llm_gateway import _build_completion_kwargs

    save_model(tmp_path, model="openai/test", base_url="https://relay.example/v1",
               api_key="desktop-test-key")
    env = model_environment(tmp_path)
    settings = Settings(_env_file=None, models_json=env["CLICKCLICK_MODELS_JSON"])
    request = _build_completion_kwargs("openai/test", [{"role": "user", "content": "hello"}],
                                       None, None, None, settings)
    assert request["api_base"] == "https://relay.example/v1"
    assert request["api_key"] == "desktop-test-key"


def test_private_api_config_and_native_definitions(tmp_path):
    result = save_model(tmp_path / "data", model="openai/test", base_url="https://example.test/v1", api_key="never-echo-key")
    assert "never-echo-key" not in json.dumps(result)
    assert "never-echo-key" not in json.dumps(model_summary(tmp_path / "data"))
    assert json.loads(model_environment(tmp_path / "data")["CLICKCLICK_MODELS_JSON"])["openai/test"]["api_key"] == "never-echo-key"
    with pytest.raises(ValueError):
        save_model(tmp_path, model="chatgpt/model", base_url="https://example.test", api_key="key")
    home = tmp_path / "install space"
    install_payload(sample_payload(tmp_path), home, platform_check=False)
    command, args = installed_command(home, "serve")
    definition = plistlib.loads(mac_definition(home))
    assert definition["ProgramArguments"] == [command, *args]
    assert definition["KeepAlive"] is True
    xml = windows_definition(home)
    assert "InteractiveToken" in xml and "LeastPrivilege" in xml and "IgnoreNew" in xml
    assert "serve" in xml and "install space" in xml


@pytest.mark.parametrize("value", ["example.com:5555", "1.1.1.1:5555", "192.168.0.1:0", "127.0.0.1:65536", "192.168.0.1:5555;evil", "http://127.0.0.1:5555", "0.0.0.0:5555"])
def test_device_endpoint_validation(value):
    with pytest.raises(ValueError):
        endpoint(value)


async def test_wifi_pairing_distinct_ports_and_no_secret_error(monkeypatch):
    calls = []
    class Process:
        returncode = 0
        async def communicate(self, code):
            assert code == b"123456\n"
            return b"Successfully paired", b""
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        calls.append(args)
        assert "123456" not in args
        return Process()
    monkeypatch.setattr("desktop.devices.adb.adb_bin", lambda: "adb")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    paired = await pair("192.168.1.2:37123", "123456")
    assert paired["paired"] and "connected" not in paired
    async def run(args, **kwargs):
        calls.append(tuple(args))
        return b"List of devices attached\n192.168.1.2:39876 device product:p model:m\nphone unauthorized\nold offline\n"
    monkeypatch.setattr("desktop.devices.adb._run_async", run)
    connected = await connect("192.168.1.2:39876")
    assert connected["connected"]
    assert calls[0] == ("adb", "pair", "192.168.1.2:37123")
    assert ("adb", "connect", "192.168.1.2:39876") in calls
    assert [d["online"] for d in connected["devices"]] == [True, False, False]
    async def failed(*args, **kwargs):
        p = Process()
        p.returncode = 1
        return p
    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed)
    assert "123456" not in json.dumps(await pair("192.168.1.2:37123", "123456"))


async def test_real_http_mcp_auth_lifespan_and_disconnect(tmp_path):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    mcp_port = free_port()
    while mcp_port == port:
        mcp_port = free_port()
    mcp_base = f"http://127.0.0.1:{mcp_port}"
    env = {**fixture_env(tmp_path, port), "CLICKCLICK_MCP_HTTP_ENABLED": "true",
           "CLICKCLICK_MCP_HTTP_PORT": str(mcp_port)}
    root = Path(__file__).resolve().parents[1]
    with (tmp_path / "http-backend.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-m", "tests.mcp_fixture_backend"], cwd=root, env=env, stdout=log, stderr=log)
    with (tmp_path / "http-mcp.log").open("wb") as log:
        gateway = subprocess.Popen([sys.executable, "-m", "control_api.mcp_http"], cwd=root, env=env, stdout=log, stderr=log)
    try:
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=10) as client:
            for _ in range(200):
                assert process.poll() is None, (tmp_path / "http-backend.log").read_text()
                try:
                    if (await client.get("/api/assistant/identity")).is_success:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(.1)
            else:
                pytest.fail("HTTP backend startup timed out")
            token = (tmp_path / "data/mcp-token").read_text()
            headers = {"Authorization": "Bearer " + token}
            for _ in range(100):
                assert gateway.poll() is None, (tmp_path / "http-mcp.log").read_text()
                try:
                    identity = await client.get(mcp_base + "/mcp/identity", headers=headers)
                    if identity.is_success:
                        break
                except httpx.RequestError:
                    pass
                await asyncio.sleep(.1)
            else:
                pytest.fail("Independent HTTP MCP startup timed out")
            backend_identity = (await client.get("/api/assistant/identity")).json()
            assert identity.json()["pid"] != backend_identity["pid"]
            moved = await client.post("/mcp/", headers=headers)
            assert moved.status_code == 410 and moved.json()["mcp_url"] == mcp_base + "/mcp/"
            assert (await client.post(mcp_base + "/mcp/")).status_code == 401
            assert (await client.post(mcp_base + "/mcp/", headers={**headers, "Origin": "https://evil.example"})).status_code == 403
            assert (await client.get("/api/setup/status", headers={**headers, "Host": "evil.example"})).status_code == 403
            assert (await client.get("/api/setup/status", headers=headers)).is_success
            model = await client.post("/api/setup/model", headers=headers, json={"model": "openai/test", "base_url": "https://example.test/v1", "api_key": "hidden-key"})
            assert model.is_success and "hidden-key" not in model.text
            assert "hidden-key" not in (await client.get("/api/setup/status", headers=headers)).text
            assert "hidden-key" not in (await client.get("/api/assistant/status")).text
            invalid = await client.post("/api/setup/model", headers=headers, json={"api_key": "secret-in-invalid-body"})
            assert invalid.status_code == 422 and "secret-in-invalid-body" not in invalid.text
            invalid_pair = await client.post("/api/setup/pair", headers=headers, json={"code": "secret-code"})
            assert invalid_pair.status_code == 422 and "secret-code" not in invalid_pair.text
            async with streamable_http_client(mcp_base + "/mcp/", http_client=httpx.AsyncClient(headers=headers, trust_env=False)) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                    init = await session.initialize()
                    assert "wireless" in init.instructions
                    tools = (await session.list_tools()).tools
                    assert len(tools) == 8
                    task = await session.call_tool("start_task", {"instruction": "HTTP fixture task", "request_key": "http"})
                    assert not task.isError
                    tid = task.structuredContent["tasks"][0]["task_id"]
            assert process.poll() is None
            (tmp_path / "release").touch()
            for _ in range(150):
                current = (await client.get("/api/assistant/tasks/" + tid)).json()
                if current["status"] == "succeeded":
                    break
                await asyncio.sleep(.05)
            assert current["status"] == "succeeded"
            async with streamable_http_client(mcp_base + "/mcp/", http_client=httpx.AsyncClient(headers=headers, trust_env=False)) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
                    await session.initialize()
                    tasks = await session.call_tool("get_task", {"task_ids": tid})
                    assert tasks.structuredContent["tasks"][0]["status"] == "succeeded"
    finally:
        gateway.terminate()
        try:
            gateway.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gateway.kill()
            gateway.wait()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def test_pair_timeout_reaps_process_and_has_no_secret(monkeypatch):
    class Process:
        returncode = None
        killed = False
        reaped = False
        async def communicate(self, value):
            return b"", b""
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            self.reaped = True
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    async def timeout(awaitable, seconds):
        assert seconds == 20
        awaitable.close()
        raise TimeoutError()
    monkeypatch.setattr("desktop.devices.adb.adb_bin", lambda: "adb")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "wait_for", timeout)
    with pytest.raises(TimeoutError) as error:
        await pair("192.168.1.2:37123", "654321")
    assert process.killed and process.reaped
    assert "654321" not in str(error.value)


def test_application_allowlist_excludes_private_and_internal(tmp_path):
    from desktop.packaging import application_files
    root = Path(__file__).resolve().parents[1]
    # Real allowlist must exclude the checkout's existing private/development material.
    files = {p.as_posix() for p in application_files(root)}
    assert "agent/prompts/revisable_executor.md" in files
    assert "docs/desktop-setup.zh-CN.md" in files
    assert "web/dist/index.html" in files
    assert {p for p in files if p.startswith("desktop/assets/")} == {"desktop/assets/clickclick-app.png"}
    import tomllib
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "assets/*.png" in metadata["tool"]["setuptools"]["package-data"]["desktop"]
    assert not any(p.startswith(("data/", "evaluation/", "tests/", "openspec/", ".git/", ".agents/")) for p in files)
    assert {p for p in files if p.startswith("docs/")} == {"docs/local-mcp.zh-CN.md", "docs/desktop-setup.zh-CN.md"}
    assert not any(".env" in p or "__pycache__" in p or "integrations/android_world" in p for p in files)
    assert not any(p.startswith("skills/") and ("mobileworld" in p or "androidworld" in p) for p in files)


def test_installed_profile_uses_private_environment(tmp_path, monkeypatch):
    from desktop.install import installed_environment
    home = tmp_path / "installed"
    install_payload(sample_payload(tmp_path), home, platform_check=False)
    monkeypatch.setenv("PYTHONPATH", "developer packages")
    monkeypatch.setenv("PYTHONHOME", "developer python")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "https://developer-driver.invalid")
    env, workspace = installed_environment(home)
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["CLICKCLICK_DRIVER_URL"] == ""
    assert env["CLICKCLICK_SKILLS_DIR"] == str(home / "data/skills")
    assert env["CLICKCLICK_DATA_DIR"] == str(home / "data")
    assert workspace == home / "versions/1.0.0/app"


def test_source_api_does_not_require_optional_mcp(tmp_path):
    script = '''
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "mcp" or name.startswith("mcp."):
        raise ImportError("optional MCP SDK absent")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from control_api.main import create_app
app = create_app(include_temp_runs=False)
assert not any(getattr(route, "name", "") == "mcp" for route in app.routes)
print("source API works without SDK")
'''
    env = fixture_env(tmp_path, free_port())
    env["CLICKCLICK_MCP_HTTP_ENABLED"] = "false"
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
