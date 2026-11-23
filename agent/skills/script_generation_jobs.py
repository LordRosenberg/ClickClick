"""Post-task drafting for tasks that explicitly requested script recording."""

from __future__ import annotations

import asyncio
import hashlib
from contextvars import Context
from io import BytesIO
import json
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from zipfile import ZIP_DEFLATED, ZipFile

from agent.revisable.store import TaskStore
from agent.skills.learning import LearningBudget, LearningStopped
from agent.skills.script_schema import GuiScript, script_fingerprint, visual_templates
from agent.skills.script_visual import read_visual_asset
from shared.file_lock import FileLock, FileLockBusy
from shared.llm_gateway import GatewayError
from shared.schemas import TaskStatus


_LOG = logging.getLogger(__name__)
_RECORD = "script_generation"
_KEY = "mixed"
_PENDING = {"waiting", "queued", "running", "verifying"}
_TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


async def _author(task, *, settings, store, destination, budget):
    from agent.skills.script_mixed_learning import author_mixed_script

    return await author_mixed_script(task, settings=settings, store=store, destination=destination, budget=budget)


def _bundle_bytes(destination: Path, script: GuiScript, script_bytes: bytes) -> bytes:
    """Archive only the validated script and the assets that it actually references."""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("script.json", script_bytes)
        assets = {}
        for template in visual_templates(script):
            name = Path(template.png_path).as_posix()
            data = read_visual_asset(template, destination)
            if name in assets and assets[name] != data:
                raise ValueError("conflicting script asset references")
            assets[name] = data
        for name, data in sorted(assets.items()):
            archive.writestr(name, data)
        provenance = destination / "provenance.json"
        if provenance.is_file() and not provenance.is_symlink():
            archive.writestr("provenance.json", provenance.read_bytes())
    return output.getvalue()


def _validated_script(script_bytes: bytes) -> tuple[GuiScript, str]:
    document = json.loads(script_bytes)
    if not isinstance(document, dict):
        raise ValueError("script candidate is not an object")
    envelope = document.get("candidate", document)
    if not isinstance(envelope, dict):
        raise ValueError("script candidate envelope is not an object")
    # The JSON validation path preserves strict tuple fields used by the schema
    # when the reviewed candidate is wrapped with provenance.
    script = GuiScript.model_validate_json(json.dumps(envelope.get("script", envelope), ensure_ascii=False))
    actual_hash = script_fingerprint(script)
    if "script" in envelope and envelope.get("script_hash") != actual_hash:
        raise ValueError("script candidate hash mismatch")
    return script, actual_hash


