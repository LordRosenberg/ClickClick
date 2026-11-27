"""Device ownership and evidence for the explicit GUI-script evaluation entry point."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from agent.action_observation import ActionObservationTransaction
from agent.revisable.store import TaskStore
from agent.skills.script_checkpoint import ScriptCheckpoint, pending_script_operation
from agent.skills.learning import LearningStopped
from agent.skills.script_runner import (check_conditions, observed_condition_facts, resolve_semantic_target,
                                        run_gui_script)
from agent.skills.script_schema import GuiScript, SemanticSelector, script_fingerprint
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
from shared.recovery_runtime import RecoveryCoordinator, run_owned_task
from shared.recovery_store import RecoveryStore, digest
from shared.schemas import AgentState, TaskStatus


@dataclass
class EvaluationLease:
    quarantined: bool = False
    marker: Path | None = None
    avd: str = ""

    def bind_replay(self, runtime, task_id):
        if self.marker is not None:
            from evaluation.mobileworld.long_run_health import atomic_json
            atomic_json(self.marker, {"avd": self.avd, "runtime": str(Path(runtime).resolve()), "task_id": task_id})


@contextmanager
def exclusive_emulator(serial, avd, adb="adb", *, resume=None):
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
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = None
            if resume is None or previous != {"avd": avd, **resume}:
                raise RuntimeError(f"previous evaluation needs device reconciliation before reuse: {marker}")
        else:
            marker.write_text(avd, encoding="utf-8")
        lease = EvaluationLease(marker=marker, avd=avd)
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
        self.actions = sum(row["payload"].get("decision") == "act"
                           for row in store.db.list_agent_records(store.task_id, "event"))
        self.limited_attempts = sum(
            json.loads(row["intent_json"]).get("script_attempt", {}).get("phase") == "limited"
            for row in RecoveryStore(store.db, store.artifacts).operations(store.task_id)
            if row["kind"] == "device_action"
        )

    def observation(self, package):
        key = package.observation_id
        if key not in self.observation_ids:
            if self.store.db.get_agent_record(self.store.task_id, "observation", key) is None:
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
        execution = current_execution.get()
        if execution is None:
            raise RuntimeError("replay evidence requires an owned device action")
        state = execution.state
        seq = state.step_number
        event = {
            "step": seq, "script_step_id": parent_step_id or step.id, "origin": origin, "decision": "act",
            "observation_id": before.observation_id, "submitted_action": sanitized,
            "action_result": payload,
        }
        if matched is not None:
            event["target_snapshot"] = {
                "index": matched.index, "role": matched.role, "resource_id": matched.resource_id,
                "raw_text": matched.text if not matched.states.get("password") else "",
                "raw_a11y_label": matched.desc, "raw_hint": matched.hint,
            }
        state.step_number += 1
        state.revisable.execution_count += 0 if result.detail.get("device_action_units") == 0 else 1
        with self.store.db.transaction():
            self.store.put("event", str(seq), event)
            self.store.db.add_step_and_update_state(self.store.task_id, step.id, seq, event, state)


def needs_reconciliation(db, task_id):
    store = RecoveryStore(db, db.artifacts)
    return bool(pending_script_operation(store, task_id) or store.operations(task_id, pending=True)
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
        return _save_result(execution, self.result, self.fixture)


def _save_result(execution, result, fixture):
    from agent.orchestrator import prepare_task_checkpoint
    from shared.hitl import HumanInputBroker, InputConflict

    store = execution.store
    db, state, task_id = store.db, execution.state, execution.owner.task_id
    broker = HumanInputBroker(db)
    status = {"succeeded": TaskStatus.SUCCEEDED, "cancelled": TaskStatus.CANCELLED,
              "paused": TaskStatus.PAUSED}.get(result.status, TaskStatus.FAILED)
    with db.transaction():
        store.check(execution.owner)
        db.append_agent_record(task_id, "gui_script_run", "final", {
            "receipt": result.model_dump(mode="json"), "fixture": fixture,
        })
        if status == TaskStatus.PAUSED:
            prepare_task_checkpoint(db, store.artifacts, task_id, state)
            state.revisable.paused_at = time.time()
            if result.reason == "step_confirmation_required":
                current = broker.current(task_id)
                if (broker.is_script_confirmation(current)
                        and current["checkpoint"].get("step_id") != result.failed_step_id):
                    broker.finish_script_confirmation(task_id)
                    current = None
                if current is None or current["status"] not in {"pending", "resolved"}:
                    try:
                        broker.begin(
                            task_id, {"purpose": "clarification", "questions": [{
                                "id": "verify_script_submission",
                                "question": f"请核实步骤 {result.failed_step_id} 是否生成了本次提交记录。如能查到，请让 App 显示该记录。",
                                "reason": "ClickClick 将重新读取 App 中的证据。确认前不会重新提交。",
                            }]}, {"script_replay": True, "step_id": result.failed_step_id},
                            role="executor", invocation_id=execution.invocation_id, call_id="verify-script-result",
                        )
                    except InputConflict:
                        # Deadline and query limits still apply. The pause retains the verification request.
                        pass
        else:
            broker.finish_script_confirmation(task_id)
        db.update_task(task_id, status=status, state=state,
                       failure_reason=None if status == TaskStatus.SUCCEEDED else result.reason,
                       clear_failure_reason=status == TaskStatus.SUCCEEDED)
        store.publish_manifest(task_id, state=state)
    return status


async def _execute_replay(execution, script, *, settings, parameters, fixture, assist=None,
                          formal_recovery=False, takeover_seconds=30, asset_root=None, ocr=None,
                          budget=None, cancelled=None, replay_options=None):
    db, artifacts, task_id = execution.store.db, execution.store.artifacts, execution.owner.task_id
    options = replay_options or {}
    if ocr is None and options.get("ocr"):
        from evaluation.gui_script_ocr import LocalScriptOCR

        executable, language = options["ocr"]
        ocr = LocalScriptOCR(Path(executable), language)
    if assist is None and options.get("contracted"):
        from evaluation.gui_script_auto_repair import contracted_recovery

        def assist(owned, transaction, evidence):
            return contracted_recovery(driver=owned, transaction=transaction, evidence=evidence,
                                       asset_root=asset_root, ocr=ocr)
    elif assist is None and options.get("assist_policy"):
        from evaluation.gui_script_assist import AssistPolicy, recovery_session

        policy = AssistPolicy.model_validate(options["assist_policy"])

        def assist(owned, transaction, evidence):
            return recovery_session(policy, settings=settings, driver=owned, transaction=transaction,
                                    evidence=evidence, asset_root=asset_root, ocr=ocr)
    evidence = ScriptEvidence(TaskStore(db, artifacts, task_id), parameters)
    checkpoint = ScriptCheckpoint(execution, script, parameters)
    builder = ScriptRecoveryObservationBuilder() if assist is not None else ObservationBuilder()
    transaction = ActionObservationTransaction(execution.driver, builder)
    takeover = assist(execution.driver, transaction, evidence) if assist is not None else None

    async def formal(context):
        from evaluation.gui_script_executor import recover_step

        return await recover_step(context, settings=settings, execution=execution, transaction=transaction)

    def capture(package):
        if fixture is not None and "initial_state_hash" not in fixture and not evidence.actions:
            options = {"parameters": parameters, "asset_root": asset_root, "ocr": ocr}
            initial = observed_condition_facts(script, script.initial, package, **options)
            goal = observed_condition_facts(script, script.final, package, **options)
            if initial is not None and goal is not None and check_conditions(script, script.initial, package, **options):
                fixture["initial_state_hash"] = digest({"initial": initial, "goal": goal,
                                                        "activity": package.ui.activity})
                fixture["goal_initially_met"] = check_conditions(script, script.final, package, **options)
        evidence.observation(package)

    return await run_gui_script(
        script, driver=execution.driver, transaction=transaction,
        parameters=parameters, takeover=takeover, formal_takeover=formal if formal_recovery else None,
        takeover_seconds=takeover_seconds, checkpoint=checkpoint,
        on_observation=capture, on_action=evidence.action, asset_root=asset_root, ocr=ocr,
        before_dispatch=replay_dispatch_meter(budget), cancelled=cancelled,
    )


async def resume_replay_task(orch, task_id, binding):
    """Continue the same replay from the Console's existing resume scheduler."""
    execution = current_execution.get()
    if execution is None or execution.owner.task_id != task_id:
        raise RuntimeError("GUI replay requires durable device ownership")
    script = GuiScript.model_validate(binding["script"])
    if script_fingerprint(script) != binding["script_hash"]:
        raise ValueError("replay_script_binding_changed")
    task = orch.db.get_task(task_id)
    if task.device_serial != binding["serial"]:
        raise ValueError("replay_device_binding_changed")
    fixture = binding["fixture"]
    if fixture is not None and fixture.get("app_build"):
        build = await execution.driver.read_app_build(script.app_id)
        if not build or build.get("package") != script.app_id or build.get("version_code") != fixture["app_build"]:
            orch.db.update_task(task_id, status=TaskStatus.PAUSED, failure_reason="replay_resume_fixture_changed")
            return TaskStatus.PAUSED
    previous = orch.db.get_agent_record(task_id, "gui_script_run", "final")
    if previous is not None:
        fixture = previous["payload"]["fixture"]
    orch.db.update_task(task_id, status=TaskStatus.RUNNING)
    result = await _execute_replay(
        execution, script, settings=orch.settings, parameters=binding["parameters"],
        fixture=fixture, formal_recovery=binding["formal_recovery"],
        takeover_seconds=binding["takeover_seconds"],
        asset_root=Path(binding["asset_root"]) if binding.get("asset_root") else None,
        cancelled=lambda: orch.is_cancel_requested(task_id),
        replay_options=binding.get("replay_options"),
    )
    return _save_result(execution, result, fixture)


