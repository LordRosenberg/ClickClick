"""Resumable MobileWorld batch runner with AndroidWorld-v6-style health gates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from evaluation.mobileworld.budgets import episode_budgets
from evaluation.mobileworld.recovery import BatchPaused, check_pause, detect_emulator_incident, read_json, run_with_recovery
from evaluation.mobileworld.recording import finalize_interrupted_recording
from evaluation.mobileworld.environment_fixes import (
    ensure_task_asset_route, inspect_initialization_errors, inspect_task_asset_access,
    verify_mattermost_readiness_fix,
)
from evaluation.mobileworld.long_run_health import (
    FullRestartRequired, MobileWorldTarget, atomic_json, ensure_channels,
    prepare_clickclick_device, preventive_restart_check, restart_container,
    verify_official_session_fix, verify_official_task_fix, verify_task_apps,
)


DEFAULT_CASES = [
    {"task": "AdjustBrightnessMinimumTask", "difficulty": "简单", "max_steps": 30, "max_seconds": 360},
    {"task": "SetAlarmTask", "difficulty": "中等", "max_steps": 30, "max_seconds": 600},
    {"task": "MastodonReplyTask", "difficulty": "中等偏难", "max_steps": 30, "max_seconds": 780},
    {"task": "MastodonManageMultiListTask", "difficulty": "较难", "max_steps": 30, "max_seconds": 1000},
    {"task": "MattermostCustomerFeedbackAnalysisTask", "difficulty": "困难", "max_steps": 30, "max_seconds": 1500},
]


def verify_reusable_recording(case_dir: Path) -> None:
    """A recording defect alone never authorizes replacing an existing outcome."""
    result_path = case_dir / 'mobileworld-result.json'
    if not result_path.exists():
        return
    manifest = case_dir / 'video/recording-manifest.json'
    previous = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
    if not previous.get('error') and previous.get('segments'):
        return
    raise RuntimeError(
        'Existing case has incomplete recording; preserve its outcome and '
        'repair recording evidence without rerunning the task: ' + case_dir.name
    )


def request(method: str, url: str, **kwargs: Any) -> Any:
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 300), **kwargs)
    response.raise_for_status()
    return response.json()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_cases(path: str) -> list[dict[str, Any]]:
    if not path:
        return [dict(row) for row in DEFAULT_CASES]
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("case manifest must be a non-empty JSON list")
    return value


def cases_from_preflight(path: str, max_steps: int, max_seconds: int) -> list[dict[str, Any]]:
    source = Path(path)
    manifest = json.loads(source.read_text(encoding="utf-8"))
    results = json.loads((source.parent / "results.json").read_text(encoding="utf-8"))
    tasks = manifest["tasks"]
    if not tasks or len(tasks) != 117 or len(results) != len(tasks):
        raise ValueError("expected the complete 117-task GUI-only preflight")
    names = [row["name"] for row in tasks]
    ready = {row["task"] for row in results if row.get("status") == "ready"}
    if len(set(names)) != len(names) or set(names) != ready:
        raise ValueError("preflight has duplicate tasks or tasks without a ready result")
    if max_steps <= 0 or max_seconds <= 0:
        raise ValueError("step and time budgets must be positive")
    return [
        {"task": name, "difficulty": "未分层", "max_steps": max_steps, "max_seconds": max_seconds}
        for name in names
    ]


def score_summary(results: list[dict[str, Any]]) -> dict[str, int | float | None]:
    invalid = [row for row in results if row.get("environment_status") == "invalid_initialization"]
    valid = [row for row in results if row.get("environment_status") != "invalid_initialization"
             and isinstance(row.get("score"), dict) and "score" in row["score"]]
    passed = sum(float(row["score"]["score"]) >= 1.0 for row in valid)
    return {
        "episodes": len(results), "valid": len(valid), "passed": passed,
        "invalid_initialization": len(invalid),
        "success_rate": passed / len(valid) if valid else None,
    }


def task_metadata(target: MobileWorldTarget, task: str) -> dict[str, Any]:
    return request("GET", f"{target.backend}/task/metadata", params={"task_name": task})


def archive_invalid_initialization(root: Path, case_dir: Path) -> bool:
    result_path = case_dir / "mobileworld-result.json"
    if not result_path.exists():
        return False
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("environment_status") != "invalid_initialization":
        return False
    destination = root / f"{case_dir.name}.invalid-initialization-{time.time_ns()}"
    if case_dir.resolve().parent != root.resolve() or destination.resolve().parent != root.resolve():
        raise RuntimeError("refusing to archive a case outside the run root")
    case_dir.rename(destination)
    return True


def reset_task_baseline(target: MobileWorldTarget, task: str) -> dict[str, Any]:
    apps = list(task_metadata(target, task).get("apps") or [])
    verify_mattermost_readiness_fix(target, apps)
    ensure_task_asset_route(target, apps)
    operation = {"task_name": task, "req_device": target.environment_device}
    since = datetime.now(timezone.utc).isoformat()
    try:
        initialized = request("POST", f"{target.backend}/task/init", json=operation)
        if initialized != "OK":
            raise RuntimeError(f"MobileWorld baseline initializer returned unexpected status: {type(initialized).__name__}")
        check = inspect_initialization_errors(target, apps, since)
        access = inspect_task_asset_access(target, apps)
        if not check["ready"] or not access["ready"]:
            raise RuntimeError("MobileWorld baseline initialization is unhealthy; batch stopped")
    finally:
        torn_down = request("POST", f"{target.backend}/task/tear_down", json=operation)
    return {"initialized": initialized, "torn_down": torn_down,
            "fixture_initialization": check, "asset_access": access}


def cleanup_interrupted(target: MobileWorldTarget, state: dict[str, Any]) -> dict[str, Any] | None:
    current = state.get("current") or {}
    if current.get("phase") not in {"initializing", "running", "scoring", "failed"}:
        return None
    task = current.get("task")
    if not task:
        return None
    operation = {"task_name": task, "req_device": target.environment_device}
    try:
        result = request("POST", f"{target.backend}/task/tear_down", json=operation)
        return {"task": task, "result": result, "at": time.time()}
    except Exception as exc:
        return {"task": task, "error": f"{type(exc).__name__}: {exc}", "at": time.time()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="")
    parser.add_argument("--preflight-manifest", default="")
    parser.add_argument("--max-round", "--max-steps", dest="max_steps", type=int, default=50)
    parser.add_argument("--max-seconds", type=int, default=2400)
    parser.add_argument("--max-model-calls", type=int, default=None,
                        help="Default per-case model-call limit; case manifest values take precedence")
    parser.add_argument("--output-root", default="data/mobileworld/runs/full-5")
    parser.add_argument("--backend", default="http://127.0.0.1:6800")
    parser.add_argument("--target", default="127.0.0.1:5556")
    parser.add_argument("--expected-device-serial", default="EMULATOR36X2X12X0")
    parser.add_argument("--environment-device", default="emulator-5554")
    parser.add_argument("--container", default="mobile_world_env_0")
    parser.add_argument("--model", default="chatgpt/gpt-5.6-sol")
    parser.add_argument("--collector-apk", default="data/mobileworld/device-apks/clickclick-collector-0.4.5-debug.apk")
    parser.add_argument("--ime-apk", default="data/mobileworld/device-apks/ADBKeyboard.apk")
    parser.add_argument("--no-auto-restart", action="store_true")
    parser.add_argument("--no-recording", action="store_true", help="Keep screenshots and traces without screen recording")
    args = parser.parse_args()

    root = Path(args.output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        check_pause(root)
    except BatchPaused:
        return
    state_path, results_path = root / "run-state.json", root / "results.json"
    stop_marker, pause_marker = root / "STOP", root / "PAUSE_AFTER_EPISODE"
    target = MobileWorldTarget(args.backend, args.target, args.expected_device_serial, args.environment_device, args.container)
    collector_apk, ime_apk = Path(args.collector_apk).resolve(), Path(args.ime_apk).resolve()
    if args.cases and args.preflight_manifest:
        parser.error("--cases and --preflight-manifest are mutually exclusive")
    cases = (cases_from_preflight(args.preflight_manifest, args.max_steps, args.max_seconds)
             if args.preflight_manifest else load_cases(args.cases))
    for row in cases:
        limits = episode_budgets(
            row["max_steps"], row.get("max_model_calls", args.max_model_calls), row["max_seconds"],
        )
        row["max_model_calls"] = limits["model_calls"]
        row["limits"] = limits
    official = {row["name"]: row for row in request("GET", f"{target.backend}/task/list")}
    for row in cases:
        if row["task"] not in official:
            raise RuntimeError(f"unknown MobileWorld task: {row['task']}")
        metadata = task_metadata(target, row["task"])
        if "agent-mcp" in metadata.get("tags", []) or "agent-user-interaction" in metadata.get("tags", []):
            raise RuntimeError(f"task is not GUI-only: {row['task']}")
        row["metadata"] = metadata
        row["goal"] = request("GET", f"{target.backend}/task/goal", params={"task_name": row["task"]})

    batch_apps = sorted({app for case in cases for app in case["metadata"].get("apps") or []})
    atomic_json(root / "environment-prerequisites.json", {
        "readiness_fix": verify_mattermost_readiness_fix(target, batch_apps),
        "asset_route": ensure_task_asset_route(target, batch_apps),
    })

    manifest = {
        "created_at": time.time(), "model": args.model, "target": target.__dict__, "cases": cases,
        "recording_enabled": not args.no_recording,
        "difficulty_note": "实验分层；MobileWorld 当前任务 API 未提供 complexity 字段",
        "source_hashes": {
            name: sha256(Path(__file__).with_name(name))
            for name in ("run_full.py", "run_single.py", "budgets.py", "clock_health.py", "recording.py", "long_run_health.py", "app_session.py", "taodian_fixture.py", "prepare_device.py", "adb_proxy.py", "environment_fixes.py", "fixture_context.py", "patch_mattermost_readiness.py", "recovery.py")
        },
        "preflight_manifest": str(Path(args.preflight_manifest).resolve()) if args.preflight_manifest else None,
        "preflight_sha256": sha256(Path(args.preflight_manifest)) if args.preflight_manifest else None,
    }
    atomic_json(root / "manifest.json", manifest)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"completed": [], "current": None}
    try:
        check_pause(root)
    except BatchPaused:
        state["paused_before_start"] = True
        atomic_json(state_path, state)
        return
    interrupted = cleanup_interrupted(target, state)
    if interrupted:
        state.setdefault("interrupted_cleanups", []).append(interrupted)
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.exists() else []

    prepare_clickclick_device(target, collector_apk, ime_apk)
    for ordinal, case in enumerate(cases, start=1):
        task = case["task"]
        if task in state["completed"]:
            continue
        if stop_marker.exists() or pause_marker.exists():
            state["stopped_before"] = task
            atomic_json(state_path, state)
            break

        maintenance = preventive_restart_check(target, task, float(case["max_seconds"]))
        restarted = None
        if maintenance["restart_due"]:
            if args.no_auto_restart:
                raise RuntimeError(f"preventive restart required before {task}")
            restarted = restart_container(target, Path(__file__).with_name("adb_proxy.py"))
            prepare_clickclick_device(target, collector_apk, ime_apk)
        try:
            health = ensure_channels(target, root, collector_apk, ime_apk)
        except FullRestartRequired:
            if args.no_auto_restart or restarted is not None:
                raise
            restarted = restart_container(target, Path(__file__).with_name("adb_proxy.py"))
            prepare_clickclick_device(target, collector_apk, ime_apk)
            health = ensure_channels(target, root, collector_apk, ime_apk, allow_full_restart=False)

        apps = list(case["metadata"].get("apps") or [])
        verify_official_session_fix(target, apps)
        verify_official_task_fix(target, task)
        app_check = verify_task_apps(target, apps)
        state["current"] = {
            "ordinal": ordinal, "task": task, "phase": "initializing", "maintenance": maintenance,
            "restart": restarted, "health_evidence": health["evidence"], "app_check": app_check,
            "updated_at": time.time(),
        }
        atomic_json(state_path, state)
        case_dir = root / f"{ordinal:03d}-{task}"
        result_path = case_dir / "mobileworld-result.json"
        if archive_invalid_initialization(root, case_dir):
            results = [row for row in results if row["task"] != task]
            atomic_json(results_path, results)
            atomic_json(root / "summary.json", score_summary(results))
        if not args.no_recording:
            verify_reusable_recording(case_dir)
        state["current"]["phase"] = "running"
        atomic_json(state_path, state)
        command = [
            sys.executable, "-m", "evaluation.mobileworld.run_single",
            "--task", task, "--backend", target.backend,
            "--environment-device", target.environment_device,
            "--target", target.adb_target, "--expected-device-serial", target.expected_serial,
            "--container", target.container,
            "--model", args.model, "--max-steps", str(case["max_steps"]),
            "--max-seconds", str(case["max_seconds"]),
            "--collector-apk", str(collector_apk), "--ime-apk", str(ime_apk),
            "--data-dir", str(case_dir), "--output", str(result_path),
        ]
        if case["max_model_calls"] is not None:
            command.extend(["--max-model-calls", str(case["max_model_calls"])])
        if args.no_recording:
            command.append("--no-recording")
        started = time.time()
        recovered_existing = result_path.exists()
        if recovered_existing:
            completed = subprocess.CompletedProcess(command, 0, "", "")
        else:
            def launch():
                try:
                    return subprocess.run(
                        command, text=True, encoding="utf-8", errors="replace",
                        capture_output=True, timeout=float(case["max_seconds"]),
                    )
                except subprocess.TimeoutExpired:
                    lifecycle = read_json(case_dir / "episode-lifecycle.json")
                    atomic_json(case_dir / "episode-lifecycle.json", {
                        **lifecycle, "phase": "failed", "exception_type": "TimeoutExpired",
                        "environment_failure": lifecycle.get("model_started") is False,
                    })
                    try:
                        finalize_interrupted_recording(target, case_dir / "video")
                    except Exception as exc:
                        atomic_json(case_dir / "recording-timeout-cleanup.json", {"error": str(exc)})
                    return subprocess.CompletedProcess(command, 124, "", "Episode process deadline exceeded")

            def recover_environment():
                state["current"].update(phase="recovering_environment", updated_at=time.time())
                atomic_json(state_path, state)
                restart = restart_container(target, Path(__file__).with_name("adb_proxy.py"))
                check_pause(root)
                prepare_clickclick_device(target, collector_apk, ime_apk)
                restored = ensure_channels(target, root, collector_apk, ime_apk, allow_full_restart=False)
                verify_official_session_fix(target, apps)
                verify_official_task_fix(target, task)
                verified_apps = verify_task_apps(target, apps)
                state["current"].update(phase="running", updated_at=time.time())
                atomic_json(state_path, state)
                return {"restart": restart, "health_evidence": restored["evidence"],
                        "app_check": verified_apps}

            try:
                completed = run_with_recovery(
                    root, case_dir, launch, recover_environment, enabled=not args.no_auto_restart,
                    detect_incident=lambda directory: detect_emulator_incident(target, directory),
                    resume_failed=(case_dir / "runner-failure.json").exists(),
                )
            except BatchPaused:
                state["current"].update(phase="paused", updated_at=time.time())
                state["stopped_before"] = task
                atomic_json(state_path, state)
                break
        if completed.returncode != 0:
            failure = {
                "task": task, "difficulty": case["difficulty"], "started_at": started,
                "finished_at": time.time(), "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-4000:], "stderr_tail": completed.stderr[-4000:],
            }
            atomic_json(case_dir / "runner-failure.json", failure)
            state["current"].update(
                phase="environment_blocked" if (case_dir / "environment-invalid.json").exists() else "failed",
                failure=str(case_dir / "runner-failure.json"),
            )
            atomic_json(state_path, state)
            raise RuntimeError(f"MobileWorld case failed to execute: {task}")

        result = json.loads(result_path.read_text(encoding="utf-8"))
        environment_invalid = result.get("environment_status") == "invalid_initialization"
        recording_path = case_dir / "video" / "recording-manifest.json"
        recording = ({"enabled": False} if args.no_recording else
                     json.loads(recording_path.read_text(encoding="utf-8")) if recording_path.exists() else {})
        if not environment_invalid and not args.no_recording and (recording.get("error") or not recording.get("segments")):
            raise RuntimeError(f"MobileWorld case lacks complete recording: {task}")

        result.update(
            ordinal=ordinal, difficulty=case["difficulty"], started_at=started,
            finished_at=time.time(), health_evidence=health["evidence"], app_check=app_check,
            preventive_restart=maintenance, container_restart=restarted,
            recovered_existing_result=recovered_existing, recording=recording,
        )
        # A baseline-reset failure can leave a valid result without marking
        # the task completed. Resuming must not count that episode twice.
        results = [row for row in results if row.get("task") != task]
        results.append(result)
        atomic_json(results_path, results)
        atomic_json(root / "summary.json", score_summary(results))
        if environment_invalid:
            state["current"].update(phase="environment_blocked", updated_at=time.time())
            atomic_json(state_path, state)
            print(json.dumps({"task": task, "environment_status": "invalid_initialization",
                              "batch_stopped": True, "score": None}), flush=True)
            break
        baseline = reset_task_baseline(target, task)
        atomic_json(case_dir / "post-task-baseline-reset.json", baseline)
        state["completed"].append(task)
        state["current"] = {"task": task, "phase": "complete", "updated_at": time.time()}
        atomic_json(state_path, state)
        print(json.dumps({
            "task": task, "difficulty": case["difficulty"],
            "environment_status": result.get("environment_status"),
            "score": result.get("score"),
        }, ensure_ascii=False), flush=True)
        if pause_marker.exists():
            state["paused_after"] = task
            atomic_json(state_path, state)
            break


if __name__ == "__main__":
    main()
