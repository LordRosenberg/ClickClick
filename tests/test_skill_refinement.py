"""Evidence-directed learning interfaces through the production host loop."""
import copy

import pytest

from agent.skills.analysis_session import validate_analysis, retained_admission_context
from agent.skills.candidate_group import materialize_proposal, CandidateGroup
from agent.skills.diagnosis import check_diagnosis
from agent.skills.learning import LearningBudget, digest, run_task_learning, validate_candidate
from agent.skills.pending import get_pending
from agent.skills.skill_edits import materialize_candidate
from agent.skills.strategy import select_strategy
from agent.skills.snapshot import LibrarySnapshot
from test_task_skill_learning import candidate, context
from test_skill_evidence_admission import setup_backend, supported_review
from test_skill_verification_feedback import Validator
from test_skill_candidate_group import setup, group_review


def localized(c, base, edits):
    return {**c.model_dump(exclude={"new_text"}), "base_hash": digest(base), "edits": edits}


def write_base(library, c):
    path = library.root / c.target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(c.new_text, encoding="utf-8")


def test_local_edit_materializes_full_body_and_keeps_metadata_guard(context):
    *_, library = context
    c = candidate(); write_base(library, c)
    edits = [{"old": "description: Query syntax", "new": "description: Exact query matching"},
        {"old": "version: 0.1.0", "new": "version: 0.1.1"}]
    updated = materialize_candidate(localized(c, c.new_text, edits), library)
    assert updated.new_text.endswith("Verify returned detail.\n")
    assert validate_candidate(updated, library, {"com.demo"}) == c.new_text
    bad = materialize_candidate(localized(c, c.new_text,
        [{"old": "name: demo-core", "new": "name: changed-identity"}]), library)
    with pytest.raises(ValueError, match="metadata changed"):
        validate_candidate(bad, library, {"com.demo"})


@pytest.mark.parametrize("fault", ["stale", "absent", "duplicate", "overlap", "mixed", "escape"])
def test_ambiguous_or_stale_edits_never_write_files(context, fault):
    *_, library = context
    c = candidate(); write_base(library, c)
    value = localized(c, c.new_text, [{"old": "Query syntax", "new": "Exact queries"}])
    if fault == "stale": value["base_hash"] = "0" * 64
    if fault == "absent": value["edits"][0]["old"] = "not present"
    if fault == "duplicate": value["edits"][0]["old"] = "\n"
    if fault == "overlap": value["edits"].append({"old": "syntax", "new": "matching"})
    if fault == "mixed": value["new_text"] = c.new_text
    if fault == "escape": value["target"] = "../SKILL.md"
    with pytest.raises(ValueError): materialize_candidate(value, library)
    assert (library.root/c.target).read_text(encoding="utf-8") == c.new_text


def test_edits_apply_against_original_base_not_inserted_text(context):
    *_, library = context
    c = candidate(); write_base(library, c)
    value = localized(c, c.new_text, [{"old": "Query syntax", "new": "Exact query syntax"},
        {"old": "## Executor notes\n", "new": "## Executor notes\nCheck applicability first.\n"}])
    updated = materialize_candidate(value, library)
    assert "Exact query syntax" in updated.new_text and "Check applicability first." in updated.new_text
    value["edits"].append({"old": "Check applicability first.", "new": "Changed"})
    with pytest.raises(ValueError, match="exactly once"): materialize_candidate(value, library)


def test_crlf_snapshot_exposes_exact_decoded_text_hash_for_local_edits(context, tmp_path):
    *_, library = context
    c = candidate(); write_base(library, c)
    (library.root/c.target).write_bytes(c.new_text.replace("\n", "\r\n").encode())
    frozen = LibrarySnapshot.freeze(library.root, tmp_path/"crlf-snapshot")
    page = frozen.read(c.target)
    assert page["hash"] != page["text_hash"] and page["text_hash"] == digest(c.new_text)
    value = localized(c, page["text"], [{"old": "Query syntax", "new": "Exact queries"}])
    assert "Exact queries" in materialize_candidate(value, library).new_text


@pytest.mark.asyncio
async def test_single_localized_candidate_reaches_complete_review_and_pending(context, tmp_path):
    settings, _, task, store, library = context
    c = candidate(); write_base(library, c)
    backend, _, _ = setup_backend(context, tmp_path)
    value = localized(c, c.new_text, [{"old": "Query syntax", "new": "Exact queries"},
        {"old": "version: 0.1.0", "new": "version: 0.1.1"}])
    backend.decisions = iter([{"decision": "propose", "candidate": value}])
    seen = []
    async def review(model, payload, budget):
        seen.append(copy.deepcopy(payload))
        return supported_review()
    backend.review_preflight = review
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["ok"] and result["eligible"] and len(seen) == 1
    assert "Exact queries" in seen[0]["candidate"]["new_text"] and "edits" not in seen[0]["candidate"]
    item = get_pending(result["pending_id"], root=library.root)
    assert item.meta["review"]["candidate_hash"] == digest(seen[0]["candidate"]["new_text"])
    assert (library.root/c.target).read_text(encoding="utf-8") == c.new_text