async def replay_owned(script, *, goal, settings, driver, serial, parameters, max_seconds, assist=None,
                       takeover_seconds=30.0, fixture=None, asset_root=None, ocr=None, budget=None,
                       formal_recovery=False, resume_task_id=None, on_task_created=None, replay_options=None):
    """Run only in the explicit evaluation process, recording an isolated task."""
    if not goal.strip() or max_seconds <= 0:
        raise ValueError("a task goal and positive overall deadline are required")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    db.artifacts = artifacts
    binding = dict(fixture) if fixture is not None else None
    binding_data = {
        "script": script.model_dump(mode="json"), "script_hash": script_fingerprint(script),
        "goal": goal, "serial": serial, "parameters": parameters, "fixture": binding,
        "formal_recovery": formal_recovery, "takeover_seconds": takeover_seconds,
        "asset_root": str(Path(asset_root).resolve()) if asset_root is not None else None,
        "replay_options": replay_options,
    }
    worker = None
    store = RecoveryStore(db, artifacts)
    try:
        if resume_task_id is None:
            from shared.hitl import HitlConfig, HumanInputBroker

            state = AgentState(instruction=goal)
            state.revisable.limits.deadline_at = time.time() + max_seconds
            state.revisable.limits.device_actions = settings.default_task_device_actions
            state.revisable.limits.executor_decisions = settings.default_task_executor_decisions
            state.revisable.limits.model_calls = settings.default_task_model_calls
            with db.transaction():
                task_id = db.create_task(goal, state, device_serial=serial).id
                db.append_agent_record(task_id, "gui_script_run", "binding", binding_data)
                HumanInputBroker(db).configure(task_id, HitlConfig(enabled=True))
                store.enable(task_id, "manual")
            if on_task_created is not None:
                on_task_created(task_id)
        else:
            task_id = resume_task_id
            task = db.get_task(task_id)
            record = db.get_agent_record(task_id, "gui_script_run", "binding")
            if task is None or record is None:
                raise ValueError("replay_task_not_found")
            saved = record["payload"]
            if any(saved.get(key) != binding_data[key] for key in (
                    "script_hash", "goal", "serial", "parameters", "asset_root")):
                raise ValueError("replay_resume_binding_changed")
            if any((saved.get("fixture") or {}).get(key) != (binding or {}).get(key)
                   for key in ("app_build", "avd")):
                raise ValueError("replay_resume_fixture_changed")
            previous = db.get_agent_record(task_id, "gui_script_run", "final")
            binding = previous["payload"]["fixture"] if previous is not None else saved["fixture"]
            takeover_seconds = saved["takeover_seconds"]
            formal_recovery = saved["formal_recovery"]
            replay_options = saved.get("replay_options")
            assist = None
            ocr = None
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
                return {"status": task.status.value, "reason": "task_already_terminal", "task_id": task_id,
                        "device_reconciliation_required": needs_reconciliation(db, task_id)}
            with db.transaction():
                store.control(task_id, "run")
                db.update_task(task_id, status=TaskStatus.QUEUED)
            if store.operations(task_id, pending=True) or store.metadata(task_id)["worker_token"]:
                from agent.runtime import create_orchestrator
                from agent.traces import TraceWriter

                orch = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, driver=driver,
                                           artifacts=artifacts)
                recovered = await RecoveryCoordinator(orch, settings).recover(task_id, explicit=True)
                if recovered.get("status") != TaskStatus.QUEUED.value:
                    return {**recovered, "device_reconciliation_required": True}

        async def execute(execution):
            return await _execute_replay(
                execution, script, settings=settings, parameters=parameters, fixture=binding, assist=assist,
                formal_recovery=formal_recovery, takeover_seconds=takeover_seconds,
                asset_root=asset_root, ocr=ocr, budget=budget, replay_options=replay_options,
            )

        worker = _OwnedReplay(db, artifacts, driver, execute, binding)
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
                    "device_reconciliation_required": True}
        pending = needs_reconciliation(db, task_id)
        if worker.result is None:
            return {"status": "unknown", "reason": terminal.value, "task_id": task_id,
                    "device_reconciliation_required": pending}
        return {"status": worker.result.status, "task_id": task_id,
                "reason": worker.result.reason, "receipt": worker.result.model_dump(mode="json"),
                "fixture": binding, "device_reconciliation_required": pending or worker.result.status == "paused"}
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
