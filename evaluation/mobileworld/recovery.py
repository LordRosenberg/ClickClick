"""Bounded recovery of initialization failures and proven emulator crashes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time
from typing import Callable

import requests

from evaluation.mobileworld.long_run_health import atomic_json
from evaluation.mobileworld.recording import RecordingError


def phase(directory: Path, name: str, *, model_started: bool = False, **extra) -> None:
    atomic_json(directory / "episode-lifecycle.json", {
        "phase": name, "model_started": model_started, "at": time.time(), **extra,
    })


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def audited_episode(directory: Path, execute: Callable) -> None:
    phase(directory, "initializing")
    try:
        execute()
    except Exception as exc:
        state = read_json(directory / "episode-lifecycle.json")
        transport_failure = isinstance(exc, (
            subprocess.CalledProcessError, subprocess.TimeoutExpired,
            requests.ConnectionError, requests.Timeout, RecordingError,
        )) or (isinstance(exc, requests.HTTPError) and exc.response is not None
               and exc.response.status_code >= 500)
        atomic_json(directory / "episode-lifecycle.json", {
            **state, "phase": "failed", "at": time.time(),
            "environment_failure": transport_failure,
            "exception_type": type(exc).__name__, "error": str(exc),
        })
        raise


def retryable_initialization(directory: Path) -> bool:
    state = read_json(directory / "episode-lifecycle.json")
    result = read_json(directory / "mobileworld-result.json")
    # Missing state is not evidence that no model invocation occurred.
    if state.get("model_started") is not False or (directory / "clickclick.db").exists():
        return False
    if result.get("score") is not None or result.get("clickclick_task_id"):
        return False
    return (state.get("environment_failure") is True
            or result.get("environment_status") == "invalid_initialization")


def detect_emulator_incident(target, directory: Path) -> dict:
    """Read only: never call the official health endpoint (it may restart AVD)."""
    result = read_json(directory / "mobileworld-result.json")
    if result.get("score") is not None:
        return {"confirmed": False, "reason": "already_scored"}
    context = read_json(directory / "attempt-start.json")
    failure = read_json(directory / "runner-failure.json")
    started = context.get("started_at", failure.get("started_at"))
    finished = context.get("finished_at", failure.get("finished_at", time.time()))
    if not isinstance(started, (int, float)) or not isinstance(finished, (int, float)) or finished < started:
        return {"confirmed": False, "reason": "missing_attempt_time_window"}
    try:
        logs = subprocess.run([
            "docker", "logs", "--timestamps", "--since",
            datetime.fromtimestamp(started, timezone.utc).isoformat(), "--until",
            datetime.fromtimestamp(finished, timezone.utc).isoformat(), target.container,
        ], capture_output=True, text=True, errors="replace", timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"confirmed": False, "reason": "evidence_unavailable", "error_type": type(exc).__name__}
    # Narrow, host-side fatal/restart evidence; neither model text nor generic
    # ADB/HTTP errors can authorize discarding an executed benchmark attempt.
    signals = [line[:1500] for line in (logs.stdout + "\n" + logs.stderr).splitlines()
               if "crashhandler_die: fatal:" in line
               or "[HEALTH] Successfully restarted emulator with AVD" in line]
    report = {"confirmed": bool(signals), "kind": "emulator_crash_or_restart",
              "started_at": started, "finished_at": finished, "signals": signals[:20]}
    atomic_json(directory / "environment-incident.json", report)
    return report


class BatchPaused(RuntimeError):
    pass


def check_pause(root: Path) -> None:
    if any((root / name).exists() for name in ("STOP", "PAUSE_AFTER_EPISODE")):
        raise BatchPaused("Batch pause requested")


def run_with_recovery(root: Path, directory: Path, execute: Callable,
                      recover: Callable, *, enabled: bool = True,
                      detect_incident: Callable | None = None,
                      resume_failed: bool = False, max_retries: int = 2):
    """Only environment-invalid attempts can be replaced; never a valid score."""
    prior = [read_json(path) for path in root.glob(f"{directory.name}.environment-failure-*/recovery.json")]
    retries = max([int(row.get("retry", 0)) for row in prior] or [0])
    while True:
        check_pause(root)
        if resume_failed:
            failure = read_json(directory / "runner-failure.json")
            completed = subprocess.CompletedProcess([], failure.get("returncode", 1),
                                                     failure.get("stdout_tail", ""), failure.get("stderr_tail", ""))
            resume_failed = False
        else:
            started = time.time()
            atomic_json(directory / "attempt-start.json", {"started_at": started})
            completed = execute()
            atomic_json(directory / "attempt-start.json", {"started_at": started, "finished_at": time.time()})
        if completed.returncode:
            existing = read_json(directory / "runner-failure.json")
            atomic_json(directory / "runner-failure.json", {
                **existing,
                "returncode": completed.returncode, "at": time.time(),
                "stdout_tail": (completed.stdout or "")[-4000:],
                "stderr_tail": (completed.stderr or "")[-4000:],
            })
        if not enabled or read_json(directory / "mobileworld-result.json").get("score") is not None:
            return completed
        initialization_failure = retryable_initialization(directory)
        incident = ({"confirmed": False} if initialization_failure or detect_incident is None
                    or not completed.returncode else detect_incident(directory))
        if not initialization_failure and not incident.get("confirmed"):
            return completed
        invalid = {"environment_status": "invalid_initialization" if initialization_failure else "interrupted_execution",
                   "excluded_from_agent_score": True, "incident": incident, "retries_used": retries}
        atomic_json(directory / "environment-invalid.json", invalid)
        if retries >= max_retries:
            atomic_json(directory / "recovery-limit.json", {"max_retries": max_retries, **invalid})
            return completed
        check_pause(root)
        archive = root / f"{directory.name}.environment-failure-{time.time_ns()}"
        if directory.resolve().parent != root.resolve() or archive.resolve().parent != root.resolve():
            raise ValueError("refusing to archive outside batch root")
        # Console queries briefly open read-only SQLite handles on Windows.
        for rename_attempt in range(10):
            check_pause(root)
            try:
                directory.rename(archive)
                break
            except PermissionError:
                if rename_attempt == 9:
                    raise
                time.sleep(0.2)
        retries += 1
        evidence = {"task_directory": directory.name, "archive": str(archive),
                    "retry": retries, "started_at": time.time(), "status": "recovering", **invalid}
        audit_path = archive / "recovery.json"
        atomic_json(audit_path, evidence)
        check_pause(root)
        try:
            evidence["recovery"] = recover()
            evidence["status"] = "recovered"
        except Exception as exc:
            evidence.update(status="recovery_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            evidence["finished_at"] = time.time()
            atomic_json(audit_path, evidence)
        check_pause(root)
