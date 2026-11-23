from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.skills import learning as learning_module, pending as pending_module
from agent.skills.learning import Candidate, CRITERIA, digest
from agent.skills.admission import source_validation
from agent.skills.exploration import build_review_contracts
from agent.skills.pending import approve_pending, get_pending, list_pending, stage_pending_patch
from agent.traces import TraceWriter
from evaluation import gui_skill_loop as loop
from evaluation.gui_skill_budget import bind_model_budget, replay_dispatch_meter
from evaluation.gui_skill_case_registry import CaseIdentity, RegisteredCase
from evaluation.gui_skill_regression import PreparedCase
from shared.artifacts import ArtifactStore
from shared.config import get_settings
from shared.db import Database
from shared.schemas import TaskStatus
from shared.task_budgets import TaskBudgetExhausted


APP = "com.example.timer"
TARGET = f"apps/{APP}/workflows/timer/SKILL.md"
TEXT = f"""---
name: timer
description: Configure the timer.
version: '1.0.0'
kind: workflow
app: {APP}
capability: configure_timer
---

## Procedure

Enter the requested duration and leave the timer unstarted.

## Verification

Check the timer shows the requested duration and still has a Start button.
"""


def plan_for(**case):
    return loop.SkillLoopPlan.model_validate({"cases": {"source": {
        "case_key": "two-minutes", "app_id": APP, "build": "2", **case,
    }}})


OPTIONS = loop.FixtureOptions("adb", "emulator-5554", "isolated", "clean", None, "model", 8, 30, 30)


@pytest.mark.asyncio
async def test_registered_case_is_reused_before_existing_or_recording(monkeypatch, tmp_path):
    registered = object()
    registry = SimpleNamespace(lookup=lambda identity: registered)
    monkeypatch.setattr(loop, "record_case", lambda *args, **kwargs: pytest.fail("already registered"))
    monkeypatch.setattr(loop, "verify_case", lambda *args, **kwargs: pytest.fail("already registered"))
    case = plan_for(existing="existing.json").cases["source"]
    assert await loop._ensure_case(registry, case, TARGET, tmp_path, tmp_path, tmp_path, OPTIONS) == (registered, "reused")


@pytest.mark.asyncio
async def test_missing_case_is_recorded_then_registered(monkeypatch, tmp_path):
    present = {"type": "target_present", "target": {"text": "Ready"}}
    request = {"app_id": APP, "build": "2", "setup_goal": "Prepare the timer", "goal": "Set two minutes",
               "result": [present], "guards": [present], "branch": [present], "branch_after": [present],
               "branch_action": "tap", "branch_expected": True}
    selection = plan_for(recording=request).cases["source"]
    received = []

    async def record(actual, **kwargs):
        assert actual == selection.recording
        received.append("recorded-and-verified")
        return SimpleNamespace(definition="case", asset_root=tmp_path, verification="replay")

    def register(identity, definition, *, asset_root, verification):
        assert (identity.skill_target, definition, asset_root, verification) == (TARGET, "case", tmp_path, "replay")
        received.append("registered")
        return "persistent-case"

    monkeypatch.setattr(loop, "record_case", record)
    registry = SimpleNamespace(lookup=lambda identity: None, register=register)
    assert await loop._ensure_case(registry, selection, TARGET, tmp_path, tmp_path, tmp_path, OPTIONS) == (
        "persistent-case", "recorded",
    )
    assert received == ["recorded-and-verified", "registered"]


