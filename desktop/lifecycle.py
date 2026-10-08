"""Tray-facing lifecycle operations, independent of the native icon toolkit."""

from pathlib import Path
import time
from urllib.parse import quote
import uuid
import webbrowser

import httpx

from desktop import service, shutdown
from control_api.mcp_launcher import startup_lock

ACTIVE_STATES = ("queued", "running", "pausing")


class DesktopController:
    def __init__(self, home):
        self.home = Path(home).resolve()

    def client(self):
        token = (self.home / "data/mcp-token").read_text(encoding="utf-8").strip()
        return httpx.Client(base_url=service.base_url(self.home), timeout=10, trust_env=False,
                            headers={"Authorization": "Bearer " + token})

    def active_tasks(self, client):
        tasks = []
        for status in ACTIVE_STATES:
            cursor = None
            while True:
                response = client.get("/api/assistant/tasks", params={
                    "status": status, "limit": 50, **({"cursor": cursor} if cursor else {})})
                response.raise_for_status()
                page = response.json()
                tasks.extend(page["tasks"])
                cursor = page.get("next_cursor")
                if not cursor:
                    break
        return list({task["task_id"]: task for task in tasks}.values())

    def status(self):
        value = service.backend_status(self.home)
        if not value["running"]:
            return {"state": "stopped", "tasks": []}
        if service.has_http_service(self.home) and not service.http_status(self.home)["running"]:
            raise RuntimeError("HTTP MCP service is unavailable; inspect data/mcp-http.log")
        with self.client() as client:
            tasks = self.active_tasks(client)
        return {"state": "busy" if tasks else "running", "tasks": tasks}

    def start(self):
        with startup_lock(self.home / "data/.lifecycle.lock"):
            if not service.backend_status(self.home)["running"]:
                service.native("install", self.home)
                service.native("start", self.home)
            service.wait_ready(self.home)

    def open_console(self):
        self.start()
        if not webbrowser.open(service.base_url(self.home)):
            raise RuntimeError("无法打开浏览器，请访问 " + service.base_url(self.home))

    def stop(self, choose, *, timeout=120):
        """Return False on user return; never terminate tasks to force a stop."""
        with startup_lock(self.home / "data/.lifecycle.lock"):
            if not service.backend_status(self.home)["running"]:
                if service.http_status(self.home)["running"]:
                    service.native("stop", self.home)
                    service.wait_stopped(self.home)
                return True
            job = uuid.uuid4().hex
            with self.client() as client:
                response = client.post("/api/setup/shutdown/prepare", json={"job": job})
                response.raise_for_status()
                try:
                    tasks = self.active_tasks(client)
                    if tasks:
                        while tasks:
                            shown = {t["task_id"] for t in tasks}
                            action = choose(tasks)
                            if action not in {"pause", "cancel"}:
                                return False
                            # Renew after the user dialog; it can stay open indefinitely.
                            response = client.post("/api/setup/shutdown/prepare", json={"job": job})
                            response.raise_for_status()
                            tasks = self.active_tasks(client)
                            if {t["task_id"] for t in tasks}.issubset(shown):
                                break
                            # If the dialog lease expired, show newly admitted tasks too.
                        for task in tasks:
                            response = client.post("/api/tasks/" + quote(task["task_id"], safe="") + "/" + action)
                            response.raise_for_status()
                        deadline = time.monotonic() + timeout
                        while self.active_tasks(client):
                            if time.monotonic() >= deadline:
                                raise RuntimeError("任务尚未安全暂停或取消，后台继续运行。请在控制台查看任务后重试关闭。")
                            time.sleep(.5)
                    service.require_idle(self.home)
                    service.native("stop", self.home)
                    service.wait_stopped(self.home)
                    return True
                finally:
                    # Also works after the backend has stopped. Only remove our lease.
                    shutdown.release(self.home / "data", job)
