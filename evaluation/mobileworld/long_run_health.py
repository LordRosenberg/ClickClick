"""Fail-closed health and maintenance gates for MobileWorld runs.

All host-side ADB access is pinned to the forwarded MobileWorld endpoint. The
hardware serial check is repeated after every reconnect/restart so this module
cannot fall through to a host emulator with the same ADB display name.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from driver.accessibility import AccessibilityCollectorClient
from driver.adb import adb_bin


ADB_KEYBOARD = "com.android.adbkeyboard/.AdbIME"
APP_PACKAGES = {
    "Calendar": "org.fossify.calendar", "Camera": "com.android.camera2",
    "Chrome": "com.android.chrome", "Clock": "com.google.android.deskclock",
    "Contacts": "com.google.android.contacts",
    "Docreader": "at.tomtasche.reader",
    "Files": "com.google.android.documentsui",
    "Gallery": "gallery.photomanager.picturegalleryapp.imagegallery",
    "Mail": "com.gmailclone", "Maps": "com.google.android.apps.maps",
    "Mastodon": "org.joinmastodon.android.mastodon",
    "Mattermost": "com.mattermost.rnbeta",
    "Messages": "com.google.android.apps.messaging", "SMS": "com.google.android.apps.messaging",
    "Settings": "com.android.settings", "Taodian": "com.testmall.app",
}
MATTERMOST_HELPER = "/app/service/src/mobile_world/runtime/app_helpers/mattermost.py"
THANKSGIVING_TASK_HELPER = "/app/service/src/mobile_world/tasks/definitions/gmail/thanksgiving_prep.py"
MOBILEWORLD_AVD_LOCKS = (
    "/root/.android/avd/Pixel_8_API_34_x86_64.avd/multiinstance.lock",
    "/root/.android/avd/Pixel_8_API_34_x86_64.avd/hardware-qemu.ini.lock",
)
PREVENTIVE_RESTART_POLICY = {
    "version": "container-uptime-boundary-v1", "max_uptime_s": 3600,
    "preparation_cleanup_reserve_s": 300,
}


class FullRestartRequired(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__("MobileWorld health remains degraded after targeted recovery")
        self.report = report


@dataclass(frozen=True)
class MobileWorldTarget:
    backend: str = "http://127.0.0.1:6800"
    adb_target: str = "127.0.0.1:5556"
    expected_serial: str = "EMULATOR36X2X12X0"
    environment_device: str = "emulator-5554"
    container: str = "mobile_world_env_0"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _run(command: list[str], *, timeout: float = 30, binary: bool = False) -> bytes | str:
    result = subprocess.run(command, check=True, capture_output=True, timeout=timeout, text=not binary)
    return result.stdout


def adb(target: MobileWorldTarget, *args: str, timeout: float = 30, binary: bool = False) -> bytes | str:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _run([adb_bin(), "-s", target.adb_target, *args], timeout=timeout, binary=binary)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt == 2:
                break
            # Reconnect only the explicit MobileWorld forwarding endpoint.
            subprocess.run(
                [adb_bin(), "connect", target.adb_target],
                capture_output=True, timeout=10,
            )
            time.sleep(1)
    assert last_error is not None
    raise last_error


def verify_identity(target: MobileWorldTarget) -> dict[str, str]:
    state = str(adb(target, "get-state", timeout=15)).strip()
    serial = str(adb(target, "shell", "getprop", "ro.serialno", timeout=15)).strip()
    # The emulator console command is not forwarded over a TCP ADB bridge.
    avd = str(adb(target, "shell", "getprop", "ro.boot.qemu.avd_name", timeout=15)).strip()
    if state != "device" or serial != target.expected_serial:
        raise RuntimeError(
            f"refusing ADB target {target.adb_target}: state={state!r}, "
            f"expected serial={target.expected_serial!r}, actual={serial!r}"
        )
    return {"state": state, "serial": serial, "avd": avd}


def guest_uptime(target: MobileWorldTarget) -> float:
    raw = str(adb(target, "shell", "cat", "/proc/uptime", timeout=15)).strip()
    try:
        uptime = float(raw.split()[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"cannot read MobileWorld guest uptime: {raw!r}") from exc
    if not math.isfinite(uptime) or uptime < 0:
        raise RuntimeError(f"invalid MobileWorld guest uptime: {raw!r}")
    return uptime


def container_uptime(target: MobileWorldTarget) -> float:
    """Use container/QEMU lifetime; MobileWorld snapshot loads rewind guest uptime."""
    raw = str(_run([
        "docker", "inspect", "--format", "{{.State.StartedAt}}", target.container,
    ], timeout=20)).strip()
    try:
        started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"cannot parse MobileWorld container start time: {raw!r}") from exc
    uptime = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
    if not math.isfinite(uptime) or uptime < 0:
        raise RuntimeError(f"invalid MobileWorld container uptime: {uptime!r}")
    return uptime


def preventive_restart_check(target: MobileWorldTarget, task: str, task_max_seconds: float) -> dict[str, Any]:
    policy = PREVENTIVE_RESTART_POLICY
    required = float(task_max_seconds) + policy["preparation_cleanup_reserve_s"]
    if required >= policy["max_uptime_s"]:
        raise RuntimeError("task budget cannot fit preventive restart policy")
    uptime = container_uptime(target)
    return {
        "task": task, "checked_at": time.time(), "policy": dict(policy),
        "uptime_source": "docker_container_started_at",
        "uptime_s": uptime, "task_max_seconds": task_max_seconds,
        "projected_uptime_s": uptime + required,
        "restart_due": uptime + required >= policy["max_uptime_s"],
    }


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("ADB screencap did not return a PNG")
    width, height = struct.unpack(">II", payload[16:24])
    if width < 1 or height < 1:
        raise RuntimeError(f"invalid screenshot dimensions: {width}x{height}")
    return width, height


def _tree_nodes(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    return 1 + sum(_tree_nodes(child) for child in node.get("children") or [])


async def _collector_probe(target: MobileWorldTarget) -> dict[str, Any]:
    client = AccessibilityCollectorClient(target.adb_target)
    started = time.monotonic()
    try:
        health = await client.health(timeout=3)
        if not health.get("ready"):
            raise RuntimeError(f"collector health: {health}")
        await client.warm(timeout=5)
        snapshot = await client.fetch_primary(timeout=10)
        raw = snapshot.to_raw_tree(elapsed_ms=(time.monotonic() - started) * 1000)
        count = _tree_nodes(raw)
        if count <= 1:
            raise RuntimeError("collector returned no window/content nodes")
        return {"ready": True, "latency_s": time.monotonic() - started, "node_count": count}
    finally:
        await client.close()


def _probe(name: str, function) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = dict(function())
        value.setdefault("ready", True)
    except Exception as exc:
        value = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
    value.update(channel=name, latency_s=value.get("latency_s", time.monotonic() - started))
    return value


def probe_channels(target: MobileWorldTarget) -> dict[str, Any]:
    def backend() -> dict[str, Any]:
        health = requests.get(f"{target.backend}/health", timeout=10)
        health.raise_for_status()
        tasks = requests.get(f"{target.backend}/task/list", timeout=15)
        tasks.raise_for_status()
        return {"ready": health.json() is not None and len(tasks.json()) > 0, "task_count": len(tasks.json())}

    def pixels() -> dict[str, Any]:
        payload = adb(target, "exec-out", "screencap", "-p", timeout=15, binary=True)
        assert isinstance(payload, bytes)
        width, height = _png_dimensions(payload)
        return {"ready": True, "width": width, "height": height, "bytes": len(payload)}

    def ime() -> dict[str, Any]:
        installed = str(adb(target, "shell", "ime", "list", "-s", timeout=15))
        current = str(adb(target, "shell", "settings", "get", "secure", "default_input_method", timeout=15)).strip()
        return {"ready": ADB_KEYBOARD in installed and current == ADB_KEYBOARD, "current": current}

    return {
        "identity": _probe("identity", lambda: {"ready": True, **verify_identity(target)}),
        "backend": _probe("backend", backend),
        "pixels": _probe("pixels", pixels),
        "collector": _probe("collector", lambda: asyncio.run(_collector_probe(target))),
        "ime": _probe("ime", ime),
    }


def prepare_clickclick_device(target: MobileWorldTarget, collector_apk: Path, ime_apk: Path) -> dict[str, Any]:
    script = Path(__file__).with_name("prepare_device.py")
    python = Path(__file__).parents[2] / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        import sys
        python = Path(sys.executable)
    output = _run([
        str(python), str(script), "--target", target.adb_target,
        "--expected-device-serial", target.expected_serial,
        "--collector-apk", str(collector_apk), "--ime-apk", str(ime_apk),
    ], timeout=180)
    return json.loads(str(output))


def ensure_channels(
    target: MobileWorldTarget, evidence_root: Path, collector_apk: Path,
    ime_apk: Path, *, allow_full_restart: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {"started_at": time.time(), "attempts": [], "recoveries": []}
    first = probe_channels(target)
    report["attempts"].append(first)
    degraded = [name for name, value in first.items() if not value.get("ready")]
    if degraded and set(degraded) <= {"collector", "ime"}:
        report["recoveries"].append({
            "channels": degraded, "operation": "reconcile_clickclick_collector_and_ime",
            "result": prepare_clickclick_device(target, collector_apk, ime_apk),
        })
        report["attempts"].append(probe_channels(target))
    final = report["attempts"][-1]
    remaining = [name for name, value in final.items() if not value.get("ready")]
    report.update(finished_at=time.time(), ready=not remaining, remaining_degraded=remaining)
    evidence = evidence_root / "health" / f"pre-task-{time.time_ns()}.json"
    atomic_json(evidence, report)
    report["evidence"] = str(evidence)
    if remaining:
        if allow_full_restart:
            raise FullRestartRequired(report)
        raise RuntimeError(f"MobileWorld health remains degraded: {remaining}")
    return report


def required_packages(apps: list[str]) -> dict[str, str]:
    unknown = sorted(set(apps) - set(APP_PACKAGES))
    if unknown:
        raise RuntimeError(f"no verified MobileWorld package mapping for: {unknown}")
    return {name: APP_PACKAGES[name] for name in apps}


def verify_task_apps(target: MobileWorldTarget, apps: list[str]) -> dict[str, Any]:
    packages = required_packages(apps)
    missing, evidence = [], {}
    for name, package in packages.items():
        result = subprocess.run(
            [adb_bin(), "-s", target.adb_target, "shell", "pm", "path", package],
            capture_output=True, text=True, timeout=20,
        )
        present = result.returncode == 0 and "package:" in result.stdout
        evidence[name] = {"package": package, "installed": present}
        if not present:
            missing.append(name)
    if missing:
        raise RuntimeError(f"MobileWorld image is missing task apps: {missing}")
    return {"apps": evidence, "checked_at": time.time()}


def verify_official_session_fix(target: MobileWorldTarget, apps: list[str]) -> None:
    """Mattermost snapshot tokens require MobileWorld's upstream expiry patch."""
    if "Mattermost" not in apps:
        return
    result = subprocess.run(
        ["docker", "exec", target.container, "grep", "-q", "_extend_session_expiry", MATTERMOST_HELPER],
        capture_output=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "MobileWorld container lacks the official Mattermost session-expiry "
            "initialization fix (2026-04-15); update the official helper before testing"
        )


