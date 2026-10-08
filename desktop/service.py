"""Per-user native background lifecycle, independently owned by the OS."""

from __future__ import annotations

import getpass
import hashlib
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET

from desktop.files import private_write, read_json
from desktop.install import installed_command, installed


def service_id(home):
    return "clickclick-" + hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:12]


def autostart_enabled(home):
    return read_json(Path(home) / "installation.json").get("autostart", True) is True


def windows_definition(home, *, job=None, tray=False):
    command, args = installed_command(home, "update-worker" if job else "tray" if tray else "serve")
    if job:
        args += ["--job", job]
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    def child(parent, name, text=None, **attrs):
        item = ET.SubElement(parent, "{" + namespace + "}" + name, attrs)
        item.text = text
        return item
    task = ET.Element("{" + namespace + "}Task", {"version": "1.2"})
    user = getpass.getuser()
    triggers = child(task, "Triggers")
    if not job and autostart_enabled(home):
        logon = child(triggers, "LogonTrigger")
        child(logon, "Enabled", "true")
        child(logon, "UserId", user)
    principal = child(child(task, "Principals"), "Principal", id="Author")
    child(principal, "UserId", user)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    for name, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false", "StartWhenAvailable": "true", "ExecutionTimeLimit": "PT0S"}.items():
        child(settings, name, value)
    if not job:
        retry = child(settings, "RestartOnFailure")
        child(retry, "Interval", "PT1M")
        child(retry, "Count", "3")
    action = child(child(task, "Actions", Context="Author"), "Exec")
    # pythonw suppresses a desktop console window, while serve logs to a file.
    no_window = Path(command).with_name("pythonw.exe")
    child(action, "Command", str(no_window if no_window.exists() else command))
    child(action, "Arguments", subprocess.list2cmdline(args))
    return ET.tostring(task, encoding="unicode")


def mac_definition(home, *, job=None, tray=False):
    command, args = installed_command(home, "update-worker" if job else "tray" if tray else "serve")
    if job:
        args += ["--job", job]
    logs = Path(home).resolve() / "data"
    log = "update-worker.log" if job else "tray-launch.log" if tray else "service.log"
    return plistlib.dumps({"Label": "com.clickclick." + service_id(home) + ("-update-" + job if job else "-tray" if tray else ""),
        "ProgramArguments": [command, *args], "RunAtLoad": True, "KeepAlive": not bool(job) and not tray,
        "ThrottleInterval": 30, "ProcessType": "Background",
        "StandardOutPath": str(logs / log), "StandardErrorPath": str(logs / log)})


def schedule_update(home, job):
    import re
    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise ValueError("Invalid update job")
    name = service_id(home) + "-update-" + job
    definition = Path(home) / "data" / (name + (".xml" if sys.platform == "win32" else ".plist"))
    if sys.platform == "win32":
        private_write(definition, windows_definition(home, job=job))
        commands = [["schtasks", "/Create", "/TN", name, "/XML", str(definition), "/F"],
                    ["schtasks", "/Run", "/TN", name]]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    elif sys.platform == "darwin":
        private_write(definition, mac_definition(home, job=job).decode())
        commands = [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(definition)]]
        kwargs = {}
    else:
        raise ValueError("Desktop updater supports Windows/macOS")
    for command in commands:
        result = subprocess.run(command, capture_output=True, timeout=30, **kwargs)
        if result.returncode:
            raise RuntimeError("Native updater registration/start failed")


def remove_update(home, job):
    import re
    if not isinstance(job, str) or not re.fullmatch(r"[a-f0-9]{32}", job):
        raise ValueError("Invalid update job")
    name = service_id(home) + "-update-" + job
    if sys.platform == "win32":
        result = subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True,
                                 timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    elif sys.platform == "darwin":
        result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.clickclick." + name],
                                 capture_output=True, timeout=30)
    else:
        raise ValueError("Desktop updater supports Windows/macOS")
    if result.returncode:
        raise RuntimeError("Could not remove pending updater job; do not resume task admission yet")


