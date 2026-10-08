"""Run one MobileWorld task with ClickClick owning observation and actions."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.adb import adb_bin
from driver.factory import get_driver
from evaluation.mobileworld.app_session import inspect_task_sessions
from evaluation.mobileworld.budgets import episode_budgets
from evaluation.mobileworld.clock_health import ensure_task_clock, restore_task_clock
from evaluation.mobileworld.environment_fixes import (
    ensure_task_asset_route, inspect_initialization_errors, inspect_task_asset_access,
    verify_mattermost_readiness_fix,
)
from evaluation.mobileworld.long_run_health import (
    MobileWorldTarget, atomic_json, prepare_clickclick_device,
    verify_official_session_fix, verify_official_task_fix,
)
from evaluation.mobileworld.recording import EpisodeRecorder
from evaluation.mobileworld.fixture_context import prepare_fixture_context, fixture_prompt_context
from evaluation.mobileworld.photo_picker_health import prepare_photo_picker
from evaluation.mobileworld.recovery import audited_episode, phase
from shared.artifacts import ArtifactStore
from shared.config import get_settings
from shared.db import Database
from shared.revisable import TaskLimits
from shared.schemas import AgentState, TaskStatus


TERMINAL_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def _request(
    method: str,
    url: str,
    *,
    timeout: float = 300,
    **kwargs: Any,
) -> Any:
    response = requests.request(method, url, timeout=timeout, **kwargs)
    response.raise_for_status()
    return response.json()


def _verify_device(target: str, expected_serial: str) -> None:
    result = subprocess.run(
        [adb_bin(), "-s", target, "shell", "getprop", "ro.serialno"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    actual = result.stdout.strip()
    if actual != expected_serial:
        raise RuntimeError(
            f"refusing device {target}: expected {expected_serial}, got {actual or '<empty>'}"
        )


def _require_init_ok(value: Any) -> None:
    """An HTTP 200 alone does not prove that the benchmark loaded a task."""
    if value == "OK" or (isinstance(value, dict) and value.get("status") == "OK"):
        return
    raise RuntimeError(f"MobileWorld task initializer returned unexpected status: {type(value).__name__}")


def _submit_final_answer(args: argparse.Namespace, answer: str) -> dict[str, Any]:
    """Relay ClickClick's final text to the official answer cache, not the GUI.

    MobileWorld's answer-scored tasks read AndroidController.interaction_cache.
    Direct ClickClick device actions do not populate that cache, so only a
    model-produced terminal answer is mirrored through the official answer step.
    """
    if not answer.strip():
        raise RuntimeError("ClickClick completed without a final answer to submit")
    response = _request(
        "POST", f"{args.backend}/step",
        json={
            "device": args.environment_device,
            "action": {"action_type": "answer", "text": answer},
        },
    )
    if not isinstance(response, dict) or response.get("result") != "OK":
        raise RuntimeError(f"MobileWorld answer submission returned unexpected response: {response!r}")
    return {"text": answer, "official_action": "answer", "result": response["result"]}


def _write_result(args: argparse.Namespace, result: dict[str, Any]) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    output = Path(args.output).resolve() if args.output else Path(args.data_dir).resolve() / "mobileworld-result.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


async def _run_clickclick(args: argparse.Namespace, goal: str) -> dict[str, Any]:
    limits = episode_budgets(args.max_steps, args.max_model_calls, args.max_seconds)
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    settings = (getattr(args, "runtime_settings", None) or get_settings()).model_copy(
        update={
            "data_dir": data_dir,
            "default_model": args.model,
            "manager_model": args.model,
            "executor_model": args.model,
            "accessibility_collector_enabled": True,
            "accessibility_collector_apk_path": str(Path(args.collector_apk).resolve()),
            "ime_auto_setup": True,
            "ime_apk_path": str(Path(args.ime_apk).resolve()),
        }
    )
    settings.apply_chatgpt_token_dir()
    database = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    traces = TraceWriter(database, artifacts)
    driver = get_driver(settings, serial=args.target)
    orchestrator = create_orchestrator(
        database,
        traces,
        driver=driver,
        artifacts=artifacts,
        settings=settings,
        max_steps=limits["executor_decisions"],
        max_role_invocations=limits["model_calls"],
    )
    state = AgentState(
        instruction=goal,
        manager_model=args.model,
        executor_model=args.model,
    )
    started = time.time()
    state.revisable.limits = TaskLimits(
        prediction_rounds=limits["rounds"],
        deadline_at=started + limits["seconds"],
    )
    record = database.create_task(goal, state, device_serial=args.target)
    action_steps = 0
    status = record.status
    rounds = 0
    answer_submission = None
    try:
        # Keep one runtime/observation session for the entire episode. The loop's
        # attempted-ACT boundary still counts rejected zero-unit actions and
        # stops before an extra terminal prediction after exhausting the budget.
        status = await orchestrator.run_task(record.id)
        current = database.get_task(record.id)
        action_steps = int(current.state.revisable.execution_count) if current and current.state else 0
        if current and current.state:
            runtime = current.state.revisable
            if status in TERMINAL_STATUSES:
                runtime.prediction_round_count = min(limits["rounds"], runtime.prediction_round_count + 1)
                database.update_task(record.id, state=current.state)
            rounds = runtime.prediction_round_count
        if status == TaskStatus.SUCCEEDED:
            final_answer = current.state.revisable.completion_reason if current and current.state else None
            if final_answer:
                answer_submission = _submit_final_answer(args, str(final_answer))
        if status not in TERMINAL_STATUSES:
            status = TaskStatus.FAILED
            database.update_task(record.id, status=status, failure_reason="mobileworld_round_limit_exhausted")
        task = database.get_task(record.id)
        return {
            "clickclick_task_id": record.id,
            "clickclick_status": status.value,
            "logical_action_steps": action_steps,
            "mobileworld_rounds": rounds,
            "executor_decisions": task.state.step_number if task and task.state else 0,
            "failure_reason": task.failure_reason if task else None,
            "limits": limits,
            "elapsed_s": time.time() - started,
            "role_invocations": (
                task.state.role_invocation_count
                if task is not None and task.state is not None
                else 0
            ),
            "steps": database.list_steps(record.id),
            "answer_submission": answer_submission,
        }
    finally:
        database.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--backend", default="http://127.0.0.1:6800")
    parser.add_argument("--environment-device", default="emulator-5554")
    parser.add_argument("--target", default="127.0.0.1:5556")
    parser.add_argument("--expected-device-serial", default="EMULATOR36X2X12X0")
    parser.add_argument("--container", default="mobile_world_env_0")
    parser.add_argument("--model", default="chatgpt/gpt-5.6-sol")
    parser.add_argument("--max-round", "--max-steps", dest="max_steps", type=int, default=50)
    parser.add_argument("--max-model-calls", type=int, default=None,
                        help="Optional local resource guard, disabled by default; not a MobileWorld round limit")
    parser.add_argument("--max-seconds", type=float, default=2400)
    parser.add_argument("--collector-apk", required=True)
    parser.add_argument("--ime-apk", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--keep-task", action="store_true")
    parser.add_argument("--no-recording", action="store_true", help="Keep screenshots and traces without screen recording")
    args = parser.parse_args()
    try:
        limits = episode_budgets(args.max_steps, args.max_model_calls, args.max_seconds)
    except ValueError as exc:
        parser.error(str(exc))

    audited_episode(Path(args.data_dir).resolve(), lambda: _execute_episode(args, limits))


def _execute_episode(args: argparse.Namespace, limits: dict) -> None:
    _verify_device(args.target, args.expected_device_serial)
    _request("POST", f"{args.backend}/init", json={"device": args.environment_device})
    goal = _request(
        "GET", f"{args.backend}/task/goal", params={"task_name": args.task}
    )
    operation = {"task_name": args.task, "req_device": args.environment_device}
    metadata = _request("GET", f"{args.backend}/task/metadata", params={"task_name": args.task})
    target = MobileWorldTarget(
        backend=args.backend, adb_target=args.target,
        expected_serial=args.expected_device_serial,
        environment_device=args.environment_device, container=args.container,
    )
    verify_official_session_fix(target, list(metadata.get("apps") or []))
    verify_official_task_fix(target, args.task)
    readiness_fix = verify_mattermost_readiness_fix(target, list(metadata.get("apps") or []))
    initialized = False
    clock_check: dict[str, Any] = {}
    recorder: EpisodeRecorder | None = None
    result: dict[str, Any] = {}
    try:
        asset_route = ensure_task_asset_route(target, list(metadata.get("apps") or []))
        initialization_since = datetime.now(timezone.utc).isoformat()
        _require_init_ok(_request("POST", f"{args.backend}/task/init", json=operation))
        initialized = True
        initialization_check = inspect_initialization_errors(
            target, list(metadata.get("apps") or []), initialization_since,
        )
        initialization_check["asset_route"] = asset_route
        initialization_check["readiness_fix"] = readiness_fix
        initialization_check["asset_access"] = inspect_task_asset_access(target, list(metadata.get("apps") or []))
        initialization_check["ready"] &= initialization_check["asset_access"]["ready"]
        initialization_check["photo_picker"] = prepare_photo_picker(
            target, list(metadata.get("apps") or []),
        )
        initialization_check["ready"] &= initialization_check["photo_picker"]["ready"]
        fixture_context = prepare_fixture_context(
            target, list(metadata.get("apps") or []), Path(args.data_dir),
        )
        initialization_check["fixture_context"] = fixture_context
        initialization_check["ready"] &= fixture_context["ready"]
        if not initialization_check["ready"]:
            _write_result(args, {
                "task": args.task, "goal": goal, "model": args.model,
                "max_steps_budget": args.max_steps, "limits": limits,
                "environment_status": "invalid_initialization",
                "initialization_check": initialization_check,
                "clickclick_task_id": None, "clickclick_status": "not_started",
                "logical_action_steps": 0, "score": None,
            })
            return
        # A MobileWorld task snapshot may rewind the Collector/IME state even
        # when the pre-task batch health gate passed. Reconcile after init.
        prepare_clickclick_device(
            target, Path(args.collector_apk).resolve(), Path(args.ime_apk).resolve()
        )
        if not args.no_recording:
            recorder = EpisodeRecorder(target, Path(args.data_dir).resolve() / "video")
            recorder.start()
        session_check = inspect_task_sessions(target, list(metadata.get("apps") or []))
        session_check["fixture_initialization"] = initialization_check
        clock_check = ensure_task_clock(target, list(metadata.get("apps") or []), args.task)
        session_check["clock"] = clock_check
        session_check["ready"] = session_check["ready"] and clock_check["ready"]
        if not session_check["ready"]:
            _write_result(args, {
                "task": args.task, "goal": goal, "model": args.model,
                "max_steps_budget": args.max_steps,
                "limits": limits,
                "environment_status": "invalid_initialization",
                "initialization_check": session_check,
                "clickclick_task_id": None, "clickclick_status": "not_started",
                "logical_action_steps": 0, "score": None,
            })
            return
        with fixture_prompt_context(fixture_context, Path(args.data_dir)):
            phase(Path(args.data_dir), "executing", model_started=True)
            result = asyncio.run(_run_clickclick(args, str(goal)))
        result["task"] = args.task
        result["goal"] = goal
        result["model"] = args.model
        result["max_steps_budget"] = args.max_steps
        result["environment_status"] = "ready"
        result["initialization_check"] = session_check
        result["fixture_context"] = fixture_context
        # Preserve measured runtime costs even if recording/scoring/cleanup
        # fails. This receipt has no official task outcome.
        atomic_json(Path(args.data_dir) / "runtime-result.json", {
            **result, "score": None, "environment_status": "not_scored",
        })
        # Some official evaluators run `adb root`, restarting adbd and killing
        # attached shell processes. Finalize the complete agent video first.
        # A video failure must still preserve the first official task score.
        recording_error = None
        if recorder is not None:
            try:
                recorder.stop()
            except Exception as exc:
                recording_error = exc
            recorder = None
        phase(Path(args.data_dir), "scoring", model_started=True)
        result["score"] = _request(
            "GET", f"{args.backend}/task/eval", json=operation
        )
        _write_result(args, result)
        if recording_error is not None:
            raise recording_error
    finally:
        # Cleanup failures must not disguise the original failure's category.
        primary_error = sys.exc_info()[1]
        cleanup_errors = []
        if recorder is not None:
            try:
                recorder.stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        if initialized and not args.keep_task:
            try:
                _request("POST", f"{args.backend}/task/tear_down", json=operation)
            except Exception as exc:
                cleanup_errors.append(exc)
            try:
                restore_task_clock(target, clock_check)
                if clock_check:
                    atomic_json(Path(args.data_dir) / "clock-cleanup.json", clock_check)
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            atomic_json(Path(args.data_dir) / "cleanup-errors.json", {
                "errors": [f"{type(exc).__name__}: {exc}" for exc in cleanup_errors],
            })
            if primary_error is None:
                raise cleanup_errors[0]


if __name__ == "__main__":
    main()