def verify_official_task_fix(target: MobileWorldTarget, task: str) -> None:
    """Catch a known upstream image/task-code mismatch before scored runs."""
    if task != "ThanksgivingPrepTask":
        return
    result = subprocess.run(
        ["docker", "exec", target.container, "grep", "-q",
         "from mobile_world.runtime.app_helpers.system import reset_chrome",
         THANKSGIVING_TASK_HELPER],
        capture_output=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "MobileWorld container lacks the official ThanksgivingPrepTask "
            "reset_chrome import; update the official task module before testing"
        )


def recover_stale_avd_locks(target: MobileWorldTarget) -> list[str]:
    """Move exact stale locks aside only when this container has no emulator."""
    processes = str(_run(["docker", "exec", target.container, "ps", "-eo", "comm="], timeout=20))
    if any(name.strip().startswith(("qemu", "emulator")) for name in processes.splitlines()):
        return []
    moved: list[str] = []
    for path in MOBILEWORLD_AVD_LOCKS:
        exists = subprocess.run(
            ["docker", "exec", target.container, "test", "-e", path],
            capture_output=True, timeout=15,
        ).returncode == 0
        if exists:
            backup = f"{path}.stale-{time.time_ns()}"
            _run(["docker", "exec", target.container, "mv", path, backup], timeout=20)
            moved.append(backup)
    return moved


