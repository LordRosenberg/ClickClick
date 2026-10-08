"""Deferred cleanup of owned program files, isolated from mutable user data."""

from contextlib import ExitStack
import logging
from pathlib import Path
import re
import shutil
import uuid

from control_api.mcp_launcher import startup_lock
from desktop.files import read_json, write_json
from desktop.install import installed


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def running_versions(home):
    import psutil
    versions = Path(home).resolve() / "versions"
    result = set()
    for process in psutil.process_iter():
        try:
            executable = Path(process.exe()).resolve()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # A system process with no accessible executable cannot be our user
            # runtime unless it is Python; fail closed for inaccessible Python.
            try:
                if "python" in process.name().lower():
                    raise RuntimeError("Cannot verify ownership of a Python process")
            except psutil.NoSuchProcess:
                pass
            continue
        if executable.is_relative_to(versions):
            result.add(executable.relative_to(versions).parts[0])
    return result


def queue(home):
    write_json(Path(home) / "data/program-cleanup.json", {"version": installed(home)[2]["version"]})


def retry(home, *, install_locked=False):
    """Retain rollback files while either installer or update worker is active."""
    home = Path(home).resolve()
    pending = home / "data/program-cleanup.json"
    if not pending.exists():
        return {"removed": [], "deferred": []}
    try:
        with ExitStack() as stack:
            if not install_locked:
                stack.enter_context(startup_lock(home / "data/.install.lock", timeout=0))
            stack.enter_context(startup_lock(home / "data/.update-run.lock", timeout=0))
            current = installed(home)[2]["version"]
            if read_json(pending).get("version") != current:
                pending.unlink()  # A rollback invalidates the cleanup request.
                return {"removed": [], "deferred": []}
            versions = home / "versions"
            garbage = home / ".obsolete"
            if linked(versions) or linked(garbage):
                raise ValueError("Program cleanup refuses redirected directories")
            protected = running_versions(home) | {current}
            removed, deferred = [], []
            for path in versions.iterdir():
                if path.name in protected:
                    if path.name != current:
                        deferred.append(path.name)
                    continue
                if (linked(path) or not path.is_dir() or
                        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", path.name) or
                        path.resolve().parent != versions.resolve()):
                    continue
                if read_json(path / "manifest.json").get("version") != path.name:
                    continue  # Never remove an unrecognized directory.
                garbage.mkdir(exist_ok=True)
                target = garbage / (path.name + "-" + uuid.uuid4().hex)
                try:
                    path.rename(target)
                    removed.append(path.name)
                except OSError:
                    deferred.append(path.name)
            if garbage.exists():
                for path in garbage.iterdir():
                    if (linked(path) or not path.is_dir() or
                            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}-[a-f0-9]{32}", path.name) or
                            path.resolve().parent != garbage.resolve()):
                        continue
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        deferred.append(path.name)
            if not deferred:
                pending.unlink()
            return {"removed": removed, "deferred": deferred}
    except (OSError, ValueError, RuntimeError):
        logging.exception("Program cleanup deferred; user data is unaffected")
        return {"removed": [], "deferred": ["retry-pending"]}
