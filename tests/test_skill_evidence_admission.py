"""Admission facts, immutable bindings and total-cost boundaries, without a device."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent.skills.admission import check_review_contract, source_validation
from agent.skills.exploration import build_review_contracts
from agent.skills.learning import LearningBudget, LearningStopped, digest, run_task_learning, validation_gate
from agent.skills.pending import approve_pending, get_pending
from agent.skills.snapshot import LibrarySnapshot
from evaluation.skill_learning import Experiment
from test_task_skill_learning import Backend, candidate, context, review, validation


def supported_review():
    return {**review(), "acceptance": "source_evidence"}


def setup_backend(context, tmp_path, verdict=None):
    settings, _, task, store, library = context
    library.root.mkdir(exist_ok=True)
    frozen = LibrarySnapshot.freeze(library.root, tmp_path / "frozen")
    c = candidate()
    backend = Backend([{"decision": "propose", "candidate": c.model_dump()}])
    backend.claim_admission = True
    backend.freeze_library = lambda: frozen
    backend.review_contracts = lambda draft: build_review_contracts(draft.new_text, library, settings)
    backend.review_evidence = lambda draft: [{"source": "source/observation:screen@1", "text": "Search", "truncated": False}]
    async def admission(*args): return copy.deepcopy(verdict or supported_review())
    backend.review_preflight = admission
    return backend, frozen, c


@pytest.mark.asyncio
async def test_failed_source_uses_one_independent_review_without_trials_and_stays_pending(context, tmp_path):
    settings, _, task, store, library = context
    backend, _, c = setup_backend(context, tmp_path)
    async def forbidden(*args, **kwargs): raise AssertionError("duplicate review or ordinary validation")
    backend.review = forbidden
    result = await run_task_learning(task, settings=settings, library=library, store=store,
                                    backend=backend, validator=forbidden)
    assert result["eligible"] and not backend.explored
    assert len(result["reviews"]) == 1
    item = get_pending(result["pending_id"], root=library.root)
    assert item.meta["review"]["validation"]["utility"] == "unmeasured"
    assert not (library.root / c.target).exists()
    assert approve_pending(item.id, root=library.root).read_text(encoding="utf-8") == c.new_text


@pytest.mark.asyncio
async def test_unresolved_local_gap_does_not_run_whole_matrix(context, tmp_path):
    settings, _, task, store, library = context
    r = supported_review()
    r.update(verdict="insufficient", tests=[{"claim": "quoted phrase works", "gap": "no resulting observation",
        "check": "read the linked post-action observation", "decision": "retain only if the expected result is observed"}])
    r["criteria"]["root_cause"]["status"] = "insufficient"
    backend, _, _ = setup_backend(context, tmp_path, r)
    backend.decisions = iter([{"decision": "propose", "candidate": candidate().model_dump()}, {"decision": "skip", "reason": "missing record"}])
    async def forbidden(*args, **kwargs): raise AssertionError("local gap is not a matrix request")
    result = await run_task_learning(task, settings=settings, library=library, store=store,
                                    backend=backend, validator=forbidden)
    assert result.get("eligible") is not True
    assert not backend.explored


@pytest.mark.parametrize("edit", ["body", "benefit", "evidence", "review", "contract", "library", "notes_only", "measured"])
def test_source_receipt_is_bound_and_cannot_claim_measured_utility(context, tmp_path, edit):
    _, _, task, _, _ = context
    backend, frozen, c = setup_backend(context, tmp_path)
    r = supported_review()
    contracts = backend.review_contracts(c)
    v = source_validation(c, "", review=r, evidence=backend.review_evidence(c), source_id=task.id,
                          contracts=contracts, manifest=frozen.manifest)
    kwargs = dict(baseline_manifest=frozen.manifest, review=r, contracts_hash=digest(contracts))
    assert validation_gate(c, digest(""), v, **kwargs) == (True, "source_evidence_reviewed")
    if edit == "body": c.new_text += "New route."
    elif edit == "benefit": c.benefit = "Proven 90% faster on all tasks"
    elif edit == "evidence": v["evidence"][0]["text"] = "invented"
    elif edit == "review": r["criteria"]["root_cause"]["reason"] = "changed"
    elif edit == "contract": kwargs["contracts_hash"] = "changed"
    elif edit == "library": kwargs["baseline_manifest"] = {**frozen.manifest, "hash": "changed"}
    elif edit == "notes_only":
        v["evidence"][0]["source"] = "source/note:summary@1"
        v["evidence_hash"] = digest(v["evidence"])
    elif edit == "measured": v["utility"] = "measured"
    assert not validation_gate(c, digest(""), v, **kwargs)[0]
    assert not validation_gate(c, digest(""), v, baseline_manifest=frozen.manifest)[0]


@pytest.mark.parametrize("bad", [
    {"acceptance": "source_evidence", "tests": ["run all guards"]},
    {"acceptance": "source_evidence", "tests": [{"claim": "x", "gap": "y", "check": "z", "decision": ""}]},
    {"acceptance": "unknown"},
    {"acceptance": "source_evidence", "changes": ["repair scope"]},
])
def test_review_contract_rejects_generic_tests_or_pass_with_blockers(bad):
    with pytest.raises(ValueError): check_review_contract({**supported_review(), **bad})


@pytest.mark.asyncio
async def test_measured_claim_cannot_use_source_gate_and_validator_gets_parent_budget(context, tmp_path):
    settings, _, task, store, library = context
    r = {**supported_review(), "acceptance": "measured_utility", "verdict": "insufficient",
        "tests": [{"claim": "faster whole task", "gap": "no comparator", "check": "matched source",
                   "decision": "positive gain with preserved goal supports the claim"}]}
    r["criteria"]["marginal_value"]["status"] = "insufficient"
    backend, frozen, c = setup_backend(context, tmp_path, r)
    budget = LearningBudget()
    seen = []
    async def validate(actual, old, *, budget):
        seen.append(budget)
        return None
    async def final(*args): return {**r, "tests": [], "changes": []}
    backend.review = final
    result = await run_task_learning(task, settings=settings, library=library, store=store,
                                    backend=backend, validator=validate, budget=budget)
    assert seen == [budget] and not result["eligible"]


def test_child_allocations_share_parent_caps_and_failed_receipts_are_not_free():
    b = LearningBudget(max_calls=20, reserve_calls=4, max_actions=10, cleanup_actions=2)
    b.meter("model_call_started", {})
    first = b.reserve_validation("one", calls=10, actions=4)
    with pytest.raises(LearningStopped, match="already_running"):
        b.reserve_validation("parallel", calls=1, actions=1)
    b.settle_validation(first, {"role_invocations": 3, "logical_action_steps": 2, "elapsed_s": 1})
    assert b.calls == 4 and b.actions == 2
    second = b.reserve_validation("failed", calls=10, actions=4)
    b.settle_validation(second, None)
    assert b.calls == 14 and b.actions == 6
    assert b.snapshot()["calls_include_reserved_upper_bounds"]
    with pytest.raises(LearningStopped, match="validation_model_budget"):
        b.reserve_validation("no new allowance", calls=3, actions=1)
    with pytest.raises(ValueError): b.settle_validation(second, {})


@pytest.mark.asyncio
async def test_evaluator_enforces_parent_allocation_before_launch_and_counts_failure(tmp_path):
    ex = Experiment.__new__(Experiment)
    ex.root = tmp_path
    ex.args = SimpleNamespace(rounds=2, seconds=100)
    ex.validation_budget = LearningBudget(max_calls=16, reserve_calls=4, max_actions=6)
    visits = []
    async def worker(name, *args):
        visits.append(name)
        assert ex.validation_budget.calls == 12
        raise RuntimeError("worker crashed without receipt")
    ex._run_episode_async = worker
    with pytest.raises(RuntimeError, match="crashed"):
        await ex._episode_async("first", "case", tmp_path)
    with pytest.raises(LearningStopped, match="validation_model_budget"):
        await ex._episode_async("second", "case", tmp_path)
    assert visits == ["first"]
    assert json.loads((tmp_path / "validation-cost.json").read_text())["calls"] == 12


def test_validation_time_never_gets_a_hidden_multiplier(monkeypatch):
    b = LearningBudget(max_seconds=10, started=100, job_started=100)
    monkeypatch.setattr("agent.skills.learning.time.monotonic", lambda: 111)
    with pytest.raises(LearningStopped, match="time_budget"): b.check()


@pytest.mark.asyncio
async def test_short_second_read_is_not_deferred_for_its_maximum_possible_size(context, monkeypatch):
    from test_learner_conversation import backend
    from shared.llm_gateway import GatewayResponse, ToolCall
    b = backend(context)
    context[3].put("note", "large", {"title": "large", "content": "a" * 6400})
    context[3].put("note", "small", {"title": "small", "content": "distinct necessary prerequisite"})
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages)
        kwargs["attempt_meter"]("model_call_started", {})
        if len(calls) == 1:
            return GatewayResponse(model=model, content="", tool_calls=[
                ToolCall(id=key, name="read_history", arguments=json.dumps({"source": f"note:{key}@1", "full": True}))
                for key in ("large", "small")])
        results = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
        assert len(results) == 2
        assert not any(r.get("status") == "deferred" for r in results)
        assert "distinct necessary prerequisite" in json.dumps(results)
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No independent rule"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget(max_calls=5, reserve_calls=1)
    await b.diagnose("test", "read two facts", {}, budget)
    assert budget.calls == 2 and not b.learner_conversation.compactions


@pytest.mark.asyncio
async def test_insufficient_time_does_not_shrink_one_side_or_launch_a_child(tmp_path):
    ex = Experiment.__new__(Experiment)
    ex.root = tmp_path
    ex.args = SimpleNamespace(rounds=2, seconds=100)
    ex.validation_budget = LearningBudget(max_calls=100, max_actions=50, max_seconds=90)
    async def forbidden(*args): raise AssertionError("must not launch")
    ex._run_episode_async = forbidden
    with pytest.raises(LearningStopped, match="full_episode_time"):
        await ex._episode_async("shortened", "case", tmp_path)
    assert ex.validation_budget.calls == ex.validation_budget.actions == 0
    assert not ex.validation_budget.validation_children


@pytest.mark.asyncio
async def test_source_reclassification_cannot_erase_confirmed_regression(context, tmp_path):
    settings, _, task, store, library = context
    r = {**supported_review(), "acceptance": "measured_utility", "verdict": "insufficient",
        "tests": [{"claim": "net gain", "gap": "no comparison", "check": "matched outcomes",
                   "decision": "reject a regression"}]}
    r["criteria"]["marginal_value"]["status"] = "insufficient"
    backend, frozen, c = setup_backend(context, tmp_path, r)
    async def validate(actual, old, *, budget):
        result = validation(actual, old)
        result["trials"][2]["candidate_success"] = False
        return result
    async def wrong_reclassification(*args): return supported_review()
    backend.review = wrong_reclassification
    result = await run_task_learning(task, settings=settings, library=library, store=store,
                                    backend=backend, validator=validate)
    item = get_pending(result["pending_id"], root=library.root)
    assert not result["eligible"]
    assert item.meta["review"]["gate_reason"] == "confirmed_regression"
    assert item.meta["review"]["validation"]["trials"][2]["candidate_success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["review", "source", "body"])
async def test_pending_source_admission_rechecks_review_task_and_body(context, tmp_path, field):
    settings, _, task, store, library = context
    backend, _, _ = setup_backend(context, tmp_path)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    item = get_pending(result["pending_id"], root=library.root)
    meta = json.loads(item.path.read_text(encoding="utf-8"))
    if field == "review": meta["review"]["review"]["criteria"]["root_cause"]["reason"] = "different review"
    elif field == "source": meta["source_task_id"] = "other-task"
    elif field == "body":
        body = item.path.with_suffix(".md")
        body.write_text(body.read_text(encoding="utf-8") + "Changed claim", encoding="utf-8")
    item.path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError): approve_pending(item.id, root=library.root)


def test_child_overrun_reports_actual_consumption_and_stops():
    b = LearningBudget(max_calls=20, reserve_calls=2, max_actions=10)
    child = b.reserve_validation("broken worker", calls=4, actions=3)
    with pytest.raises(LearningStopped, match="exceeded_allocation"):
        b.settle_validation(child, {"role_invocations": 7, "logical_action_steps": 5})
    assert b.calls == 7 and b.actions == 5
    assert child["status"] == "overran_allocation"
    assert not b.snapshot()["calls_include_reserved_upper_bounds"]


@pytest.mark.asyncio
async def test_quarantined_validation_does_not_charge_a_never_launched_child(tmp_path):
    ex = Experiment.__new__(Experiment)
    ex.root = tmp_path
    ex.validation_quarantined = True
    ex.validation_budget = LearningBudget()
    with pytest.raises(RuntimeError, match="quarantined"):
        await ex._episode_async("blocked", "case", tmp_path)
    assert ex.validation_budget.calls == 0 and not ex.validation_budget.validation_children