class ScriptGenerationJobs:
    """Durable intent and one background authoring worker per process."""

    def __init__(self, db, artifacts, settings, *, verification=None):
        self.db, self.artifacts, self.settings = db, artifacts, settings
        self.verification = verification
        self._jobs: dict[str, asyncio.Task] = {}
        self._author_slot = asyncio.Semaphore(1)
        self._closed = False

    def _read(self, task_id: str) -> dict | None:
        record = self.db.get_agent_record(task_id, _RECORD, _KEY)
        return record["payload"] if record else None

    def _write(self, task_id: str, status: str, **evidence) -> None:
        self.db.append_agent_record(task_id, _RECORD, _KEY, {
            "status": status, "updated_at": time.time(), **evidence,
        })

    def request(self, task_id: str) -> bool:
        """Record the user's intent in the task-creation transaction."""
        with self.db.transaction():
            task = self.db.get_task(task_id)
            if task is None or not task.generate_test_script or task.status in _TERMINAL or self._read(task_id):
                return False
            self._write(task_id, "waiting")
            return True

    def status(self, task_id: str) -> dict | None:
        record = self._read(task_id)
        if record and record["status"] == "waiting":
            task = self.db.get_task(task_id)
            if task and task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return {**record, "status": "blocked", "reason": "source_task_not_succeeded"}
        return record

    def approve(self, task_id: str) -> dict:
        draft = self._read(task_id)
        if (self.verification is None or not draft or draft["status"] != "draft"
                or draft.get("verification_status") != "verified_pending_review" or not draft.get("verification_id")):
            raise ValueError("script is not awaiting publication review")
        self.verification.approve(draft["verification_id"], task_id, draft["script_hash"])
        details = {key: value for key, value in draft.items() if key not in {"status", "updated_at"}}
        self._write(task_id, "draft", **{**details, "verification_status": "approved",
                                        "verification_reason": None, "replay_required": False})
        return self._read(task_id)

    def enqueue(self, task_id: str) -> bool:
        """Schedule an opted-in, succeeded task outside its execution context."""
        return Context().run(self._enqueue, task_id)

    def _enqueue(self, task_id: str) -> bool:
        if self._closed or task_id in self._jobs:
            return False
        with self.db.transaction():
            task = self.db.get_task(task_id)
            record = self._read(task_id)
            if not (task and task.generate_test_script and task.status is TaskStatus.SUCCEEDED
                    and record and record["status"] in _PENDING):
                return False
            if record["status"] == "waiting":
                self._write(task_id, "queued")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False  # Durable work is resumed when the service starts.
        job = loop.create_task(self._run(task_id), name=f"script-generation-{task_id}", context=Context())
        self._jobs[task_id] = job
        job.add_done_callback(lambda _: self._jobs.pop(task_id, None), context=Context())
        return True

    def resume_pending(self) -> None:
        if self._closed:
            self._author_slot = asyncio.Semaphore(1)
        self._closed = False
        for record in self.db.find_agent_records(_RECORD, [_KEY]):
            if record["payload"]["status"] in _PENDING:
                self.enqueue(record["task_id"])

    async def close(self) -> None:
        self._closed = True
        jobs = tuple(self._jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def _run(self, task_id: str) -> None:
        task_key = hashlib.sha256(task_id.encode()).hexdigest()
        async with self._author_slot:
            root = self.artifacts.resolve(".script_generation")
            lock = FileLock(root / "locks" / f"{task_key}.lock")
            try:
                lock.acquire()
            except FileLockBusy:
                return
            except OSError:
                _LOG.exception("Could not claim script-generation job for %s", task_id)
                self._write(task_id, "error", reason="script_generation_lock_failed")
                return
            try:
                task, record = self.db.get_task(task_id), self._read(task_id)
                if not (task and task.generate_test_script and task.status is TaskStatus.SUCCEEDED
                        and record and record["status"] in _PENDING):
                    return
                if record["status"] == "verifying":
                    details = {key: record[key] for key in ("bundle_ref", "script_hash", "step_modes", "replay_required")}
                    await self._verify(task, details)
                else:
                    self._write(task_id, "running")
                    await self._generate(task, root, task_key)
            except asyncio.CancelledError:
                if (self._read(task_id) or {}).get("status") != "verifying":
                    self._write(task_id, "queued", reason="interrupted")
                raise
            except (LearningStopped, TimeoutError):
                self._write(task_id, "blocked", reason="script_generation_budget_exhausted")
            except GatewayError:
                self._write(task_id, "error", reason="script_generation_provider_error")
            except Exception:
                _LOG.exception("Script generation failed for %s", task_id)
                self._write(task_id, "error", reason="script_generation_failed")
            finally:
                lock.close()

    async def _generate(self, task, root: Path, task_key: str) -> None:
        budget = LearningBudget(
            max_calls=self.settings.learning_default_calls,
            max_seconds=self.settings.learning_default_seconds,
            max_actions=0,
            reserve_calls=0,
        )
        with TemporaryDirectory(prefix=f"{task_key[:12]}-", dir=root) as workspace:
            destination = Path(workspace) / "draft"
            async with asyncio.timeout(self.settings.learning_default_seconds):
                report = await _author(task, settings=self.settings,
                                       store=TaskStore(self.db, self.artifacts, task.id),
                                       destination=destination, budget=budget)
            if report.get("status") not in {"candidate", "draft"}:
                reason = report.get("reason")
                reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", reason) else None
                state = "error" if report.get("status") == "error" else "blocked"
                self._write(task.id, state, reason=reason or "script_generation_no_draft")
                return
            script_bytes = (destination / "script.json").read_bytes()
            script, script_hash = _validated_script(script_bytes)
            archive = _bundle_bytes(destination, script, script_bytes)
            ref = self.artifacts.save_content_addressed_bytes(
                f"script_generation_bundles/{task_key}", archive, suffix=".zip",
            )
            details = {"bundle_ref": ref, "script_hash": script_hash, "replay_required": True,
                       "step_modes": ["image" if step.action.type == "tap_visual" else "tree" for step in script.steps]}
            if self.verification is None:
                self._write(task.id, "draft", **details)
            else:
                self._write(task.id, "verifying", **details)
                await self._verify(task, details)

    async def _verify(self, task, details):
        if self.verification is None:
            self._write(task.id, "draft", **details, verification_status="blocked",
                        verification_reason="fixture_not_configured")
            return
        try:
            result = await self.verification.verify(task, self.artifacts.resolve(details["bundle_ref"]))
        except TimeoutError:
            result = {"verification_status": "blocked", "verification_reason": "time_budget"}
        except Exception:
            _LOG.exception("Isolated script verification failed for %s", task.id)
            result = {"verification_status": "error", "verification_reason": "script_verification_failed"}
        details = {**details, **result}
        details["replay_required"] = result["verification_status"] not in {"verified_pending_review", "automatically_registered"}
        self._write(task.id, "draft", **details)
