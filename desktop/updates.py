"""Official release discovery and explicitly confirmed, independent upgrades."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import httpx

from desktop.files import read_json, write_json
from desktop.install import installed, installed_environment, make_launcher
from desktop.versions import OFFICIAL_REPO, TAG_PREFIX, TARGETS, MAX_INSTALLER_BYTES, Version, artifact_name, tag_version
from control_api.mcp_launcher import startup_lock

CHECK_INTERVAL = 12 * 60 * 60
ACTIVE = {"queued", "downloading", "installing"}


def paths(home):
    return Path(home).resolve() / "data"


def status(home):
    root, _, current = installed(home)
    config = read_json(Path(home) / "installation.json")
    feed = read_json(paths(home) / "update-feed.json")
    available = feed.get("available")
    if available and Version(available["version"]) <= Version(current["version"]):
        feed["available"] = None
    return {"supported": True, "current_version": current["version"],
            "auto_check": config.get("auto_check_updates", True),
            "feed": feed,
            "operation": read_json(paths(home) / "update-operation.json"),
            "maintenance": (paths(home) / "update-maintenance.json").exists()}


def notice(home):
    """Read-only Console notice; never expose credentials or control/job details."""
    value = status(home)
    available = value["feed"].get("available")
    return {"supported": True, "current_version": value["current_version"],
            "available_version": available["version"] if available else None,
            "error": value["feed"].get("error") or value["operation"].get("error")}


def client():
    def trusted(request):
        if request.url.scheme != "https" or request.url.host not in {
            "api.github.com", "github.com", "release-assets.githubusercontent.com",
            "objects.githubusercontent.com", "github-releases.githubusercontent.com",
        }:
            raise ValueError("Update redirect is outside official HTTPS artifact hosts")
    return httpx.Client(timeout=20, follow_redirects=True,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ClickClick-desktop-updater"},
        event_hooks={"request": [trusted]})


def json_response(connection, url, *, asset=False):
    headers = {"Accept": "application/octet-stream"} if asset else {}
    with connection.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        value = bytearray()
        deadline = time.monotonic() + 60
        for chunk in response.iter_bytes():
            value.extend(chunk)
            if len(value) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                raise ValueError("Update metadata exceeds size limit")
    try:
        return json.loads(value)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid desktop update metadata") from exc


def asset_url(asset):
    identifier = asset.get("id")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Invalid official release asset identifier")
    return f"https://api.github.com/repos/{OFFICIAL_REPO}/releases/assets/{identifier}"


def validate_manifest(manifest, version):
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or manifest.get("version") != version:
        raise ValueError("Desktop update manifest version/schema mismatch")
    platforms = manifest.get("platforms", {})
    if not isinstance(platforms, dict) or set(platforms) != TARGETS:
        raise ValueError("Desktop update release is incomplete")
    import re
    for target, entry in platforms.items():
        if (not isinstance(entry, dict) or entry.get("name") != artifact_name(version, target)
            or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_INSTALLER_BYTES
            or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("Invalid desktop update asset metadata")
    return platforms


def discover(home, connection):
    root, _, current = installed(home)
    inventory = read_json(root / "manifest.json")
    target = inventory["platform"] + "-" + inventory["architecture"]
    releases = []
    # Scan the published list, not /latest (Collector uses the same repository).
    for page in range(1, 11):
        batch = json_response(connection, f"https://api.github.com/repos/{OFFICIAL_REPO}/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Invalid desktop release list")
        releases.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("Release list exceeds scan limit; retry or use the official releases page")
    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name", "")
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX):
            continue
        version = tag_version(tag)
        if not Version(version).pre and Version(version) > Version(current["version"]):
            candidates.append((Version(version), version, release))
    if not candidates:
        return None
    _, version, release = max(candidates, key=lambda c: c[0])
    assets = release.get("assets", [])
    if not isinstance(assets, list) or any(not isinstance(a, dict) for a in assets):
        raise ValueError("Invalid desktop release asset list")
    metadata = [a for a in assets if a.get("name") == "desktop-update.json"]
    if len(metadata) != 1:
        raise ValueError("Desktop update manifest missing or duplicated")
    platforms = validate_manifest(json_response(connection, asset_url(metadata[0]), asset=True), version)
    if target not in platforms:
        raise ValueError("No update for this installed platform/architecture")
    entry = platforms[target]
    binaries = [a for a in assets if a.get("name") == entry["name"]]
    if len(binaries) != 1 or binaries[0].get("size") != entry["size"]:
        raise ValueError("Desktop installer missing or size mismatch")
    digest = binaries[0].get("digest")
    if digest and digest != "sha256:" + entry["sha256"]:
        raise ValueError("Official release asset digest disagrees with update manifest")
    return {"version": version, "target": target, "asset_url": asset_url(binaries[0]), **entry,
            "release_url": f"https://github.com/{OFFICIAL_REPO}/releases/tag/{TAG_PREFIX}{version}",
            "notes": str(release.get("body") or "")[:4000]}


def check(home, *, connection=None):
    # Serialize scheduled/manual checks without holding the backend event loop.
    with startup_lock(paths(home) / ".update-check.lock"):
        try:
            if connection is not None:
                available = discover(home, connection)
            else:
                with client() as connection:
                    available = discover(home, connection)
            feed = {"checked_at": time.time(), "available": available, "error": None}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            feed = {"checked_at": time.time(), "available": None,
                    "error": "无法检查官方桌面更新：网络不可用、限流或发布清单不完整。请稍后重试。"}
        write_json(paths(home) / "update-feed.json", feed)
    return status(home)


def configure(home, auto_check):
    with startup_lock(paths(home) / ".installation-config.lock"):
        location = Path(home) / "installation.json"
        value = read_json(location)
        value["auto_check_updates"] = bool(auto_check)
        write_json(location, value)
    return status(home)


def operation(home, **changes):
    location = paths(home) / "update-operation.json"
    value = read_json(location)
    value.update(changes, updated_at=time.time())
    write_json(location, value)
    return value


def request(home, version, *, confirmed=False):
    if confirmed is not True:
        raise ValueError("请确认具体版本后再升级。")
    Version(version)
    with startup_lock(paths(home) / ".update-request.lock"):
        previous = read_json(paths(home) / "update-operation.json")
        if previous.get("state") in ACTIVE or (paths(home) / "update-maintenance.json").exists():
            raise ValueError("已有更新正在执行；请查看进度，异常退出后使用恢复入口。")
        feed = read_json(paths(home) / "update-feed.json")
        available = feed.get("available")
        if feed.get("error") or not available or available["version"] != version:
            raise ValueError("确认的版本不再可用，请重新检查更新。")
        from desktop import service
        service.require_idle(home)
        job = uuid.uuid4().hex
        write_json(paths(home) / "update-operation.json", {"state": "queued", "job": job,
            "version": version, "updated_at": time.time(), "error": None})
        try:
            service.schedule_update(home, job)
        except (OSError, RuntimeError, ValueError) as exc:
            operation(home, state="failed", error="系统未能启动独立更新器，请检查后台注册权限后重试。")
            raise RuntimeError("系统未能启动独立更新器；已安装文件保留。") from exc
    return status(home)


def require_admission(data_dir):
    from desktop import shutdown
    if shutdown.active(data_dir):
        from fastapi import HTTPException
        raise HTTPException(409, "ClickClick 正在关闭，暂不接收新任务或恢复任务。")
    if (Path(data_dir) / "update-maintenance.json").exists():
        from fastapi import HTTPException
        raise HTTPException(409, "ClickClick 正在升级，暂不接收新任务或恢复任务；请等待更新完成。")


def prepare(home, job, db):
    from desktop import shutdown
    if shutdown.active(paths(home)):
        raise ValueError("ClickClick 正在关闭，请稍后重试升级。")
    from shared.schemas import TaskStatus
    value = read_json(paths(home) / "update-operation.json")
    if value.get("job") != job or value.get("state") != "downloading":
        raise ValueError("更新请求已失效。")
    # No await between DB check and gate write: task admissions in the API's
    # single event loop cannot pass their final synchronous check concurrently.
    for state in ("queued", "running", "pausing"):
        if db.assistant_task_page(status=TaskStatus(state), limit=1):
            raise ValueError("还有活动手机任务，请先完成、暂停或取消，再重试升级。")
    write_json(paths(home) / "update-maintenance.json", {"job": job})
    return {"prepared": True}


def clear_gate(home, job):
    gate = paths(home) / "update-maintenance.json"
    if read_json(gate).get("job") == job:
        gate.unlink(missing_ok=True)


def download(connection, available, directory):
    destination = Path(directory) / available["name"]
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + 15 * 60
    try:
        with connection.stream("GET", available["asset_url"], headers={"Accept": "application/octet-stream"}) as response:
            response.raise_for_status()
            with destination.open("wb") as stream:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > available["size"] or time.monotonic() > deadline:
                        raise ValueError("Installer download exceeds expected size/time limit")
                    digest.update(chunk)
                    stream.write(chunk)
        if size != available["size"] or digest.hexdigest() != available["sha256"]:
            raise ValueError("Installer checksum/size validation failed")
        return destination
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def execute_installer(archive, home):
    if sys.platform == "win32":
        command = [str(archive), "--unattended", "--home", str(Path(home).resolve())]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
        result = subprocess.run(command, capture_output=True, timeout=20 * 60, **kwargs)
    elif sys.platform == "darwin":
        # ditto preserves macOS bundle metadata/permissions; reject zip traversal
        # and unexpected binaries before extraction. Do not bypass Gatekeeper.
        from zipfile import ZipFile
        from desktop.install import payload_member
        with ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                payload_member(entry.filename.rstrip("/"))
        bundle = archive.with_suffix(".app")
        subprocess.run(["ditto", "-x", "-k", str(archive), str(archive.parent)], check=True, capture_output=True, timeout=120)
        binary = bundle / "Contents/MacOS" / archive.stem
        if not binary.is_file():
            raise ValueError("macOS installer bundle is incomplete")
        subprocess.run(["spctl", "--assess", "--type", "execute", str(bundle)],
                       check=True, capture_output=True, timeout=30)
        result = subprocess.run([str(binary), "--unattended", "--home", str(Path(home).resolve())],
                                capture_output=True, timeout=20 * 60)
    else:
        raise ValueError("Desktop upgrade supports Windows/macOS")
    if result.returncode:
        raise RuntimeError("Installer failed; old version will be restored")


def restore(home, old):
    from desktop import service
    # Stop only the identity-checked ClickClick backend; never another port owner.
    previous = service.backend_status(home)
    if previous["running"]:
        service.require_idle(home)
    service.stop_tray(home)
    if service.backend_status(home)["running"] or service.http_status(home)["running"]:
        service.native("stop", home)
        service.wait_stopped(home)
        service.wait_process_exited(home, previous.get("backend", {}).get("pid"))
    write_json(Path(home) / "current.json", old)
    make_launcher(Path(home))
    from desktop.entries import install_entries
    install_entries(home)
    service.native("install", home)
    service.native("start", home)
    service.wait_ready(home)
    service.start_tray(home)


def run(home, job):
    with startup_lock(paths(home) / ".update-run.lock", timeout=0):
        value = read_json(paths(home) / "update-operation.json")
        if value.get("job") != job or value.get("state") != "queued":
            raise ValueError("Update operation is not queued")
        old = read_json(Path(home) / "current.json")
        installing = False
        keep_gate = False
        try:
            operation(home, state="downloading")
            with client() as connection:
                available = discover(home, connection)
                if not available or available["version"] != value["version"]:
                    raise ValueError("Confirmed release changed; check and confirm again")
                with tempfile.TemporaryDirectory(prefix="installer-", dir=paths(home)) as temp:
                    artifact = download(connection, available, temp)
                    from desktop import service
                    with httpx.Client(base_url=service.base_url(home), timeout=15, trust_env=False) as local:
                        token = (paths(home) / "mcp-token").read_text().strip()
                        response = local.post("/api/setup/updates/prepare", headers={"Authorization": "Bearer " + token}, json={"job": job})
                        if not response.is_success:
                            raise ValueError("后台拒绝升级：请先完成、暂停或取消活动任务。")
                    operation(home, state="installing")
                    installing = True
                    execute_installer(artifact, home)
            if installed(home)[2]["version"] != value["version"]:
                raise RuntimeError("Installer did not activate the confirmed version")
            from desktop import service
            service.wait_ready(home)
            operation(home, state="succeeded", error=None)
        except Exception as exc:
            import logging
            logging.exception("Desktop update failed (job %s)", job)
            recovered = None
            if installing:
                try:
                    with startup_lock(paths(home) / ".install.lock", timeout=0):
                        restore(home, old)
                    recovered = True
                except Exception:
                    recovered = False
                    keep_gate = True
            operation(home, state="failed", rolled_back=recovered,
                error="更新未完成。" + ("旧版本已恢复。" if recovered else "") +
                      ("请检查启动日志并手动恢复；用户数据已保留。" if recovered is False else
                       "请检查网络、发布文件和任务状态后重试。"))
        finally:
            if not keep_gate:
                clear_gate(home, job)


def recover(home, *, confirmed=False):
    if confirmed is not True:
        raise ValueError("恢复更新状态需要明确确认。")
    with startup_lock(paths(home) / ".update-request.lock", timeout=0):
        with startup_lock(paths(home) / ".update-run.lock", timeout=0):
            with startup_lock(paths(home) / ".install.lock", timeout=0):
                value = read_json(paths(home) / "update-operation.json")
                if value.get("state") == "queued":
                    from desktop import service
                    service.remove_update(home, value["job"])
                clear_gate(home, value.get("job"))
                operation(home, state="failed", error="已恢复任务接收；请检查当前版本和启动状态后重新检查更新。")
    return status(home)