@pytest.mark.asyncio
async def test_existing_case_replays_before_registry_write(monkeypatch, tmp_path):
    path = tmp_path / "existing.json"
    path.write_text("{}", encoding="utf-8")
    case = SimpleNamespace(setup=SimpleNamespace(app_id=APP), build="2")
    monkeypatch.setattr(loop.CaseDefinition, "model_validate_json", lambda data: case)
    monkeypatch.setattr(loop, "record_case", lambda *args, **kwargs: pytest.fail("the existing case should be used"))

    async def verify(definition, **kwargs):
        assert definition is case and kwargs["asset_root"] == tmp_path
        return "verified-replay"

    def register(identity, definition, *, asset_root, verification):
        assert (identity.case_key, definition, asset_root, verification) == (
            "two-minutes", case, tmp_path, "verified-replay",
        )
        return "existing-case"

    monkeypatch.setattr(loop, "verify_case", verify)
    registry = SimpleNamespace(lookup=lambda identity: None, register=register)
    selection = plan_for(existing=path.name).cases["source"]
    assert await loop._ensure_case(registry, selection, TARGET, tmp_path, tmp_path, tmp_path, OPTIONS) == (
        "existing-case", "existing",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("pair_status,exposure,expected", [
    ("failed", True, "blocked"), ("passed", False, "blocked"), ("passed", True, "awaiting_publication_review"),
])
async def test_pair_result_controls_staging_without_changing_active_skill(tmp_path, monkeypatch, pair_status, exposure, expected):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    candidate = Candidate(target=TARGET, new_text=TEXT, gist="timer", evidence=["event:1@1"], scope="timer",
                          benefit="avoid needless steps", risks="incorrect duration")
    task = SimpleNamespace(id="source-task", status=TaskStatus.SUCCEEDED)

    async def learn(runtime, task_id, library, output, options):
        review = {"verdict": "pass", "criteria": {k: {"status": "pass", "reason": "evidence"} for k in CRITERIA}}
        pending = stage_pending_patch(target_rel=TARGET, root=library.root, app=APP, new_text=TEXT, gist="timer",
            review_metadata={"experiment": "task_explore", "eligible": True, "review": review,
                             "candidate_contract": candidate.model_dump(), "candidate_hash": digest(TEXT),
                             "base_hash": digest(""), "baseline_manifest": library.manifest})
        return task, {"pending_id": pending.id}

    @contextmanager
    def lease(*args):
        yield SimpleNamespace(quarantined=False)

    definition = SimpleNamespace(setup=SimpleNamespace(script_id="timer"), prepared_case=lambda: "ready")
    registered = SimpleNamespace(case=definition, path=Path("case.json"), asset_root=tmp_path,
                                 verification=SimpleNamespace(initial_state_hash="initial"))

    async def ensure(*args):
        return registered, "reused"

    async def pair(**kwargs):
        assert kwargs["candidate"] == candidate and kwargs["cases"] == {"source": "ready"}
        return {"prepared_gate": {"status": pair_status}, "candidate_hash": digest(TEXT), "base_hash": digest(""),
                "baseline_library_hash": loop.library_manifest(baseline)["hash"]}

    monkeypatch.setattr(loop, "_learn", learn)
    monkeypatch.setattr(loop, "exclusive_emulator", lease)
    monkeypatch.setattr(loop, "_ensure_case", ensure)
    monkeypatch.setattr(loop, "case_fingerprint", lambda case: "case-hash")
    monkeypatch.setattr(loop, "validate_gui_skill", pair)
    monkeypatch.setattr(loop, "_skill_delivery", lambda *args: {"verified": exposure})
    monkeypatch.setattr(loop, "AndroidSnapshotFixture", lambda *args, **kwargs: SimpleNamespace(quarantined=False))
    report = await loop.run_skill_loop(runtime=tmp_path, task_id=task.id, plan=plan_for(), plan_dir=tmp_path,
        baseline_skills=baseline, registry_root=tmp_path / "registry", output=tmp_path / "out", options=OPTIONS)

    assert report["status"] == expected
    assert not (baseline / TARGET).exists()
    if expected == "awaiting_publication_review":
        pending = get_pending(report["pending_id"], root=baseline)
        proof = pending.meta["review"]["prepared_gui_regression"]
        assert proof["candidate_hash"] == digest(TEXT) and proof["cases"]["source"]["case_hash"] == "case-hash"
    else:
        assert not (baseline / "_pending").exists()


def test_delivery_verifies_exact_role_projection_and_version(tmp_path):
    db = Database(tmp_path / "clickclick.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    task = db.create_task("Set the timer")
    traces = TraceWriter(db, artifacts)
    pack = loop.parse_skill_markdown(TEXT)
    body_hash = loop.stable_hash(pack.section_for("executor"))
    arm = {"runtime_directory": str(tmp_path), "clickclick_task_id": task.id}

    def note(version, content_hash):
        ref = artifacts.save_json("llm", {"role": "executor", "active_skills": [
            {"skill_id": pack.id, "version": version, "content_hash": content_hash},
        ]})
        traces.write(task.id, kind="executor_tick", message="executor request", payload={"request_ref": ref})

    try:
        note("0.9.0", body_hash)
        note(pack.version, "other body")
        assert loop._delivered_roles(arm, pack, {"executor": body_hash}) == []
        note(pack.version, body_hash)
        assert loop._delivered_roles(arm, pack, {"executor": body_hash}) == ["executor"]
    finally:
        db.close()


def test_candidate_for_another_observed_device_profile_is_not_selected(tmp_path):
    scoped = TEXT.replace("capability: configure_timer", "capability: configure_timer\n"
                          "interface_scope: system\ndevice_profiles: [other_profile]")
    candidate = Candidate(target=TARGET, new_text=scoped, gist="timer", evidence=["event:1@1"],
                          scope="timer", benefit="avoid needless steps", risks="incorrect duration")
    library = SimpleNamespace(root=tmp_path, manifest={})
    review = {"verdict": "pass", "criteria": {k: {"status": "pass", "reason": "evidence"} for k in CRITERIA}}
    pending = stage_pending_patch(target_rel=TARGET, root=tmp_path, app=APP, new_text=scoped, gist="timer",
        review_metadata={"experiment": "task_explore", "review": review, "baseline_manifest": library.manifest,
                         "candidate_contract": candidate.model_dump(), "candidate_hash": digest(scoped)})

    result = {"pending_id": pending.id, "observed_source_profiles": ["actual_profile"]}
    assert loop._selected_candidate(plan_for(), result, library) is None


@pytest.mark.asyncio
async def test_automatic_loop_selects_plan_and_preserves_the_same_total_budget(monkeypatch, tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    case = SimpleNamespace(setup=SimpleNamespace(script_id="timer", steps=[SimpleNamespace(action=SimpleNamespace(type="tap"))]),
                           prepared_case=lambda: "ready")
    registered = SimpleNamespace(case=case, path=tmp_path / "case.json", asset_root=tmp_path,
                                 verification=SimpleNamespace(initial_state_hash="ready"))
    task = SimpleNamespace(id="source-task", status=TaskStatus.SUCCEEDED)
    candidate = Candidate(target=TARGET, new_text=TEXT, gist="timer", evidence=["event:1@1"], scope="timer",
                          benefit="avoid needless steps", risks="incorrect duration")
    phases = []
    budgets = []

    async def learn(runtime, task_id, library, output, options, *, budget):
        budgets.append(budget)
        budget.meter("model_call_started", {})
        review = {"verdict": "pass", "criteria": {k: {"status": "pass", "reason": "evidence"} for k in CRITERIA}}
        pending = stage_pending_patch(target_rel=TARGET, root=library.root, app=APP, new_text=TEXT, gist="timer",
            review_metadata={"experiment": "task_explore", "eligible": True, "review": review,
                             "candidate_contract": candidate.model_dump(), "candidate_hash": digest(TEXT),
                             "base_hash": digest(""), "baseline_manifest": library.manifest})
        return task, {"pending_id": pending.id}

    async def plan(runtime, actual_task, options, selected, old, app, registry, report, output, budget):
        assert actual_task is task and selected == candidate and app == APP
        budgets.append(budget)
        budget.meter("model_call_started", {})
        return plan_for()

    async def ensure(*args, budget, progress):
        budgets.append(budget)
        return registered, "reused"

    @contextmanager
    def lease(*args):
        yield SimpleNamespace(quarantined=False)

    def fixture(*args, budget, rounds, **kwargs):
        budgets.append(budget)
        assert rounds == 3  # Two repeated setup actions and equal remaining arm caps.
        return SimpleNamespace(quarantined=False)

    async def pair(**kwargs):
        assert kwargs["rounds"] == 3 and kwargs["cases"] == {"source": "ready"}
        budgets[-1].meter("model_call_started", {})
        return {"prepared_gate": {"status": "failed"}}

    monkeypatch.setattr(loop, "_learn", learn)
    monkeypatch.setattr(loop, "_plan_for_candidate", plan)
    monkeypatch.setattr(loop, "_ensure_case", ensure)
    monkeypatch.setattr(loop, "exclusive_emulator", lease)
    monkeypatch.setattr(loop, "case_fingerprint", lambda case: "case-hash")
    monkeypatch.setattr(loop, "AndroidSnapshotFixture", fixture)
    monkeypatch.setattr(loop, "validate_gui_skill", pair)
    report = await loop.run_skill_loop(runtime=tmp_path, task_id=task.id, baseline_skills=baseline,
        registry_root=tmp_path / "cases", output=tmp_path / "run", options=OPTIONS,
        limits=loop.SkillLoopLimits(max_calls=3, max_actions=8, max_seconds=30), progress=phases.append)

    assert phases == ["learning", "planning", "regression"]
    assert len({id(budget) for budget in budgets}) == 1
    assert report["cases"]["source"]["origin"] == "reused"
    assert report["budget"]["remaining_calls"] == 0
    assert report["reason"] == "paired_regression_not_passed"


def test_native_attempts_and_replays_cannot_overrun_the_shared_budget():
    budget = loop.LearningBudget(max_calls=1, max_actions=1, max_seconds=30, reserve_calls=0)
    attempts = []
    orch = SimpleNamespace(_model_call_meter=lambda *args: lambda kind, payload: attempts.append(kind))
    bind_model_budget(orch, budget)
    meter = orch._model_call_meter(None, None, None)
    meter("model_call_started", {})
    with pytest.raises(TaskBudgetExhausted) as exc:
        meter("model_call_started", {})
    assert exc.value.dimension == "model_calls" and attempts == ["model_call_started"]
    dispatch = replay_dispatch_meter(budget)
    dispatch()
    with pytest.raises(loop.LearningStopped, match="action_budget"):
        dispatch()


@pytest.mark.parametrize("receipt", [None, {"passed": True}, {
    "expected": {"timer.txt": "a" * 64}, "before": {"timer.txt": "a" * 64},
    "after": {"timer.txt": "b" * 64}, "passed": True,
}])
def test_publication_omits_unavailable_or_inconsistent_preservation(receipt):
    arm = {"environment_status": "ready", "ordinary_completed": True,
           "preparation": {"conditions_met": True, "goal_already_met": False},
           "assessment": {"score": True, "branch_observed": True, "guards_passed": True,
                          "file_preservation": receipt},
           "logical_action_steps": 1, "role_invocations": 2, "ordinary_elapsed_s": 3}
    facts = loop._publication_arm(arm, {}, {"timer.txt": "a" * 64})
    assert "file_preservation" not in facts


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,call_limit,calls,decision,reason", [
    ({"decision": "low", "reason": "Observed it", "evidence_cases": [{"kind": "source"}]},
     "low", 2, 2, "low", "Observed branch and invariant"),
    ("unreadable JSON", "low", 2, 2, "low", "Observed branch and invariant"),
    ({"decision": "low", "reason": "x" * 1201, "evidence_cases": ["source"]},
     "unreadable JSON", 3, 2, "insufficient", "publication_impact_invalid_response"),
    ({"decision": "low", "reason": "Missing required case array"},
     "low", 2, 2, "low", "Observed branch and invariant"),
    ("unreadable JSON", "low", 1, 1, "insufficient", "publication_impact_review_unavailable"),
    ("high", "low", 2, 1, "high", "Observed branch and invariant"),
    ("insufficient", "low", 2, 1, "insufficient", "Observed branch and invariant"),
    ({"decision": "low", "reason": "Observed it", "evidence_cases": ["unknown"]},
     "low", 2, 1, "insufficient", "publication_impact_lacks_case_evidence"),
    (loop.GatewayError("Provider detail", category="auth"),
     "low", 2, 1, "insufficient", "publication_impact_review_unavailable"),
])
async def test_publication_schema_correction_uses_same_packet_and_budget(
    monkeypatch, first, second, call_limit, calls, decision, reason,
):
    messages_for_attempts = []
    replies = iter((first, second))

    async def complete(model, messages, *, attempt_meter, **options):
        assert "response_format" not in options
        attempt_meter("model_call_started", {})
        messages_for_attempts.append(messages)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        if reply in ("low", "high", "insufficient"):
            reply = {"decision": reply, "reason": "Observed branch and invariant", "evidence_cases": ["source"]}
        content = json.dumps(reply) if isinstance(reply, dict) else reply
        return SimpleNamespace(content=content, usage=None)

    monkeypatch.setattr(learning_module, "complete", complete)
    settings = SimpleNamespace(skill_reviewer_model="test", context_policy=lambda *_: {},
                               executor_context_tokens=16_000)
    candidate = SimpleNamespace(new_text=TEXT, target=TARGET, gist="timer")
    condition = ["visibly constrained"]
    definition = SimpleNamespace(branch_expected=True, branch=condition, branch_after=condition,
                                 guards=condition, result=condition, model_dump=lambda **kwargs: {"guards": condition})
    budget = loop.LearningBudget(max_calls=call_limit, max_actions=0, max_seconds=60, reserve_calls=0)
    impact = await loop._publication_impact(candidate, "", APP, {"source": definition},
                                           {"trials": [{"kind": "source"}]}, {"review": {}}, settings, budget)

    assert (impact.decision, impact.reason) == (decision, reason)
    assert len(messages_for_attempts) == budget.calls == calls
    first_prompt = messages_for_attempts[0][0]["content"]
    assert '"evidence_cases":["source"]' in first_prompt
    if calls == 2:
        second_prompt = messages_for_attempts[1][0]["content"]
        assert second_prompt == first_prompt + loop._PUBLICATION_CORRECTION
        assert messages_for_attempts[1][1] == messages_for_attempts[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("impact,status,proof_changed,interrupted", [
    ("low", "published", False, False), ("low", "blocked", True, False),
    ("low", "published", False, True),
    ("high", "awaiting_publication_review", False, False), ("insufficient", "blocked", False, False),
])
async def test_publication_uses_exact_regression_and_independent_impact(tmp_path, monkeypatch, impact, status,
                                                                      proof_changed, interrupted):
    old_text = TEXT.replace("'1.0.0'", "'0.9.0'").replace("Enter the requested duration", "Use the timer keypad")
    baseline = tmp_path / "library"
    target = baseline / TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(old_text.encode())
    library = loop.LibrarySnapshot.freeze(baseline, tmp_path / "snapshot")
    task = SimpleNamespace(id="source", status=TaskStatus.SUCCEEDED)
    candidate = Candidate(target=TARGET, new_text=TEXT, gist="timer", evidence=["source/observation:old@1"],
                          scope="timer", benefit="clear sequence", risks="duration stays unstarted")
    review = {"verdict": "pass", "acceptance": "source_evidence", "changes": [], "tests": [],
              "criteria": {key: {"status": "pass", "reason": "witnessed"} for key in CRITERIA}}
    contracts = build_review_contracts(TEXT, loop.SkillLibrary(library.root), get_settings())
    validation = source_validation(candidate, old_text, review=review, source_id=task.id, contracts=contracts,
                                   evidence=[{"source": candidate.evidence[0], "text": "Keypad shown"}],
                                   manifest=library.manifest)
    metadata = {"eligible": True, "experiment": "task_explore", "review": review, "validation": validation,
                "candidate_contract": candidate.model_dump(), "candidate_hash": digest(TEXT),
                "base_hash": digest(old_text), "baseline_manifest": library.manifest, "contracts_hash": digest(contracts)}
    original_file = {"/storage/emulated/0/Documents/timer.txt": "c" * 64}

    def arm(actions, observed_branch):
        return {"environment_status": "ready", "ordinary_completed": True,
                "preparation": {"conditions_met": True, "goal_already_met": False},
                "assessment": {"score": True, "branch_observed": observed_branch, "guards_passed": True,
                               "file_preservation": {"expected": original_file, "before": original_file,
                                                     "after": original_file, "passed": True}},
                "logical_action_steps": actions, "role_invocations": actions + 3, "ordinary_elapsed_s": 1.25}

    pair_keys = ("candidate_hash", "base_hash", "baseline_library_hash", "candidate_library_hash")
    regression = {**{key: validation[key] for key in pair_keys}, "prepared_gate": {"status": "passed"},
                  "trials": [{"kind": "source", "matched_environment": True, "independent_oracle": True,
                              "baseline_success": True, "candidate_success": True,
                              "candidate_branch_matches": True, "guard_passed": True,
                              "candidate_execution": {"available": True, "action_types": {"tap": 1}}}],
                  "arms": {"baseline-source": arm(5, False), f"{digest(TEXT)[:8]}-source": arm(1, True)}}
    ref = tmp_path / "prepared-validation.json"
    loop.atomic_json(ref, regression)
    report = {"status": "blocked", "candidate_hash": digest(TEXT), "app_id": APP, "regression_ref": str(ref),
              "baseline_library_hash": library.manifest["hash"], "cases": {"source": {"case_hash": "case"}},
              "skill_delivery": {"verified": True, "cases": {"source": {"candidate_roles": ["executor"],
                                                                        "baseline_roles": ["executor"]}}}}
    condition = ["visibly constrained"]
    definition = SimpleNamespace(branch_expected=True, branch=condition, branch_after=condition,
                                 guards=condition, result=condition, preserved_files=original_file,
                                 model_dump=lambda **kwargs: {"guards": condition})

    async def impact_review(settings, budget, model, role, prompt, packet):
        assert role == "skill_reviewer" and packet["candidate"] == TEXT and packet["prior_skill"] == old_text
        result = packet["checked_results"][0]
        assert result["guard_passed"] is True
        assert result["baseline"]["branch_observed"] is False
        assert result["baseline"]["actions"] == 5
        assert "submitted_action_types" not in result["baseline"]
        assert result["candidate"]["branch_observed"] is True
        assert result["candidate"]["actions"] == 1
        assert result["candidate"]["model_requests"] == 4
        assert result["candidate"]["execution_seconds"] == 1.25
        assert result["candidate"]["submitted_action_types"] == {"tap": 1}
        assert result["candidate"]["verified_skill_delivery_roles"] == ["executor"]
        assert result["candidate"]["goal_already_met_before_execution"] is False
        assert all(result[side]["file_preservation"] == {"before": original_file, "after": original_file}
                   for side in ("baseline", "candidate"))
        if proof_changed:
            loop.atomic_json(ref, {**regression, "trials": []})
        return {"decision": impact, "reason": "Scope and preservation assessment", "evidence_cases": ["source"]}

    monkeypatch.setattr(loop, "_ask", impact_review)
    budget = loop.LearningBudget(max_calls=10, max_actions=6, max_seconds=60, reserve_calls=0)
    async def publish():
        await loop._stage_passed(report, baseline, library, task, candidate, metadata, old_text, regression,
                                 {"source": definition}, tmp_path, OPTIONS, budget, True)

    if interrupted:
        write = pending_module._write_metadata

        def interrupted_after_replace(path, metadata):
            if metadata.get("approved_via") == "automatic":
                raise KeyboardInterrupt
            write(path, metadata)

        with monkeypatch.context() as stop:
            stop.setattr(pending_module, "_write_metadata", interrupted_after_replace)
            with pytest.raises(KeyboardInterrupt):
                await publish()
        item = get_pending(report["pending_id"], root=baseline)
        assert item.status == "pending" and item.meta["automatic_publication_intent"]
        assert target.read_bytes() == TEXT.encode()
        assert approve_pending(item.id, root=baseline, automatic=True) == target
        assert get_pending(item.id, root=baseline).status == "approved"
        return

    await publish()

    assert report["status"] == status
    if status == "published":
        pending = get_pending(report["pending_id"], root=baseline)
        assert pending.status == "approved" and pending.meta["approved_via"] == "automatic"
        assert target.read_bytes() == TEXT.encode()
        assert pending.path.with_suffix(".previous").read_bytes() == old_text.encode()
    else:
        assert target.read_bytes() == old_text.encode()
        if proof_changed:
            assert report["reason"] == "automatic_publication_blocked"
            assert not list_pending(root=baseline)
            with pytest.raises(ValueError, match="uses automatic publication"):
                approve_pending(report["pending_id"], root=baseline)
        elif impact == "high":
            with pytest.raises(ValueError, match="low-impact"):
                approve_pending(report["pending_id"], root=baseline, automatic=True)
            loop.atomic_json(ref, {**regression, "trials": []})
            with pytest.raises(ValueError, match="reviewed publication proof"):
                approve_pending(report["pending_id"], root=baseline)
            assert target.read_bytes() == old_text.encode()
        else:
            assert "pending_id" not in report


@pytest.mark.asyncio
async def test_automatic_loop_revalidates_previous_cases_on_current_snapshot(tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    candidate = Candidate(target=TARGET, new_text=TEXT, gist="timer", evidence=["event:1@1"], scope="timer",
                          benefit="improve retained paths", risks="incorrect duration")
    task = SimpleNamespace(id="source-task", status=TaskStatus.SUCCEEDED)

    def registered(key, snapshot):
        case = SimpleNamespace(setup=SimpleNamespace(script_id="shared-setup", steps=[]), key=key,
                               prepared_case=lambda: PreparedCase("shared-setup", key, APP, "2", True))
        verification = SimpleNamespace(avd=OPTIONS.avd, snapshot=snapshot, initial_state_hash=snapshot)
        return RegisteredCase(case, tmp_path / key, tmp_path / f"{key}.json", verification)

    original = registered("two-minutes", OPTIONS.snapshot)
    previous = registered("five-minutes", "different-snapshot")

    async def learn(runtime, task_id, library, output, options, *, budget):
        review = {"verdict": "pass", "criteria": {key: {"status": "pass", "reason": "evidence"} for key in CRITERIA}}
        pending = stage_pending_patch(target_rel=TARGET, root=library.root, app=APP, new_text=TEXT, gist="timer",
            review_metadata={"experiment": "task_explore", "eligible": True, "review": review,
                             "candidate_contract": candidate.model_dump(), "candidate_hash": digest(TEXT),
                             "base_hash": digest(""), "baseline_manifest": library.manifest})
        return task, {"pending_id": pending.id}

    @contextmanager
    def lease(*args):
        yield SimpleNamespace(quarantined=False)

    async def replay(case, *, snapshot, **kwargs):
        assert case is previous.case and snapshot == OPTIONS.snapshot
        return SimpleNamespace(initial_state_hash="fresh")

    def find(**scope):
        assert scope == {"skill_target": TARGET, "app_id": APP, "build": "2", "strict": True}
        return [(CaseIdentity(TARGET, item.case.key, APP, "2"), item) for item in (original, previous)]

    def fixture(cases, *, case_asset_roots, verified_initials, **options):
        assert cases == {"source": original.case, "registered-1": previous.case}
        assert case_asset_roots == {"source": original.asset_root, "registered-1": previous.asset_root}
        assert verified_initials == {"source": OPTIONS.snapshot, "registered-1": "fresh"}
        return SimpleNamespace(quarantined=False)

    async def pair(*, cases, **options):
        assert [case.case_id for case in cases.values()] == ["source", "registered-1"]
        return {"prepared_gate": {"status": "inconclusive"}}

    monkeypatch.setattr(loop, "_learn", learn)
    monkeypatch.setattr(loop, "exclusive_emulator", lease)
    monkeypatch.setattr(loop, "CaseRegistry", lambda path: SimpleNamespace(lookup=lambda key: original, find_for_skill=find))
    monkeypatch.setattr(loop, "case_fingerprint", lambda case: case.key)
    monkeypatch.setattr(loop, "verify_case", replay)
    monkeypatch.setattr(loop, "AndroidSnapshotFixture", fixture)
    monkeypatch.setattr(loop, "validate_gui_skill", pair)
    report = await loop.run_skill_loop(runtime=tmp_path, task_id=task.id, plan=plan_for(), baseline_skills=baseline,
        registry_root=tmp_path / "cases", output=tmp_path / "run", options=OPTIONS,
        limits=loop.SkillLoopLimits(max_calls=10, max_actions=16, max_seconds=60), auto_publish=True)
    assert report["status"] == "blocked" and report["reason"] == "paired_regression_not_passed"
    assert report["cases"]["registered-1"]["fixture_reverified"] is True
