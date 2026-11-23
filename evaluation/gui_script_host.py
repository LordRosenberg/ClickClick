"""Device ownership and evidence for the explicit GUI-script evaluation entry point."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from agent.action_observation import ActionObservationTransaction
from agent.revisable.store import TaskStore
from agent.skills.learning import LearningStopped
from agent.skills.script_runner import (check_conditions, observed_condition_facts, resolve_semantic_target,
                                        run_gui_script)
from agent.skills.script_schema import SemanticSelector
from driver.android import AndroidDriver
from driver.environment import AndroidProvisioningOptions
from driver.factory import get_driver
from evaluation.gui_script_auto_repair import ScriptRecoveryObservationBuilder
from evaluation.gui_skill_budget import replay_dispatch_meter
from perception.observation import ObservationBuilder
from shared.app_resolver import build_default_resolver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.recovery_context import current_execution
from shared.recovery_runtime import run_owned_task
from shared.recovery_store import RecoveryStore, digest
from shared.schemas import AgentState, TaskStatus


@dataclass
class EvaluationLease:
    quarantined: bool = False


@contextmanager
def exclusive_emulator(serial, avd, adb="adb"):
    """Claim a dedicated emulator across independent evaluation CLI processes."""
    if not re.fullmatch(r"emulator-[0-9]+", serial) or not avd:
        raise ValueError("select an explicit dedicated emulator serial and AVD name")
    path = Path(tempfile.gettempdir()) / f"clickclick-gui-eval-{serial}.lock"
    marker = path.with_suffix(".unsettled")
    with path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("another GUI evaluation owns this emulator") from exc
        identity = subprocess.run([adb, "-s", serial, "emu", "avd", "name"],
                                  check=True, capture_output=True, text=True, timeout=10)
        if identity.stdout.splitlines()[:1] != [avd]:
            raise ValueError("emulator AVD identity does not match the authorized fixture")
        if marker.exists():
            raise RuntimeError(f"previous evaluation needs device reconciliation before reuse: {marker}")
        marker.write_text(avd, encoding="utf-8")
        lease = EvaluationLease()
        yield lease
        if not lease.quarantined:
            marker.unlink()


def evaluation_settings(env_file, runtime):
    return Settings(_env_file=env_file, data_dir=runtime, platform="android", use_fixture_driver=False,
                    driver_url="", driver_urls_json="", skill_version_audit_enabled=False,
                    hitl_enabled=False, task_recovery_policy="manual")


class ScriptEvidence:
    """Write the native task evidence that the separate Learner can review."""

    def __init__(self, store, parameters):
        self.store = store
        self.parameters = parameters
        self.observation_ids = set()
        self.actions = 0

    def observation(self, package):
        key = package.observation_id
        if key not in self.observation_ids:
            self.store.put("observation", key, {
                "app": package.ui.app_id, "text": package.text_for_llm,
                "accepted": package.accepted, "captured_monotonic_ms": package.captured_monotonic_ms,
            })
            self.observation_ids.add(key)
        execution = current_execution.get()
        if execution is not None and package.accepted:
            execution.basis = key

    def action(self, step, before, action, result, after, *, origin="script", parent_step_id=None):
        self.observation(before)
        if after is not None:
            self.observation(after)
        self.actions += 1
        sanitized = action.model_dump(mode="json", exclude_none=True)
        target = getattr(step.action, "target", None)
        matched = resolve_semantic_target(before, target, self.parameters) if isinstance(target, SemanticSelector) else None
        if matched is not None:
            sanitized = {**sanitized, "type": step.action.type, "index": matched.index}
            sanitized.pop("x", None)
            sanitized.pop("y", None)
        if "text" in sanitized:
            sanitized.pop("text")
            sanitized["text_redacted"] = True
        payload = result.model_dump(mode="json", include={"success", "receipt"})
        event = {
            "step": self.actions, "script_step_id": parent_step_id or step.id, "origin": origin, "decision": "act",
            "observation_id": before.observation_id, "submitted_action": sanitized,
            "action_result": payload,
        }
        if matched is not None:
            event["target_snapshot"] = {
                "index": matched.index, "role": matched.role, "resource_id": matched.resource_id,
                "raw_text": matched.text if not matched.states.get("password") else "",
                "raw_a11y_label": matched.desc, "raw_hint": matched.hint,
            }
        execution = current_execution.get()
        if execution is None:
            raise RuntimeError("replay evidence requires an owned device action")
        state = execution.state
        seq = state.step_number
        state.step_number += 1
        state.revisable.execution_count += 0 if result.detail.get("device_action_units") == 0 else 1
        with self.store.db.transaction():
            self.store.put("event", str(self.actions), event)
            self.store.db.add_step_and_update_state(self.store.task_id, step.id, seq, event, state)


def needs_reconciliation(db, task_id):
    return bool(RecoveryStore(db, db.artifacts).operations(task_id, pending=True)
                or db._conn.execute("SELECT 1 FROM device_runtime WHERE quarantine=1 LIMIT 1").fetchone())


class _OwnedReplay:
    """Use the normal device-ownership wrapper with a script-specific worker."""

    def __init__(self, db, artifacts, driver, execute, fixture):
        self.db, self.artifacts, self.driver, self.execute = db, artifacts, driver, execute
        self.fixture = fixture
        self.result = None

    def _resolve_driver(self, serial):
        return self.driver

    async def _run_owned_task(self, task_id, **limits):
        execution = current_execution.get()
        if execution is None:
            raise RuntimeError("GUI replay requires durable device ownership")
        self.db.update_task(task_id, status=TaskStatus.RUNNING)
        self.result = await self.execute(execution)
        status = {"succeeded": TaskStatus.SUCCEEDED, "cancelled": TaskStatus.CANCELLED}.get(
            self.result.status, TaskStatus.FAILED)
        TaskStore(self.db, self.artifacts, task_id).put("gui_script_run", "final", {
            "receipt": self.result.model_dump(mode="json"), "fixture": self.fixture,
        })
        self.db.update_task(task_id, status=status, failure_reason=None if status == TaskStatus.SUCCEEDED else self.result.reason)
        return status


async def replay_owned(script, *, goal, settings, driver, serial, parameters, max_seconds, assist=None,
                       takeover_seconds=30.0, fixture=None, asset_root=None, ocr=None, budget=None):
    """Run only in the explicit evaluation process, recording an isolated task."""
    if not goal.strip() or max_seconds <= 0:
        raise ValueError("a task goal and positive overall deadline are required")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    db.artifacts = artifacts
    state = AgentState(instruction=goal)
    state.revisable.limits.deadline_at = time.time() + max_seconds
    task_id = db.create_task(goal, state, device_serial=serial).id
    RecoveryStore(db, artifacts).enable(task_id, "manual")
    evidence = ScriptEvidence(TaskStore(db, artifacts, task_id), parameters)
    binding = dict(fixture) if fixture is not None else None

    async def execute(execution):
        builder = ScriptRecoveryObservationBuilder() if assist is not None else ObservationBuilder()
        transaction = ActionObservationTransaction(execution.driver, builder)
        takeover = assist(execution.driver, transaction, evidence) if assist is not None else None

        def capture(package):
            if binding is not None and "initial_state_hash" not in binding and not evidence.actions:
                options = {"parameters": parameters, "asset_root": asset_root, "ocr": ocr}
                initial = observed_condition_facts(script, script.initial, package, **options)
                goal = observed_condition_facts(script, script.final, package, **options)
                if initial is not None and goal is not None and check_conditions(script, script.initial, package, **options):
                    binding["initial_state_hash"] = digest({"initial": initial, "goal": goal,
                                                            "activity": package.ui.activity})
                    binding["goal_initially_met"] = check_conditions(script, script.final, package, **options)
            evidence.observation(package)

        return await run_gui_script(script, driver=execution.driver, transaction=transaction,
                                    parameters=parameters, takeover=takeover, takeover_seconds=takeover_seconds,
                                    on_observation=capture, on_action=evidence.action, asset_root=asset_root, ocr=ocr,
                                    before_dispatch=replay_dispatch_meter(budget))

    worker = _OwnedReplay(db, artifacts, driver, execute, binding)
    try:
        try:
            terminal = await asyncio.wait_for(run_owned_task(worker, task_id), max_seconds)
        except LearningStopped as exc:
            pending = needs_reconciliation(db, task_id)
            reason = str(exc)
            if not pending:
                db.update_task(task_id, status=TaskStatus.FAILED, failure_reason=reason)
            return {"status": "unknown" if pending else "failed", "reason": reason, "task_id": task_id,
                    "device_reconciliation_required": pending}
        except TimeoutError:
            return {"status": "unknown", "reason": "run_deadline_exceeded", "task_id": task_id,
                    "device_reconciliation_required": needs_reconciliation(db, task_id)}
        pending = (needs_reconciliation(db, task_id) or (worker.result is not None
                   and any(step.execution == "unknown" for step in worker.result.steps)))
        if worker.result is None or pending:
            return {"status": "unknown", "reason": terminal.value, "task_id": task_id,
                    "device_reconciliation_required": pending}
        return {"status": worker.result.status, "task_id": task_id,
                "receipt": worker.result.model_dump(mode="json"), "fixture": binding}
    finally:
        with suppress(Exception):
            journal = getattr(worker, "execution_journal", None)
            if journal is not None and hasattr(journal, "close"):
                journal.close()
        db.close()


async def with_android_driver(settings, serial, operation, *, full_resolution=False):
    if full_resolution:
        # Recorded crops and touch geometry use full ADB screenshots. The
        # regular driver factory enables a potentially resized video stream.
        driver = AndroidDriver(
            serial=serial,
            resolver=build_default_resolver(settings),
            ime_auto_setup=settings.ime_auto_setup,
            collector_enabled=settings.accessibility_collector_enabled,
            provisioning=AndroidProvisioningOptions(
                collector_apk_path=settings.accessibility_collector_apk_path,
                ime_apk_path=settings.ime_apk_path,
                stay_awake_while_plugged=settings.device_stay_awake_while_plugged,
            ),
        )
        await asyncio.to_thread(driver.connect, serial)
    else:
        driver = await asyncio.to_thread(get_driver, settings, serial=serial)
    try:
        return await operation(driver)
    finally:
        close = getattr(driver, "close_observation_provider", None)
        if close is not None:
            await close()
