"""Run a Console Skill loop in a separate ADB and Skill-library process."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from threading import Thread
from types import SimpleNamespace


def _emit(**event):
    print(json.dumps({"channel": "skill-loop", **event}, ensure_ascii=True), flush=True)


def _configure(payload):
    # The parent passes effective settings through stdin. Child overrides never
    # affect the Console or a normal ClickClick task.
    for key, value in payload["settings"].items():
        env = "CLICKCLICK_" + key.upper()
        if value is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = value if isinstance(value, str) else json.dumps(value)
    port = str(payload["fixture"]["adb_port"])
    os.environ.update(ADB_SERVER_SOCKET="tcp:127.0.0.1:" + port,
                      ANDROID_ADB_SERVER_ADDRESS="127.0.0.1", ANDROID_ADB_SERVER_PORT=port)
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")


async def _adb_query(adb, *args):
    child = await asyncio.create_subprocess_exec(adb, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=10)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    return stdout.decode("utf-8", errors="replace") if child.returncode == 0 else ""


async def _run(payload):
    from driver.adb import adb_bin
    from evaluation.gui_skill_loop import FixtureOptions, SkillLoopLimits, run_skill_loop
    from shared.config import Settings

    fixture = payload["fixture"]
    adb = adb_bin()
    serial = fixture["serial"]
    devices = await _adb_query(adb, "devices")
    actual = await _adb_query(adb, "-s", serial, "emu", "avd", "name")
    if f"{serial}\tdevice" not in devices.splitlines() or actual.splitlines()[:1] != [fixture["avd"]]:
        return {"status": "blocked", "reason": "fixture_unavailable"}
    if (await _adb_query(adb, "-s", serial, "shell", "getprop", "sys.boot_completed")).strip() != "1":
        return {"status": "blocked", "reason": "fixture_unavailable"}
    snapshots = await _adb_query(adb, "-s", serial, "emu", "avd", "snapshot", "list")
    if not any(fixture["snapshot"] in line.split() for line in snapshots.splitlines()):
        return {"status": "blocked", "reason": "fixture_unavailable"}
    if payload.get("operation") == "verify_recorded_script":
        from evaluation.gui_scripts import _replay

        report = await _replay(SimpleNamespace(script=Path(payload["script"]), goal=payload["goal"],
            serial=serial, avd=fixture["avd"], adb=adb, snapshot=fixture["snapshot"],
            seconds=payload["seconds"], output=Path(payload["output"]), env_file=None, verify=True, repair=False,
            auto_enroll_registry=Path(payload["registry"]), parameters=None, assets=None,
            ocr_executable=None, assist_policy=None))
        return {"status": report["update"], "reason": report.get("reason") or report["first"].get("reason")}

    limits = SkillLoopLimits(**payload["limits"])
    runtime = Path(payload["runtime"])
    settings = Settings(_env_file=None, data_dir=runtime)
    model = fixture.get("model") or settings.executor_model or settings.manager_model or settings.default_model
    options = FixtureOptions(adb, serial, fixture["avd"], fixture["snapshot"], None, model,
                             limits.max_actions, limits.max_seconds, limits.max_seconds)
    report = await run_skill_loop(runtime=runtime, task_id=payload["task_id"], plan=None,
        plan_dir=runtime, baseline_skills=Path(payload["baseline"]), registry_root=runtime / "skill-loop" / "cases",
        output=Path(payload["output"]), options=options, limits=limits, auto_publish=True,
        progress=lambda phase: _emit(phase=phase))
    case = (report.get("cases") or {}).get("source", {})
    return {"status": report["status"], "reason": report.get("reason"), "case_origin": case.get("origin"),
            "pending_id": report.get("pending_id")}


async def _session(payload, stdin_fd):
    work = asyncio.create_task(_run(payload))
    loop = asyncio.get_running_loop()

    def watch_parent():
        try:
            os.read(stdin_fd, 1)
        except OSError:
            pass
        try:
            loop.call_soon_threadsafe(work.cancel)
        except RuntimeError:
            pass

    Thread(target=watch_parent, daemon=True, name="skill-loop-cancellation").start()
    try:
        result = await work
    except asyncio.CancelledError:
        result = {"status": "cancelled"}
    except TimeoutError:
        result = {"status": "blocked", "reason": "fixture_unavailable"}
    except Exception as exc:
        output = Path(payload["output"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "worker-error.txt").write_text(type(exc).__name__ + "\n", encoding="utf-8")
        result = {"status": "error", "reason": "skill_loop_worker_failed"}
    _emit(result=result)


if __name__ == "__main__":
    pipe = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    data = json.loads(pipe.readline())
    _configure(data)
    asyncio.run(_session(data, pipe.fileno()))
