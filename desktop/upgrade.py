"""Validated installation transaction shared by interactive and unattended setup."""

from contextlib import contextmanager
from pathlib import Path
import uuid

from control_api.mcp_launcher import startup_lock
from desktop import cleanup, service
from desktop.entries import install_entries
from desktop.files import read_json
from desktop.install import install_payload


@contextmanager
def task_gate(home, running):
    """Close admission before the final idle check; don't cancel active tasks."""
    from desktop import shutdown
    from desktop.lifecycle import DesktopController
    job = None
    if running and not (home / "data/update-maintenance.json").exists():
        job = uuid.uuid4().hex
        with DesktopController(home).client() as client:
            response = client.post("/api/setup/shutdown/prepare", json={"job": job})
            if not response.is_success:
                raise RuntimeError("无法暂停任务接收，请检查后台状态后重试安装。")
    try:
        yield
    finally:
        if job:
            shutdown.release(home / "data", job)


def install(home, payload, *, no_start=False):
    home = Path(home).resolve()
    with startup_lock(home / "data/.install.lock", timeout=0):
        # Direct CLI installation needs the same preflight as the bootstrap.
        install_payload(payload, home, activate=False)
        old = read_json(home / "current.json")
        previous = service.backend_status(home) if old else {"running": False}
        if previous["running"]:
            service.require_idle(home)
        activated = False
        try:
            with task_gate(home, previous["running"]), startup_lock(home / "data/.lifecycle.lock", timeout=0):
                if previous["running"]:
                    service.require_idle(home)
                service.stop_tray(home)
                if previous["running"] or service.http_status(home)["running"]:
                    if previous["running"]:
                        service.require_idle(home)
                    service.native("stop", home)
                    service.wait_stopped(home)
                    service.wait_process_exited(home, previous.get("backend", {}).get("pid"))
                activated = True
                result = install_payload(payload, home)
                result.update(install_entries(home))
                # Refresh both login entries even for a deliberately stopped install.
                service.native("install", home)
                service.native("install", home, tray=True)
                if not no_start:
                    service.native("start", home)
                    result.update(service.wait_ready(home))
            # The new tray takes the lifecycle lock itself while checking startup.
            if not no_start:
                result.update(service.start_tray(home))
            cleanup.queue(home)
        except Exception:
            if old and activated:
                from desktop.updates import restore
                restore(home, old)
            raise
        if not no_start:
            result["program_cleanup"] = cleanup.retry(home, install_locked=True)
        return result
