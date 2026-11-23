"""Paired ordinary execution against host-owned, prepared GUI test cases."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol
from uuid import uuid4

from agent.skills.learning import Candidate, LearningBudget, digest, validate_candidate
from agent.skills.library import SkillLibrary
from agent.skills.skill_edits import DeleteCandidate
from agent.skills.snapshot import LibrarySnapshot, library_manifest
from agent.skills.verification import CHECKS
from evaluation.mobileworld.long_run_health import atomic_json
from evaluation.skill_learning import EvaluationInfrastructureError, Experiment


@dataclass(frozen=True)
class PreparedCase:
    case_id: str
    goal: str
    app_id: str
    build: str
    branch_expected: bool


@dataclass(frozen=True)
class FixtureContext:
    case: PreparedCase
    nonce: str
    output: Path
    model: str


@dataclass(frozen=True)
class PreparedState:
    nonce: str
    app_id: str
    build: str
    initial_state_hash: str
    configuration_hash: str
    conditions_met: bool
    goal_already_met: bool
    evidence_ref: str


@dataclass(frozen=True)
class OrdinaryResult:
    nonce: str
    configuration_hash: str
    library_hash: str
    completed: bool
    actions: int
    requests: int
    elapsed_s: float
    runtime_directory: str | None = None
    clickclick_task_id: str | None = None


@dataclass(frozen=True)
class IndependentAssessment:
    nonce: str
    score: bool
    branch_observed: bool | None
    guards_passed: bool | None
    evidence_ref: str
    file_preservation: dict | None = None


@dataclass(frozen=True)
class CleanupReceipt:
    nonce: str
    verified_clean: bool
    evidence_ref: str


class PreparedFixture(Protocol):
    """Trusted evaluation host contract for an owned fixture.

    The host must hold exclusive fixture use across the full pair. Fingerprint
    the observed initial state and effective runtime before ordinary
    execution. The worker must confirm that runtime and the library it actually
    used. Assessment uses independent observations or a fixture oracle, and
    cleanup must also handle partial setup or a cancelled execution. Each
    callback must cancel and join any device work before returning or raising.
    """

    async def prepare(self, context: FixtureContext) -> PreparedState: ...

    async def execute(
        self, context: FixtureContext, skills: Path, rounds: int, seconds: float
    ) -> OrdinaryResult: ...

    async def assess(
        self, context: FixtureContext, preparation: PreparedState, execution: OrdinaryResult
    ) -> IndependentAssessment: ...

    async def cleanup(self, context: FixtureContext) -> CleanupReceipt: ...


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvaluationInfrastructureError(reason)


def _nonempty(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


class _PreparedExperiment(Experiment):
    def __init__(self, *, output, baseline, cases, fixture, model, rounds, seconds, fixture_seconds):
        # The benchmark constructor also sets up an authored reference arm and
        # Learner configuration. This runner evaluates an already-fixed candidate.
        self.root = output
        self.baseline_snapshot = LibrarySnapshot.freeze(baseline, output / "baseline-library")
        self.baseline = self.baseline_snapshot.root
        self.args = SimpleNamespace(
            environment="prepared_gui", rounds=rounds, seconds=seconds + 3 * fixture_seconds,
            **{kind: cases[kind].case_id if kind in cases else None for kind in CHECKS},
        )
        self.config = {
            "environment": self.args.environment, "model": model, "rounds": rounds,
            "ordinary_seconds": seconds, "fixture_callback_seconds": fixture_seconds,
            "cases": {kind: asdict(case) for kind, case in cases.items()},
            "baseline_manifest": self.baseline_snapshot.manifest,
        }
        self.case_baselines = {}
        self.validation_runs = {}
        self.fixture = fixture
        self.cases = {case.case_id: case for case in cases.values()}
        self.case_checks = {kind: case.case_id for kind, case in cases.items()}
        self.ordinary_seconds = seconds
        self.fixture_seconds = fixture_seconds
        self.preparations = {}
        self.arms = {}
        atomic_json(output / "manifest.json", self.config)

    def _validation_cases(self):
        kinds = [kind for kind in CHECKS if kind in self.case_checks]
        kinds.extend(sorted(self.case_checks.keys() - set(CHECKS), key=lambda kind: int(kind.split("-", 1)[1])))
        return [(kind, self.case_checks[kind]) for kind in kinds]

    async def _run_episode_async(self, name, case_id, skills):
        case = self.cases[case_id]
        directory = self.root / name
        directory.mkdir()
        context = FixtureContext(case, uuid4().hex, directory, self.config["model"])
        began = time.monotonic()
        result = {"environment_status": "unscored", "goal": case.goal, "nonce": context.nonce}
        expected_library = library_manifest(skills)["hash"]
        phase = "preparation"
        error = None
        try:
            preparation = await asyncio.wait_for(self.fixture.prepare(context), self.fixture_seconds)
            result["preparation"] = asdict(preparation)
            _require(preparation.nonce == context.nonce, "preparation_nonce_mismatch")
            _require((preparation.app_id, preparation.build) == (case.app_id, case.build), "app_build_mismatch")
            _require(preparation.conditions_met is True and preparation.goal_already_met is False,
                     "initial_conditions_unverified_or_goal_already_met")
            _require(all(map(_nonempty, (preparation.initial_state_hash, preparation.configuration_hash,
                                         preparation.evidence_ref))), "initial_identity_unavailable")
            signature = (preparation.initial_state_hash, preparation.configuration_hash)
            _require(self.preparations.setdefault(case_id, signature) == signature, "paired_initial_identity_mismatch")
            result.update(initial_state_hash=signature[0], configuration_hash=signature[1])

            phase = "ordinary_execution"
            execution = await asyncio.wait_for(
                self.fixture.execute(context, skills, self.args.rounds, self.ordinary_seconds),
                self.ordinary_seconds,
            )
            _require(execution.nonce == context.nonce, "execution_nonce_mismatch")
            _require(type(execution.completed) is bool and all(type(n) is int and n >= 0
                     for n in (execution.actions, execution.requests)) and type(execution.elapsed_s) in (int, float)
                     and math.isfinite(execution.elapsed_s) and execution.elapsed_s >= 0, "invalid_execution_receipt")
            result["execution"] = asdict(execution)
            result.update(logical_action_steps=execution.actions, role_invocations=execution.requests,
                          ordinary_completed=execution.completed, ordinary_elapsed_s=execution.elapsed_s)
            _require(execution.configuration_hash == signature[1], "execution_configuration_mismatch")
            _require(execution.library_hash == expected_library == library_manifest(skills)["hash"],
                     "execution_library_mismatch")
            if execution.runtime_directory or execution.clickclick_task_id:
                runtime_path = Path(execution.runtime_directory or "").resolve()
                _require(runtime_path.is_relative_to(directory) and _nonempty(execution.clickclick_task_id),
                         "invalid_execution_artifact_location")
                result.update(runtime_directory=str(runtime_path), clickclick_task_id=execution.clickclick_task_id)
            result["library_hash"] = expected_library

            phase = "independent_assessment"
            assessment = await asyncio.wait_for(
                self.fixture.assess(context, preparation, execution), self.fixture_seconds,
            )
            result["assessment"] = asdict(assessment)
            _require(assessment.nonce == context.nonce, "assessment_nonce_mismatch")
            _require(type(assessment.score) is bool and _nonempty(assessment.evidence_ref)
                     and all(value is None or type(value) is bool
                             for value in (assessment.branch_observed, assessment.guards_passed)),
                     "independent_outcome_unavailable")
            result.update(score=assessment.score, budgeted_success=(assessment.score and execution.completed
                          and execution.actions <= self.args.rounds and execution.requests <= self.args.rounds * 6
                          and execution.elapsed_s <= self.ordinary_seconds))
        except EvaluationInfrastructureError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"{phase}_{type(exc).__name__}"
        except asyncio.CancelledError:
            result["cancelled"] = True
            raise
        finally:
            cleanup = asyncio.create_task(asyncio.wait_for(self.fixture.cleanup(context), self.fixture_seconds))
            interrupted = None
            try:
                try:
                    receipt = await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    result["cancelled"] = True
                    interrupted = exc
                    receipt = await asyncio.shield(cleanup)
                result["cleanup"] = asdict(receipt)
                _require(receipt.nonce == context.nonce and receipt.verified_clean is True
                         and _nonempty(receipt.evidence_ref), "cleanup_not_verified")
                _require(library_manifest(skills)["hash"] == expected_library, "library_changed_during_arm")
            except EvaluationInfrastructureError as exc:
                error = " / ".join(filter(None, (error, str(exc))))
            except Exception as exc:
                error = " / ".join(filter(None, (error, f"cleanup_{type(exc).__name__}")))
            finally:
                result["elapsed_s"] = time.monotonic() - began
                if not error and not result.get("cancelled") and "score" in result and "cleanup" in result:
                    result["environment_status"] = "ready"
                if error:
                    result["infrastructure_error"] = error
                self.arms[name] = result
                atomic_json(directory / "episode-result.json", result)
            if interrupted is not None:
                raise interrupted
        if error:
            raise EvaluationInfrastructureError(error)
        return result


async def validate_gui_skill(
    *, candidate: Candidate | DeleteCandidate, old_text: str, baseline_skills: Path, output: Path,
    cases: Mapping[str, PreparedCase], fixture: PreparedFixture, model: str, rounds: int, seconds: float,
    cleanup_seconds: float = 15.0, budget: LearningBudget | None = None,
) -> dict:
    """Evaluate one exact tested draft without running Learner or publishing anything.

    `seconds` is the ordinary-worker cap. `cleanup_seconds` bounds each of the
    three host callbacks (prepare, independent assessment, cleanup). The shared
    budget, when provided, must have room for all four bounds before an arm starts.
    """
    if isinstance(candidate, DeleteCandidate):
        return {"validation_stage": "unsupported_operation", "reason": "prepared_deletion_not_supported",
                "trials": [], "arms": {}}
    if "source" not in cases or any(kind not in CHECKS and not re.fullmatch(r"registered-[1-9][0-9]*", kind)
                                    for kind in cases):
        raise ValueError("a source case and only declared verification kinds are required")
    if (len({case.case_id for case in cases.values()}) != len(cases)
            or any(not all(map(_nonempty, (case.case_id, case.goal, case.app_id, case.build)))
                   or type(case.branch_expected) is not bool for case in cases.values())):
        raise ValueError("prepared cases require unique IDs, explicit build and branch expectation")
    if (not _nonempty(model) or type(rounds) is not int or rounds <= 0
            or not all(type(limit) in (int, float) and math.isfinite(limit) and limit > 0
                       for limit in (seconds, cleanup_seconds))):
        raise ValueError("a model and positive finite evaluation limits are required")

    baseline_skills, output = Path(baseline_skills).resolve(), Path(output).resolve()
    if not baseline_skills.is_dir() or output.is_relative_to(baseline_skills):
        raise ValueError("evaluation output must be outside an existing baseline library")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("evaluation output must be empty")
    experiment = _PreparedExperiment(output=output, baseline=baseline_skills, cases=cases, fixture=fixture,
                                     model=model, rounds=rounds, seconds=seconds, fixture_seconds=cleanup_seconds)
    candidate = candidate.model_copy(deep=True)
    if validate_candidate(candidate, SkillLibrary(experiment.baseline), {cases["source"].app_id}) != old_text:
        raise ValueError("exact candidate base changed")
    report = await experiment.validate(candidate, old_text, budget=budget)
    failed, unknown = [], []
    for trial in report["trials"]:
        kind = trial["kind"]
        control = experiment.arms["baseline-" + kind]
        treatment = experiment.arms[report["candidate_hash"][:8] + "-" + kind]
        trial["config_hash"] = digest({
            "registered": trial["config_hash"], "observed_runtime": control["configuration_hash"],
            "observed_initial_state": control["initial_state_hash"],
        })
        trial["baseline_success"] = control["budgeted_success"]
        trial["candidate_success"] = treatment["budgeted_success"]
        assessment, expected = treatment["assessment"], cases[kind].branch_expected
        trial["candidate_branch_matches"] = (None if assessment["branch_observed"] is None
                                              else assessment["branch_observed"] == expected)
        # A former positive path protects its goal and invariants. A revised
        # workflow may legitimately reach them by a new route.
        branch_required = not kind.startswith("registered-") or not expected
        trial["candidate_branch_required"] = branch_required
        trial["guard_passed"] = assessment["guards_passed"]
        if not trial["baseline_success"] or not trial["candidate_success"]:
            trial.update(efficiency_gain=0, request_gain=None)
        if not trial["matched_environment"] or not trial["independent_oracle"]:
            unknown.append({"kind": kind, "reason": "unmatched_or_unscored"})
            continue
        for valid, reason in (
            (trial["candidate_success"], "candidate_ordinary_completion_failed"),
            (not branch_required or trial["candidate_branch_matches"], "candidate_wrong_branch"),
            (trial["guard_passed"], "candidate_guard_failed"),
        ):
            if valid is False:
                failed.append({"kind": kind, "reason": reason})
        if (branch_required and trial["candidate_branch_matches"] is None or trial["guard_passed"] is None
                or control["assessment"]["guards_passed"] is not True):
            unknown.append({"kind": kind, "reason": "branch_or_guard_unverified"})
        if (kind in {"near_miss", "related_normal"} or kind.startswith("registered-")) and not trial["baseline_success"]:
            unknown.append({"kind": kind, "reason": "control_not_successful"})
    unrun = set(cases) - {trial["kind"] for trial in report["trials"]}
    unknown.extend({"kind": kind, "reason": "pair_not_completed"} for kind in sorted(unrun))
    report["prepared_gate"] = {
        "status": "failed" if failed else ("inconclusive" if unknown else "passed"),
        "failed": failed, "unknown": unknown,
    }
    report["arms"] = experiment.arms
    atomic_json(output / "prepared-validation.json", report)
    return report
