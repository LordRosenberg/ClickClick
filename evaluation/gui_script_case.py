"""A snapshot-backed ordinary Android App fixture for isolated Skill A/B trials."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import shlex
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from agent.action_observation import ActionObservationTransaction
from agent.revisable.store import TaskStore
from agent.runtime import create_orchestrator
from agent.skills.script_learning import _native_matches_action
from agent.skills.script_runner import _matches, check_conditions, observed_condition_facts, resolve_semantic_target
from agent.skills.script_schema import (GuiScript, ScriptCondition, SemanticSelector, VisualCondition,
                                        VisualObjectCondition, visual_templates)
from agent.skills.snapshot import library_manifest
from agent.traces import TraceWriter
from evaluation.gui_script_host import (evaluation_settings, needs_reconciliation, replay_owned,
                                        with_android_driver)
from evaluation.gui_skill_budget import bind_model_budget, bounded_seconds, ordinary_limits, settle_actions
from evaluation.gui_skill_regression import (CleanupReceipt, IndependentAssessment, OrdinaryResult,
                                             PreparedCase, PreparedState)
from evaluation.skill_learning import EvaluationInfrastructureError, atomic_json
from perception.observation import ObservationBuilder
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import RecoveryStore, digest
from shared.schemas import AgentState, TaskStatus


_PRIVATE_RESTORE_WAIT_SECONDS = 10
_FILE_HASH_WAIT_SECONDS = 10
_SHARED_FILE_PATH = re.compile(r"/storage/emulated/0/(?:[\w.-][\w. -]*/)*[\w.-][\w. -]*")
_TOUCH_ACTIONS = frozenset({"tap", "tap_xy"})


def _safe_shared_file(path: str) -> bool:
    return bool(_SHARED_FILE_PATH.fullmatch(path) and not {".", ".."}.intersection(path.split("/")))


class CaseDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    setup: GuiScript
    goal: str = Field(min_length=1)
    build: str = Field(pattern=r"^[0-9]+$")
    parameters: dict[str, str] = Field(default_factory=dict)
    result: list[ScriptCondition] = Field(min_length=1)
    guards: list[ScriptCondition] = Field(min_length=1)
    branch: list[ScriptCondition] = Field(min_length=1)
    branch_action: Literal["tap", "tap_xy", "long_press", "type", "replace_text", "scroll", "back", "launch"]
    branch_after: list[ScriptCondition] = Field(min_length=1)
    branch_expected: bool
    preserved_files: dict[str, str] = Field(default_factory=dict, max_length=16)
    branch_touch_target: SemanticSelector | None = None

    @field_validator("preserved_files")
    @classmethod
    def validate_preserved_files(cls, files):
        if any(not _safe_shared_file(path) or not re.fullmatch(r"[a-f0-9]{64}", sha256)
               for path, sha256 in files.items()):
            raise ValueError("preserved files require exact Android shared storage paths and lowercase SHA-256")
        return files

    @model_validator(mode="after")
    def validate_branch_touch_target(self):
        target = self.branch_touch_target
        if target is not None and (self.branch_action not in _TOUCH_ACTIONS or target.occurrence is not None
                                   or target.relative_to is not None or target.expected_count not in (None, 1)):
            raise ValueError("a branch touch target requires tap or tap_xy and a unique semantic selector")
        return self

    @model_serializer(mode="wrap")
    def serialize_case(self, serialize):
        data = serialize(self)
        if not self.preserved_files:
            data.pop("preserved_files", None)
        if self.branch_touch_target is None:
            data.pop("branch_touch_target", None)
        return data

    def prepared_case(self):
        return PreparedCase(self.setup.script_id, self.goal, self.setup.app_id, self.build, self.branch_expected)


async def _snapshot_adb(adb, serial, *arguments):
    worker = await asyncio.create_subprocess_exec(
        adb, "-s", serial, *arguments,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await worker.communicate()
    except BaseException:
        if worker.returncode is None:
            worker.kill()
        await worker.wait()
        raise
    return worker.returncode, stdout


def _private_snapshot_transport(serial):
    address = os.environ.get("ADB_SERVER_SOCKET", "").strip()
    socket = re.fullmatch(r"tcp:(?:127\.0\.0\.1|localhost|\[::1\]):([0-9]{1,5})", address)
    return bool(socket and 1024 <= int(socket[1]) <= 65535 and int(socket[1]) != 5037
                and re.fullmatch(r"emulator-[0-9]+", serial))


async def _shared_file_hashes(adb, serial, paths):
    if not paths:
        return {}
    if not _private_snapshot_transport(serial):
        raise EvaluationInfrastructureError("preserved-file checks require a selected private Android emulator")
    hashes = {}
    for path in paths:
        if not _safe_shared_file(path):
            raise EvaluationInfrastructureError("preserved-file path is outside Android shared storage")
        try:
            code, output = await asyncio.wait_for(
                _snapshot_adb(adb, serial, "shell", "sha256sum", shlex.quote(path)), _FILE_HASH_WAIT_SECONDS,
            )
        except (OSError, TimeoutError):
            hashes[path] = None
            continue
        line = re.fullmatch(rb"([a-f0-9]{64}) [ *]" + re.escape(path.encode("utf-8")) + rb"\r?\n", output)
        hashes[path] = line[1].decode("ascii") if code == 0 and line else None
    return hashes


async def _wait_private_snapshot_transport(adb, serial):
    async def command(*arguments):
        return await asyncio.wait_for(_snapshot_adb(adb, serial, *arguments), _PRIVATE_RESTORE_WAIT_SECONDS)

    try:
        return await command("wait-for-device")
    except TimeoutError:
        pass

    # `reconnect offline` applies to a server's offline transports. Verify that
    # this private server lists no other devices before asking it to reconnect.
    try:
        code, stdout = await command("devices")
        lines = [line.split() for line in stdout.splitlines() if line.strip()]
        selected = serial.encode("ascii")
        offline = [[b"List", b"of", b"devices", b"attached"], [selected, b"offline"]]
        online = [[b"List", b"of", b"devices", b"attached"], [selected, b"device"]]
        if code or lines not in (offline, online):
            raise EvaluationInfrastructureError("private ADB recovery requires a single selected emulator")
        if lines == offline:
            code, _ = await command("reconnect", "offline")
            if code:
                raise EvaluationInfrastructureError("private ADB could not reconnect the restored emulator")
        return await command("wait-for-device")
    except TimeoutError as exc:
        raise EvaluationInfrastructureError("restored emulator did not reconnect to private ADB") from exc


async def restore_snapshot(adb, serial, snapshot):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot):
        raise ValueError("provide an existing named snapshot of the dedicated test emulator")
    code, stdout = await _snapshot_adb(adb, serial, "emu", "avd", "snapshot", "load", snapshot)
    if code or stdout.strip().splitlines()[-1:] != [b"OK"]:
        raise EvaluationInfrastructureError("emulator snapshot restore failed")
    # The emulator console can return before the selected Android transport reconnects.
    if _private_snapshot_transport(serial):
        code, _ = await _wait_private_snapshot_transport(adb, serial)
    else:
        code, _ = await _snapshot_adb(adb, serial, "wait-for-device")
    if code:
        raise EvaluationInfrastructureError("restored emulator did not reconnect to ADB")


@contextmanager
def isolated_skill_library(path: Path):
    key = "CLICKCLICK_SKILLS_DIR"
    previous = os.environ.get(key)
    os.environ[key] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


@dataclass
class _ArmEvidence:
    trace_packages: dict
    initial_state_hash: str = ""
    branch_observed: bool | None = None
    uncertain_input: bool = False
    initial_file_hashes: dict[str, str | None] | None = None


def _executor_touch_receipts(db, task_id):
    receipts = {}
    for trace in db.list_traces(task_id):
        tick = trace.payload
        if trace.kind != "executor_tick" or tick.get("decision") != "act":
            continue
        receipt = (tick.get("action_result") or {}).get("receipt") or {}
        transaction, basis = receipt.get("transaction_id"), tick.get("observation_id")
        if transaction and basis and tick.get("basis_observation_id") == basis and tick.get("observation_accepted") is True:
            receipts[(basis, transaction)] = tick
    return receipts


def _inside_bounds(bounds, x, y):
    return len(bounds) == 4 and bounds[0] <= x < bounds[2] and bounds[1] <= y < bounds[3]


def _branch_touch_matches(case, before, after, action, receipt, tick):
    selector = case.branch_touch_target
    target = resolve_semantic_target(before, selector, case.parameters)
    if target is None or len(_matches(before, selector, case.parameters)) != 1:
        return None
    result = tick.get("action_result") or {}
    native = result.get("receipt") or {}
    if (not receipt.get("transaction_id") or native.get("transaction_id") != receipt["transaction_id"]
            or result.get("success") is not True or native.get("dispatch_succeeded") is not True
            or native.get("observation_accepted") is not True
            or native.get("visible_change") == "none"
            or not (native.get("effect_outcome") == "confirmed"
                    or (native.get("effect_outcome") == "unknown"
                        and native.get("effect_reason") == "action_dispatched_observation_available"))
            or (native.get("effect_observation_id") or native.get("observation_id")) != after.observation_id
            or not _native_matches_action(tick, action, before.observation_id, allow_visual=True)):
        return None
    dispatched = tick["action"]
    if action["type"] == "tap":
        return dispatched.get("type") == "tap" and action.get("index") == dispatched.get("index") == target.index
    pipeline = tick.get("action_pipeline") or {}
    coordinates = pipeline.get("driver_coordinates") or {}
    x, y = coordinates.get("x"), coordinates.get("y")
    if (not before.coordinate_space_id or pipeline.get("coordinate_space_id") != before.coordinate_space_id
            or coordinates != (result.get("detail") or {}).get("dispatched_coordinates")
            or not all(type(value) in (int, float) and math.isfinite(value) for value in (x, y))
            or (x, y) != (dispatched.get("x"), dispatched.get("y"))):
        return None
    # Android's ADB tap dispatch truncates the coordinates to device pixels.
    x, y = int(x), int(y)
    return _inside_bounds(target.bounds, x, y) and not any(
        peer.index != target.index and peer.interactable and peer.clickable and peer.states.get("enabled") is not False
        and _inside_bounds(peer.bounds, x, y)
        for peer in before.ui.elements
    )


class AndroidSnapshotFixture:
    """The driver, ordinary orchestrator and library are used only in this evaluator."""

    def __init__(self, definitions, *, adb, serial, avd, snapshot, env_file, rounds, seconds,
                 asset_root=None, case_asset_roots=None, verified_initials=None, ocr=None, budget=None):
        self.definitions = (dict(definitions) if isinstance(definitions, Mapping)
                            else {case.setup.script_id: case for case in definitions})
        self.definition_ids = {id(case): case_id for case_id, case in self.definitions.items()}
        self.adb, self.serial, self.avd = adb, serial, avd
        self.snapshot, self.env_file = snapshot, env_file
        self.rounds, self.seconds = rounds, seconds
        self.asset_root = asset_root
        self.case_asset_roots = case_asset_roots or {}
        self.verified_initials = verified_initials or {}
        self.ocr = ocr
        self.budget = budget
        self.evidence = {}
        self.quarantined = False
        root = Path(__file__).resolve().parents[1]
        self.runtime_hash = digest({p.relative_to(root).as_posix(): digest(p.read_text(encoding="utf-8"))
                                    for name in ("agent", "driver", "perception", "shared")
                                    for p in (root / name).rglob("*.py")})

    def _case(self, context):
        return self.definitions[context.case.case_id]

    def _condition_options(self, case):
        return {"parameters": case.parameters,
                "asset_root": self.case_asset_roots.get(self.definition_ids[id(case)], self.asset_root),
                "ocr": self.ocr}

    def _settings(self, context, name):
        return evaluation_settings(self.env_file, context.output / name).model_copy(update={
            "default_model": context.model, "manager_model": context.model, "executor_model": context.model,
        })

    def _configuration(self, context, build, settings):
        return digest({"runtime": self.runtime_hash, "app": context.case.app_id, "build": build,
                       "avd": self.avd, "snapshot": self.snapshot, "case": self._case(context).model_dump(mode="json"),
                       "model": context.model, "provider": digest(settings.provider_for(context.model)),
                       "architecture": settings.agent_architecture, "observation": settings.executor_context_tokens,
                       "rounds": self.rounds, "seconds": self.seconds})

    async def _build(self, driver, context):
        build = await driver.read_app_build(context.case.app_id)
        if (not build or build.get("package") != context.case.app_id
                or build.get("version_code") != context.case.build):
            raise EvaluationInfrastructureError("fixture App build differs from the declared build")
        return build["version_code"]

    @staticmethod
    async def _observe(driver, context):
        package = await ActionObservationTransaction(driver, ObservationBuilder()).observe_current(attach_image=False)
        if not (package.accepted and (package.ui.capture_complete or package.clean_png)
                and package.ui.app_id == context.case.app_id):
            raise EvaluationInfrastructureError("fixture observation cannot provide an independent App-local check")
        return package

    def _initial_state(self, case, observation):
        options = self._condition_options(case)
        observed = observed_condition_facts(case.setup, case.setup.final, observation, **options)
        goal = observed_condition_facts(case.setup, case.result, observation, **options)
        if observed is None or goal is None:
            raise EvaluationInfrastructureError("initial-state requirements or preexisting result are unobservable")
        return {"witnesses": observed, "goal_witnesses": goal, "activity": observation.ui.activity,
                "ready": check_conditions(case.setup, case.setup.final, observation, **options),
                "goal_already_met": check_conditions(case.setup, case.result, observation, **options)}

    def checked_initial_state(self, case, observation):
        facts = self._initial_state(case, observation)
        options = self._condition_options(case)
        for name, status in (("branch", "branch_ready"), ("guards", "guards_passed")):
            conditions = getattr(case, name)
            witnesses = observed_condition_facts(case.setup, conditions, observation, **options)
            if witnesses is None:
                raise EvaluationInfrastructureError(f"prepared case {name} cannot be independently observed")
            facts[f"{name}_witnesses"] = witnesses
            facts[status] = check_conditions(case.setup, conditions, observation, **options)
        return facts

    def _paired_initial_state(self, case, observation):
        recorded = self.verified_initials.get(self.definition_ids[id(case)])
        if recorded is None:
            return self._initial_state(case, observation)
        state = self.checked_initial_state(case, observation)
        if digest(state) != recorded:
            raise EvaluationInfrastructureError("initial state differs from the registered replay")
        return state

    async def prepare(self, context):
        case = self._case(context)
        if self.quarantined:
            raise EvaluationInfrastructureError("isolated device requires reconciliation")
        evidence = self.evidence[context.nonce] = _ArmEvidence({})
        await restore_snapshot(self.adb, self.serial, self.snapshot)
        settings = self._settings(context, "prepare-runtime")

        async def run(driver):
            build = await self._build(driver, context)
            seconds = bounded_seconds(self.budget, self.seconds)
            evidence.uncertain_input = True
            prepared = await replay_owned(case.setup, goal=f"Prepare fixture {context.case.case_id}",
                                          settings=settings, driver=driver, serial=self.serial,
                                          parameters=case.parameters, max_seconds=seconds,
                                          asset_root=self._condition_options(case)["asset_root"], ocr=self.ocr,
                                          budget=self.budget)
            evidence.uncertain_input = bool(prepared.get("device_reconciliation_required"))
            if prepared["status"] != "succeeded":
                raise EvaluationInfrastructureError("fixture setup did not complete independently")
            observation = await self._observe(driver, context)
            facts = self._paired_initial_state(case, observation)
            evidence.initial_state_hash = digest(facts)
            path = context.output / "initial-check.json"
            report = {"observation_id": observation.observation_id, "facts": facts}
            if case.preserved_files:
                initial = await _shared_file_hashes(self.adb, self.serial, case.preserved_files)
                evidence.initial_file_hashes = initial
                report["file_preservation"] = {"expected": case.preserved_files, "observed": initial,
                                               "passed": initial == case.preserved_files}
            atomic_json(path, report)
            if case.preserved_files and evidence.initial_file_hashes != case.preserved_files:
                raise EvaluationInfrastructureError("preserved files differ from the declared initial SHA-256")
            return PreparedState(context.nonce, context.case.app_id, build, evidence.initial_state_hash,
                                 self._configuration(context, build, settings), facts["ready"],
                                 facts["goal_already_met"], str(path))

        return await with_android_driver(settings, self.serial, run, full_resolution=_uses_images(case))

    async def execute(self, context, skills, rounds, seconds):
        case = self._case(context)
        settings = self._settings(context, "ordinary-runtime")
        evidence = self.evidence[context.nonce]

        async def run(driver):
            build = await self._build(driver, context)
            if digest(self._paired_initial_state(case, await self._observe(driver, context))) != evidence.initial_state_hash:
                raise EvaluationInfrastructureError("verified initial state changed before ordinary execution")
            if case.preserved_files:
                latest = await _shared_file_hashes(self.adb, self.serial, case.preserved_files)
                if latest != evidence.initial_file_hashes:
                    raise EvaluationInfrastructureError("preserved files changed before ordinary execution")
            library_hash = library_manifest(skills)["hash"]
            db = Database(settings.db_path)
            artifacts = ArtifactStore(settings.artifacts_dir)
            try:
                traces = TraceWriter(db, artifacts)
                with isolated_skill_library(skills):
                    orch = create_orchestrator(db, traces, settings=settings, driver=driver, artifacts=artifacts,
                                               max_steps=rounds, max_role_invocations=rounds * 6)
                    bind_model_budget(orch, self.budget)
                    persist = orch._persist_observation_files

                    def remember(package):
                        if package.accepted and (package.ui.capture_complete or package.clean_png):
                            evidence.trace_packages[package.observation_id] = package
                        return persist(package)

                    orch._persist_observation_files = remember
                    state = AgentState(instruction=case.goal)
                    state.revisable.limits = ordinary_limits(self.budget, rounds, seconds)
                    action_allowance = state.revisable.limits.device_actions
                    task = db.create_task(case.goal, state, device_serial=self.serial)
                    recovery = RecoveryStore(db, artifacts)
                    recovery.enable(task.id, "manual")
                    began = time.monotonic()
                    try:
                        await orch.run_task(task.id)
                    finally:
                        evidence.uncertain_input = needs_reconciliation(db, task.id)
                        task = db.get_task(task.id)
                        settle_actions(self.budget, task, action_allowance, evidence.uncertain_input)
                if evidence.uncertain_input or library_manifest(skills)["hash"] != library_hash:
                    raise EvaluationInfrastructureError("unknown device effect or isolated library changed during ordinary run")
                events = TaskStore(db, artifacts, task.id).records("event")
                touch_receipts = _executor_touch_receipts(db, task.id) if case.branch_touch_target is not None else None
                branch_actions = _TOUCH_ACTIONS if touch_receipts is not None else {case.branch_action}
                relevant = []
                options = self._condition_options(case)
                for row in events:
                    event = row["payload"]
                    receipt = (event.get("action_result") or {}).get("receipt") or {}
                    action = event.get("submitted_action") or {}
                    if action.get("type") not in branch_actions:
                        continue
                    before = evidence.trace_packages.get(event.get("observation_id"))
                    after = evidence.trace_packages.get(receipt.get("effect_observation_id") or receipt.get("observation_id"))
                    if not (receipt.get("dispatch_succeeded") and receipt.get("observation_accepted")
                            and before is not None and after is not None and before is not after
                            and before.ui.app_id == after.ui.app_id == case.setup.app_id):
                        relevant.append(None)
                        continue
                    if (observed_condition_facts(case.setup, case.branch, before, **options) is None
                            or observed_condition_facts(case.setup, case.branch_after, after, **options) is None):
                        relevant.append(None)
                        continue
                    branch = (check_conditions(case.setup, case.branch, before, **options)
                              and check_conditions(case.setup, case.branch_after, after, **options))
                    if branch and touch_receipts is not None:
                        if observed_condition_facts(case.setup, case.branch_after, before, **options) is None:
                            branch = None
                        else:
                            tick = touch_receipts.get((before.observation_id, receipt.get("transaction_id")), {})
                            branch = (not check_conditions(case.setup, case.branch_after, before, **options)
                                      and _branch_touch_matches(case, before, after, action, receipt, tick))
                    relevant.append(branch)
                evidence.branch_observed = True if True in relevant else (
                    False if None not in relevant and events else None)
                return OrdinaryResult(context.nonce, self._configuration(context, build, settings), library_hash,
                                      task.status == TaskStatus.SUCCEEDED, task.state.revisable.execution_count,
                                      task.state.role_invocation_count, time.monotonic() - began,
                                      str(settings.data_dir), task.id)
            finally:
                db.close()

        return await with_android_driver(settings, self.serial, run, full_resolution=_uses_images(case))

    async def assess(self, context, preparation, execution):
        case = self._case(context)
        settings = self._settings(context, "check-runtime")

        async def run(driver):
            package = await self._observe(driver, context)
            options = self._condition_options(case)
            if (observed_condition_facts(case.setup, case.result, package, **options) is None
                    or observed_condition_facts(case.setup, case.guards, package, **options) is None):
                raise EvaluationInfrastructureError("independent result or guard is unobservable")
            passed = check_conditions(case.setup, case.result, package, **options)
            guards = check_conditions(case.setup, case.guards, package, **options)
            evidence = self.evidence[context.nonce]
            report = {"observation_id": package.observation_id, "result": passed,
                      "guards": guards, "branch_observed": evidence.branch_observed}
            if case.preserved_files:
                after = await _shared_file_hashes(self.adb, self.serial, case.preserved_files)
                preserved = evidence.initial_file_hashes == after == case.preserved_files
                report["file_preservation"] = {"expected": case.preserved_files,
                                               "before": evidence.initial_file_hashes, "after": after,
                                               "passed": preserved}
                guards = report["guards"] = guards and preserved
            path = context.output / "independent-check.json"
            atomic_json(path, report)
            return IndependentAssessment(context.nonce, passed, evidence.branch_observed, guards, str(path),
                                         file_preservation=report.get("file_preservation"))

        return await with_android_driver(settings, self.serial, run, full_resolution=_uses_images(case))

    async def cleanup(self, context):
        evidence = self.evidence.pop(context.nonce, None)
        path = context.output / "cleanup.json"
        if self.quarantined or (evidence is not None and evidence.uncertain_input):
            self.quarantined = True
            atomic_json(path, {"restored": False, "reason": "device input outcome unknown"})
            return CleanupReceipt(context.nonce, False, str(path))
        try:
            await restore_snapshot(self.adb, self.serial, self.snapshot)
        except BaseException:
            self.quarantined = True
            atomic_json(path, {"restored": False, "reason": "clean fixture restore not verified"})
            raise
        atomic_json(path, {"restored": True, "snapshot": self.snapshot})
        return CleanupReceipt(context.nonce, True, str(path))


def _uses_images(case):
    return next(visual_templates(case.setup), None) is not None or any(
        isinstance(condition, (VisualCondition, VisualObjectCondition)) or getattr(condition, "visual", None) is not None
        for condition in (*case.result, *case.guards, *case.branch, *case.branch_after)
    )
