"""Explicit script authoring, replay/repair and ordinary-App Skill evaluation."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import time

from agent.revisable.store import TaskStore
from agent.skills.learning import Candidate, LearningBudget, LearningStopped, validate_candidate
from agent.skills.library import SkillLibrary
from agent.skills.script_learning import author_script, repair_script
from agent.skills.script_schema import GuiScript, script_fingerprint, visual_templates
from agent.skills.skill_edits import DeleteCandidate
from driver.adb import adb_bin
from evaluation.gui_script_assist import AssistPolicy, recovery_session
from evaluation.gui_script_auto_repair import contracted_recovery, is_low_impact_initial, is_low_impact_repair
from evaluation.gui_script_case import AndroidSnapshotFixture, CaseDefinition, restore_snapshot
from evaluation.gui_script_host import (evaluation_settings, exclusive_emulator, replay_owned,
                                        with_android_driver)
from evaluation.gui_script_ocr import LocalScriptOCR
from evaluation.gui_script_release import register_script, registered_original_context
from evaluation.gui_skill_regression import validate_gui_skill
from evaluation.skill_learning import EvaluationInfrastructureError, atomic_json
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.config import Settings
from shared.llm_gateway import GatewayError
from shared.recovery_store import digest
from shared.schemas import TaskStatus


def _load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _script(value):
    if not isinstance(value, dict):
        raise ValueError("a script or reviewed candidate object is required")
    envelope = value.get("candidate", value)
    if not isinstance(envelope, dict):
        raise ValueError("missing script candidate")
    script = GuiScript.model_validate_json(json.dumps(envelope.get("script", envelope), ensure_ascii=False))
    if "script" in envelope and envelope.get("script_hash") != script_fingerprint(script):
        raise ValueError("candidate script hash does not match the supplied script")
    return script


def _replay_artifact(path):
    """Resolve a registry directory once, leaving explicitly pinned artifacts unchanged."""
    if not path.is_dir():
        document = _load(path)
        return path, document, _script(document)
    revisions = [(int(match[1]), item) for item in path.iterdir()
                 if (match := re.fullmatch(r"([1-9][0-9]*)\.json", item.name))]
    if not revisions:
        raise ValueError("script registry directory has no numbered revision")
    revision, selected = max(revisions)
    if selected.is_symlink() or not selected.is_file() or selected.resolve().parent != path.resolve():
        raise ValueError("latest registered revision is not an owned regular artifact")
    document = _load(selected)
    script = _script(document)
    review = document.get("review") or {}
    verification = document.get("verification") or {}
    second = verification.get("second") or {}
    receipt = second.get("receipt") or {}
    if (script.revision != revision or script.script_id != path.name or document.get("replay_required") is not False
            or review.get("decision") != "accept" or review.get("script_hash") != script_fingerprint(script)
            or verification.get("update") != "verified_pending_review" or second.get("status") != "succeeded"
            or receipt.get("script_hash") != script_fingerprint(script) or receipt.get("status") != "succeeded"
            or receipt.get("script_id") != script.script_id or receipt.get("revision") != revision
            or receipt.get("assisted") is not False
            or receipt.get("final_asserted") is not True):
        raise ValueError("latest registered revision failed its immutable enrollment checks")
    return selected, document, script


def _assets(directory, envelope, explicit):
    if explicit is not None:
        return explicit.resolve()
    directory = directory.resolve()
    name = envelope.get("asset_directory")
    root = (directory / name).resolve() if name else directory
    if not root.is_relative_to(directory):
        raise ValueError("recorded visual bundle escapes its script directory")
    return root


def _workspace(path):
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError("the run output must be empty")
    return path


@contextmanager
def _repair_signal_destination(args, fallback, provenance, source_script_hash):
    source_id = (provenance or {}).get("source_task_id") or (provenance or {}).get("task_id")
    if not source_id or not source_script_hash:
        yield fallback, None
        return
    source_runtime = getattr(args, "skill_source_runtime", None)
    settings = (evaluation_settings(args.env_file, source_runtime) if source_runtime is not None
                else Settings(_env_file=args.env_file))
    if not settings.db_path.is_file():
        yield fallback, None
        return
    readonly = Database(settings.db_path, read_only=True)
    try:
        source = readonly.get_task(source_id)
        recording = readonly.get_agent_record(source_id, "script_generation", "mixed") if source else None
    finally:
        readonly.close()
    if (source is None or source.status != TaskStatus.SUCCEEDED or source.instruction != provenance.get("goal")
            or not source.generate_test_script or not recording or recording["payload"].get("script_hash") != source_script_hash):
        yield fallback, None
        return
    db = Database(settings.db_path)
    try:
        yield TaskStore(db, ArtifactStore(settings.artifacts_dir, create=False), source_id), source_id
    finally:
        db.close()


async def _learn(args, *, runtime, task_id, script=None, evidence=None, remaining=None, source_provenance=None,
                 source_script_hash=None):
    settings = evaluation_settings(args.env_file, runtime)
    db = Database(settings.db_path, read_only=script is None)
    try:
        task = db.get_task(task_id)
        if task is None:
            raise ValueError("the source task does not exist in the supplied runtime")
        store = TaskStore(db, ArtifactStore(settings.artifacts_dir, create=False), task_id)
        seconds = min(settings.learning_default_seconds, remaining) if remaining is not None else settings.learning_default_seconds
        if seconds <= 0:
            return {"status": "skipped", "reason": "job_deadline_exceeded"}
        budget = LearningBudget(max_calls=settings.learning_default_calls, max_actions=0,
                                max_seconds=seconds, max_job_seconds=seconds,
                                reserve_calls=min(8, settings.learning_default_calls // 3))
        if script is None:
            return await author_script(task, settings=settings, store=store, budget=budget)
        with _repair_signal_destination(args, store, source_provenance, source_script_hash) as (signal_store, source_id):
            result = await repair_script(task, settings=settings, store=store, base_script=script,
                                         run_evidence=evidence, budget=budget, signal_store=signal_store,
                                         source_task_id=source_id)
            if source_id and result["status"] == "candidate":
                result["candidate"]["provenance"].update(source_task_id=source_id, source_script_hash=source_script_hash)
            return result
    finally:
        db.close()


async def _replay(args):
    script_path, raw, script = _replay_artifact(args.script)
    envelope = raw.get("candidate", raw)
    provenance = envelope.get("provenance") or {}
    review = envelope.get("review") or {}
    source_provenance = provenance if (review.get("decision") == "accept"
                                        and review.get("script_hash") == script_fingerprint(script)) else None
    source_script_hash = script_fingerprint(script) if source_provenance and script.revision == 1 else None
    if provenance.get("goal", args.goal) != args.goal:
        raise ValueError("the supplied goal differs from the reviewed script goal")
    if script.platform != "android":
        raise ValueError("this live evaluation entry point requires an Android emulator")
    parameters = _load(args.parameters) if args.parameters else {}
    asset_root = _assets(script_path.parent, envelope, args.assets)
    ocr = LocalScriptOCR(args.ocr_executable, args.ocr_language) if args.ocr_executable else None
    script.bind_parameters(parameters)
    policy = AssistPolicy.model_validate_json(args.assist_policy.read_text(encoding="utf-8-sig")) if args.assist_policy else None
    auto_registry = getattr(args, "auto_repair_registry", None)
    initial_registry = getattr(args, "auto_enroll_registry", None)
    if args.repair and (not policy or not args.snapshot):
        raise ValueError("a two-round repair requires explicit takeover and a clean reference snapshot")
    if auto_registry is not None and (policy is not None or not args.snapshot):
        raise ValueError("automatic repair needs a clean failure snapshot and derives its own takeover policy")
    if args.verify and (policy is not None or not args.snapshot):
        raise ValueError("repeatability verification requires a clean snapshot and independently executed replays")
    if initial_registry is not None and (not args.verify or script.revision != 1 or not source_provenance
                                         or source_provenance.get("goal") != args.goal):
        raise ValueError("initial automatic enrollment requires --verify and an exact independently reviewed v1 draft")
    output = _workspace(args.output)
    expires = time.monotonic() + args.seconds
    original_fixture = None
    auto_block = None
    if auto_registry is not None:
        try:
            original_fixture, source_provenance = registered_original_context(script, args.goal, auto_registry)
            source_script_hash = (script_fingerprint(script) if script.revision == 1
                                  else source_provenance.get("source_script_hash"))
        except (OSError, ValueError, KeyError):
            auto_block = "registered_original_fixture_unavailable"
        if original_fixture and (original_fixture["avd"] != args.avd
                                 or original_fixture["parameters_hash"] != digest(parameters)):
            auto_block = "registered_original_fixture_incompatible"

    async def run(artifact, runtime, assist_policy=None, expected_build=None, *, contracted=False,
                  original_snapshot=None):
        left = expires - time.monotonic()
        if left <= 0:
            return {"status": "unknown", "reason": "job_deadline_exceeded"}
        snapshot = original_snapshot or args.snapshot
        if snapshot:
            try:
                await asyncio.wait_for(restore_snapshot(args.adb, args.serial, snapshot), left)
            except (EvaluationInfrastructureError, TimeoutError):
                if original_snapshot is None:
                    raise
                return {"status": "unknown", "reason": "registered_original_snapshot_unavailable"}
        settings = evaluation_settings(args.env_file, runtime)

        async def on_device(driver):
            build = await driver.read_app_build(artifact.app_id)
            if (not build or build.get("package") != artifact.app_id or not build.get("version_code")
                    or (expected_build is not None and build.get("version_code") != expected_build)):
                return {"status": "unknown", "reason": "app_build_unverified_or_changed"}

            def assist(owned, transaction, evidence):
                if contracted:
                    return contracted_recovery(driver=owned, transaction=transaction, evidence=evidence,
                                               asset_root=asset_root, ocr=ocr)
                return recovery_session(assist_policy, settings=settings, driver=owned,
                                        transaction=transaction, evidence=evidence, asset_root=asset_root, ocr=ocr)

            result = await replay_owned(artifact, goal=args.goal, settings=settings, driver=driver,
                                        serial=args.serial, parameters=parameters,
                                        max_seconds=expires - time.monotonic(),
                                        assist=assist if (assist_policy or contracted) else None,
                                        takeover_seconds=assist_policy.max_seconds if assist_policy else 30.0,
                                        asset_root=asset_root, ocr=ocr,
                                        fixture={"app_build": build["version_code"], "snapshot": snapshot,
                                                 "avd": args.avd, "parameters_hash": digest(parameters)})
            return {**result, "runtime": str(runtime), "app_build": build["version_code"],
                    "snapshot": snapshot}

        return await with_android_driver(settings, args.serial, on_device,
                                         full_resolution=next(visual_templates(artifact), None) is not None)

    with exclusive_emulator(args.serial, args.avd, args.adb) as lease:
        automatic = auto_registry is not None and auto_block is None
        first = await run(script, output / "first-runtime", policy, contracted=automatic)
        lease.quarantined = bool(first.get("device_reconciliation_required"))
        atomic_json(output / "first.json", first)
        report = {"first": first, "update": "no_assisted_success" if (args.repair or automatic) else "not_requested"}
        if auto_registry is not None:
            report["automatic_repair"] = True
        if auto_block:
            report.update(update="automatic_repair_deferred", reason=auto_block)
        candidate = None
        if args.verify:
            report["update"] = "validation_incomplete"
            if first["status"] == "succeeded" and first["receipt"]["final_asserted"]:
                candidate = script
        if ((args.repair or automatic) and first["status"] == "succeeded" and first["receipt"]["assisted"]
                and first["receipt"]["final_asserted"]):
            repair = await _learn(args, runtime=output / "first-runtime", task_id=first["task_id"],
                                  script=script, evidence=first, remaining=expires - time.monotonic(),
                                  source_provenance=source_provenance, source_script_hash=source_script_hash)
            atomic_json(output / "repair.json", repair)
            report["update"] = repair["status"]
            if repair["status"] != "candidate" and repair.get("reason"):
                report["reason"] = repair["reason"]
            if repair.get("skill_signal") is not None:
                report["skill_signal"] = repair["skill_signal"]
            if repair["status"] == "candidate":
                candidate = _script(repair)
        if candidate is not None:
            second = await run(candidate, output / "second-runtime", expected_build=first["app_build"])
            lease.quarantined = bool(second.get("device_reconciliation_required"))
            atomic_json(output / "second.json", second)
            report["second"] = second
            passed = (second["status"] == "succeeded" and not second["receipt"]["assisted"]
                      and second["receipt"]["final_asserted"] and first.get("fixture") == second.get("fixture")
                      and bool((second.get("fixture") or {}).get("initial_state_hash"))
                      and second["fixture"].get("goal_initially_met") is False)
            report["update"] = "verified_pending_review" if passed else "validation_incomplete"
            if initial_registry is not None and passed:
                if not is_low_impact_initial(candidate):
                    report["reason"] = "initial_script_requires_impact_review"
                else:
                    try:
                        path = register_script(candidate, envelope, report, initial_registry, asset_root=asset_root,
                                               automatic_initial=True)
                    except FileExistsError:
                        report.update(update="automatic_enrollment_deferred", reason="revision_already_registered")
                    else:
                        report.update(update="automatically_registered", registered_path=str(path))
            if automatic and passed:
                original = await run(candidate, output / "original-runtime",
                                     expected_build=original_fixture["app_build"],
                                     original_snapshot=original_fixture["snapshot"])
                lease.quarantined = bool(original.get("device_reconciliation_required"))
                atomic_json(output / "original.json", original)
                report["original"] = original
                if (original["status"] != "succeeded" or original.get("fixture") != original_fixture
                        or original["receipt"]["assisted"] or not original["receipt"]["final_asserted"]):
                    report.update(update="original_regression_incomplete",
                                  reason=original.get("reason", "registered_original_state_not_verified"))
                elif not is_low_impact_repair(script, candidate):
                    report["reason"] = "repair_requires_impact_review"
                else:
                    try:
                        path = register_script(candidate, repair["candidate"], report, auto_registry,
                                               asset_root=asset_root, automatic_base=script)
                    except FileExistsError:
                        report.update(update="automatic_repair_deferred", reason="revision_already_registered")
                    else:
                        report.update(update="automatically_registered", registered_path=str(path))
    atomic_json(output / "report.json", report)
    return report


async def _regress(args):
    definitions = {key: CaseDefinition.model_validate_json(json.dumps(value, ensure_ascii=False))
                   for key, value in _load(args.cases).items()}
    if "source" not in definitions or any(case.setup.platform != "android" for case in definitions.values()):
        raise ValueError("declare an Android source case")
    candidate_data = _load(args.candidate)
    candidate = (DeleteCandidate if candidate_data.get("operation") == "delete" else Candidate).model_validate(candidate_data)
    if isinstance(candidate, DeleteCandidate):
        return {"status": "unsupported_operation", "reason": "prepared_deletion_not_supported"}
    old_text = validate_candidate(candidate, SkillLibrary(args.baseline_skills), {definitions["source"].setup.app_id})
    ocr = LocalScriptOCR(args.ocr_executable, args.ocr_language) if args.ocr_executable else None
    fixture = AndroidSnapshotFixture(definitions.values(), adb=args.adb, serial=args.serial, avd=args.avd,
                                     snapshot=args.snapshot, env_file=args.env_file, rounds=args.rounds,
                                     seconds=args.seconds, asset_root=(args.assets or args.cases.parent).resolve(), ocr=ocr)
    with exclusive_emulator(args.serial, args.avd, args.adb) as lease:
        report = await validate_gui_skill(
            candidate=candidate, old_text=old_text, baseline_skills=args.baseline_skills, output=args.output,
            cases={kind: case.prepared_case() for kind, case in definitions.items()}, fixture=fixture,
            model=args.model, rounds=args.rounds, seconds=args.seconds, cleanup_seconds=args.fixture_seconds,
        )
        lease.quarantined = fixture.quarantined
        return report


def _device_args(parser, *, snapshot_required=False):
    parser.add_argument("--serial", required=True, help="dedicated emulator ADB serial")
    parser.add_argument("--avd", required=True, help="expected AVD name")
    parser.add_argument("--snapshot", required=snapshot_required, help="existing clean snapshot of this emulator")
    parser.add_argument("--adb", help="verify an expected ADB path against the driver configuration")
    parser.add_argument("--seconds", required=True, type=float)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ocr-executable", type=Path, help="local Tesseract for explicit text identity checks")
    parser.add_argument("--ocr-language", default="eng", help="Tesseract language identifiers such as eng+chi_sim")


async def _select_recording_regions(args, actions):
    from agent.skills.script_visual_learning import select_visual_recording
    from evaluation.gui_visual_record import _read_frame

    settings = evaluation_settings(args.env_file, args.output.parent)
    budget = LearningBudget(max_calls=settings.learning_default_calls, max_actions=0,
                            max_seconds=settings.learning_default_seconds, reserve_calls=0)
    for action in actions:
        if action.get("selection") is not None or action.get("after_crop") is not None:
            continue
        if action.get("before_crop") is not None:
            raise ValueError("manual before crop requires a manual after crop")
        before, image = _read_frame(action["before_png"])
        after, after_image = _read_frame(action["after_png"])
        click = action["click"]
        if (not isinstance(click, (tuple, list)) or len(click) != 2
                or any(type(value) is not int for value in click)
                or not (0 <= click[0] < image.width and 0 <= click[1] < image.height)):
            raise ValueError("recording click must be a pixel inside the before capture")
        if image.size != after_image.size:
            raise ValueError("before/after capture dimensions must agree")
        references = []
        for reference in action.get("reference_before", ()):
            pixels, reference_image = _read_frame(reference)
            if reference_image.size != image.size:
                raise ValueError("before reference frames must use the recording dimensions")
            references.append(pixels)
        action["selection"] = await select_visual_recording(
            settings=settings, budget=budget, app_id=args.app_id, goal=args.goal, size=image.size,
            click=tuple(click), before=before, after=after, references=tuple(references),
        )
        # Preserve the exact same bytes for geometric verification and publication.
        action.update(before_png=before, after_png=after, reference_before=tuple(references))


def _record(args):
    from evaluation.gui_visual_record import record_visual_script, record_visual_sequence

    if args.output.exists():
        raise FileExistsError(args.output)
    if args.manifest is not None:
        if any(value is not None for value in (args.after, args.click, args.before_crop, args.after_crop,
                                               args.before_roi, args.before_fallback_roi, args.after_roi, args.threshold,
                                               args.selection, args.reference_before)):
            raise ValueError("put step screenshots, coordinates and options in the recording manifest")
        manifest = _load(args.manifest)
        if not isinstance(manifest, list) or not 1 <= len(manifest) <= 128:
            raise ValueError("recording manifest requires 1 to 128 action objects")
        actions = []
        for item in manifest:
            if not isinstance(item, dict) or not {"before_png", "after_png", "click"}.issubset(item):
                raise ValueError("each recorded action requires before_png, after_png and click")
            action = dict(item)
            for field in ("before_png", "after_png"):
                action[field] = args.manifest.parent / action[field]
            if "reference_before" in action:
                if not isinstance(action["reference_before"], list):
                    raise ValueError("reference_before must be an array of image paths")
                action["reference_before"] = [args.manifest.parent / path for path in action["reference_before"]]
            actions.append(action)
    else:
        if args.after is None or args.click is None:
            raise ValueError("single-step recording requires --after and --click")
        options = {} if args.threshold is None else {"threshold": args.threshold}
        actions = [dict(before_png=args.before, after_png=args.after, click=args.click,
                        before_crop=args.before_crop, after_crop=args.after_crop,
                        before_roi=args.before_roi, before_fallback_roi=args.before_fallback_roi,
                        after_roi=args.after_roi, selection=_load(args.selection) if args.selection else None,
                        reference_before=args.reference_before or (), **options)]
    learned_steps = sum(action.get("selection") is None and action.get("after_crop") is None for action in actions)
    if learned_steps:
        asyncio.run(_select_recording_regions(args, actions))
    if args.manifest is not None:
        script = record_visual_sequence(actions=actions, destination=args.output,
                                        script_id=args.script_id, app_id=args.app_id)
    else:
        script = record_visual_script(destination=args.output, script_id=args.script_id, app_id=args.app_id,
                                      **actions[0])
    return {"status": "recorded", "script": str(args.output / "script.json"),
            "steps": len(script.steps), "script_hash": script_fingerprint(script),
            "learner_selected_steps": learned_steps,
            "additional_before_frames_verified": sum(len(action.get("reference_before", ())) for action in actions)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft", help="produce a reviewed draft from an existing task")
    draft.add_argument("--runtime", type=Path, required=True)
    draft.add_argument("--task-id", required=True)
    draft.add_argument("--output", type=Path, required=True)
    record = commands.add_parser("record", help="make a visual script and PNG assets from recorded screenshots")
    recording = record.add_mutually_exclusive_group(required=True)
    recording.add_argument("--before", type=Path)
    recording.add_argument("--manifest", type=Path)
    record.add_argument("--after", type=Path)
    record.add_argument("--click", nargs=2, type=int)
    record.add_argument("--goal", help="optional goal for choosing the visual outcome")
    record.add_argument("--selection", type=Path, help="validated selection JSON to reproduce without a model call")
    record.add_argument("--reference-before", type=Path, action="append",
                        help="additional screenshot of the demonstrated initial state; repeatable")
    record.add_argument("--before-crop", nargs=4, type=int)
    record.add_argument("--after-crop", nargs=4, type=int)
    record.add_argument("--before-roi", nargs=4, type=float)
    record.add_argument("--before-fallback-roi", nargs=4, type=float)
    record.add_argument("--after-roi", nargs=4, type=float)
    record.add_argument("--threshold", type=float)
    record.add_argument("--script-id", required=True)
    record.add_argument("--app-id", required=True)
    record.add_argument("--output", type=Path, required=True)
    replay = commands.add_parser("replay", help="run the script on an explicitly selected emulator")
    _device_args(replay)
    replay.add_argument("--script", type=Path, required=True,
                        help="pin a script file or use the latest revision in a registered script directory")
    replay.add_argument("--goal", required=True)
    replay.add_argument("--parameters", type=Path)
    replay.add_argument("--assets", type=Path, help="visual asset directory, defaults to the script directory")
    replay.add_argument("--assist-policy", type=Path)
    replay.add_argument("--skill-source-runtime", type=Path,
                        help="optional runtime containing the original reviewed recording task")
    replay.add_argument("--auto-enroll-registry", type=Path,
                        help="with --verify, enroll a low-impact reviewed initial script after two live passes")
    replay_mode = replay.add_mutually_exclusive_group()
    replay_mode.add_argument("--repair", action="store_true")
    replay_mode.add_argument("--verify", action="store_true", help="verify two independent clean starts")
    replay_mode.add_argument("--auto-repair-registry", type=Path,
                             help="repair a registered script and enroll proven low-impact updates")
    regress = commands.add_parser("regress", help="run an isolated ordinary-App Skill comparison")
    _device_args(regress, snapshot_required=True)
    regress.add_argument("--cases", type=Path, required=True)
    regress.add_argument("--candidate", type=Path, required=True)
    regress.add_argument("--baseline-skills", type=Path, required=True)
    regress.add_argument("--model", required=True)
    regress.add_argument("--rounds", type=int, required=True, help="maximum ordinary device actions per arm")
    regress.add_argument("--fixture-seconds", type=float, required=True)
    regress.add_argument("--assets", type=Path, help="visual asset directory, defaults to the case file directory")
    skill_loop = commands.add_parser("skill-loop", help="learn, prepare missing cases and compare a Skill in isolation")
    _device_args(skill_loop, snapshot_required=True)
    skill_loop.add_argument("--runtime", type=Path, required=True, help="runtime containing the completed source task")
    skill_loop.add_argument("--task-id", required=True)
    skill_loop.add_argument("--plan", type=Path, required=True)
    skill_loop.add_argument("--registry", type=Path, required=True)
    skill_loop.add_argument("--baseline-skills", type=Path, required=True)
    skill_loop.add_argument("--model", required=True, help="same ordinary task model for setup and both arms")
    skill_loop.add_argument("--rounds", type=int, required=True)
    skill_loop.add_argument("--fixture-seconds", type=float, required=True)
    approve = commands.add_parser("approve", help="register a reviewed and verified script revision")
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument("--report", type=Path, required=True)
    approve.add_argument("--registry", type=Path, required=True)
    approve.add_argument("--assets", type=Path, help="visual asset directory, defaults to the candidate bundle")
    args = parser.parse_args(argv)
    if args.command == "draft":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise FileExistsError(args.output)
        result = asyncio.run(_learn(args, runtime=args.runtime, task_id=args.task_id))
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)
    elif args.command == "record":
        try:
            result = _record(args)
        except GatewayError as exc:
            category = "auth" if "missing credentials" in str(exc).lower() else exc.category
            result = {"status": "failed", "reason": "visual_learner_request_failed", "category": category}
        except (LearningStopped, TimeoutError):
            result = {"status": "failed", "reason": "visual_learner_budget_ended"}
    elif args.command == "approve":
        raw = _load(args.candidate)
        candidate = raw.get("candidate", raw)
        assets = _assets(args.candidate.parent, candidate, args.assets)
        path = register_script(_script(raw), candidate, _load(args.report), args.registry, asset_root=assets)
        result = {"status": "registered", "path": str(path)}
    else:
        if not 0 < args.seconds < float("inf"):
            parser.error("--seconds must be a positive finite duration")
        executable = Path(adb_bin()).resolve()
        if args.adb and Path(shutil.which(args.adb) or args.adb).resolve() != executable:
            parser.error("--adb must match the native driver's ADB executable; select it using PATH")
        args.adb = str(executable)
        if args.command == "skill-loop":
            from evaluation.gui_skill_loop import FixtureOptions, SkillLoopPlan, run_skill_loop

            if args.rounds <= 0 or not 0 < args.fixture_seconds < float("inf"):
                parser.error("provide positive round and fixture limits")
            plan = SkillLoopPlan.model_validate_json(args.plan.read_bytes())
            ocr = LocalScriptOCR(args.ocr_executable, args.ocr_language) if args.ocr_executable else None
            options = FixtureOptions(args.adb, args.serial, args.avd, args.snapshot, args.env_file,
                                     args.model, args.rounds, args.seconds, args.fixture_seconds, ocr)
            result = asyncio.run(run_skill_loop(runtime=args.runtime, task_id=args.task_id, plan=plan,
                plan_dir=args.plan.parent, baseline_skills=args.baseline_skills, registry_root=args.registry,
                output=args.output, options=options))
        else:
            result = asyncio.run(_replay(args) if args.command == "replay" else _regress(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "replay":
        invalid = {"validation_incomplete", "original_regression_incomplete"}
        return 0 if result["first"]["status"] == "succeeded" and result["update"] not in invalid else 1
    if args.command == "regress":
        return 0 if result.get("prepared_gate", {}).get("status") == "passed" else 1
    if args.command == "skill-loop":
        return 0 if result["status"] in {"awaiting_publication_review", "regression_passed"} else 1
    if args.command == "record":
        return 0 if result.get("status") == "recorded" else 1
    if args.command == "approve":
        return 0
    return 0 if result["status"] == "candidate" else 1


if __name__ == "__main__":
    raise SystemExit(main())