@pytest.mark.asyncio
async def test_revision_can_edit_current_draft_without_canonical_base(context, tmp_path):
    settings, _, task, store, library = context
    backend, _, c = setup_backend(context, tmp_path)
    value = localized(c, c.new_text, [{"old": "Query syntax", "new": "Exact queries"}])
    backend.decisions = iter([{"decision": "propose", "candidate": c.model_dump()},
        {"decision": "propose", "candidate": value}])
    seen = []
    async def review(model, payload, budget):
        seen.append(payload)
        r = supported_review()
        if len(seen) == 1:
            r.update(verdict="revise", changes=["Clarify description applicability"])
        return r
    backend.review_preflight = review
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["eligible"] and len(seen) == 2
    assert "Exact queries" in seen[1]["candidate"]["new_text"]
    assert not (library.root/c.target).exists()


def test_group_can_materialize_mixed_complete_and_localized_drafts(setup):
    library, group = setup.library, setup.group
    p = group.patches[0]
    local = localized(p, p.new_text, [{"old": "Scoped query escaping", "new": "Exact phrase queries"}])
    result = materialize_proposal({"patches": [local, group.patches[1].model_dump()],
        "dependencies": {group.patches[1].target: [p.target]}}, library, drafts=[p.model_dump()])
    assert isinstance(result, CandidateGroup) and "Exact phrase queries" in result.patches[0].new_text
    assert result.dependencies == {group.patches[1].target: [p.target]}


@pytest.mark.asyncio
async def test_group_readonly_repair_materializes_local_edits_before_review(setup):
    settings, task, store, library, backend, group = (
        setup.settings, setup.task, setup.store, setup.library, setup.backend, setup.group)
    p = group.patches[1]
    local = localized(p, p.new_text, [{"old": "Scoped query escaping", "new": "Exact phrase queries"}])
    async def diagnose(*args):
        return {"decision": "propose", "candidate": {"patches": [group.patches[0].model_dump(), local]}}
    backend.diagnose = diagnose
    seen = []
    async def review(model, system, payload, budget):
        current = CandidateGroup.model_validate(payload["candidate"])
        seen.append(current)
        result = group_review(current)
        if len(seen) == 1:
            result["patch_reviews"][p.target].update(verdict="revise", changes=["Clarify matching description"])
        return result
    backend.review = review
    # First proposal uses the normal outer host, second the existing group repair.
    count = 0
    async def diagnose_first(*args):
        nonlocal count
        count += 1
        return {"decision": "propose", "candidate": group.model_dump()} if count == 1 else await diagnose(*args)
    backend.diagnose = diagnose_first
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["eligible"] and len(seen) == 2
    assert "Exact phrase queries" in seen[1].patches[1].new_text


@pytest.mark.parametrize("kind", ["delivery_gap", "adoption_gap", "ineffective_rule", "remaining_blocker", "environment_fault"])
def test_concrete_diagnosis_needs_resolvable_native_evidence(kind):
    value = {"kind": kind, "reason": "Observed decision boundary", "evidence": []}
    with pytest.raises(ValueError, match="requires native evidence"): check_diagnosis(value)
    value["evidence"] = ["source/event:act@1", "source/observation:screen@1"]
    assert check_diagnosis(value, lambda ref: ("event" if "event:" in ref else "observation", {}, 0)) == value
    with pytest.raises(ValueError, match="native execution"):
        check_diagnosis(value, lambda ref: ("official_skill", {}, 0))


def test_unknown_diagnosis_does_not_invent_facts_and_survives_projection():
    value = {"kind": "unknown", "reason": "Post-action evidence absent", "evidence": []}
    review = {**supported_review(), "diagnosis": value}
    projected = retained_admission_context({"review_feedback": review}, {})
    assert projected["review_feedback"]["diagnosis"] == value


def test_behavioral_diagnosis_cannot_use_only_exposure_observation():
    with pytest.raises(ValueError, match="action and observation"):
        check_diagnosis({"kind": "remaining_blocker", "reason": "Rule supposedly worked",
            "evidence": ["observation:screen@1"]}, lambda ref: ("observation", {}, 0))


