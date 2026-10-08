"""Connect stdio adapters to one independently owned local backend."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

INDEPENDENT_BACKEND_ERROR = (
    "This Windows client prohibits an independent backend process. "
    "Start python -m control_api.main in a separate terminal first, "
    "then connect MCP with --no-start. No backend was started.")


def local_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise ValueError("MCP backend URL must be a loopback HTTP(S) origin")
    return value.rstrip("/")


@contextmanager
def startup_lock(path: Path, timeout: float = 30):
    """OS-backed lock: release on process exit, without stale lock deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for ClickClick backend startup lock")
                time.sleep(.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_backend(base_url: str, workspace: Path, data_dir: Path, *, auto_start=True,
                   timeout=30, verify=True) -> dict:
    base_url = local_url(base_url)
    data_dir = data_dir.resolve()

    def discover(client):
        try:
            response = client.get("/api/assistant/identity")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise RuntimeError("Backend TLS certificate verification failed; check local certificate and hostname") from exc
            return None
        response.raise_for_status()
        payload = response.json()
        if (not isinstance(payload, dict) or not payload.get("data_dir") or not payload.get("workspace")
                or payload.get("service") != "clickclick" or payload.get("assistant_api_version") != 1
                or Path(payload.get("data_dir", "")).resolve() != data_dir
                or Path(payload.get("workspace", "")).resolve() != workspace.resolve()):
            raise RuntimeError("Backend identity/workspace/data directory mismatch; select the correct port and workspace")
        return payload

    with startup_lock(data_dir / ".mcp-launch.lock", timeout=timeout):
        with httpx.Client(base_url=base_url, timeout=2, trust_env=False, verify=verify) as client:
            existing = discover(client)
            if existing:
                return existing
            if not auto_start:
                raise RuntimeError("ClickClick backend is unavailable; start clickclick-api or omit --no-start")
            parts = urlsplit(base_url)
            env = os.environ.copy()
            env.update(CLICKCLICK_DATA_DIR=str(data_dir), CLICKCLICK_API_HOST=parts.hostname,
                       CLICKCLICK_API_PORT=str(parts.port or (443 if parts.scheme == "https" else 80)))
            kwargs = {"cwd": str(workspace), "env": env, "stdin": subprocess.DEVNULL,
                      "close_fds": True}
            executable = sys.executable
            if os.name == "nt":
                # DETACHED_PROCESS alone still inherits the client's Job Object.
                # Require breakaway: otherwise closing stdio could kill phone work.
                kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
                # Avoid the venv redirector creating another kill-on-close job.
                # CPython's launcher environment preserves this virtualenv.
                executable = getattr(sys, "_base_executable", sys.executable)
                env["__PYVENV_LAUNCHER__"] = sys.executable
            else:
                kwargs["start_new_session"] = True
            with (data_dir / "mcp-backend.log").open("ab") as log:
                try:
                    process = subprocess.Popen([executable, "-m", "control_api.mcp_backend"],
                                               stdout=log, stderr=log, **kwargs)
                except OSError as exc:
                    if os.name == "nt" and getattr(exc, "winerror", None) == 5:
                        raise RuntimeError(INDEPENDENT_BACKEND_ERROR) from exc
                    raise
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        if process.returncode == 78:
                            raise RuntimeError(INDEPENDENT_BACKEND_ERROR)
                        raise RuntimeError("ClickClick backend startup failed; inspect data_dir/mcp-backend.log")
                    existing = discover(client)
                    if existing:
                        return existing
                    time.sleep(.2)
                raise TimeoutError("ClickClick backend startup timed out; inspect data_dir/mcp-backend.log")
            except BaseException:
                # Only this launch's process tree is cleaned up. Some Windows
                # virtualenv Python executables are redirectors with a child.
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                    else:
                        process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise


def client_config(workspace: Path, base_url: str, data_dir: Path) -> dict:
    return {"mcpServers": {"clickclick": {
        "command": str(Path(sys.executable).absolute()),
        "args": ["-m", "control_api.mcp", "--workspace", str(workspace.resolve()),
                 "--backend-url", local_url(base_url), "--data-dir", str(data_dir.resolve())],
    }}}
