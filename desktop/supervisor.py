"""OS-owned desktop service supervising independently scheduled local services."""

import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from control_api.mcp_launcher import startup_lock
from desktop.files import read_json, write_json
from desktop.install import installed, installed_command


def ensure_ports(home):
    """Allocate once; later conflicts must not silently invalidate registration."""
    home = Path(home).resolve()
    with startup_lock(home / "data/.installation-config.lock"):
        path = home / "installation.json"
        config = read_json(path)
        backend = config.get("port", 18080)
        saved = config.get("mcp_port")
        if saved is not None:
            if type(saved) is not int or not 1 <= saved <= 65535 or saved == backend:
                raise ValueError("Invalid saved HTTP MCP port")
            return saved
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", backend + 1 if backend < 65535 else 0))
            except OSError:
                listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        write_json(path, {**config, "mcp_port": port})
        return port


def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class ServiceGroup:
    """Restart a crashed child without cancelling work in its healthy sibling."""

    def __init__(self, launch, *, clock=time.monotonic):
        self.launch, self.clock = launch, clock
        self.children = {}
        self.failures = {}
        self.started = {}
        self.retry_at = {}

    def tick(self):
        now = self.clock()
        for name in ("backend", "http-mcp"):
            child = self.children.get(name)
            if child is not None and child.poll() is None:
                continue
            if child is not None:
                logging.error("Desktop %s exited with code %s", name, child.returncode)
                failures = 0 if now - self.started[name] >= 60 else self.failures.get(name, 0)
                self.failures[name] = failures + 1
                self.children.pop(name)
                self.retry_at[name] = now + min(2 ** min(failures, 5), 30)
            if now >= self.retry_at.get(name, 0):
                try:
                    self.children[name] = self.launch(name)
                except OSError:
                    logging.exception("Could not launch desktop %s", name)
                    failures = self.failures.get(name, 0)
                    self.failures[name] = failures + 1
                    self.retry_at[name] = now + min(2 ** min(failures, 5), 30)
                    continue
                self.started[name] = now

    def close(self):
        for name in ("http-mcp", "backend"):
            child = self.children.get(name)
            if child is not None:
                terminate(child)


def run(home):
    home = Path(home).resolve()
    stopped = threading.Event()
    def stop(signum, frame):
        stopped.set()
    previous_handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    state_path = home / "data/service-state.json"
    stop_path = home / "data/service-stop.request"
    with startup_lock(home / "data/.desktop-service.lock", timeout=0):
        ensure_ports(home)
        version = installed(home)[2]["version"]
        def launch(name):
            command, args = installed_command(home, name)
            # OS service owns this entire process tree; no independent breakaway.
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            return subprocess.Popen([command, *args], stdin=subprocess.DEVNULL,
                creationflags=flags, close_fds=True)
        group = ServiceGroup(launch)
        previous_state = None
        try:
            while not stopped.is_set():
                if read_json(stop_path).get("pid") == os.getpid():
                    break
                group.tick()
                state = {"pid": os.getpid(), "version": version,
                    "children": {name: child.pid for name, child in group.children.items()}}
                if state != previous_state:
                    write_json(state_path, state)
                    previous_state = state
                stopped.wait(.5)
        finally:
            group.close()
            state_path.unlink(missing_ok=True)
            if read_json(stop_path).get("pid") == os.getpid():
                stop_path.unlink(missing_ok=True)
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