def test_diagnosis_rejects_blank_reason_and_non_native_resolver():
    with pytest.raises(ValueError, match="meaningful reason"):
        check_diagnosis({"kind": "unknown", "reason": "  ", "evidence": []})
    with pytest.raises(ValueError, match="native execution"):
        check_diagnosis({"kind": "delivery_gap", "reason": "Claimed routing gap",
            "evidence": ["event:act@1"]}, lambda ref: True)


@pytest.mark.parametrize("ref", ["validation.trials[0].candidate_execution.candidate_activation", "obs_33b3a329a96d4f1980e2f7aa20fc7221"])
def test_diagnosis_rejects_actual_native_bad_reference_shapes_with_specific_hint(ref):
    with pytest.raises(ValueError, match="Invalid diagnosis evidence") as error:
        check_diagnosis({"kind": "unknown", "reason": "Source passed, no failure diagnosis", "evidence": [ref]})
    assert ref in str(error.value) and "kind:key@version" in str(error.value)


def test_unresolved_diagnosis_identifies_exact_bad_reference():
    ref="validation_body_source_candidate/observation:wrong@3"
    def resolve(value): raise ValueError("Unknown or ambiguous observation reference")
    with pytest.raises(ValueError) as error:
        check_diagnosis({"kind": "unknown", "reason": "Missing observation", "evidence": [ref]}, resolve)
    assert ref in str(error.value) and "exact returned namespace" in str(error.value)


@pytest.mark.parametrize("mode", ["source_evidence", "measured_utility"])
def test_ineffective_scoped_rule_cannot_receive_passing_admission(mode):
    from agent.skills.admission import check_review_contract
    review = {**supported_review(), "acceptance": mode, "diagnosis": {
        "kind": "ineffective_rule", "reason": "Followed inside the claimed scope and failed",
        "evidence": ["event:act@1", "observation:screen@1"]}}
    with pytest.raises(ValueError, match="scoped rule as ineffective"):
        check_review_contract(review)
    review["verdict"] = "revise"
    check_review_contract(review)


def test_writing_projection_keeps_choice_actual_cost_and_uncertainty():
    from agent.skills.exploration import diagnostic_payload_projection
    comparison = select_strategy(analysis(), [])
    payload = {"source": {"instruction": "Synthetic goal"}, "candidate_json_schema": {},
        "review_feedback": {"verdict": "pass"}, "acquisition_ledger": {"recent_probes": [{
            "strategy_comparison": comparison, "outcome": "blocked", "probe_cost": {"calls": 2, "actions": 1}}]}}
    projected = diagnostic_payload_projection(payload)
    item = projected["acquisition_ledger"]["recent_probes"][0]
    assert item["strategy_comparison"]["selected"] == "a" and item["outcome"] == "blocked"
    assert item["probe_cost"] == {"calls": 2, "actions": 1}
    assert comparison["options"][0]["status"] == "hypothesis"  # Never promoted by projection.


def test_conversation_pairs_call_ids_per_turn_and_rejects_orphans(context):
    from agent.skills.analysis_session import LearnerConversation
    session = LearnerConversation(context[3].artifacts, {"task_id": context[2].id})
    prefix = [{"role": "system", "content": "Synthetic"}, {"role": "user", "content": "Phase"}]
    batch = [{"role": "assistant", "content": "", "tool_calls": [{"id": "r"}]},
        {"role": "tool", "tool_call_id": "r", "content": "{}"}]
    session.save([*prefix, *batch, *batch])
    with pytest.raises(ValueError, match="unpaired tool output"):
        session.save([*prefix, *batch, batch[1]])
    with pytest.raises(ValueError, match="unpaired tool call"):
        session.save([*prefix, batch[0], batch[0]])


@pytest.mark.asyncio
async def test_failed_task_keeps_local_admission_and_diagnosis_in_next_analysis(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, c = setup_backend(context, tmp_path)
    store.put("event", "act", {"step": 1, "observation_id": "screen"})
    backend.resolve_evidence = lambda ref: ("event" if "event:" in ref else "observation", {}, 0)
    checks = {"checks": ["source"], "reason": "Check original task breakthrough"}
    async def preflight(*args): return {**supported_review(), "verification": checks}
    async def final(*args): return {**supported_review(), "diagnosis": {
        "kind": "remaining_blocker", "reason": "The rule works locally; later coverage is unresolved",
        "evidence": ["event:act@1", "observation:screen@1"]}, "verification": {"checks": [], "reason": "Need a distinct hypothesis"}}
    backend.review_preflight, backend.review = preflight, final
    seen = []
    async def diagnose(model, system, payload, budget):
        seen.append(copy.deepcopy(payload))
        if len(seen) == 1: return {"decision": "propose", "candidate": c.model_dump()}
        assert payload["failure_diagnoses"][0]["kind"] == "remaining_blocker"
        assert payload["task_goal_feedback"]["retained_pending_ids"]
        return {"decision": "skip", "reason": "No further grounded route"}
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=Validator(frozen, tmp_path, {"source": (False, False)}), task_success_goal=True)
    assert result["eligible"] and result["task_goal"]["status"] == "unresolved" and len(seen) == 2