def restart_container(target: MobileWorldTarget, proxy_script: Path, timeout_s: float = 240) -> dict[str, Any]:
    """Restart only the named MobileWorld container and re-establish its ADB bridge."""
    started = time.time()
    _run(["docker", "restart", target.container], timeout=90)
    deadline, last_error = time.monotonic() + timeout_s, ""
    stale_locks_moved: list[str] = []
    recovery_at = time.monotonic() + min(75, timeout_s / 2)
    recovery_attempted = False
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{target.backend}/health", timeout=5)
            if response.ok:
                break
        except Exception as exc:
            last_error = str(exc)
        if not recovery_attempted and time.monotonic() >= recovery_at:
            recovery_attempted = True
            stale_locks_moved = recover_stale_avd_locks(target)
            if stale_locks_moved:
                _run(["docker", "restart", target.container], timeout=90)
        time.sleep(3)
    else:
        raise RuntimeError(f"MobileWorld backend did not recover: {last_error}")
    _run(["docker", "cp", str(proxy_script), f"{target.container}:/tmp/clickclick_mobileworld_adb_proxy.py"], timeout=30)
    _run(["docker", "exec", "-d", target.container, "python3", "/tmp/clickclick_mobileworld_adb_proxy.py"], timeout=30)
    subprocess.run([adb_bin(), "disconnect", target.adb_target], capture_output=True, timeout=15)
    for _ in range(40):
        subprocess.run([adb_bin(), "connect", target.adb_target], capture_output=True, timeout=10)
        try:
            identity = verify_identity(target)
            return {
                "started_at": started, "finished_at": time.time(),
                "identity": identity, "stale_avd_locks_moved": stale_locks_moved,
            }
        except Exception as exc:
            last_error = str(exc)
            time.sleep(3)
    raise RuntimeError(f"MobileWorld ADB did not recover safely: {last_error}")