def native(action, home, *, tray=False):
    name = service_id(home) + ("-tray" if tray else "")
    owned = []
    if sys.platform == "win32":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
        if action == "install":
            definition = Path(home) / ("tray-service.xml" if tray else "service.xml")
            private_write(definition, windows_definition(home, tray=tray))
            command = ["schtasks", "/Create", "/TN", name, "/XML", str(definition), "/F"]
        elif action == "start":
            command = ["schtasks", "/Run", "/TN", name]
        elif action == "stop":
            if not tray:
                owned = service_processes(home)
                request_service_stop(home, owned)
            command = ["schtasks", "/End", "/TN", name]
        elif action == "remove":
            command = ["schtasks", "/Delete", "/TN", name, "/F"]
        else:
            raise ValueError("Unknown lifecycle action")
    elif sys.platform == "darwin":
        kwargs = {}
        label = "com.clickclick." + name
        domain = f"gui/{os.getuid()}"
        login_definition = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        definition = login_definition if autostart_enabled(home) else Path(home) / (name + ".plist")
        if action == "install":
            private_write(definition, mac_definition(home, tray=tray).decode())
            if definition != login_definition:
                login_definition.unlink(missing_ok=True)
            # New definitions are loaded at start, existing service stays until explicit stop.
            return {"registered": True, "path": str(definition)}
        elif action == "start":
            loaded = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True, timeout=10)
            command = (["launchctl", "kickstart", domain + "/" + label] if not loaded.returncode else
                       ["launchctl", "bootstrap", domain, str(definition)])
        elif action == "stop":
            command = ["launchctl", "bootout", domain + "/" + label]
        elif action == "remove":
            definition.unlink(missing_ok=True)
            login_definition.unlink(missing_ok=True)
            return {"removed": True}
        else:
            raise ValueError("Unknown lifecycle action")
    else:
        raise ValueError("Consumer background service supports Windows/macOS")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, **kwargs)
    if owned:
        finish_service_exit(owned)
        # A coordinated exit may have already completed the native job.
        if result.returncode and all(not process.is_running() for _, process in owned):
            return {"accepted": True, "action": action}
    if result.returncode:
        raise RuntimeError("Background service operation failed; inspect OS policy/permissions and service.log. "
                           "Installed files are preserved; launch ClickClick independently if necessary.")
    return {"accepted": True, "action": action}


def service_processes(home):
    """Capture only this installation's supervisor/children before stopping."""
    state = read_json(Path(home) / "data/service-state.json")
    candidates = {"serve": state.get("pid"), **state.get("children", {})}
    if not any(isinstance(pid, int) and pid > 0 for pid in candidates.values()):
        return []
    import psutil
    home = Path(home).resolve()
    owned = []
    for operation in ("http-mcp", "backend", "serve"):
        pid = candidates.get(operation)
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            process = psutil.Process(pid)
            if not Path(process.exe()).resolve().is_relative_to(home / "versions"):
                continue  # Stale PID reused by another application.
            args = process.cmdline()
            if ("desktop.main" not in args or "--home" not in args or args[-1] != operation or
                    Path(args[args.index("--home") + 1]).resolve() != home):
                raise RuntimeError("Cannot confirm desktop service process ownership")
            owned.append((operation, process))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("Cannot verify desktop service process ownership") from exc
    return owned


def request_service_stop(home, owned):
    """Windows /End does not reliably reap children; ask the supervisor first."""
    if not owned:
        return
    from desktop.files import write_json
    import psutil
    for operation, process in owned:
        if operation != "serve":
            continue
        write_json(Path(home) / "data/service-stop.request", {"pid": process.pid})
        try:
            process.wait(timeout=35)
        except psutil.TimeoutExpired:
            pass  # Native stop and verified-child cleanup remain the fallback.


