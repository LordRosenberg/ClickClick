"""Owner-tagged, expiring task admission lease for a deliberate desktop stop."""

from pathlib import Path
import re
import time

from desktop.files import read_json, write_json

LEASE_SECONDS = 300


def gate_path(data_dir):
    return Path(data_dir) / "shutdown-maintenance.json"


def active(data_dir):
    return read_json(gate_path(data_dir)).get("expires_at", 0) > time.time()


def prepare(data_dir, job):
    if not re.fullmatch(r"[a-f0-9]{32}", job):
        raise ValueError("Invalid shutdown request")
    if (Path(data_dir) / "update-maintenance.json").exists():
        raise ValueError("ClickClick 正在升级，请等待升级完成后再关闭。")
    previous = read_json(gate_path(data_dir))
    if active(data_dir) and previous.get("job") != job:
        raise ValueError("ClickClick 已有关闭操作正在进行。")
    write_json(gate_path(data_dir), {"job": job, "expires_at": time.time() + LEASE_SECONDS})
    return {"prepared": True}


def release(data_dir, job):
    path = gate_path(data_dir)
    if read_json(path).get("job") == job:
        path.unlink(missing_ok=True)


def recover(data_dir):
    """A freshly started backend has no surviving desktop stop operation."""
    gate_path(data_dir).unlink(missing_ok=True)
