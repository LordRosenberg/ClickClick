"""Exercise every GUI-only MobileWorld initializer without model actions.

This is a resumable dry run of the *official* task lifecycle. It does not
produce benchmark scores or let ClickClick perform task actions.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time
from pathlib import Path
from typing import Any

import requests

from evaluation.mobileworld.app_session import inspect_task_sessions
from evaluation.mobileworld.fixture_context import prepare_fixture_context
from evaluation.mobileworld.environment_fixes import (
    ensure_task_asset_route, inspect_initialization_errors, inspect_task_asset_access,
    verify_mattermost_readiness_fix,
)
from evaluation.mobileworld.long_run_health import (
    MobileWorldTarget, atomic_json, ensure_channels, prepare_clickclick_device,
    preventive_restart_check, restart_container, verify_identity,
    verify_official_session_fix, verify_official_task_fix, verify_task_apps,
)


def request(method: str, url: str, **kwargs: Any) -> Any:
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 300), **kwargs)
    response.raise_for_status()
    return response.json()


def gui_tasks(target: MobileWorldTarget) -> list[dict[str, Any]]:
    tasks = request("GET", f"{target.backend}/task/list")
    result = []
    for row in tasks:
        metadata = request("GET", f"{target.backend}/task/metadata", params={"task_name": row["name"]})
        tags = set(metadata.get("tags") or [])
        if tags.isdisjoint({"agent-mcp", "agent-user-interaction"}):
            result.append(metadata)
    return result


def preflight_one(
    target: MobileWorldTarget, task: dict[str, Any], root: Path,
    collector_apk: Path, ime_apk: Path,
) -> dict[str, Any]:
    name = str(task["name"])
    apps = list(task.get("apps") or [])
    operation = {"task_name": name, "req_device": target.environment_device}
    row: dict[str, Any] = {"task": name, "apps": apps, "started_at": time.time()}
    init_attempted = False
    phase = "identity"
    try:
        verify_identity(target)
        phase = "official_session_fix"
        verify_official_session_fix(target, apps)
        phase = "official_task_fix"
        verify_official_task_fix(target, name)
        row["readiness_fix"] = verify_mattermost_readiness_fix(target, apps)
        phase = "app_installation"
        row["app_check"] = verify_task_apps(target, apps)
        phase = "task_goal"
        row["goal_available"] = bool(request(
            "GET", f"{target.backend}/task/goal", params={"task_name": name}
        ))
        if not row["goal_available"]:
            raise RuntimeError("empty task goal")
        phase = "task_init"
        row["asset_route"] = ensure_task_asset_route(target, apps)
        initialization_since = datetime.now(timezone.utc).isoformat()
        init_attempted = True
        value = request("POST", f"{target.backend}/task/init", json=operation)
        if value != "OK":
            raise RuntimeError("initializer did not return OK")
        row["fixture_initialization"] = inspect_initialization_errors(target, apps, initialization_since)
        row["asset_access"] = inspect_task_asset_access(target, apps)
        if not row["fixture_initialization"]["ready"] or not row["asset_access"]["ready"]:
            raise RuntimeError("Mattermost fixture initialization failed")
        phase = "fixture_account_context"
        row["fixture_context"] = prepare_fixture_context(target, apps, root / "fixture-context" / name)
        if not row["fixture_context"]["ready"]:
            raise RuntimeError("Documented fixture login prerequisites unavailable")
        phase = "device_reconciliation"
        prepare_clickclick_device(target, collector_apk, ime_apk)
        phase = "channel_health"
        health = ensure_channels(
            target, root, collector_apk, ime_apk, allow_full_restart=False,
        )
        row["health_evidence"] = health["evidence"]
        phase = "app_session"
        row["session_check"] = inspect_task_sessions(target, apps)
        phase = "task_eval"
        score = request("GET", f"{target.backend}/task/eval", json=operation)
        row["scorer_reachable"] = isinstance(score, dict) and "score" in score
        row["initial_score_zero"] = (
            row["scorer_reachable"] and float(score["score"]) == 0.0
        )
        row["status"] = (
            "ready" if row["session_check"]["ready"]
            and row["scorer_reachable"] and row["initial_score_zero"]
            else "needs_investigation"
        )
    except Exception as exc:
        row.update(status="needs_investigation", failure_phase=phase,
                   error_type=type(exc).__name__)
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            row["http_status"] = exc.response.status_code
    finally:
        if init_attempted:
            try:
                row["teardown_ok"] = request(
                    "POST", f"{target.backend}/task/tear_down", json=operation
                ) == "OK"
            except Exception as exc:
                row["teardown_ok"] = False
                row["teardown_error_type"] = type(exc).__name__
            if not row["teardown_ok"]:
                row["status"] = "needs_investigation"
        row["finished_at"] = time.time()
        row["duration_s"] = round(row["finished_at"] - row["started_at"], 2)
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="data/mobileworld/preflight-all")
    parser.add_argument("--backend", default="http://127.0.0.1:6800")
    parser.add_argument("--target", default="127.0.0.1:5556")
    parser.add_argument("--expected-device-serial", default="EMULATOR36X2X12X0")
    parser.add_argument("--environment-device", default="emulator-5554")
    parser.add_argument("--container", default="mobile_world_env_0")
    parser.add_argument("--collector-apk", default="data/mobileworld/device-apks/clickclick-collector-0.4.5-debug.apk")
    parser.add_argument("--ime-apk", default="data/mobileworld/device-apks/ADBKeyboard.apk")
    parser.add_argument("--limit", type=int, default=0, help="first N tasks for smoke checks; 0 means all")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    results_path = root / "results.json"
    target = MobileWorldTarget(
        args.backend, args.target, args.expected_device_serial,
        args.environment_device, args.container,
    )
    tasks = gui_tasks(target)
    if args.limit:
        tasks = tasks[:args.limit]
    apps = sorted({app for task in tasks for app in task.get("apps") or []})
    atomic_json(root / "environment-prerequisites.json", {
        "readiness_fix": verify_mattermost_readiness_fix(target, apps),
        "asset_route": ensure_task_asset_route(target, apps),
    })
    atomic_json(root / "manifest.json", {"target": target.__dict__, "tasks": tasks})
    existing = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []
    completed = {row["task"] for row in existing
                 if row.get("status") == "ready" or not args.retry_failed}
    collector_apk = Path(args.collector_apk).resolve()
    ime_apk = Path(args.ime_apk).resolve()
    for ordinal, task in enumerate(tasks, start=1):
        if task["name"] in completed:
            continue
        if (root / "STOP").exists():
            break
        maintenance = preventive_restart_check(target, task["name"], 300)
        if maintenance["restart_due"]:
            restart_container(target, Path(__file__).with_name("adb_proxy.py"))
            prepare_clickclick_device(target, collector_apk, ime_apk)
        row = preflight_one(target, task, root, collector_apk, ime_apk)
        row["maintenance"] = maintenance
        row["ordinal"] = ordinal
        previous = next((index for index, old in enumerate(existing)
                         if old["task"] == row["task"]), None)
        if previous is None:
            existing.append(row)
        else:
            existing[previous] = row
        atomic_json(results_path, existing)
        print(json.dumps({
            "ordinal": ordinal, "total": len(tasks), "task": row["task"],
            "status": row["status"], "duration_s": row["duration_s"],
            "error_type": row.get("error_type"),
        }, ensure_ascii=False), flush=True)
    atomic_json(root / "summary.json", {
        "total_gui_tasks": len(tasks), "checked": len(existing),
        "ready": sum(row["status"] == "ready" for row in existing),
        "needs_investigation": sum(row["status"] != "ready" for row in existing),
    })


if __name__ == "__main__":
    main()