def finish_service_exit(owned):
    """Reap captured owned processes, including orphaned children of old builds."""
    import psutil
    for _, process in owned:
        try:
            process.wait(timeout=.25)
        except psutil.TimeoutExpired:
            try:
                process.terminate()
                process.wait(timeout=10)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        except psutil.NoSuchProcess:
            pass


def configure_autostart(home, enabled):
    """Change future login registration without ending the running session."""
    from desktop.files import write_json
    from control_api.mcp_launcher import startup_lock
    with startup_lock(Path(home) / "data/.installation-config.lock"):
        path = Path(home) / "installation.json"
        previous = read_json(path)
        write_json(path, {**previous, "autostart": bool(enabled)})
        try:
            native("install", home)
            native("install", home, tray=True)
        except Exception:
            write_json(path, previous)
            # Try restoring both definitions, but never report success on failure.
            for tray in (False, True):
                try:
                    native("install", home, tray=tray)
                except Exception:
                    pass
            raise
    return {"autostart": bool(enabled)}


def stop_tray(home, timeout=20):
    """Ask either a shortcut-owned or OS-owned manager to leave on activation."""
    import time
    from control_api.mcp_launcher import startup_lock
    previous_pid = read_json(Path(home) / "data/tray-state.json").get("pid")
    request = Path(home) / "data/tray-close.request"
    private_write(request, "version activation\n")
    deadline = time.monotonic() + timeout
    while True:
        try:
            with startup_lock(Path(home) / "data/.tray.lock", timeout=0):
                if sys.platform == "darwin":
                    label = "com.clickclick." + service_id(home) + "-tray"
                    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/" + label],
                                            capture_output=True, timeout=10)
                    if loaded.returncode == 0:
                        # A stopped macOS UI can leave the previous version loaded.
                        native("stop", home, tray=True)
                finish_tray_exit(home, previous_pid)
                return
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise RuntimeError("ClickClick 托盘未能退出，请先结束托盘操作后重试安装。")
            time.sleep(.2)


def finish_tray_exit(home, pid):
    """A released tray lock permits reaping an otherwise lingering GUI process."""
    if not isinstance(pid, int) or pid <= 0:
        return
    import psutil
    try:
        process = psutil.Process(pid)
        executable = Path(process.exe()).resolve()
        if not executable.is_relative_to(Path(home).resolve() / "versions"):
            return
        args = process.cmdline()
        if ("desktop.main" not in args or "--home" not in args or args[-1] != "tray" or
                Path(args[args.index("--home") + 1]).resolve() != Path(home).resolve()):
            raise RuntimeError("无法确认旧托盘进程身份，停止安装。")
        try:
            process.wait(timeout=2)
            return
        except psutil.TimeoutExpired:
            pass
        # Prefer ending the owned OS job so RestartOnFailure cannot restart it.
        try:
            native("stop", home, tray=True)
        except RuntimeError:
            process.terminate()  # A shortcut-owned tray has no running OS job.
        wait_process_exited(home, pid, timeout=5)
    except psutil.NoSuchProcess:
        return
    except psutil.AccessDenied as exc:
        raise RuntimeError("旧托盘未能退出，请检查系统权限后重试。") from exc


def wait_process_exited(home, pid, *, timeout=15):
    """Verify OS process exit as well as a released lock/closed HTTP port."""
    if not isinstance(pid, int) or pid <= 0:
        return
    import psutil
    try:
        process = psutil.Process(pid)
        executable = Path(process.exe()).resolve()
        if not executable.is_relative_to((Path(home).resolve() / "versions").resolve()):
            return  # Stale PID reused by a different application.
        process.wait(timeout=timeout)
    except psutil.NoSuchProcess:
        return
    except (psutil.TimeoutExpired, psutil.AccessDenied) as exc:
        raise RuntimeError("旧 ClickClick 进程尚未确认退出，请等待后重试安装。") from exc


