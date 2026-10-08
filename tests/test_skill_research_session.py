"""Owned research continuity never grants publication or extra budget."""
import asyncio
import pytest
from agent.skills.learning import LearningBudget, run_task_learning
from agent.skills.exploration import ExplorationBackend
from agent.skills.environment import ResearchEnvironment
from test_task_skill_learning import context, Backend, candidate, review, validation
from test_skill_incremental_environment import Store
from test_skill_acquisition_repairs import adapter


def probe(question):
    return {"decision":"explore", "question":question, "max_actions":1}


class SessionBackend(Backend):
    def __init__(self, decisions, *, retain=True, restore=None):
        super().__init__(decisions)
        self.retain_probe_state = retain
        self.fixture_reset_adapter = adapter(restore)
        self.environment = ResearchEnvironment(Store(), reset_adapter=self.fixture_reset_adapter)
        self.environment.configure({"mode":"isolated", "scope":"owned fixture", "expected_condition":"probe",
            "cleanup":"trusted adapter", "cleanup_actions":1})
        self.environment_transition = None
        self.release_count = 0
        self.prior_effects = []
    async def release_device(self):
        self.release_count += 1
    complete_probe = ExplorationBackend.complete_probe
    async def diagnose(self, model, system, payload, budget):
        budget.meter("model_call_started", {})
        return next(self.decisions)
    async def explore(self, question, max_actions, budget, *, context=None):
        self.prior_effects.append(self.environment.receipt()["unresolved_effects"])
        budget.cleanup_actions = 1
        budget.action()
        intent = self.environment.intent({"type":"tap","index":1}, "fresh-current-observation")
        self.environment.effect(intent, {"success":True, "receipt":{"dispatch_succeeded":True}})
        self.explored.append(question)
        return {"outcome":"finish", "actions_attempted":1, "question":question}


@pytest.mark.asyncio
async def test_continuity_requires_operator_reset_before_any_request(context):
    settings, _, task, store, library = context
    backend = SessionBackend([])
    backend.fixture_reset_adapter = None
    budget = LearningBudget()
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, budget=budget)
    assert result["reason"] == "continuous_research_requires_owned_fixture"
    assert budget.calls == budget.actions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("retain", [False, True])
async def test_two_probes_retain_live_effects_only_in_owned_session_and_clean_on_exit(context, retain):
    settings, _, task, store, library = context
    backend = SessionBackend([probe("First boundary"), probe("Follow-up boundary"),
        {"decision":"skip","reason":"insufficient value"}, {"decision":"skip","reason":"still insufficient"}], retain=retain)
    budget = LearningBudget(max_calls=24, max_actions=5)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, budget=budget)
    assert result["skipped"]
    assert backend.prior_effects == ([0, 1] if retain else [0, 0])
    assert backend.release_count == (1 if retain else 2)
    assert budget.actions == (3 if retain else 4)
    assert result["cost"]["actions"] == budget.actions
    assert backend.environment.receipt()["verified_clean"]
    if retain:
        assert result["environment"]["status"] == "closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("new_evidence", [False, True])
async def test_second_repair_requires_new_completed_evidence_and_total_stays_two(context, new_evidence):
    settings, _, task, store, library = context
    decisions = [probe("A"), probe("A repaired"), probe("B"), probe("B repaired"), probe("C")]
    backend = SessionBackend(decisions)
    verdicts = iter(["revise", "run", "revise", "run", "revise"] if new_evidence else ["revise", "revise"])
    async def audit(experiment, requested, budget, *, context):
        budget.meter("model_call_started", {})
        return {"verdict":next(verdicts), "reasons":["Missing discrimination"]}
    backend.review_probe = audit
    budget = LearningBudget(max_calls=24, max_actions=5)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, budget=budget)
    assert result["reason"] == "probe_review_stopped"
    assert result["acquisition_ledger"]["probe_repairs"] == (2 if new_evidence else 1)
    assert len(backend.explored) == (2 if new_evidence else 0)
    assert backend.environment.receipt()["verified_clean"]
    assert budget.max_calls == 24 and budget.max_actions == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_candidate_adjudication_requires_actual_cleanup(context, monkeypatch, failure):
    settings, _, task, store, library = context
    c = candidate()
    backend = SessionBackend([probe("Boundary"), {"decision":"propose","candidate":c.model_dump()}],
        restore={"teardown_success":False} if failure else None)
    visited = []
    def contracts(c):
        assert backend.environment.receipt()["verified_clean"]
        visited.append("contracts")
        return {"prompt":"test"}
    async def validate(c, old):
        assert backend.environment.receipt()["verified_clean"]
        visited.append("validation")
        return validation(c, old)
    async def ask(*args):
        return review("reject")
    backend.review_contracts = contracts
    monkeypatch.setattr(LearningBudget, "ask", ask)
    budget = LearningBudget(max_calls=16, max_actions=4)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget, validator=validate)
    if failure:
        assert not visited and result["reason"] == "research_cleanup_unverified"
        assert not result["environment"]["verified_clean"] and result.get("pending_id") is None
    else:
        assert visited == ["contracts", "validation"]
        assert result["environment"]["verified_clean"] and not result["eligible"]
    assert not (library.root / c.target).exists()


@pytest.mark.asyncio
async def test_unexpected_failure_still_cleans_owned_session(context):
    settings, _, task, store, library = context
    backend = SessionBackend([probe("Boundary")])
    with pytest.raises((RuntimeError, StopIteration)):
        await run_task_learning(task, settings=settings, library=library, store=store,
            backend=backend, budget=LearningBudget(max_actions=3))
    assert backend.release_count == 1 and backend.environment.receipt()["verified_clean"]


@pytest.mark.asyncio
async def test_explicit_cleanup_ends_retained_probe_before_next_diagnosis(context):
    settings, _, task, store, library = context
    backend = SessionBackend([probe("Boundary"), {"decision":"skip", "reason":"no useful rule"},
        {"decision":"skip", "reason":"still no useful rule"}])
    original_explore, original_diagnose = backend.explore, backend.diagnose
    observed = []
    async def explore(*args, **kwargs):
        value = await original_explore(*args, **kwargs)
        backend.environment.set_stage("cleanup")
        args[2].set_phase("cleanup")
        return {**value, "environment":backend.environment.receipt()}
    async def diagnose(model, system, payload, budget):
        if backend.explored:
            observed.append(payload)
            assert backend.environment.receipt()["verified_clean"]
            assert budget.phase == "probe"
            assert payload["evidence"]["environment_restoration"]["verified_clean"]
            assert payload["evidence"]["environment"]["unresolved_effects"] == 0
        return await original_diagnose(model, system, payload, budget)
    backend.explore, backend.diagnose = explore, diagnose
    budget = LearningBudget(max_calls=24, max_actions=3)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget)
    assert result["skipped"] and observed
    assert backend.release_count == 1 and budget.actions == 2
    assert result["environment"]["verified_clean"]
