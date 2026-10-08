"""Release integrity, explicit consent and task-safe update lifecycle."""

import asyncio
import hashlib
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from desktop import updates, service
from desktop.files import read_json, write_json
from desktop.install import install_payload, installed
from desktop.versions import Version, TARGETS, artifact_name, tag_version
from scripts.desktop_release import assemble, publish
from shared.db import Database
from shared.schemas import TaskStatus
from tests.test_desktop_onboarding import sample_payload
from tests.test_task_pause import app


@pytest.fixture
def home(tmp_path):
    location = tmp_path / "installed"
    install_payload(sample_payload(tmp_path, version="1.0.0"), location, platform_check=False)
    return location


def feed(version="1.1.0", content=b"verified installer"):
    manifest = {"schema": 1, "version": version, "platforms": {
        t: {"name": artifact_name(version, t), "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest()} for t in TARGETS}}
    release = {"tag_name": "desktop-v" + version, "draft": False, "prerelease": False,
        "body": "Release notes", "assets": [{"id": 11, "name": "desktop-update.json"},
            {"id": 12, **manifest["platforms"]["windows-x86_64"]}]}
    return manifest, release


def connection(manifest, releases, content=b"verified installer", seen=None):
    def response(request):
        if seen is not None:
            seen.append(request)
        assert "Authorization" not in request.headers
        if request.url.path.endswith("/releases"):
            return httpx.Response(200, json=releases)
        if request.url.path.endswith("/11"):
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith("/12"):
            return httpx.Response(200, content=content)
        raise AssertionError(str(request.url))
    return httpx.Client(transport=httpx.MockTransport(response))


def test_semver_precedence_and_tag_namespace():
    ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
               "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
    assert sorted(reversed(ordered), key=Version) == ordered
    assert Version("1.0.0+build.1") == Version("1.0.0+build.2")
    for value in ("01.0.0", "1.0", "1.0.0-01", "1.0.0/evil", ""):
        with pytest.raises(ValueError):
            Version(value)
    for tag in ("collector-v1.0.0", "desktop-v1.0.0+build.1"):
        with pytest.raises(ValueError):
            tag_version(tag)


def release_files(directory, version="1.1.0"):
    for target in TARGETS:
        path = directory / artifact_name(version, target)
        path.write_bytes(target.encode())
        platform, architecture = target.split("-", 1)
        write_json(path.with_name(path.stem + "-inventory.json"),
                   {"version": version, "platform": platform, "architecture": architecture})
        path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)


def test_complete_release_manifest_and_workflow(tmp_path):
    release_files(tmp_path)
    result = assemble(tmp_path, "1.1.0")
    assert set(result["platforms"]) == TARGETS
    updates.validate_manifest(result, "1.1.0")
    artifact = tmp_path / artifact_name("1.1.0", "windows-x86_64")
    artifact.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        assemble(tmp_path, "1.1.0")
    artifact.unlink()
    with pytest.raises(ValueError, match="Missing"):
        assemble(tmp_path, "1.1.0")
    workflow = yaml.load(Path(".github/workflows/desktop-build.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["tags"] == ["desktop-v*"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["jobs"]["publish"]["needs"] == ["prepare", "desktop"]
    assert workflow["on"]["push"]["branches"] == ["develop", "main"]
    assert workflow["jobs"]["publish"]["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/desktop-v')"
    assert len(workflow["jobs"]["desktop"]["strategy"]["matrix"]["os"]) == 3


def test_publication_keeps_draft_until_all_uploads_succeed(tmp_path, monkeypatch):
    release_files(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "LordRosenberg/ClickClick")
    calls = []
    def invoke(args, **kwargs):
        calls.append(args)
        if args[2] == "view":
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("scripts.desktop_release.subprocess.run", invoke)
    publish(tmp_path, "desktop-v1.1.0")
    assert [c[2] for c in calls] == ["view", "create", "upload", "edit"]
    assert "--draft" in calls[1] and "--draft=false" in calls[-1]
    def failing(args, **kwargs):
        result = invoke(args, **kwargs)
        if args[2] == "upload":
            raise RuntimeError("upload failed")
        return result
    calls.clear()
    monkeypatch.setattr("scripts.desktop_release.subprocess.run", failing)
    with pytest.raises(RuntimeError):
        publish(tmp_path, "desktop-v1.1.0")
    assert [c[2] for c in calls] == ["view", "create", "upload"]


def test_stable_discovery_ignores_collector_preview_old_and_drafts(home):
    manifest, release = feed()
    releases = [{"tag_name": "collector-v999.0.0"}, {**release, "tag_name": "desktop-v8.0.0", "draft": True},
        {**release, "tag_name": "desktop-v9.0.0", "prerelease": True},
        {**release, "tag_name": "desktop-v10.0.0-rc.1"}, {**release, "tag_name": "desktop-v0.9.0"}, release]
    seen = []
    with connection(manifest, releases, seen=seen) as http:
        result = updates.check(home, connection=http)
    assert result["feed"]["available"]["version"] == "1.1.0"
    assert result["feed"]["available"]["asset_url"].endswith("/12")
    assert len(seen) == 2  # Metadata only, no installer or model requests.
    assert not result["operation"]
    assert not updates.configure(home, False)["auto_check"]
    assert "port" in read_json(home / "installation.json")


@pytest.mark.parametrize("damage", ["incomplete", "wrong-version", "digest", "platform", "offline"])
def test_invalid_or_unavailable_feed_is_visible_and_never_installs(home, damage):
    manifest, release = feed()
    if damage == "incomplete":
        manifest["platforms"].pop("macos-aarch64")
    if damage == "wrong-version":
        manifest["version"] = "2.0.0"
    if damage == "digest":
        release["assets"][1]["digest"] = "sha256:" + "0" * 64
    if damage == "platform":
        root = installed(home)[0]
        inventory = read_json(root / "manifest.json")
        inventory["architecture"] = "unsupported"
        write_json(root / "manifest.json", inventory)
    if damage == "offline":
        def fail(request):
            raise httpx.ConnectError("network down", request=request)
        http = httpx.Client(transport=httpx.MockTransport(fail))
    else:
        http = connection(manifest, [release])
    with http:
        result = updates.check(home, connection=http)
    assert result["feed"]["error"] and result["feed"]["available"] is None
    assert result["current_version"] == "1.0.0" and not result["operation"]


@pytest.mark.parametrize("content", [b"verified installer", b"corrupt", b"verified installer longer"])
def test_download_checksum_size_and_cleanup(tmp_path, content):
    manifest, release = feed()
    entry = {**manifest["platforms"]["windows-x86_64"], "asset_url": updates.asset_url(release["assets"][1])}
    with connection(manifest, [], content=content) as http:
        if content == b"verified installer":
            assert updates.download(http, entry, tmp_path).read_bytes() == content
        else:
            with pytest.raises(ValueError):
                updates.download(http, entry, tmp_path)
            assert not (tmp_path / entry["name"]).exists()


def test_confirmation_and_concurrent_request(home, monkeypatch):
    manifest, release = feed()
    with connection(manifest, [release]) as http:
        updates.check(home, connection=http)
    jobs = []
    monkeypatch.setattr(service, "require_idle", lambda _: None)
    monkeypatch.setattr(service, "schedule_update", lambda _, job: jobs.append(job))
    with pytest.raises(ValueError, match="确认"):
        updates.request(home, "1.1.0")
    with pytest.raises(ValueError, match="版本"):
        updates.request(home, "2.0.0", confirmed=True)
    assert not jobs
    assert updates.request(home, "1.1.0", confirmed=True)["operation"]["state"] == "queued"
    with pytest.raises(ValueError, match="已有"):
        updates.request(home, "1.1.0", confirmed=True)
    assert len(jobs) == 1


@pytest.mark.parametrize("task_status", [TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.PAUSED])
def test_final_prepare_checks_active_tasks(home, task_status):
    db = Database(home / "data/test.db")
    try:
        task = db.create_task("task", device_serial="fixture")
        db.update_task(task.id, status=task_status)
        updates.operation(home, job="a" * 32, state="downloading")
        if task_status == TaskStatus.PAUSED:
            assert updates.prepare(home, "a" * 32, db) == {"prepared": True}
            assert updates.status(home)["maintenance"]
        else:
            with pytest.raises(ValueError, match="活动"):
                updates.prepare(home, "a" * 32, db)
            assert not updates.status(home)["maintenance"]
    finally:
        db.close()


async def test_inflight_create_and_paused_resume_blocked_by_final_gate(app):
    pool = app.state.driver_pool
    original = pool.inventory
    entered, release = asyncio.Event(), asyncio.Event()
    async def inventory():
        entered.set()
        await release.wait()
        return await original()
    pool.inventory = inventory
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        request = asyncio.create_task(http.post("/api/tasks", json={"instruction": "goal", "device_serials": ["fixture"]}))
        await entered.wait()
        data_dir = app.state.db.path.parent
        write_json(data_dir / "update-maintenance.json", {"job": "a" * 32})
        release.set()
        assert (await request).status_code == 409
        assert not app.state.db.assistant_task_page(status=None, limit=10)
        task = app.state.db.create_task("paused goal", device_serial="fixture")
        app.state.db.update_task(task.id, status=TaskStatus.PAUSED)
        assert (await http.post(f"/api/tasks/{task.id}/resume")).status_code == 409
        assert app.state.db.get_task(task.id).status == TaskStatus.PAUSED


def test_staging_new_runtime_preserves_pointer_and_user_data(home, tmp_path):
    secret = home / "data/api-model.json"
    secret.write_text("private key")
    before = read_json(home / "current.json")
    stage = install_payload(sample_payload(tmp_path, version="1.1.0"), home, platform_check=False, activate=False)
    assert stage["staged"] and "1.1.0" in stage["python"]
    assert read_json(home / "current.json") == before and secret.read_text() == "private key"


def test_native_updater_is_independent_and_one_shot(home):
    job = "a" * 32
    xml = service.windows_definition(home, job=job)
    assert "update-worker" in xml and job in xml
    assert "LogonTrigger" not in xml and "RestartOnFailure" not in xml
    definition = plistlib.loads(service.mac_definition(home, job=job))
    assert definition["KeepAlive"] is False and definition["RunAtLoad"] is True
    assert "update-worker" in definition["ProgramArguments"]
    assert job in definition["Label"]


def test_official_client_rejects_untrusted_redirect_before_request(monkeypatch):
    real_client = httpx.Client
    seen = []
    def redirect(request):
        seen.append(request)
        assert "Authorization" not in request.headers and "private-key" not in str(request.headers)
        return httpx.Response(302, headers={"Location": "https://attacker.example/installer"})
    monkeypatch.setenv("GH_TOKEN", "private-key")
    monkeypatch.setenv("CLICKCLICK_API_KEY", "private-key")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(redirect), **kwargs))
    with updates.client() as http:
        with pytest.raises(ValueError, match="official HTTPS"):
            http.get("https://api.github.com/repos/LordRosenberg/ClickClick/releases")
    assert len(seen) == 1


def test_restore_restarts_prior_program_without_rewinding_data(home, tmp_path, monkeypatch):
    old = read_json(home / "current.json")
    install_payload(sample_payload(tmp_path, version="1.1.0"), home, platform_check=False)
    private = home / "data/api-model.json"
    private.write_text("newest config")
    calls = []
    monkeypatch.setattr(service, "backend_status", lambda _: {"running": True})
    monkeypatch.setattr(service, "require_idle", lambda _: calls.append("idle"))
    monkeypatch.setattr(service, "wait_stopped", lambda _: calls.append("stopped"))
    monkeypatch.setattr(service, "stop_tray", lambda _: calls.append("tray-stopped"))
    monkeypatch.setattr("desktop.entries.install_entries", lambda _: calls.append("entries"))
    monkeypatch.setattr(service, "start_tray", lambda _: calls.append("tray-started"))
    def native(action, location):
        calls.append(action)
        if action in {"install", "start"}:
            assert read_json(location / "current.json") == old
    monkeypatch.setattr(service, "native", native)
    monkeypatch.setattr(service, "wait_ready", lambda _: calls.append("ready"))
    updates.restore(home, old)
    assert calls == ["idle", "tray-stopped", "stop", "stopped", "entries", "install", "start", "ready", "tray-started"]
    assert private.read_text() == "newest config"


def test_bootstrap_delegates_to_new_staged_engine(home, tmp_path, monkeypatch):
    import desktop.bootstrap as bootstrap
    import sys
    old = read_json(home / "current.json")
    new_root = home / "versions/1.1.0"
    monkeypatch.setattr(sys, "argv", ["installer", "--home", str(home), "--unattended", "--no-start"])
    monkeypatch.setattr(sys, "platform", "linux")  # Test delegation, not native Windows DLL APIs.
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    def stage(payload, location, *, activate):
        assert not activate and read_json(location / "current.json") == old
        return {"root": str(new_root), "python": str(new_root / "runtime/python.exe")}
    monkeypatch.setattr(bootstrap, "install_payload", stage)
    def execute(argv, **kwargs):
        assert argv[0] == str(new_root / "runtime/python.exe")
        assert "install" in argv and "--no-start" in argv and "--no-open" in argv
        assert kwargs["cwd"] == new_root / "app"
        assert read_json(home / "current.json") == old
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")
    monkeypatch.setattr("subprocess.run", execute)
    bootstrap.main()


async def test_periodic_check_respects_cached_interval_and_opt_out(home, monkeypatch):
    from fastapi import FastAPI
    from control_api.setup import mount_setup_api
    from shared.config import Settings
    import time
    monkeypatch.setenv("CLICKCLICK_INSTALL_HOME", str(home))
    api = FastAPI()
    mount_setup_api(api, settings=Settings(_env_file=None, data_dir=home / "data", api_host="127.0.0.1"),
                    submission_lock=asyncio.Lock())
    checks = []
    checked = asyncio.Event()
    loop = asyncio.get_running_loop()
    def check(location):
        checks.append(True)
        write_json(location / "data/update-feed.json", {"checked_at": time.time(), "available": None})
        loop.call_soon_threadsafe(checked.set)
    monkeypatch.setattr(updates, "check", check)
    async def enter():
        async with api.router.lifespan_context(api):
            # Cached/disabled starts must stay idle; the first start must finish
            # its real threaded write before the simulated restart, even on a
            # slow disk. A fixed 200 ms sleep raced the metadata commit.
            if not checks:
                await asyncio.wait_for(checked.wait(), timeout=10)
            else:
                for _ in range(20):
                    await asyncio.sleep(.01)
    await enter()
    assert len(checks) == 1
    await enter()
    assert len(checks) == 1  # Restart within 12 hours reuses cached metadata.
    updates.configure(home, False)
    (home / "data/update-feed.json").unlink()
    await enter()
    assert len(checks) == 1


@pytest.mark.parametrize("failure", ["busy", "installer", "startup", "rollback", None])
def test_worker_failure_recovery_and_persistent_data(home, tmp_path, monkeypatch, failure):
    job = "a" * 32
    updates.operation(home, job=job, version="1.1.0", state="queued")
    (home / "data/mcp-token").write_text("local-auth")
    private = home / "data/api-model.json"
    private.write_text("secret")
    database = home / "data/clickclick.db"
    database.write_bytes(b"task history")
    gate = home / "data/update-maintenance.json"
    manifest, release = feed()
    from contextlib import nullcontext
    def local_response(request):
        assert request.headers["Authorization"] == "Bearer local-auth"
        if failure == "busy":
            return httpx.Response(409)
        write_json(gate, {"job": job})
        return httpx.Response(200, json={"prepared": True})
    # Save constructor before patching the shared httpx module.
    real_client = httpx.Client
    official_client = connection(manifest, [release])
    monkeypatch.setattr(updates, "client", lambda: nullcontext(official_client))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(local_response), **kwargs))
    called = []
    def execute(artifact, location):
        called.append(artifact.read_bytes())
        if failure in {"installer", "rollback"}:
            raise RuntimeError("installer died")
        install_payload(sample_payload(tmp_path, version="1.1.0"), location, platform_check=False)
    def ready(_):
        if failure == "startup":
            raise RuntimeError("new backend failed")
    def restore(location, pointer):
        if failure == "rollback":
            raise RuntimeError("old backend also unavailable")
        write_json(location / "current.json", pointer)
    monkeypatch.setattr(updates, "execute_installer", execute)
    monkeypatch.setattr(updates, "restore", restore)
    monkeypatch.setattr(service, "wait_ready", ready)
    try:
        updates.run(home, job)
    finally:
        official_client.close()
    result = updates.status(home)
    assert private.read_text() == "secret" and database.read_bytes() == b"task history"
    assert result["operation"]["state"] == ("succeeded" if failure is None else "failed")
    assert result["current_version"] == ("1.1.0" if failure is None else "1.0.0")
    assert result["maintenance"] == (failure == "rollback")
    assert bool(called) == (failure != "busy")
    if failure in {"installer", "startup"}:
        assert result["operation"]["rolled_back"] is True
    with pytest.raises(ValueError, match="not queued"):
        updates.run(home, job)


def test_explicit_recovery_refuses_live_worker_and_removes_delayed_job(home, monkeypatch):
    job = "a" * 32
    updates.operation(home, job=job, state="queued")
    write_json(home / "data/update-maintenance.json", {"job": job})
    calls = []
    monkeypatch.setattr(service, "remove_update", lambda _, j: calls.append(j))
    with pytest.raises(ValueError):
        updates.recover(home)
    with updates.startup_lock(home / "data/.update-run.lock", timeout=0):
        with pytest.raises(OSError):
            updates.recover(home, confirmed=True)
    assert not calls and updates.status(home)["maintenance"]
    result = updates.recover(home, confirmed=True)
    assert calls == [job] and not result["maintenance"]
    assert result["operation"]["state"] == "failed"


async def test_authenticated_update_api_and_source_unavailability(home, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(home / "data"))
    monkeypatch.setenv("CLICKCLICK_MCP_HTTP_ENABLED", "true")
    monkeypatch.setenv("CLICKCLICK_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_INSTALL_HOME", str(home))
    from control_api.main import create_app
    api = create_app(include_temp_runs=False)
    try:
        # Auth middleware expects the configured backend port, not installation CLI metadata.
        from shared.config import get_settings
        port = get_settings().api_port
        token = (home / "data/mcp-token").read_text()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url=f"http://127.0.0.1:{port}") as http:
            assert (await http.get("/api/setup/updates")).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            assert (await http.get("/api/setup/updates", headers=headers)).json()["current_version"] == "1.0.0"
            notice = (await http.get("/api/desktop/update-notice")).json()
            assert set(notice) == {"supported", "current_version", "available_version", "error"}
            assert (await http.get("/api/desktop/update-notice", headers={"Origin": "https://evil.example"})).status_code == 403
            assert (await http.post("/api/setup/updates/install", headers=headers, json={"version": "1.1.0"})).status_code == 409
            assert (await http.post("/api/setup/updates/preferences", headers=headers, json={"auto_check": False})).json()["auto_check"] is False
    finally:
        api.state.db.close()
    monkeypatch.delenv("CLICKCLICK_INSTALL_HOME")
    api = create_app(include_temp_runs=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url=f"http://127.0.0.1:{port}") as http:
            assert (await http.get("/api/setup/updates", headers=headers)).json() == {"supported": False}
            assert (await http.get("/api/desktop/update-notice")).json() == {"supported": False}
            assert (await http.post("/api/setup/updates/check", headers=headers)).status_code == 409
    finally:
        api.state.db.close()