def start_tray(home, timeout=20):
    import time
    from desktop.install import installed
    native("install", home, tray=True)
    native("start", home, tray=True)
    version = installed(home)[2]["version"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_json(Path(home) / "data/tray-state.json")
        if state.get("visible") and state.get("version") == version:
            return {"tray_visible": True}
        time.sleep(.2)
    raise RuntimeError("托盘未能启动，请查看 data/tray.log 或 data/tray-launch.log，再从桌面入口重试。")


def base_url(home):
    return "http://127.0.0.1:" + str(read_json(Path(home) / "installation.json").get("port", 18080))


def has_http_service(home):
    if not read_json(Path(home) / "current.json"):
        return False
    return (installed(home)[0] / "app/desktop/supervisor.py").is_file()


def mcp_base_url(home):
    if not has_http_service(home):
        return base_url(home)  # Older program restored by rollback.
    from desktop.supervisor import ensure_ports
    return "http://127.0.0.1:" + str(ensure_ports(home))


def http_status(home, *, timeout=2):
    import httpx
    if not has_http_service(home):
        return {"running": False}
    try:
        token = (Path(home) / "data/mcp-token").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {"running": False}  # New/failed installation before either service started.
    with httpx.Client(base_url=mcp_base_url(home), timeout=timeout, trust_env=False,
                     headers={"Authorization": "Bearer " + token}) as client:
        try:
            response = client.get("/mcp/identity")
        except httpx.RequestError:
            return {"running": False}
        if not response.is_success:
            raise RuntimeError("HTTP MCP port belongs to an unrecognized service")
        identity = response.json()
        if (identity.get("service") != "clickclick-mcp" or
                Path(identity.get("data_dir", "")).resolve() != (Path(home) / "data").resolve() or
                Path(identity.get("workspace", "")).resolve() != (installed(home)[0] / "app").resolve()):
            raise RuntimeError("HTTP MCP port belongs to another installation")
        return {"running": True, "backend": identity}


def backend_status(home, *, timeout=2):
    import httpx
    from desktop.install import installed
    _, _, _ = installed(home)
    with httpx.Client(base_url=base_url(home), timeout=timeout, trust_env=False) as client:
        try:
            response = client.get("/api/assistant/identity")
        except httpx.RequestError:
            return {"running": False}
        if not response.is_success:
            raise RuntimeError("Port is occupied by an unrecognized service; check the configured ClickClick port")
        identity = response.json()
        root, _, _ = installed(home)
        if (identity.get("service") != "clickclick" or
                Path(identity.get("data_dir", "")).resolve() != (Path(home) / "data").resolve() or
                Path(identity.get("workspace", "")).resolve() != (root / "app").resolve()):
            raise RuntimeError("Port is occupied by another backend; do not connect or terminate it")
        return {"running": True, "backend": identity, "console_url": base_url(home)}


def require_idle(home):
    import httpx
    if not backend_status(home)["running"]:
        return
    with httpx.Client(base_url=base_url(home), timeout=10, trust_env=False) as client:
        for status in ("queued", "running", "pausing"):
            response = client.get("/api/assistant/tasks", params={"limit": 1, "status": status})
            response.raise_for_status()
            if response.json()["tasks"]:
                raise RuntimeError("Tasks are active; complete or pause/cancel them before restarting ClickClick")


def wait_stopped(home, timeout=15):
    import time
    processes = read_json(Path(home) / "data/service-state.json")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not backend_status(home)["running"] and not http_status(home)["running"]:
            for pid in [processes.get("pid"), *processes.get("children", {}).values()]:
                wait_process_exited(home, pid)
            return
        time.sleep(.25)
    raise RuntimeError("Previous backend has not stopped; do not start another instance")


def wait_ready(home, timeout=45):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = backend_status(home)
        if status["running"]:
            if not has_http_service(home):
                return status
            gateway = http_status(home)
            if gateway["running"]:
                return {**status, "http_mcp": gateway["backend"], "mcp_url": mcp_base_url(home) + "/mcp/"}
        time.sleep(.25)
    raise RuntimeError("Background startup timed out; inspect data/service.log and the configured port")
