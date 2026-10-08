"""Small native manager; the backend and phone harness remain separate."""

import logging
import os
from pathlib import Path
import threading

from desktop import service
from desktop.files import read_json, write_json
from desktop.install import installed
from desktop.lifecycle import DesktopController
from control_api.mcp_launcher import startup_lock

LABELS = {"stopped": "已停止", "running": "运行中", "busy": "正在执行任务", "error": "异常"}


def icon_image(state):
    from PIL import Image, ImageDraw
    colors = {"stopped": "#94a3b8", "running": "#34d399", "busy": "#a78bfa", "error": "#fb7185"}
    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, 63, 63), radius=15, fill="#0b1224", outline="#334155", width=2)
    # A simplified phone/cursor mark stays readable at notification-area sizes.
    accent = "#94a3b8" if state == "stopped" else "#67e8f9"
    draw.rounded_rectangle((14, 9, 42, 54), radius=7, outline="#163a50", width=9)
    draw.rounded_rectangle((14, 9, 42, 54), radius=7, outline=accent, width=4)
    draw.line((23, 48, 31, 48), fill=accent, width=3)
    draw.polygon([(29, 24), (55, 39), (44, 42), (41, 54)], fill="#f8fafc", outline="#0b1224", width=2)
    draw.ellipse((46, 3, 60, 17), fill=colors[state], outline="#0b1224", width=3)
    return image


class TrayManager:
    def __init__(self, home, controller=None):
        self.home = Path(home).resolve()
        self.controller = controller or DesktopController(self.home)
        self.state = "stopped"
        self.tasks = []
        self.error = None
        self.operation = None
        self.autostart = service.autostart_enabled(self.home)
        self._actions = threading.Lock()
        self._closed = threading.Event()
        self.icon = None

    @property
    def status_text(self):
        return self.operation or "状态：" + LABELS[self.state] + (f"（{len(self.tasks)} 个）" if self.state == "busy" else "")

    def refresh(self):
        try:
            status = self.controller.status()
            self.state, self.tasks = status["state"], status["tasks"]
            self.error = None
        except Exception:
            self.state = "error"
            self.error = "无法确认后台状态，请打开控制台或检查安装目录中的 data/service.log。"
            logging.exception("Tray status refresh failed")
        if self.icon:
            self.icon.title = "ClickClick — " + self.status_text
            self.icon.icon = icon_image(self.state)
            self.icon.update_menu()
        write_json(self.home / "data/tray-state.json", {"pid": os.getpid(), "state": self.state,
            "active_tasks": len(self.tasks), "version": installed(self.home)[2]["version"],
            "operation": self.operation, "visible": bool(self.icon and getattr(self.icon, "visible", False))})

    def perform(self, action):
        """One operation at a time, off the icon's native event loop."""
        if not self._actions.acquire(blocking=False):
            return
        self.operation = "正在处理，请稍候…"

        def worker():
            from desktop.dialogs import choose_shutdown, show_error
            try:
                if action == "open":
                    self.controller.open_console()
                elif action == "start":
                    self.controller.start()
                elif action in {"stop", "exit"}:
                    if self.controller.stop(choose_shutdown) and action == "exit":
                        self._closed.set()
                        self.icon.stop()
                elif action == "autostart":
                    service.configure_autostart(self.home, not self.autostart)
                    self.autostart = service.autostart_enabled(self.home)
                elif action == "error":
                    show_error(self.error or "后台当前正常。")
            except Exception as exc:
                logging.exception("Tray operation %s failed", action)
                message = str(exc) if isinstance(exc, RuntimeError) else "操作未完成。请检查安装目录中的 data/tray.log。"
                show_error(message)
            finally:
                self.operation = None
                self._actions.release()
                if not self._closed.is_set():
                    self.refresh()

        threading.Thread(target=worker, name="clickclick-tray-action", daemon=True).start()

    def run(self):
        import pystray
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: self.status_text, None, enabled=False),
            pystray.MenuItem("打开控制台", lambda icon, item: self.perform("open"), default=True),
            pystray.MenuItem("启动后台", lambda icon, item: self.perform("start"),
                             enabled=lambda item: not self.operation and self.state in {"stopped", "error"}),
            pystray.MenuItem("停止后台", lambda icon, item: self.perform("stop"),
                             enabled=lambda item: not self.operation and self.state in {"running", "busy"}),
            pystray.MenuItem("开机自动启动", lambda icon, item: self.perform("autostart"),
                             checked=lambda item: self.autostart, enabled=lambda item: not self.operation),
            pystray.MenuItem("查看异常说明", lambda icon, item: self.perform("error"),
                             visible=lambda item: self.error is not None),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 ClickClick（同时停止后台）", lambda icon, item: self.perform("exit"),
                             enabled=lambda item: not self.operation))
        self.icon = pystray.Icon("ClickClick", icon_image(self.state), "ClickClick", menu)

        def monitor(icon):
            icon.visible = True
            try:
                self.controller.start()
            except Exception:
                logging.exception("Tray backend startup failed")
                from desktop.dialogs import show_error
                show_error("后台未能启动，请查看安装目录中的 data/service.log。托盘保留，可重试启动。")
            while not self._closed.is_set():
                if (self.home / "data/tray-close.request").exists() and self.operation is None:
                    self._closed.set()
                    icon.stop()
                    break
                self.refresh()
                self._closed.wait(5)

        try:
            # macOS requires the icon event loop on the main thread.
            self.icon.run(setup=monitor)
        finally:
            self._closed.set()
            (self.home / "data/tray-state.json").unlink(missing_ok=True)


def main(home):
    home = Path(home).resolve()
    home.joinpath("data").mkdir(exist_ok=True)
    try:
        with startup_lock(home / "data/.tray.lock", timeout=0):
            (home / "data/tray-close.request").unlink(missing_ok=True)
            log = home / "data/tray.log"
            logging.basicConfig(filename=log, encoding="utf-8", level=logging.INFO, force=True)
            logging.getLogger("httpx").setLevel(logging.WARNING)
            TrayManager(home).run()
    except TimeoutError:
        # The visible entry remains useful when the manager is already open.
        DesktopController(home).open_console()
