"""Record and independently verify an Android Skill regression setup."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.revisable.store import TaskStore
from agent.runtime import create_orchestrator
from agent.skills.learning import LearningBudget
from agent.skills.script_mixed_learning import author_mixed_script
from agent.skills.script_schema import (
    GuiScript, Identifier, ScriptCondition, VisualCondition, VisualObjectCondition, script_fingerprint, visual_templates,
)
from agent.skills.script_visual import read_visual_asset
from agent.traces import TraceWriter
from evaluation.gui_script_case import AndroidSnapshotFixture, CaseDefinition, isolated_skill_library, restore_snapshot
from evaluation.gui_script_host import evaluation_settings, needs_reconciliation, replay_owned, with_android_driver
from evaluation.gui_script_release import _verified_run
from evaluation.gui_skill_budget import bind_model_budget, bounded_seconds, ordinary_limits, settle_actions
from evaluation.gui_skill_case_registry import CaseVerification, case_fingerprint
from evaluation.gui_skill_regression import FixtureContext
from evaluation.skill_learning import EvaluationInfrastructureError, atomic_json
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import RecoveryStore, digest
from shared.schemas import AgentState, TaskStatus


class RecordingCaseRequest(BaseModel):
    """The trusted test intent and assertions, defined before recording."""

    model_config = ConfigDict(extra="forbid", strict=True)

    app_id: Identifier
    build: str = Field(pattern=r"^[0-9]+$")
    setup_goal: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    result: list[ScriptCondition] = Field(min_length=1)
    guards: list[ScriptCondition] = Field(min_length=1)
    branch: list[ScriptCondition] = Field(min_length=1)
    branch_action: Literal["tap", "tap_xy", "long_press", "type", "replace_text", "scroll", "back", "launch"]
    branch_after: list[ScriptCondition] = Field(min_length=1)
    branch_expected: bool

    @model_validator(mode="after")
    def separate_tasks(self):
        prepare, evaluate = self.setup_goal.strip(), self.goal.strip()
        if not prepare or not evaluate or prepare.casefold() == evaluate.casefold():
            raise ValueError("provide separate nonempty setup and ordinary task goals")
        return self


@dataclass(frozen=True)
class RecordedCase:
    definition: CaseDefinition
    asset_root: Path
    candidate: dict
    verification: CaseVerification


class CaseRecordingError(EvaluationInfrastructureError):
    def __init__(self, reason: str, *, device_reconciliation_required: bool = False):
        super().__init__(reason)
        self.device_reconciliation_required = device_reconciliation_required


def _limits(model, rounds, seconds, fixture_seconds):
    if (not isinstance(model, str) or not model.strip() or type(rounds) is not int or rounds <= 0
            or any(type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0
                   for limit in (seconds, fixture_seconds))):
        raise ValueError("recording requires a model and positive finite execution limits")


async def _reset(adb, serial, snapshot, timeout):
    try:
        await asyncio.wait_for(restore_snapshot(adb, serial, snapshot), timeout)
    except (EvaluationInfrastructureError, OSError, TimeoutError) as exc:
        raise CaseRecordingError("clean fixture restore could not be verified",
                                 device_reconciliation_required=True) from exc


async def verify_case(
    definition: CaseDefinition, *, asset_root: Path, output: Path, adb, serial, avd, snapshot, env_file, model,
    rounds, seconds, fixture_seconds, ocr=None, setup_goal: str | None = None, budget=None,
) -> CaseVerification:
    """Verify a prepared case from reset without a model or ordinary task execution.

    The caller must hold the exclusive emulator lease.
    """
    _limits(model, rounds, seconds, fixture_seconds)
    if definition.setup.platform != "android":
        raise ValueError("prepared case recording requires Android")
    definition.setup.bind_parameters(definition.parameters)
    for template in visual_templates(definition.setup):
        read_visual_asset(template, asset_root)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = AndroidSnapshotFixture([definition], adb=adb, serial=serial, avd=avd, snapshot=snapshot,
                                     env_file=env_file, rounds=rounds, seconds=seconds, asset_root=asset_root, ocr=ocr)
    context = FixtureContext(definition.prepared_case(), uuid4().hex, output, model)
    settings = fixture._settings(context, "replay-runtime")
    goal = setup_goal or f"Prepare fixture {context.case.case_id}"
    uncertain = False

    async def run(driver):
        nonlocal uncertain
        build = await asyncio.wait_for(fixture._build(driver, context), fixture_seconds)
        replay_seconds = bounded_seconds(budget, seconds)
        uncertain = True
        replay = await replay_owned(
            definition.setup, goal=goal, settings=settings, driver=driver, serial=serial,
            parameters=definition.parameters, max_seconds=replay_seconds, asset_root=asset_root, ocr=ocr,
            fixture={"app_build": build, "snapshot": snapshot, "avd": avd,
                     "parameters_hash": digest(definition.parameters)}, budget=budget,
        )
        uncertain = bool(replay.get("device_reconciliation_required"))
        replay["runtime"] = str(settings.data_dir)
        atomic_json(output / "replay.json", replay)
        if replay["status"] != "succeeded" or uncertain:
            raise CaseRecordingError("prepared case replay did not complete independently",
                                     device_reconciliation_required=uncertain)
        receipt, _ = _verified_run(replay, goal)
        if receipt.assisted or receipt.script_hash != script_fingerprint(definition.setup):
            raise CaseRecordingError("prepared case replay did not match the verified setup")
        observation = await asyncio.wait_for(fixture._observe(driver, context), fixture_seconds)
        facts = fixture.checked_initial_state(definition, observation)
        atomic_json(output / "initial-check.json", {"observation_id": observation.observation_id, "facts": facts})
        if (not facts["ready"] or facts["goal_already_met"] or not facts["guards_passed"]
                or facts["branch_ready"] != definition.branch_expected):
            raise CaseRecordingError("setup did not independently reach the declared regression initial state")
        return CaseVerification(
            receipt=receipt, case_hash=case_fingerprint(definition), app_id=context.case.app_id, build=build,
            avd=avd, snapshot=snapshot, initial_state_hash=digest(facts), conditions_met=facts["ready"],
            goal_already_met=facts["goal_already_met"], branch_ready=facts["branch_ready"],
            guards_passed=facts["guards_passed"],
        )

    image_checks = (*definition.result, *definition.guards, *definition.branch, *definition.branch_after)
    uses_images = next(visual_templates(definition.setup), None) is not None or any(
        isinstance(condition, (VisualCondition, VisualObjectCondition))
        or getattr(condition, "visual", None) is not None for condition in image_checks
    )
    await _reset(adb, serial, snapshot, fixture_seconds)
    try:
        verification = await with_android_driver(settings, serial, run, full_resolution=uses_images)
    except (EvaluationInfrastructureError, OSError, TimeoutError, ValueError) as exc:
        if isinstance(exc, CaseRecordingError) and exc.device_reconciliation_required == uncertain:
            raise
        raise CaseRecordingError(str(exc), device_reconciliation_required=uncertain) from exc
    finally:
        if not uncertain:
            await _reset(adb, serial, snapshot, fixture_seconds)
    atomic_json(output / "verification.json", verification.model_dump(mode="json"))
    return verification


async def record_case(
    request: RecordingCaseRequest, *, output: Path, baseline_skills: Path, adb, serial, avd, snapshot, env_file,
    model, rounds, seconds, fixture_seconds, ocr=None, budget=None, progress=None,
) -> RecordedCase:
    """Run only the setup task with recording on, then author and verify its draft."""
    _limits(model, rounds, seconds, fixture_seconds)
    if not isinstance(request, RecordingCaseRequest):
        raise TypeError("recording requires a validated case request")
    output, baseline_skills = Path(output).resolve(), Path(baseline_skills).resolve()
    if not baseline_skills.is_dir() or output.is_relative_to(baseline_skills):
        raise ValueError("recording output must be outside an existing baseline Skill library")
    output.mkdir(parents=True, exist_ok=False)
    asset_root = output / "draft"
    settings = evaluation_settings(env_file, output / "source-runtime").model_copy(update={
        "default_model": model, "manager_model": model, "executor_model": model,
    })
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    db.artifacts = artifacts
    uncertain = False

    async def run(driver):
        nonlocal uncertain
        build = await asyncio.wait_for(driver.read_app_build(request.app_id), fixture_seconds)
        if not build or build.get("package") != request.app_id or build.get("version_code") != request.build:
            raise CaseRecordingError("recording fixture App build differs from the declared build")
        instruction = request.setup_goal.strip()
        state = AgentState(instruction=instruction, generate_test_script=True)
        state.revisable.limits = ordinary_limits(budget, rounds, seconds)
        action_allowance = state.revisable.limits.device_actions
        task = db.create_task(instruction, state, device_serial=serial)
        RecoveryStore(db, artifacts).enable(task.id, "manual")
        with isolated_skill_library(baseline_skills):
            orch = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                       driver=driver, artifacts=artifacts, max_steps=rounds,
                                       max_role_invocations=rounds * 6)
            bind_model_budget(orch, budget)
            uncertain = True
            try:
                await orch.run_task(task.id)
            finally:
                uncertain = needs_reconciliation(db, task.id)
                with suppress(Exception):
                    journal = getattr(orch, "execution_journal", None)
                    if journal is not None:
                        journal.close()
                stored = db.get_task(task.id)
                settle_actions(budget, stored, action_allowance, uncertain)
        atomic_json(output / "source.json", {
            "task_id": task.id, "status": stored.status.value, "failure_reason": stored.failure_reason,
            "script_recording": stored.generate_test_script, "app_id": request.app_id, "build": request.build,
        })
        if stored.status is not TaskStatus.SUCCEEDED or uncertain:
            raise CaseRecordingError("ClickClick did not complete the bounded setup task",
                                     device_reconciliation_required=uncertain)
        return stored

    try:
        await _reset(adb, serial, snapshot, fixture_seconds)
        try:
            task = await with_android_driver(settings, serial, run, full_resolution=True)
        finally:
            if not uncertain:
                await _reset(adb, serial, snapshot, fixture_seconds)
        author_budget = budget or LearningBudget(max_calls=settings.learning_default_calls, max_actions=0,
                                                  max_seconds=settings.learning_default_seconds, reserve_calls=0)
        report = await author_mixed_script(task, settings=settings, store=TaskStore(db, artifacts, task.id),
                                          destination=asset_root, budget=author_budget)
        atomic_json(output / "author.json", report)
        if report["status"] != "candidate":
            raise CaseRecordingError(f"setup script was not generated: {report.get('reason', 'unknown')}")
        candidate = report["candidate"]
        script = GuiScript.model_validate_json(json.dumps(candidate["script"], ensure_ascii=False))
        fingerprint = script_fingerprint(script)
        if (script.app_id != request.app_id or candidate.get("script_hash") != fingerprint
                or candidate.get("review", {}).get("decision") != "accept"
                or candidate.get("review", {}).get("script_hash") != fingerprint
                or candidate.get("provenance", {}).get("goal") != request.setup_goal.strip()):
            raise CaseRecordingError("reviewed setup script does not match the recording request")
    except (EvaluationInfrastructureError, OSError, TimeoutError, ValueError) as exc:
        if isinstance(exc, CaseRecordingError):
            raise
        raise CaseRecordingError(str(exc), device_reconciliation_required=uncertain) from exc
    finally:
        db.close()

    definition = CaseDefinition(
        setup=script, **request.model_dump(exclude={"setup_goal", "app_id"}),
    )
    if progress is not None:
        progress("replaying")
    verification = await verify_case(
        definition, asset_root=asset_root, output=output / "verification", adb=adb, serial=serial, avd=avd,
        snapshot=snapshot, env_file=env_file, model=model, rounds=rounds, seconds=seconds,
        fixture_seconds=fixture_seconds, ocr=ocr, setup_goal=request.setup_goal.strip(), budget=budget,
    )
    return RecordedCase(definition, asset_root, candidate, verification)