def analysis(selected="a", status="hypothesis"):
    return {"recovery_options": [{"id": key, "mechanism": mechanism,
        "bottleneck": "Expensive source step", "route": mechanism,
        "cost": "Complete cost unknown", "next_test": "Inspect transition " + key,
        "status": status if key == "a" else "hypothesis", "evidence": ["observation:screen@1"]}
        for key, mechanism in [("a", "Alternate interaction"), ("b", "Retain state across transitions")]],
        "strategy_choice": {"selected": selected, "reason": "Compare setup cost and earliest unknown with existing route"}}


def test_strategy_choice_keeps_uncertainty_and_rejects_wording_variants():
    value = analysis()
    validated = validate_analysis(value, lambda ref: ("observation", {}, 0))
    selected = select_strategy(validated, [])
    assert selected["selected"] == "a" and selected["options"][1]["status"] == "hypothesis"
    with pytest.raises(ValueError, match="already dispatched"):
        select_strategy(validated, [{"strategy_comparison": selected}])
    value["recovery_options"][1]["mechanism"] = value["recovery_options"][0]["mechanism"]
    with pytest.raises(ValueError, match="distinct mechanisms"): select_strategy(value, [])
    with pytest.raises(ValueError, match="contradicted"): select_strategy(analysis(status="contradicted"), [])


@pytest.mark.asyncio
async def test_host_compares_two_routes_and_returns_real_cost_without_forcing_matrix(context):
    from test_task_skill_learning import Backend
    settings, _, task, store, library = context
    backend = Backend([])
    plans = [analysis("a"), analysis("b", "unknown")]
    payloads, audits = [], []
    async def diagnose(model, system, payload, budget):
        payloads.append(copy.deepcopy(payload)); budget.meter("model_call_started", {})
        if len(payloads) <= 2:
            return {"decision": "explore", "question": "Inspect route " + str(len(payloads)),
                "hypothesis": "Alternative may remove the source bottleneck", "test": "One reversible transition",
                "max_actions": 1, "trajectory_analysis": plans[len(payloads)-1]}
        assert payload["acquisition_ledger"]["recent_probes"][0]["outcome"] == "blocked"
        assert payload["acquisition_ledger"]["recent_probes"][1]["strategy_comparison"]["selected"] == "b"
        return {"decision": "skip", "reason": "Access remains unknown; no supported skill"}
    async def audit(experiment, requested, budget, **kwargs):
        audits.append(copy.deepcopy(experiment)); budget.meter("model_call_started", {})
        return {"verdict": "run", "reasons": ["Bounded distinguishing transition"]}
    async def explore(question, requested, budget, **kwargs):
        budget.meter("model_call_started", {}); budget.action()
        return {"outcome": "blocked", "actions_attempted": 1, "learning_task_id": str(len(audits))}
    backend.diagnose, backend.review_probe, backend.explore = diagnose, audit, explore
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend,
        budget=LearningBudget(max_calls=10, reserve_calls=3))
    assert result["ok"] and result["skipped"] and len(audits) == 2
    assert result["cost"]["calls"] == 7 and result["cost"]["actions"] == 2
    rows = result["acquisition_ledger"]["recent_probes"]
    assert rows[0]["strategy_comparison"]["options"][0]["status"] == "hypothesis"
    assert rows[1]["strategy_comparison"]["options"][0]["status"] == "unknown"
    assert rows[0]["probe_cost"]["calls"] == 1


@pytest.mark.asyncio
async def test_missing_strategy_choice_repairs_before_device_dispatch(context):
    from test_task_skill_learning import Backend
    settings, _, task, store, library = context
    value = analysis(); value.pop("strategy_choice")
    backend = Backend([])
    seen = []
    async def diagnose(model, system, payload, budget):
        seen.append(payload); budget.meter("model_call_started", {})
        if len(seen) == 1:
            return {"decision": "explore", "question": "Inspect unknown", "max_actions": 1, "trajectory_analysis": value}
        assert "strategy_error" in payload
        return {"decision": "skip", "reason": "Cannot distinguish alternatives yet"}
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["ok"] and not backend.explored and len(seen) == 2
