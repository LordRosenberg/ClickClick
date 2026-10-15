"""Task-derived AndroidWorld/MobileWorld learning experiment, using ordinary ClickClick trials.

Reference skills are held by evaluation only. No candidate enters canonical skills.
Run --help for the smoke or full source/variant/near-miss/related-normal matrix.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from agent.revisable.store import TaskStore
from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget, LearningStopped, build_packet, digest, run_task_learning
from agent.skills.library import SkillLibrary
from agent.skills.snapshot import LibrarySnapshot, library_manifest
from driver.factory import get_driver
from evaluation.mobileworld.long_run_health import atomic_json
from evaluation.mobileworld.run_single import _execute_episode
from evaluation.mobileworld.budgets import episode_budgets
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database


class EvaluationInfrastructureError(RuntimeError):
    """An unscored harness attempt; never a task failure or passing guard."""


def official_success(result):
    score = result.get("score")
    # Official AndroidWorld ratios include partial completion, e.g. playlist/export.
    if (result.get("evaluation_environment") == "androidworld"
            and type(score) in (int, float) and 0 <= score <= 1):
        return score == 1
    if type(score) is bool:
        return score
    if isinstance(score, dict):
        for key in ("success", "is_successful", "score"):
            if type(score.get(key)) is bool:
                return score[key]
            if type(score.get(key)) in (int, float) and score[key] in (0, 1):
                return score[key] == 1
    if type(score) in (int, float) and score in (0, 1):
        return score == 1
    return None


def prioritized_trial_events(events):
    """Order literal failure/transition references without guessing their cause."""
    priority = []
    seen = set()

    def add(index):
        if 0 <= index < len(events) and index not in seen:
            seen.add(index)
            priority.append(events[index])

    # Terminal/replan checkpoints and known bad receipts deserve the short input.
    for i in range(len(events) - 1, -1, -1):
        payload = events[i]["payload"]
        if payload.get("decision") in {"replan", "finish", "inconclusive"}:
            add(i)
    for i in range(len(events) - 1, -1, -1):
        result = events[i]["payload"].get("action_result") or {}
        receipt = result.get("receipt") or {}
        if (result.get("success") is False or receipt.get("dispatch_succeeded") is False
                or receipt.get("observation_accepted") is False
                or receipt.get("effect_outcome") in {"failed", "timeout"}):
            add(i)
            add(i - 1)
            add(i + 1)
    # First occurrences and their following reports can expose a route change.
    types = set()
    for i, row in enumerate(events):
        kind = (row["payload"].get("submitted_action") or {}).get("type")
        if kind and kind not in types:
            types.add(kind)
            add(i)
            add(i + 1)
    for i in range(max(0, len(events) - 3), len(events)):
        add(i)
    for i in range(len(events)):
        add(i)
    return priority


def trial_execution_evidence(result, *, character_budget=3500, candidate=None):
    """Literal, task-scoped cost and behavior evidence; no fixture answers."""
    directory = Path(result.get("runtime_directory") or "")
    if not result.get("runtime_directory"):
        return {"available": False, "reason": "runtime history unavailable"}
    settings = Settings(_env_file=None, data_dir=directory)
    if not settings.db_path.is_file():
        return {"available": False, "reason": "runtime DB missing"}
    db = Database(settings.db_path)
    try:
        task = db.get_task(result["clickclick_task_id"])
        store = TaskStore(db, ArtifactStore(settings.artifacts_dir), task.id)
        events = sorted(store.records("event"), key=lambda r: r["payload"].get("step", -1))
        types, inputs = {}, []
        for row in events:
            action = row["payload"].get("submitted_action") or {}
            if action.get("type"):
                types[action["type"]] = types.get(action["type"], 0) + 1
            if action.get("text") is not None:
                inputs.append({"source": "event:" + row["key"] + "@" + str(row["version"]),
                    "type": action.get("type"), "text": str(action["text"])[:240]})
        limits = result.get("execution_limits")
        request_file = directory.parent / "request.json"
        if limits is None and request_file.is_file():
            request = json.loads(request_file.read_text(encoding="utf-8"))
            limits = {key: request.get(key) for key in ("max_steps", "max_model_calls", "max_seconds")}
        evidence = {"available": True, "task_id": task.id,
            "runtime_status": task.status.value,
            "failure_reason": (task.failure_reason or "")[:400],
            "execution_limits": limits,
            "cost": {"actions": result.get("logical_action_steps"),
                "model_requests": result.get("role_invocations"), "seconds": result.get("elapsed_s")},
            "action_types": types, "text_input_count": len(inputs), "text_inputs": [],
            "last_report": str(events[-1]["payload"].get("executor_report", ""))[:120] if events else "",
            "events": [], "omitted_events": len(events), "omitted_text_inputs": len(inputs),
            "policy": "Literal reports are claims, receipts are separate, and omissions are unknown. Failure/transition references have priority; read full native events for cause. Official oracle is separate."}
        if candidate is not None:
            from agent.skills.activation import candidate_activation
            from agent.skills.candidate_group import CandidateGroup, candidate_files
            if isinstance(candidate, CandidateGroup):
                evidence["patch_activation"] = {target: candidate_activation(store, text)
                    for target, text in candidate_files(candidate).items()}
            else:
                evidence["candidate_activation"] = candidate_activation(store, candidate.new_text)
        for item in inputs:
            if len(json.dumps({**evidence, "text_inputs": [*evidence["text_inputs"], item]}, ensure_ascii=False)) <= character_budget - 80:
                evidence["text_inputs"].append(item)
                evidence["omitted_text_inputs"] -= 1
        for row in prioritized_trial_events(events):
            payload = row["payload"]
            result = payload.get("action_result") or {}
            receipt = result.get("receipt") or {}
            item = {"source": "event:" + row["key"] + "@" + str(row["version"]),
                "step": payload.get("step"), "decision": payload.get("decision"),
                "action_type": (payload.get("submitted_action") or {}).get("type"),
                "report": str(payload.get("executor_report", ""))[:160]}
            # Dispatch or acceptance failures do not establish a UI/root cause.
            adverse = (result.get("success") is False or receipt.get("dispatch_succeeded") is False
                or receipt.get("observation_accepted") is False
                or receipt.get("effect_outcome") in {"failed", "timeout"})
            if adverse:
                flags = {key: receipt[key] for key in ("dispatch_succeeded", "observation_accepted",
                    "effect_outcome", "native_action_performed") if key in receipt}
                if result.get("success") is False:
                    flags["success"] = False
                item["receipt"] = flags
            if len(json.dumps({**evidence, "events": [*evidence["events"], item]}, ensure_ascii=False)) <= character_budget - 80:
                evidence["events"].append(item)
                evidence["omitted_events"] -= 1
        evidence["events"].sort(key=lambda item: item["step"] if type(item["step"]) is int else -1)
        return evidence
    finally:
        db.close()


class Experiment:
    supports_candidate_groups = True

    def __init__(self, args):
        continuous = getattr(args, "continuous_research", False)
        if type(continuous) is not bool:
            raise ValueError("continuous_research must be boolean")
        if continuous and (args.environment != "androidworld"
                           or not getattr(args, "disposable_research", False)
                           or getattr(args, "no_research_reset", False)):
            raise ValueError("continuous research requires the owned AndroidWorld disposable fixture")
        self.args = args
        self.root = Path(args.output).resolve()
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError("experiment output must be empty; never overwrite evidence")
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(_env_file=args.env_file)
        self.model = args.model or self.settings.default_model
        self.empty = self.root / "no-skills"
        self.empty.mkdir()
        supplied = getattr(args, "baseline_skills", None)
        self.baseline_snapshot = LibrarySnapshot.freeze(supplied or self.empty, self.root / "baseline-library")
        self.baseline = self.baseline_snapshot.root
        self.reference = self.root / "reference"
        shutil.copytree(args.reference_skills, self.reference,
            ignore=shutil.ignore_patterns("_pending", "_drafts", "_candidates"))
        self.rows = []
        self.case_baselines = {}
        self.validation_runs = {}
        self.config = {"model": self.model, "rounds": args.rounds, "seconds": args.seconds,
            "environment": args.environment, "target": args.target, "expected_serial": args.expected_serial,
            "seed_offset": getattr(args, "seed_offset", 0),
            "runtime": digest({str(p): digest(p.read_text(encoding="utf-8"))
                for folder in ("agent", "driver", "perception", "shared")
                for p in Path(folder).rglob("*.py")}),
            "reference": digest({str(p.relative_to(self.reference)): digest(p.read_text(encoding="utf-8"))
                for p in self.reference.rglob("SKILL.md")})}
        self.config.update(validation_mode="incremental" if supplied else "discovery",
            baseline_manifest=self.baseline_snapshot.manifest,
            continuous_research=continuous)
        atomic_json(self.root / "manifest.json", self.config)

    def episode(self, name, task, skills):
        directory = self.root / name
        args = SimpleNamespace(task=task, backend=self.args.backend,
            environment_device="emulator-5554", target=self.args.target,
            expected_device_serial=self.args.expected_serial, container="mobile_world_env_0",
            model=self.model, max_steps=self.args.rounds, max_model_calls=self.args.rounds * 6 if getattr(self, "validation_budget", None) else None,
            max_seconds=self._validation_seconds(), collector_apk=self.args.collector_apk,
            ime_apk=self.args.ime_apk, data_dir=str(directory), output="",
            keep_task=False, no_recording=True, runtime_settings=self.settings)
        prior = os.environ.get("CLICKCLICK_SKILLS_DIR")
        os.environ["CLICKCLICK_SKILLS_DIR"] = str(skills)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            print(f"Starting {name}: {task}", flush=True)
            with (directory / "console.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
                _execute_episode(args, episode_budgets(args.max_steps, args.max_model_calls, args.max_seconds))
            result = json.loads((directory / "mobileworld-result.json").read_text(encoding="utf-8"))
            self.rows.append({"arm": name, "task": task, "success": official_success(result),
                "actions": result.get("logical_action_steps"), "calls": result.get("role_invocations"),
                "seconds": result.get("elapsed_s"), "environment_status": result.get("environment_status"),
                "result": str(directory / "mobileworld-result.json"), "skills": digest({
                    str(p.relative_to(skills)): digest(p.read_text(encoding="utf-8")) for p in skills.rglob("SKILL.md")})})
            atomic_json(directory / "episode-result.json", result)
            atomic_json(self.root / "arms.json", self.rows)
            print(f"Finished {name}: oracle={official_success(result)}, actions={result.get('logical_action_steps')}, calls={result.get('role_invocations')}", flush=True)
            return result
        finally:
            if prior is None:
                os.environ.pop("CLICKCLICK_SKILLS_DIR", None)
            else:
                os.environ["CLICKCLICK_SKILLS_DIR"] = prior

    def _validation_seconds(self):
        budget = getattr(self, "validation_budget", None)
        if budget and budget.remaining_seconds() < self.args.seconds:
            raise LearningStopped("validation_full_episode_time_unavailable")
        return self.args.seconds

    async def _episode_async(self, name, case, skills):
        if getattr(self, "validation_quarantined", False):
            raise RuntimeError("validation device quarantined after unresolved worker shutdown")
        budget = getattr(self, "validation_budget", None)
        if budget:
            self._validation_seconds()  # Do not silently change one side of a matched pair.
        child = budget.reserve_validation(name, calls=self.args.rounds * 6, actions=self.args.rounds) if budget else None
        result = None
        try:
            result = await self._run_episode_async(name, case, skills)
            return result
        finally:
            if child is not None:
                # The worker is joined (or quarantined) before releasing its allowance.
                if result is None and not getattr(self, "validation_quarantined", False):
                    directory = self.root / name
                    paths = [directory / "episode-result.json", directory / "mobileworld-result.json",
                             directory / "runtime-result.json",
                             directory / "episodes" / case / "plan_executor" / "worker-result.json"]
                    for path in paths:
                        if path.is_file():
                            try:
                                raw = json.loads(path.read_text(encoding="utf-8"))
                                result = {**raw, "logical_action_steps": raw.get("logical_action_steps", raw.get("episode_steps"))}
                                break
                            except (ValueError, OSError):
                                pass
                try:
                    budget.settle_validation(child, result)
                finally:
                    atomic_json(self.root / "validation-cost.json", budget.snapshot())

    async def _run_episode_async(self, name, case, skills):
        if getattr(self,"validation_quarantined",False):
            raise RuntimeError("validation device quarantined after unresolved worker shutdown")
        worker = asyncio.create_task(asyncio.to_thread(self.episode,name,case,skills))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The ordinary worker already observes STOP and cancels its model/
            # action task before official teardown. Do not abandon a device thread.
            directory=self.root/name
            directory.mkdir(parents=True,exist_ok=True)
            (directory/"STOP").touch()
            receipt={"arm":name,"stop_requested":True,"worker_joined":False}
            try:
                await asyncio.wait_for(asyncio.shield(worker),30)
                receipt["worker_joined"]=True
            except TimeoutError:
                self.validation_quarantined=True
            except Exception:
                # Official operator-stop excludes a scored result; termination
                # can still be independently known from the finished worker.
                receipt["worker_joined"]=worker.done()
            atomic_json(directory/"validation-stop.json",receipt)
            raise

    def validation_checks(self):
        return [kind for kind in ("source", "variant", "near_miss", "related_normal")
                if getattr(self.args, kind, None) and not (kind == "variant"
                    and self.args.variant == self.args.source
                    and not getattr(self.args, "variant_seed_offset", None))]

    def validation_check_conditions(self):
        return {kind: {"case": getattr(self.args, kind), "seed_offset":
                      getattr(self.args, "variant_seed_offset", 0) if kind == "variant"
                      else getattr(self.args, "seed_offset", 0)}
                for kind in self.validation_checks()}

    async def validate(self, candidate, old, *, budget=None, checks=None):
        if getattr(self, "validation_budget", None) is not None:
            raise LearningStopped("validation_already_active")
        self.validation_budget = budget
        try:
            if checks is None:
                return await self._validate(candidate, old)
            return await self._validate(candidate, old, checks=checks)
        finally:
            self.validation_budget = None

    async def _validate(self, candidate, old, *, checks=None):
        from agent.skills.candidate_group import candidate_files, candidate_hash
        patches = candidate_files(candidate)
        candidate_key = candidate_hash(candidate)
        if checks is not None:
            from agent.skills.verification import verification_plan
            checks = verification_plan({"checks": list(checks), "reason": "Reviewer-selected checks"},
                                       available=self.validation_checks())["checks"]
        backend = getattr(self, "learning_backend", None)
        if backend:
            await backend.release_device()
        await asyncio.to_thread(self.release_source)
        overlay = self.root / ("overlay-" + candidate_key[:12])
        if overlay.exists():
            expected = dict(self.baseline_snapshot.manifest["files"])
            expected.update({path: hashlib.sha256(text.encode()).hexdigest() for path, text in patches.items()})
            if library_manifest(overlay)["files"] != expected:
                raise ValueError("existing candidate overlay changed")
            overlay_snapshot = LibrarySnapshot(overlay, library_manifest(overlay))
        else:
            overlay_snapshot = self.baseline_snapshot.overlay_many(patches, overlay)
        provenance = {"baseline_library_hash":self.baseline_snapshot.manifest["hash"],
                      "candidate_library_hash":overlay_snapshot.manifest["hash"]}
        trials = []
        run_number = self.validation_runs.get(candidate_key, 0)
        self.validation_runs[candidate_key] = run_number + 1
        suffix = "" if not run_number else "-repeat-" + str(run_number)
        cases = [("source", self.args.source), ("variant", self.args.variant),
                 ("near_miss", self.args.near_miss), ("related_normal", self.args.related_normal)]
        if checks is not None:
            cases = [(kind, task) for kind, task in cases if kind in checks]
        for kind, task in cases:
            if not task:
                continue
            separate_variant = kind == "variant" and getattr(self.args,"variant_seed_offset",None) is not None
            try:
                baseline = None if run_number or separate_variant else self.case_baselines.get(task)
                if baseline is None:
                    baseline = await self._episode_async("baseline-" + kind + suffix, task, self.baseline)
                    # A fresh source baseline creates a learning lease by name. Close
                    # it before another official initializer uses the same device.
                    await asyncio.to_thread(self.release_source)
                    if not separate_variant:
                        self.case_baselines[task] = baseline
                arm = candidate_key[:8] + "-" + kind + suffix
                learned = await self._episode_async(arm, task, overlay)
            except LearningStopped as exc:
                if str(exc) not in {"validation_full_episode_time_unavailable", "validation_model_budget_reserved",
                                    "validation_action_budget_reserved", "model_budget", "time_budget"}:
                    raise
                deferred = {"candidate_hash":candidate_key,"base_hash":digest(old),"trials":trials,**provenance,
                    "validation_stage":"budget_deferred","deferred_kinds":[k for k,t in cases[cases.index((kind,task)):] if t],
                    "reason":str(exc),"policy":"No new child allocation. Prior evidence/costs remain; unrun checks are unknown, never passes."}
                atomic_json(self.root / ("validation-"+candidate_key[:12]+suffix+".json"),deferred)
                return deferred
            except EvaluationInfrastructureError as exc:
                incomplete = {"candidate_hash":candidate_key,"base_hash":digest(old),"trials":trials,**provenance,
                    "validation_stage":"infrastructure_incomplete", "deferred_kinds":[k for k,t in cases[cases.index((kind,task)):] if t],
                    "error":{"kind":kind,"case_id":task,"reason":str(exc)[:1200]},
                    "policy":"The harness attempt is unscored. Prior pairs are retained; missing guards are not passes. Reviewer may inspect partial evidence, but incomplete validation cannot authorize publication."}
                atomic_json(self.root / ("validation-"+candidate_key[:12]+suffix+".json"),incomplete)
                return incomplete
            history_namespaces = {}
            history_goal_matches = {}
            candidate_store = None
            if backend:
                for side, recorded in (("baseline", baseline), ("candidate", learned)):
                    if recorded.get("runtime_directory"):
                        history_settings = Settings(_env_file=None, data_dir=Path(recorded["runtime_directory"]))
                        history_db = Database(history_settings.db_path)
                        history_task = history_db.get_task(recorded["clickclick_task_id"])
                        history_goal_matches[side] = history_task.instruction == backend.task.instruction
                        namespace = "validation_" + candidate_key[:8] + "_" + kind + "_" + side + suffix
                        history_store = TaskStore(history_db, ArtifactStore(history_settings.artifacts_dir), history_task.id)
                        backend.add_history(namespace, history_store, history_task.state)
                        if side == "candidate":
                            candidate_store = history_store
                        backend.owned_history_dbs.append(history_db)
                        history_namespaces[side + "_execution_namespace"] = namespace
            baseline_oracle, candidate_oracle = official_success(baseline), official_success(learned)
            baseline_success, candidate_success = baseline_oracle, candidate_oracle
            if self.args.environment == "androidworld":
                baseline_success = baseline["budgeted_success"] if baseline_success is not None else None
                candidate_success = learned["budgeted_success"] if candidate_success is not None else None
            trials.append({"case_id": task, "kind": kind, "goal": learned.get("goal"), **history_namespaces,
                "matched_environment": (baseline.get("environment_status") == learned.get("environment_status") == "ready"
                    and bool(baseline.get("initial_state_hash"))
                    and baseline.get("initial_state_hash") == learned.get("initial_state_hash")
                    and baseline.get("configuration_hash") == learned.get("configuration_hash")),
                "independent_oracle": baseline_success is not None and candidate_success is not None,
                "config_hash": digest({**self.config, "case": task, "goal": learned.get("goal")}),
                "baseline_success": baseline_success, "candidate_success": candidate_success,
                "baseline_oracle_success": baseline_oracle, "candidate_oracle_success": candidate_oracle,
                "efficiency_gain": (baseline.get("logical_action_steps", 0) - learned.get("logical_action_steps", 0))
                    if baseline_success and candidate_success else 0,
                "request_gain": (baseline["role_invocations"]-learned["role_invocations"])
                    if baseline_success and candidate_success and all(type(r.get("role_invocations")) is int for r in (baseline,learned)) else None,
                "baseline_execution": trial_execution_evidence(baseline),
                "candidate_execution": trial_execution_evidence(learned, candidate=candidate),
                "candidate_result": str(self.root / arm / "episode-result.json"),
                "overlay_hash": candidate_key,
                "baseline_library_hash":baseline.get("library_hash"),
                "candidate_library_hash":learned.get("library_hash")})
            if candidate_store is not None:
                # Only the newly created candidate child receives this host receipt;
                # a reused original source baseline remains read-only.
                receipt = {key: value for key, value in trials[-1].items()
                           if key not in {"baseline_execution", "candidate_execution", "candidate_result"}}
                for side, episode in (("baseline", baseline), ("candidate", learned)):
                    receipt[side + "_cost"] = {
                        "actions": episode.get("logical_action_steps"),
                        "model_requests": episode.get("role_invocations"),
                        "seconds": episode.get("elapsed_s")}
                activation = trials[-1]["candidate_execution"].get("candidate_activation")
                if isinstance(activation, dict):
                    receipt["candidate_activation"] = {key: value for key, value in activation.items()
                                                       if key != "examples"}
                receipt["type"] = "host_paired_ordinary_evaluation"
                receipt["policy"] = "Actual independent outcomes, costs and bindings; null costs are unknown. Exact prompt activation is not proof of app cause or mechanism adoption."
                row = candidate_store.put("measurement", "paired_ordinary_evaluation", receipt)
                trials[-1]["pair_evidence_ref"] = history_namespaces["candidate_execution_namespace"] + "/measurement:paired_ordinary_evaluation@" + str(row["version"])
            atomic_json(self.root / ("validation-"+candidate_key[:12]+suffix+".json"),
                {"candidate_hash":candidate_key,"base_hash":digest(old),"trials":trials,**provenance,
                 "validation_stage":"in_progress","policy":"Checkpoint only; incomplete guards never qualify for publication."})
            register = getattr(backend, "add_experience", None)
            if kind == "source" and register and trials[-1]["matched_environment"]:
                for side, success, origin in (("baseline", baseline_success, "no_skill"),
                                              ("candidate", candidate_success, "candidate_assisted")):
                    namespace = history_namespaces.get(side + "_execution_namespace")
                    if (success is True and namespace and history_goal_matches.get(side) is True
                            and len(backend.experiences) < 2):
                        register(namespace, origin=origin, oracle_success=True,
                            matched_environment=True, config_hash=trials[-1]["config_hash"])
            if (kind == "source" and (checks is not None or getattr(self.args, "source_first_validation", False))
                    and not trials[-1]["matched_environment"]):
                return {"candidate_hash":candidate_key, "base_hash":digest(old), "trials":trials, **provenance,
                    "validation_stage":"unmatched_source", "deferred_kinds":[k for k,t in cases[1:] if t],
                    "policy":"Source initial state/configuration did not match. Outcomes remain individual observations, not paired utility; guards are deferred, not passed. Restore deterministic conditions before fresh validation."}
            if (kind == "source" and (checks is not None or getattr(self.args, "source_first_validation", False))
                    and trials[-1]["matched_environment"] and trials[-1]["independent_oracle"]
                    and candidate_success is False):
                return {"candidate_hash":candidate_key, "base_hash":digest(old), "trials":trials, **provenance,
                    "validation_stage":"source_rejected", "deferred_kinds":[k for k,t in cases[1:] if t],
                    "policy":"Matched source candidate failed; guards are deferred, not passed. Revise the task decision before completing a fresh exact-body matrix. Incomplete coverage cannot qualify for promotion."}
            if (kind == "source" and (checks is not None or getattr(self.args,"source_first_validation",False))
                    and trials[-1]["matched_environment"] and trials[-1]["independent_oracle"]
                    and baseline_success is True and candidate_success is True
                    and trials[-1]["efficiency_gain"] <= 0
                    and not (type(trials[-1].get("request_gain")) is int and trials[-1]["request_gain"] > 0)):
                incomplete={"candidate_hash":candidate_key,"base_hash":digest(old),"trials":trials,**provenance,
                    "validation_stage":"source_without_gain","deferred_kinds":[k for k,t in cases[1:] if t],
                    "policy":"Matched source shows no correctness/action/request gain. Inspect activation, native adoption and full costs before further guard spending; this does not prove the mechanism false. Deferred guards never qualify for publication."}
                atomic_json(self.root / ("validation-"+candidate_key[:12]+suffix+".json"),incomplete)
                return incomplete
        complete = {"candidate_hash":candidate_key,"base_hash":digest(old),"trials":trials,**provenance,"validation_stage":"complete"}
        atomic_json(self.root / ("validation-"+candidate_key[:12]+suffix+".json"),complete)
        return complete

    async def learn(self, baseline):
        directory = Path(baseline.get("runtime_directory") or self.root / "baseline-source")
        settings = self.settings.model_copy(update={"data_dir": directory,
            "skill_learner_model": self.model, "skill_reviewer_model": self.model})
        db = Database(settings.db_path)
        artifacts = ArtifactStore(settings.artifacts_dir)
        driver = get_driver(settings, serial=self.args.target)
        task = db.get_task(baseline["clickclick_task_id"])
        backend = ExplorationBackend(task, db=db, artifacts=artifacts, driver=driver,
            settings=settings, library=SkillLibrary(self.baseline),
            retain_probe_state=getattr(self.args, "continuous_research", False))
        self.learning_backend = backend
        if getattr(self.args, "disposable_research", False):
            if not isinstance(self, AndroidWorldExperiment):
                raise ValueError("disposable research only supports the owned AndroidWorld evaluator")
            backend.fixture_reset_adapter = self.disposable_adapter(baseline, cancelled=backend.cancelled)
        decisions, experiments = [], []
        diagnose, explore = backend.diagnose, backend.explore
        async def audited_diagnose(*args, **kwargs):
            result = await diagnose(*args, **kwargs)
            decisions.append(result)
            atomic_json(self.root / "diagnoses.json", decisions)
            return result
        async def audited_explore(*args, **kwargs):
            transition = await self.prepare_research_environment(backend, context=kwargs.get("context"))
            if transition:
                kwargs["context"] = {**(kwargs.get("context") or {}), "environment_transition": transition}
            result = await explore(*args, **kwargs)
            experiments.append(result)
            atomic_json(self.root / "explorations.json", experiments)
            return result
        backend.diagnose, backend.explore = audited_diagnose, audited_explore
        budget = LearningBudget(max_calls=self.args.learn_calls, max_actions=self.args.learn_actions,
            max_seconds=self.args.learn_seconds)
        oracle_row = backend.source_store.put("measurement", "official_task_evaluation", {
            "oracle": self.args.environment + " official task oracle", "success": official_success(baseline),
            "result": baseline.get("score"), "source_artifact": "episode-result.json"})
        outcome = {"success": official_success(baseline), "result": baseline.get("score"),
                   "source": "measurement:official_task_evaluation@" + str(oracle_row["version"])}
        try:
            # Complex cases use the production mechanical trigger by default.
            forced = bool(getattr(self.args, "force_learning", False))
            trigger = build_packet(task, backend.source_store, manual=forced)
            trigger["external_evaluation"] = outcome
            if outcome["success"] is False:
                trigger["signals"].append("independent_task_failure")
            atomic_json(self.root / "trigger.json", trigger)
            result = await run_task_learning(task, settings=settings, library=backend.library,
                store=backend.source_store, backend=backend, manual=forced, budget=budget,
                validator=self.validate, outcome_evidence=outcome,
                task_success_goal=(outcome["success"] is False
                    or (outcome["success"] is True
                        and baseline.get("budgeted_success") is False)))
            if getattr(backend,"environment",None):
                result["environment"] = backend.environment.close()
            atomic_json(self.root / "learning.json", result)
            return result
        finally:
            # Preserve measured costs even if ordinary validation or cleanup raises.
            atomic_json(self.root / "phase-cost.json", budget.snapshot())
            try:
                await backend.release_device()
            finally:
                try:
                    backend.close()
                finally:
                    try:
                        await driver.close_observation_provider()
                    finally:
                        db.close()

    async def prepare_research_environment(self, backend, *, context=None):
        if getattr(self.args, "no_research_reset", False):
            process = getattr(self, "_source_process", None)
            if process is None or process.poll() is not None:
                raise RuntimeError("no-reset research needs a live terminal source lease")
            return {"fixture_reset":False,
                    "policy":"No research initializer; use actual terminal state. Preparation may fail within this job."}
        plan = ((context or {}).get("experiment") or {}).get("environment_plan")
        environment = getattr(backend, "environment", None)
        previous = environment.plan.model_dump() if environment and environment.plan else None
        if getattr(backend, "fixture_reset_adapter", None) and plan and plan != previous:
            # ExplorationBackend resets and checks the official fixture when it
            # installs a new plan. A temporary lease here would be immediately
            # torn down by that reset, running the initializer twice.
            return {"fixture_reset":False,
                    "policy":"Preparation deferred to the trusted fixture reset; no initialized state or live position is established yet."}
        return await asyncio.to_thread(self.ensure_source_fixture)

    def ensure_source_fixture(self):
        return None

    def release_source(self):
        pass

    def run(self):
        started = time.monotonic()
        try:
            baseline = self.episode("baseline-source", self.args.source, self.baseline)
            self.case_baselines[self.args.source] = baseline
            if (baseline.get("environment_status") != "ready" or not baseline.get("clickclick_task_id")
                    or str(baseline.get("failure_reason") or "").startswith("device_environment_reconciliation_failed")):
                raise RuntimeError("invalid source environment; learner must not train on fixture failure")
            result = asyncio.run(self.learn(baseline))
        finally:
            self.release_source()
        # Only evaluation receives authored reference, and only after the learning decision.
        authored = self.episode("authored-source", self.args.source, self.reference)
        report = {"learning": result, "arms": self.rows,
            "acquisition_cost": result.get("cost"), "total_seconds": time.monotonic()-started,
            "reference_success": official_success(authored),
            "reference_runtime_success": authored.get("runtime_success", authored.get("clickclick_status") == "succeeded"),
            "missing_coverage": [k for k in ("variant", "near_miss", "related_normal") if not getattr(self.args,k)],
            "publication": "manual_pending_only", "limits": "Small-sample smoke cannot establish general utility or zero regressions."}
        atomic_json(self.root / "report.json", report)
        return report



class AndroidWorldExperiment(Experiment):
    """Reuse official initialize/is_successful/teardown and persistent worker."""
    def restore_bound_absence(self, baseline):
        if not getattr(self.args, "disposable_research", False):
            return None
        if self.args.target != "emulator-5554" or self.args.expected_serial != "AndroidWorldAvd":
            raise ValueError("Bound absence restoration requires the dedicated disposable AndroidWorld lab")
        if getattr(self, "_source_process", None) is not None:
            raise ValueError("Release the source fixture lease before file restoration")
        if not baseline.get("initial_state_hash"):
            return None
        state_file = baseline.get("initial_state_file")
        if state_file is None:
            legacy_file = Path(baseline["runtime_directory"]).parent / "initial-rows.json"
            state_file = legacy_file if legacy_file.is_file() else None
        if state_file is None:
            return None
        initial_state = json.loads(Path(state_file).read_text(encoding="utf-8"))
        if digest(initial_state) != baseline["initial_state_hash"]:
            raise ValueError("Source initializer state file does not match its bound hash")
        from evaluation.skill_fixture_restoration import restore_recorded_absence
        receipt = restore_recorded_absence(self.args.adb, initial_state)
        atomic_json(self.root / ("fixture-file-restoration-" + uuid4().hex[:12] + ".json"),
            {"initial_state_hash":baseline["initial_state_hash"], **receipt})
        return receipt

    def release_source(self):
        process = getattr(self, "_source_process", None)
        if process is not None:
            self._source_done.touch()
            try:
                code = process.wait(timeout=60)
                if code:
                    raise RuntimeError("AndroidWorld source cleanup failed; inspect baseline console.log")
            finally:
                self._source_log.close()
                self._source_process = None

    def disposable_adapter(self, baseline, *, cancelled=lambda: False):
        from agent.skills.environment import TrustedFixtureReset
        if not baseline.get("initial_state_hash"):
            raise ValueError("Source fixture state verification unavailable; disposable exploration is not authorized by an unknown hash")
        if self.args.target != "emulator-5554" or self.args.expected_serial != "AndroidWorldAvd":
            raise ValueError("disposable research requires the dedicated AndroidWorld emulator")
        def identity():
            result = subprocess.check_output([self.args.adb,"-s","emulator-5554","emu","avd","name"],timeout=10).decode()
            if result.replace("\r","").splitlines()[0].strip() != "AndroidWorldAvd":
                raise ValueError("AndroidWorld emulator identity mismatch")
        identity()
        expected = {"case":self.args.source,"serial":"emulator-5554",
            "avd":"AndroidWorldAvd","initial_state_hash":baseline["initial_state_hash"]}
        async def restore():
            if cancelled():
                raise asyncio.CancelledError
            identity()
            await asyncio.to_thread(self.release_source)
            if cancelled():
                raise asyncio.CancelledError
            absence = await asyncio.to_thread(self.restore_bound_absence, baseline)
            from threading import Event
            stopped = Event()
            worker = asyncio.create_task(asyncio.to_thread(self.ensure_source_fixture,
                cancelled=lambda: stopped.is_set() or cancelled()))
            try:
                transition = await asyncio.shield(worker)
            except asyncio.CancelledError:
                stopped.set()
                try:
                    await asyncio.wait_for(asyncio.shield(worker),30)
                except BaseException:
                    self.validation_quarantined = True
                raise
            actual = json.loads((self._source_done.parent / "episodes" / self.args.source /
                "plan_executor" / "initial-rows.json").read_text(encoding="utf-8"))
            return {"complete":True,"teardown_success":True,"initialized":transition["fixture_reset"] is True,
                "case":self.args.source,"serial":"emulator-5554","avd":"AndroidWorldAvd",
                "initial_state_hash":digest(actual),"generation":transition["generation"],
                **({"recorded_absence_restoration":absence} if absence is not None else {})}
        return TrustedFixtureReset("Official AndroidWorld "+self.args.source+" task fixture only",expected,restore)

    def _episode_launch(self, directory, task, skills):
        if not self.args.androidworld_baseline or not self.args.eval_python:
            raise ValueError("AndroidWorld needs --androidworld-baseline and --eval-python")
        baseline = Path(self.args.androidworld_baseline).resolve()
        upstream = baseline / "latest-shallow"
        if not upstream.is_dir() or not (baseline / "setup-complete.json").is_file():
            raise ValueError("Existing prepared AndroidWorld baseline required")
        environment = os.environ.copy()
        environment.update(CLICKCLICK_EVAL_MODEL=self.model,
            CLICKCLICK_EVAL_AGENT_PYTHON=self.args.agent_python or sys.executable,
            CLICKCLICK_EVAL_ENV_FILE=str(Path(self.args.env_file).resolve()),
            CLICKCLICK_EVAL_SKILLS_DIR=str(skills.resolve()), PYTHONIOENCODING="utf-8",
            # Official noise-file generation iterates a set before assigning
            # random content. A task RNG seed alone does not match those files.
            PYTHONHASHSEED="0",
            PYTHONPATH=str(upstream) + os.pathsep + str(Path.cwd()))
        offset = getattr(self.args, "seed_offset", 0)
        if "-variant" in directory.name and getattr(self.args,"variant_seed_offset",None) is not None:
            offset = self.args.variant_seed_offset
        command = [self.args.eval_python, "evaluation/androidworld/run_emulator.py",
            "--baseline", str(baseline), "--output", str(directory),
            "--runtime", str(Path.cwd()), "--project", str(Path.cwd()),
            "--adb", self.args.adb, "--profile", "app", "--case", task, "--arm", "plan_executor",
            "--seed-offset", str(offset),
            "--max-steps", str(self.args.rounds), "--max-model-calls", str(self.args.rounds * 6), "--max-seconds", str(self._validation_seconds())]
        return command, environment

    def source_fixture_initialization_seconds(self):
        return min(120, self.args.learn_seconds)

    def research_preparation_horizon(self, next_research_seconds):
        environment = getattr(getattr(self, "learning_backend", None), "environment", None)
        if environment is not None and environment.stage == "cleanup":
            # Cleanup restores and verifies the fixture; it does not dispatch
            # the next research question. Its existing initializer deadline is
            # the relevant forecast. A later probe must prepare independently.
            return self.source_fixture_initialization_seconds()
        return next_research_seconds

    def ensure_source_fixture(self, *, cancelled=lambda: False):
        if cancelled():
            raise asyncio.CancelledError
        if getattr(self, "_source_process", None) is not None:
            if self._source_process.poll() is not None:
                raise RuntimeError("Source fixture lease ended unexpectedly; preserve cleanup evidence")
            return {"fixture_reset": False, "source_case": self.args.source,
                "generation": getattr(self, "_fixture_generation", 0),
                "policy": "Existing source lease reused; no initializer ran. Current viewport, display settings and records may reflect previous execution or probes. Re-ground and measure any required restoration; do not assume the initial state."}
        generation = getattr(self, "_fixture_generation", 0) + 1
        self._fixture_generation = generation
        directory = self.root / ("learning-fixture-" + str(generation))
        directory.mkdir(exist_ok=False)
        command, environment = self._episode_launch(directory, self.args.source, self.baseline)
        self._source_done = directory / "learning.done"
        ready = self._source_done.with_suffix(".ready.json")
        command += ["--prepare-learning-only", "--learning-boundary", str(self._source_done),
            "--learning-wait-seconds", str(self.args.learn_seconds + 90)]
        self._source_log = (directory / "console.log").open("w", encoding="utf-8")
        self._source_process = subprocess.Popen(command, env=environment, stdout=self._source_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self.source_fixture_initialization_seconds()
        while not ready.is_file() and self._source_process.poll() is None:
            if cancelled():
                self._source_done.touch()
                self.release_source()
                raise asyncio.CancelledError
            if time.monotonic() >= deadline:
                self.release_source()
                raise TimeoutError("Official learning fixture initialization timed out")
            time.sleep(.25)
        if not ready.is_file():
            self.release_source()
            raise RuntimeError("Matched exploration fixture unavailable; inspect learning-fixture console.log")
        if cancelled():
            self.release_source()
            raise asyncio.CancelledError
        phase = json.loads(ready.read_text(encoding="utf-8"))
        if phase.get("phase") != "initialized_unscored_fixture" or phase.get("case") != self.args.source:
            self.release_source()
            raise RuntimeError("Unexpected learning fixture identity")
        return {"fixture_reset": True, "source_case": self.args.source, "generation": generation,
            "policy": "Official source initializer restored initial collection. All prior live positions, pending rows and mutations are historical; re-ground from current observations. This initialization is not a scored task trial."}

    def episode(self, name, task, skills):
        directory = self.root / name
        if task in self.case_baselines:
            self.restore_bound_absence(self.case_baselines[task])
        command, environment = self._episode_launch(directory, task, skills)
        directory.mkdir(parents=True, exist_ok=True)
        print(f"Starting {name}: {task} on AndroidWorld", flush=True)
        result_path = directory / "episodes" / task / "plan_executor" / "result.json"
        if name == "baseline-source":
            self._source_done = directory / "learning.done"
            ready = self._source_done.with_suffix(".ready.json")
            command += ["--learning-boundary", str(self._source_done), "--learning-wait-seconds", str(self.args.learn_seconds + 90)]
            self._source_log = (directory / "console.log").open("w", encoding="utf-8")
            self._source_process = subprocess.Popen(command, env=environment, stdout=self._source_log, stderr=subprocess.STDOUT)
            process = self._source_process
            # Official worker itself has bounded action/model/time limits.
            while not ready.is_file() and process.poll() is None:
                time.sleep(.25)
            if not ready.is_file():
                self.release_source()
                raise RuntimeError(f"AndroidWorld source fixture failed; see {directory / 'console.log'}")
        else:
            with (directory / "console.log").open("w", encoding="utf-8") as log:
                process = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT)
        if process.returncode not in (None, 0) or not result_path.is_file():
            raise EvaluationInfrastructureError(f"AndroidWorld episode infrastructure failure; see {directory / 'console.log'}")
        return self.read_episode_result(name, task, skills, directory)

    def read_episode_result(self, name, task, skills, directory):
        """Export an already scored ordinary episode without dispatching it again."""
        result_path = directory / "episodes" / task / "plan_executor" / "result.json"
        raw = json.loads(result_path.read_text(encoding="utf-8"))
        manifest = json.loads((result_path.parent / "runtime-manifest.json").read_text(encoding="utf-8"))
        expected_skills = {str(p.relative_to(skills)): hashlib.sha256(p.read_bytes()).hexdigest() for p in skills.rglob("SKILL.md")}
        delivered = {str(Path(k)): v for k, v in manifest["skill_hashes"].items()}
        if manifest.get("library_manifest") != library_manifest(skills):
            raise RuntimeError("Episode did not receive frozen skills and resources")
        if delivered != expected_skills:
            raise RuntimeError("Episode did not receive the exact isolated skill library")
        request = json.loads((result_path.parent / "request.json").read_text(encoding="utf-8"))
        initial_file = result_path.parent / "initial-rows.json"
        initial_state = json.loads(initial_file.read_text(encoding="utf-8")) if initial_file.is_file() else None
        result = {"evaluation_environment": "androidworld", "clickclick_task_id": raw["task_id"], "clickclick_status": raw["status"],
            "logical_action_steps": raw["episode_steps"], "role_invocations": raw["role_invocations"],
            "elapsed_s": raw["elapsed_s"], "failure_reason": raw.get("failure_reason"),
            "score": raw["score"], "goal": request["goal"],
            "execution_limits": {key: request.get(key) for key in ("max_steps", "max_model_calls", "max_seconds")},
            "runtime_success": raw["status"] == "succeeded", "budgeted_success": raw["budgeted_success"],
            "initial_state_hash": digest(initial_state) if initial_state else None,
            "initial_state_file": str(initial_file) if initial_file.is_file() else None,
            "initial_state_coverage": "measured" if initial_state else "unavailable",
            "configuration_hash": digest({"model": manifest["model"], "provider": manifest["provider"],
                "runtime": manifest["runtime_hashes"], "architecture": manifest["settings"]["architecture"],
                "history": manifest["settings"]["executor_context_tokens"], "request": request}),
            "environment_status": "ready" if raw.get("valid") else "invalid",
            "runtime_directory": str(result_path.parent / "runtime"), "library_hash":manifest["library_manifest"]["hash"]}
        self.rows.append({"arm": name, "task": task, "success": official_success(result),
            "runtime_success": result["runtime_success"], "budgeted_success": result["budgeted_success"],
            "actions": raw["episode_steps"], "calls": raw["role_invocations"],
            "seconds": raw["elapsed_s"], "environment_status": result["environment_status"],
            "result": str(result_path), "skills": digest({str(p.relative_to(skills)): digest(p.read_text(encoding="utf-8"))
                for p in skills.rglob("SKILL.md")})})
        atomic_json(directory / "episode-result.json", result)
        atomic_json(self.root / "arms.json", self.rows)
        print(f"Finished {name}: oracle={official_success(result)}, actions={raw['episode_steps']}, calls={raw['role_invocations']}", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["androidworld", "mobileworld"], default="androidworld")
    parser.add_argument("--eval-python")
    parser.add_argument("--agent-python")
    parser.add_argument("--androidworld-baseline")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--source", required=True)
    parser.add_argument("--force-learning", action="store_true", help="Explicit optimization diagnosis; default uses production signals and skips efficient success")
    parser.add_argument("--source-first-validation", action="store_true", help="Defer guard arms when a matched source candidate fails; incomplete coverage cannot qualify for promotion")
    parser.add_argument("--variant")
    parser.add_argument("--near-miss")
    parser.add_argument("--related-normal")
    parser.add_argument("--reference-skills", required=True)
    parser.add_argument("--baseline-skills", help="Complete existing official library; omitted retains no-skill discovery mode")
    parser.add_argument("--disposable-research", action="store_true", help="Operator-owned AndroidWorld fixture reset, independently checked; never enabled on user devices")
    parser.add_argument("--continuous-research", action="store_true", help="Retain live state across audited probes in an owned AndroidWorld disposable fixture; verified cleanup remains mandatory before adjudication, ordinary validation and exit")
    parser.add_argument("--variant-seed-offset", type=int, help="Independent official generation for same-task variant arms")
    parser.add_argument("--no-research-reset", action="store_true", help="Research starts from the live terminal source state without an initializer")
    parser.add_argument("--env-file", default=".env", help="Read existing runtime configuration; never copy or record credentials")
    parser.add_argument("--model", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--collector-apk", required=True)
    parser.add_argument("--ime-apk", required=True)
    parser.add_argument("--backend", default="http://127.0.0.1:6800")
    parser.add_argument("--target", default="127.0.0.1:5556")
    parser.add_argument("--expected-serial", default="EMULATOR36X2X12X0")
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--seconds", type=float, default=240)
    parser.add_argument("--learn-calls", type=int, default=24)
    parser.add_argument("--learn-actions", type=int, default=30)
    parser.add_argument("--learn-seconds", type=float, default=900)
    args = parser.parse_args()
    if args.environment == "androidworld":
        args.target = "emulator-5554"
        args.expected_serial = "AndroidWorldAvd"
    experiment = AndroidWorldExperiment(args) if args.environment == "androidworld" else Experiment(args)
    print(json.dumps(experiment.run(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
