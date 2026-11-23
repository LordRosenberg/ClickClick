"""Console-owned jobs for isolated Skill regression on a dedicated Android emulator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent.skills.library import default_skills_root
from shared.schemas import TaskStatus


_LOG = logging.getLogger(__name__)
_RECORD = "skill_loop_job"
_ACTIVE = {"queued", "running"}
_RESULTS = {"awaiting_publication_review", "regression_passed", "published", "blocked", "error", "cancelled"}
_PHASES = {"learning", "planning", "recording", "replaying", "regression", "publication"}
_TERMINAL_TASKS = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class SkillLoopFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    serial: str = Field(pattern=r"^emulator-[0-9]+$")
    avd: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    snapshot: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    adb_port: int = Field(ge=1024, le=65535)
    model: str | None = Field(default=None, min_length=1)


def _ordinary_adb_port() -> int | None:
    socket = os.environ.get("ADB_SERVER_SOCKET", "").strip()
    port = socket.rsplit(":", 1)[-1] if socket else os.environ.get("ANDROID_ADB_SERVER_PORT", "5037")
    return int(port) if port.isdecimal() else None


def fixture_config(settings) -> SkillLoopFixture | None:
    try:
        fixture = SkillLoopFixture.model_validate_json(settings.skill_loop_fixture_json)
    except ValidationError:
        return None
    if fixture.adb_port in {5037, _ordinary_adb_port()} or _ordinary_adb_port() is None:
        return None
    return fixture


async def run_fixture_worker(payload, seconds, on_phase=None):
    worker = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "evaluation.gui_skill_console_worker", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=256 * 1024,
    )
    try:
        worker.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await worker.stdin.drain()
        final = None
        async with asyncio.timeout(seconds):
            async for line in worker.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict) or event.get("channel") != "skill-loop":
                    continue
                phase = event.get("phase")
                if on_phase is not None and phase in _PHASES:
                    on_phase(phase)
                if isinstance(event.get("result"), dict):
                    final = event["result"]
            code = await worker.wait()
        return final if not code else None
    finally:
        if worker.returncode is None:
            worker.stdin.close()
            try:
                async with asyncio.timeout(10):
                    await asyncio.gather(worker.stdout.read(), worker.wait())
            except TimeoutError:
                worker.kill()
                await worker.wait()


class SkillLoopJobs:
    def __init__(self, db, settings, pool):
        self.db, self.settings, self.pool = db, settings, pool
        self._jobs: dict[str, asyncio.Task] = {}
        self._closed = False
        self.fixture_slot = asyncio.Lock()

    def _write(self, task_id: str, status: str, **details) -> dict:
        result = {"status": status, "updated_at": time.time(), **details}
        self.db.append_agent_record(task_id, _RECORD, "latest", result)
        return result

    def status(self, task_id: str) -> dict | None:
        record = self.db.get_agent_record(task_id, _RECORD, "latest")
        if not record:
            return None
        result = record["payload"]
        if result["status"] in _ACTIVE and task_id not in self._jobs:
            return {**result, "status": "error", "reason": "skill_loop_interrupted"}
        return result

    def start(self, task, request) -> dict:
        task_id = task.id
        if task_id in self._jobs:
            return self.status(task_id)
        if self._closed or self._jobs:
            return self._write(task_id, "blocked", reason="learning_already_running")
        if task.status not in _TERMINAL_TASKS:
            return self._write(task_id, "blocked", reason="source_task_not_terminal")
        fixture = fixture_config(self.settings)
        if fixture is None:
            return self._write(task_id, "blocked", reason="fixture_not_configured")
        state = self._write(task_id, "queued")
        job = asyncio.create_task(self._run(task_id, request, fixture), name=f"skill-loop-{task_id}")
        self._jobs[task_id] = job
        job.add_done_callback(lambda _: self._jobs.pop(task_id, None))
        return state

    def cancel(self, task_id: str) -> dict | None:
        job = self._jobs.get(task_id)
        current = self.status(task_id)
        if job is None or job.done() or not current or current.get("phase") == "cancelling":
            return current
        if current["status"] == "queued":
            job.cancel()
            return self._write(task_id, "cancelled")
        job.cancel()
        return self._write(task_id, "running", phase="cancelling")

    async def close(self) -> None:
        self._closed = True
        for task_id in list(self._jobs):
            self.cancel(task_id)
        if self._jobs:
            await asyncio.gather(*tuple(self._jobs.values()), return_exceptions=True)

    async def _run(self, task_id, request, fixture) -> None:
        self._write(task_id, "running", phase="learning")
        try:
            async with self.fixture_slot:
                inventory = await asyncio.wait_for(self.pool.inventory(), timeout=10)
                if any(item.get("serial") == fixture.serial for item in inventory):
                    self._write(task_id, "blocked", reason="fixture_visible_to_ordinary_tasks")
                    return
                result = await self._worker(task_id, request, fixture)
            self._write(task_id, result.pop("status"), **result)
        except asyncio.CancelledError:
            self._write(task_id, "cancelled")
            raise
        except TimeoutError:
            self._write(task_id, "blocked", reason="skill_loop_budget_exhausted")
        except Exception as exc:
            _LOG.warning("Skill loop failed for %s (%s)", task_id, type(exc).__name__)
            self._write(task_id, "error", reason="skill_loop_worker_failed")

    async def _worker(self, task_id, request, fixture) -> dict:
        output = self.settings.data_dir.resolve() / "skill-loop" / "runs" / uuid4().hex
        payload = {
            "task_id": task_id,
            "fixture": fixture.model_dump(mode="json"),
            "settings": self.settings.model_dump(mode="json", exclude={"jev_api_key"}),
            "runtime": str(self.settings.data_dir.resolve()),
            "baseline": str(default_skills_root().resolve()),
            "output": str(output),
            "limits": request.model_dump(include={"max_calls", "max_actions", "max_seconds"}),
        }
        final = await run_fixture_worker(payload, request.max_seconds,
            on_phase=lambda phase: self._write(task_id, "running", phase=phase))
        if not final or final.get("status") not in _RESULTS:
            return {"status": "error", "reason": "skill_loop_worker_failed"}
        state = {"status": final["status"]}
        for key in ("reason", "pending_id", "case_origin"):
            value = final.get(key)
            if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", value):
                state[key] = value
        return state
