"""Learning acceptance boundaries; no provider/device required."""
import json
import asyncio
from types import SimpleNamespace

import pytest

from agent.revisable.store import TaskStore
from agent.skills.learning import (CRITERIA, Candidate, LearningBudget, LearningStopped,
    build_packet, digest, review_gate, run_task_learning, validate_candidate, validation_gate)
from agent.skills.library import SkillLibrary, serialize_skill_markdown
from agent.skills.pending import approve_pending, get_pending, stage_pending_patch
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus


@pytest.fixture
def context(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    task = db.create_task("Find recipe directions containing a hyphenated ingredient", AgentState(instruction="Find recipe"))
    db.update_task(task.id, status=TaskStatus.FAILED, failure_reason="search failed")
    task = db.get_task(task.id)
    store = TaskStore(db, artifacts, task.id)
    store.put("observation", "screen", {"app": "com.demo", "text": "Search", "step": 1,
        "stage_ids": [], "visits": [], "image_ref": None})
    library = SkillLibrary(tmp_path / "skills")
    yield settings, db, task, store, library
    db.close()


def candidate():
    return Candidate(target="apps/com.demo/core/SKILL.md", new_text=serialize_skill_markdown(
        name="demo-core", description="Query syntax", version="0.1.0", app="com.demo", kind="app_core",
        body="## Executor notes\nHyphens in search terms require a quoted phrase. Verify returned detail.\n"),
        gist="Document query syntax", evidence=["observation:screen@1"],
        scope="Quoted hyphenated search", benefit="Avoid unmatched search", risks="Verify scope")


def review(verdict="pass"):
    return {"verdict": verdict, "criteria": {k: {"status": "pass", "reason": "test evidence"} for k in CRITERIA},
        "changes": [], "tests": []}


def validation(c, base=""):
    return {"candidate_hash": digest(c.new_text), "base_hash": digest(base), "trials": [
        {"case_id": kind, "kind": kind, "matched_environment": True, "independent_oracle": True,
         "config_hash": "same-model-reset-goal", "baseline_success": kind != "source",
         "candidate_success": True, "efficiency_gain": 0}
        for kind in ("source", "variant", "near_miss", "related_normal")]}


def test_packet_bounded_without_semantic_detour(context):
    _, db, task, store, _ = context
    for i in range(90):
        store.put("event", str(i), {"step": i, "decision": "act", "executor_report": "x" * 2000,
            "submitted_action": {"type": "tap", "index": 1},
            "action_result": {"success": True, "receipt": {"visible_change": False}}})
    packet = build_packet(task, store)
    assert len(json.dumps(packet, ensure_ascii=False)) <= 12000
    assert packet["omitted_events"] > 80
    assert "repeated_action_without_visible_change" in packet["signals"]
    assert store.records("event")[0]["payload"]["executor_report"] == "x" * 2000


def test_budget_counts_actual_provider_requests_and_reserves_validation():
    budget = LearningBudget(max_calls=3, reserve_calls=1, max_actions=1)
    budget.meter("model_call_started", {})
    budget.action()
    with pytest.raises(LearningStopped, match="action_budget"):
        budget.action()
    budget.meter("model_call_started", {})
    with pytest.raises(LearningStopped, match="reserved"):
        budget.check(exploration=True)
    budget.meter("model_call_started", {})
    with pytest.raises(LearningStopped, match="model_budget"):
        budget.meter("model_call_started", {})


def test_reviewer_cannot_average_missing_or_failed_criterion():
    result = review()
    assert review_gate(result)
    result["criteria"]["regression_risk"]["status"] = "fail"
    assert not review_gate(result)
    result["criteria"].pop("regression_risk")
    assert not review_gate(result)


def test_validation_requires_transfer_and_no_regression():
    c = candidate()
    v = validation(c)
    assert validation_gate(c, digest(""), v)[0]
    v["trials"][2]["candidate_success"] = False
    assert validation_gate(c, digest(""), v) == (False, "confirmed_regression")
    v = validation(c); v["trials"].pop()
    assert validation_gate(c, digest(""), v) == (False, "incomplete_coverage")
    c.new_text += "Edited"
    assert validation_gate(c, digest(""), validation(candidate())) == (False, "stale_validation")


def test_candidate_metadata_preserved_and_app_grounded(context):
    *_, library = context
    c = candidate()
    assert validate_candidate(c, library, {"com.demo"}) == ""
    with pytest.raises(ValueError, match="not grounded"):
        validate_candidate(c, library, {"other.app"})
    p = library.root / c.target; p.parent.mkdir(parents=True); p.write_text(c.new_text, encoding="utf-8")
    c.new_text = c.new_text.replace("demo-core", "different")
    with pytest.raises(ValueError, match="metadata changed"):
        validate_candidate(c, library, {"com.demo"})


class Backend:
    def __init__(self, decisions): self.decisions = iter(decisions); self.explored = []
    async def diagnose(self, *args): return next(self.decisions)
    async def explore(self, question, max_actions, budget, *, context=None):
        budget.action(); self.explored.append(question); return {"fact": "observed query syntax"}
    def observed_apps(self): return {"com.demo"}
    def resolve_evidence(self, ref): return True
    def review_contracts(self, c): return {"prompt": "test"}
    def review_evidence(self, c): return []


@pytest.mark.asyncio
async def test_closed_loop_explores_reviews_and_stays_pending(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    backend = Backend([{"decision": "explore", "question": "Does quoting a hyphen work?", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()}])
    async def ask(*args): return review()
    monkeypatch.setattr(LearningBudget, "ask", ask)
    from agent.skills.exploration import build_review_contracts
    backend.review_contracts = lambda c: build_review_contracts(c.new_text, library, settings)
    async def validate(c, old): return validation(c, old)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert result["eligible"] and backend.explored
    assert not (library.root / c.target).exists()
    approve_pending(result["pending_id"], root=library.root)
    assert (library.root / c.target).exists()


@pytest.mark.asyncio
async def test_efficient_success_has_zero_calls(context):
    settings, db, task, store, library = context
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    backend = Backend([])
    result = await run_task_learning(db.get_task(task.id), settings=settings, library=library,
        store=store, backend=backend)
    assert result["reason"] == "no_candidate_signal"


@pytest.mark.asyncio
async def test_revision_loop_capped_at_three_reviews(context, monkeypatch):
    settings, _, task, store, library = context
    candidates = []
    for i in range(4):
        c = candidate(); c.new_text += f"Fact variant {i}\n"
        candidates.append({"decision": "propose", "candidate": c.model_dump()})
    backend = Backend([{"decision": "explore", "question": "Check query syntax", "max_actions": 1}, *candidates])
    async def ask(*args): return review("revise")
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert len(result["reviews"]) == 3 and not result["eligible"]


def test_pending_rejects_stale_body_base_and_unvalidated_pass(context):
    *_, library = context
    c = candidate()
    from agent.skills.exploration import build_review_contracts
    def stage(v):
        return stage_pending_patch(target_rel=c.target, new_text=c.new_text, gist=c.gist, root=library.root,
            review_metadata={"experiment": "task_explore", "candidate_hash": digest(c.new_text),
                "base_hash": digest(""), "review": review(), "validation": v,
                "contracts_hash": digest(build_review_contracts(c.new_text, library, Settings(_env_file=None)))})
    pending = stage(None)
    with pytest.raises(ValueError, match="missing_validation"):
        approve_pending(pending.id, root=library.root)
    pending = stage(validation(c))
    body = pending.path.with_suffix(".md"); body.write_text(c.new_text + "edit\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since review"):
        approve_pending(pending.id, root=library.root)
    pending = stage(validation(c))
    p = library.root / c.target; p.parent.mkdir(parents=True); p.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="base changed"):
        approve_pending(pending.id, root=library.root)


@pytest.mark.asyncio
async def test_learner_device_phase_uses_grounded_receipts_and_preserves_source(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    driver = FixtureDriver()
    original = task.model_dump()
    calls = []
    async def complete(model, messages, *, tools, **kwargs):
        meter = kwargs.get("attempt_meter")
        if meter: meter("model_call_started", {})
        anchor = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        decision = {"decision": "act" if not calls else "finish", "summary": "Test current home affordance",
                    "observation_id": anchor["current_observation_id"]}
        if not calls: decision["action"] = {"type": "home"}
        calls.append(decision)
        return GatewayResponse(content="", model=model,
            tool_calls=[ToolCall(id=str(len(calls)), name="submit_executor_step", arguments=json.dumps(decision))])
    monkeypatch.setattr("agent.session.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=driver,
        settings=settings, library=library)
    budget = LearningBudget(max_calls=10, reserve_calls=2)
    try:
        reset = {"fixture_reset":True, "generation":7, "source_case":"Synthetic"}
        result = await backend.explore("Test the app home navigation return state", 2, budget,
            context={"environment_transition":reset})
        assert backend.environment_transition["fixture_reset"] is False
        assert backend.environment_transition["generation"] == 7
        assert backend.environment_transition["last_probe_actions"] == 1
        assert reset["fixture_reset"] is True
        result2 = await backend.explore("Inspect the same lease", 1, budget,
            context={"environment_transition":backend.environment_transition})
        assert backend.environment_transition["fixture_reset"] is False
        assert result2["actions_attempted"] == 0
        assert len(driver.actions) == 1 and budget.actions == 1
        assert result["events"]["events"][0]["action_result"]["receipt"]
        assert all(r["source"].startswith("exploration/") for r in result["events"]["events"])
        assert db.get_task(task.id).model_dump() == original
    finally:
        backend.close()
    assert db.get_task(backend.record.id).status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancelled_learning_never_dispatches_after_model_reply(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    driver = FixtureDriver(); cancelled = False
    async def complete(model, messages, *, tools, **kwargs):
        nonlocal cancelled
        anchor = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        cancelled = True
        return GatewayResponse(content="", model=model, tool_calls=[ToolCall(id="1", name="submit_executor_step",
            arguments=json.dumps({"decision": "act", "summary": "home", "action": {"type": "home"},
                "observation_id": anchor["current_observation_id"]}))])
    monkeypatch.setattr("agent.session.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=driver,
        settings=settings, library=library, cancelled=lambda: cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await backend.explore("Test home", 2, LearningBudget(cancelled=lambda: cancelled))
        assert driver.actions == []
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_diagnosis_reads_bounded_original_history(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    requested = []
    async def complete(model, messages, **kwargs):
        requested.append(messages)
        if len(requested) == 1:
            return GatewayResponse(content="", model=model, tool_calls=[ToolCall(id="history", name="read_history",
                arguments=json.dumps({"source": "observation:screen@1"}))])
        assert any(m.get("role") == "tool" and "Search" in m["content"] for m in messages)
        return GatewayResponse(content='{"decision":"skip","reason":"ordinary affordance"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    result = await backend.diagnose("test", "learn", {}, LearningBudget())
    assert result["decision"] == "skip" and backend.record is None


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [False, True])
@pytest.mark.parametrize("gate", ["pass", "review_reject", "missing_validation", "regression"])
async def test_source_only_proposal_needs_no_probe_but_keeps_acceptance_gates(context, monkeypatch, succeeded, gate):
    settings, db, task, store, library = context
    if succeeded:
        db.update_task(task.id, status=TaskStatus.SUCCEEDED)
        task = db.get_task(task.id)
        for step in (1, 2):
            store.put("event", str(step), {"step": step, "decision": "replan"})
    before = store.records("event")
    c = candidate()
    backend = Backend([{"decision": "propose", "candidate": c.model_dump()}])
    calls = []
    async def ask(*args):
        calls.append("review")
        return review("reject" if gate == "review_reject" else "pass")
    async def validate(actual, old):
        calls.append("validation")
        result = validation(actual, old)
        if gate == "regression":
            result["trials"][2]["candidate_success"] = False
        return result
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=None if gate == "missing_validation" else validate)
    assert result["ok"] and result["pending_id"]
    assert result["eligible"] == (gate == "pass")
    assert calls == (["review"] if gate == "missing_validation" else ["validation", "review"])
    assert not backend.explored and result["cost"]["actions"] == 0
    assert store.records("event") == before
    assert not (library.root / c.target).exists()


def test_packet_observations_and_metadata_cannot_overflow(context):
    _, _, task, store, _ = context
    for i in range(50):
        store.put("stage", str(i), {"goal": "x" * 2000})
    for i in range(5):
        store.put("observation", "large" + str(i), {"app": "com.demo", "text": "y" * 2000})
    packet = build_packet(task, store, character_budget=2500)
    assert len(json.dumps(packet, ensure_ascii=False)) <= 2500
    assert packet["observations"] and packet["omitted_stages"]


@pytest.mark.asyncio
async def test_orchestrator_learning_opt_in_busy_and_duplicate(context):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    settings, db, task, store, _ = context
    settings = settings.model_copy(update={"skill_learning_mode": "task_explore"})
    orch = create_orchestrator(db, TraceWriter(db, store.artifacts), settings=settings,
        driver=FixtureDriver(), artifacts=store.artifacts)
    assert (await orch.learn_from_task(task.id))["reason"] == "explicit_personal_request_required"
    async with orch._device_lock:
        assert (await orch.learn_from_task(task.id, request={"accept_model_cost": True, "allow_device_operations": True}))["reason"] == "device_busy"
    orch._learning_jobs.add(task.id)
    assert (await orch.learn_from_task(task.id, request={"accept_model_cost": True, "allow_device_operations": True}))["reason"] == "learning_already_running"


def test_publication_rechecks_related_contracts(context):
    settings, _, _, _, library = context
    from agent.skills.exploration import build_review_contracts
    c = candidate()
    pending = stage_pending_patch(target_rel=c.target, new_text=c.new_text, gist=c.gist, root=library.root,
        review_metadata={"experiment": "task_explore", "candidate_hash": digest(c.new_text),
            "base_hash": digest(""), "review": review(), "validation": validation(c),
            "contracts_hash": digest(build_review_contracts(c.new_text, library, settings))})
    related = library.root / 'generic' / 'new-rule' / 'SKILL.md'
    related.parent.mkdir(parents=True)
    related.write_text(serialize_skill_markdown(name='new-rule', description='new contract',
        version='1', kind='generic', body='A newly introduced rule.'), encoding='utf-8')
    with pytest.raises(ValueError, match='related skills/prompts/schemas changed'):
        approve_pending(pending.id, root=library.root)


@pytest.mark.asyncio
async def test_cancel_after_diagnosis_does_not_begin_exploration(context):
    settings, _, task, store, library = context
    cancelled = False
    backend = Backend([])
    async def diagnose(*args):
        nonlocal cancelled
        cancelled = True
        return {"decision": "explore", "question": "inspect search", "max_actions": 2}
    backend.diagnose = diagnose
    with pytest.raises(asyncio.CancelledError):
        await run_task_learning(task, settings=settings, library=library, store=store,
            backend=backend, budget=LearningBudget(cancelled=lambda: cancelled))
    assert not backend.explored


@pytest.mark.asyncio
async def test_cleanup_failure_still_clears_learning_job(context, monkeypatch):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    orch = create_orchestrator(db, TraceWriter(db, store.artifacts),
        settings=settings.model_copy(update={"skill_learning_mode": "task_explore"}),
        driver=FixtureDriver(), artifacts=store.artifacts)
    closed = []
    class BackendWithFailedRelease:
        def __init__(self, *args, **kwargs):
            self.source_store = store; self.library = library
        async def release_device(self): raise RuntimeError("lease release failed")
        def close(self): closed.append(True)
    async def learn(*args, **kwargs): return {"ok": True}
    monkeypatch.setattr("agent.skills.exploration.ExplorationBackend", BackendWithFailedRelease)
    monkeypatch.setattr("agent.skills.learning.run_task_learning", learn)
    with pytest.raises(RuntimeError, match="lease release failed"):
        await orch.learn_from_task(task.id, request={"accept_model_cost": True, "allow_device_operations": True})
    assert closed == [True] and not orch._learning_jobs and not orch._device_lock.locked()




@pytest.mark.asyncio
@pytest.mark.parametrize("repairs_successfully", [True, False])
async def test_candidate_contract_repair_is_bounded_and_preserves_review(context, monkeypatch, repairs_successfully):
    settings, _, task, store, library = context
    c = candidate()
    invalid = c.model_dump(); invalid["risks"] = ["must be a string"]
    decisions = [
        {"decision": "explore", "question": "Check quoted search", "max_actions": 1},
        {"decision": "propose", "candidate": invalid},
        {"decision": "propose", "candidate": c.model_dump() if repairs_successfully else invalid},
    ]
    backend = Backend(decisions)
    payloads = []
    original = backend.diagnose
    async def diagnose(model, system, payload, budget):
        budget.meter("model_call_started", {})
        payloads.append(payload)
        return await original(model, system, payload, budget)
    backend.diagnose = diagnose
    async def ask(*args): return review("reject")
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert len(payloads) == 3 and result["cost"]["calls"] == 3
    from agent.skills.candidate_group import candidate_schema
    assert payloads[0]["candidate_json_schema"] == candidate_schema()
    assert payloads[1]["candidate_json_schema"]["$defs"]["Candidate"]["properties"]["risks"]["type"] == "string"
    assert "candidate_error" in payloads[-1]
    assert len(result["reviews"]) == int(repairs_successfully)
    assert not (library.root / c.target).exists()
    if repairs_successfully:
        assert not result["eligible"]
    else:
        assert not result["ok"] and "validation error" in result["reason"]


def test_packet_exposes_bounded_literal_notes_with_omissions(context):
    _, _, task, store, _ = context
    for i in range(40):
        store.put("note", f"coverage-{i}", {"retained": "checked some occurrences " * 100})
    packet = build_packet(task, store)
    assert 0 < len(packet["notes"]) <= 8
    assert packet["omitted_notes"] == 40 - len(packet["notes"])
    assert packet["notes"][0]["source"].startswith("note:")
    assert len(json.dumps(packet, ensure_ascii=False)) <= 12000


@pytest.mark.asyncio
async def test_probe_reads_source_history_but_cannot_ground_action_with_it(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    source_before = store.records("observation")
    task_before = db.get_task(task.id).model_dump()
    driver = FixtureDriver()
    calls = 0
    async def complete(model, messages, *, tools, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs.get("attempt_meter"):
            kwargs["attempt_meter"]("model_call_started", {})
        names = {t["function"]["name"] for t in tools}
        assert "read_source_history" in names
        assert "mechanism to test" not in messages[0]["content"]
        assert any("mechanism to test" in str(m.get("content")) for m in messages[1:])
        anchor = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        if calls == 1:
            name, args = "read_source_history", {"source": "observation:screen@1"}
        elif calls == 2:
            assert any(m.get("role") == "tool" and "Search" in m.get("content", "") for m in messages)
            name, args = "submit_executor_step", {"decision": "act", "summary": "stale home",
                "observation_id": "screen", "action": {"type": "home"}}
        else:
            assert not driver.actions
            assert any(m.get("role") == "tool" and "stale_observation" in m.get("content", "") for m in messages)
            name, args = "submit_executor_step", {"decision": "finish", "summary": "probe complete",
                "observation_id": anchor["current_observation_id"]}
        return GatewayResponse(content="", model=model,
            tool_calls=[ToolCall(id=str(calls), name=name, arguments=json.dumps(args))])
    monkeypatch.setattr("agent.session.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=driver,
        settings=settings, library=library)
    try:
        result = await backend.explore("Does the mechanism hold?", 2,
            LearningBudget(max_calls=10, reserve_calls=2),
            context={"experiment": {"hypothesis": "mechanism to test"}})
        assert result["actions_attempted"] == 0
        assert store.records("observation") == source_before
        assert db.get_task(task.id).model_dump() == task_before
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_learning_retains_prior_probe_and_hands_off_question_context(context):
    settings, _, task, store, library = context
    payloads, contexts = [], []
    decisions = iter([
        {"decision": "explore", "question": "Test mechanism one", "hypothesis": "first hypothesis", "max_actions": 1},
        {"decision": "explore", "question": "Test mechanism two", "hypothesis": "second hypothesis", "max_actions": 1},
        {"decision": "skip", "reason": "both probes disconfirm useful rule"},
        {"decision": "skip", "reason": "no other grounded valuable test"}])
    backend = Backend([])
    async def diagnose(model, system, payload, budget):
        payloads.append(payload)
        return next(decisions)
    async def explore(question, max_actions, budget, *, context=None):
        contexts.append(context)
        return {"question": question, "fact": "observed " + question}
    backend.diagnose, backend.explore = diagnose, explore
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["skipped"]
    assert contexts[0]["experiment"]["hypothesis"] == "first hypothesis"
    assert contexts[1]["previous_experiments"][0]["question"] == "Test mechanism one"
    assert [p["question"] for p in payloads[-1]["previous_experiments"]] == ["Test mechanism one", "Test mechanism two"]


@pytest.mark.asyncio
async def test_probe_cost_excludes_prior_analysis_and_includes_continuation_cleanup(context, monkeypatch):
    from agent.skills import learning
    settings, _, task, store, library = context
    clock = [1000.0]
    monkeypatch.setattr(learning.time, "monotonic", lambda: clock[0])
    budget = LearningBudget(max_calls=100, max_actions=10, max_seconds=10000,
        started=0, job_started=0, phase_started=0, calls=10)
    payloads, probes = [], []
    backend = Backend([])
    async def diagnose(model, system, payload, current_budget):
        payloads.append(payload)
        if len(payloads) == 1:
            clock[0] += 100
            current_budget.meter("model_call_started", {})
            return {"decision": "explore", "question": "Test a transition", "max_actions": 3}
        return {"decision": "skip", "reason": "No useful rule"}
    async def explore(question, actions, current_budget, *, context=None):
        clock[0] += 12
        current_budget.meter("model_call_started", {})
        current_budget.action()
        probes.append(actions)
        return {"question": question, "actions_attempted": 1,
            "outcome": "model_budget_reserved" if len(probes) == 1 else "finish",
            "events": {"events": [{"submitted_action": {"type": "tap"},
                "action_result": {"success": True, "receipt": {"dispatch_succeeded": True}}}]}}
    async def cleanup(current_budget):
        clock[0] += 5
        current_budget.meter("model_call_started", {})
        current_budget.action()
    backend.diagnose, backend.explore, backend.complete_probe = diagnose, explore, cleanup
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget)
    assert result["skipped"] and probes == [3, 2]
    receipt = payloads[1]["evidence"]["probe_cost"]
    assert (receipt["calls"], receipt["actions"], receipt["seconds"]) == (3, 3, 29)
    assert payloads[1]["previous_experiments"][0]["probe_cost"] == receipt
    assert result["acquisition_ledger"]["recent_probes"][0]["probe_cost"] == receipt
    assert result["cost"]["calls"] == 14 and result["cost"]["actions"] == 3
    assert result["cost"]["seconds"] == 1129


@pytest.mark.asyncio
async def test_identical_probe_question_stops_without_extra_actions(context):
    settings, _, task, store, library = context
    d = {"decision": "explore", "question": "Does mechanism hold?", "max_actions": 1}
    backend = Backend([d, d])
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["reason"] == "repeated_experiment_without_new_question"
    assert len(backend.explored) == 1


def test_latest_note_frontier_and_original_execution_budget_are_retained(context):
    _, _, task, store, _ = context
    task.state.revisable.limits.device_actions = 34
    task.state.revisable.limits.deadline_at = task.created_at + 900
    for i in range(20):
        store.put("note", "ledger", {"content": "old comparison " * 100 + f"unresolved frontier {i}"})
    packet = build_packet(task, store)
    assert len(packet["notes"]) == 1
    item = packet["notes"][0]
    assert item["source"] == "note:ledger@20"
    assert item["tail_preview"].endswith("unresolved frontier 19")
    assert item["tail_source"].startswith("note:ledger@20#")
    assert packet["execution_limits"]["device_actions"] == 34
    assert packet["execution_limits"]["deadline_window_seconds"] == 900


def test_time_reserve_stops_exploration_but_keeps_writing_available():
    budget = LearningBudget(max_seconds=100)
    budget.started -= 76
    with pytest.raises(LearningStopped, match="time_budget_reserved"):
        budget.check(exploration=True)
    budget.check()
    assert 0 < budget.remaining_seconds() < 25


@pytest.mark.asyncio
async def test_internal_request_boundary_returns_notes_to_reserved_writing(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    driver = FixtureDriver()
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(content="", model=model,
            tool_calls=[ToolCall(id="note", name="write_note",
                arguments=json.dumps({"note_key": "probe_fact",
                    "content": "Observed partial mechanism; complete coverage remains unresolved"}))])
    monkeypatch.setattr("agent.session.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=driver,
        settings=settings, library=library)
    budget = LearningBudget(max_calls=2, reserve_calls=1)
    try:
        evidence = await backend.explore("Test bounded mechanism", 2, budget)
        assert evidence["outcome"] == "model_budget_reserved"
        assert evidence["notes"]["items"][0]["source"].startswith("exploration/note:probe_fact@")
        assert "unresolved" in evidence["notes"]["items"][0]["preview"]
        assert budget.calls == 1 and not driver.actions
        async def writing(model, messages, **kwargs):
            kwargs["attempt_meter"]("model_call_started", {})
            return GatewayResponse(content='{"decision":"skip","reason":"partial evidence insufficient"}', model=model)
        monkeypatch.setattr("agent.skills.learning.complete", writing)
        answer = await budget.ask("test", "write", {"evidence": evidence}, settings)
        assert answer["decision"] == "skip" and budget.calls == 2
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_last_admitted_response_and_action_are_not_discarded(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    driver = FixtureDriver()
    async def complete(model, messages, **kwargs):
        meter = kwargs["attempt_meter"]
        meter("model_call_started", {})
        anchor = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        meter("model_call_finished", {})
        return GatewayResponse(content="", model=model,
            tool_calls=[ToolCall(id="last", name="submit_executor_step",
                arguments=json.dumps({"decision": "act", "summary": "last admitted home",
                    "observation_id": anchor["current_observation_id"], "action": {"type": "home"}}))])
    monkeypatch.setattr("agent.session.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=driver,
        settings=settings, library=library)
    try:
        budget = LearningBudget(max_calls=2, reserve_calls=1)
        result = await backend.explore("Test admitted response", 3, budget)
        assert result["outcome"] == "model_budget_reserved"
        assert len(driver.actions) == 1 and budget.actions == budget.calls == 1
        assert result["events"]["events"][0]["action_result"]["receipt"]
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_final_writing_request_can_return_json(monkeypatch):
    from shared.llm_gateway import GatewayResponse
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        kwargs["attempt_meter"]("model_call_finished", {})
        return GatewayResponse(content='{"decision":"skip"}', model=model)
    monkeypatch.setattr("agent.skills.learning.complete", complete)
    budget = LearningBudget(max_calls=1, reserve_calls=0)
    assert (await budget.ask("test", "write", {}, Settings(_env_file=None)))["decision"] == "skip"
    assert budget.calls == 1
    with pytest.raises(LearningStopped, match="model_budget"):
        await budget.ask("test", "write", {}, Settings(_env_file=None))


@pytest.mark.asyncio
async def test_insufficient_review_can_repair_evidence_without_retesting_body(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    second = c.model_copy(update={"evidence": [*c.evidence, "observation:additional@1"]})
    backend = Backend([{"decision": "explore", "question": "Test syntax", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()},
        {"decision": "propose", "candidate": second.model_dump()}])
    reviews, validations = [], []
    async def ask(self, model, system, payload, settings):
        reviews.append(payload)
        result = review("insufficient" if len(reviews) == 1 else "pass")
        if len(reviews) == 1: result["changes"] = ["Supply an additional source observation"]
        return result
    async def validate(c, old):
        validations.append(c.new_text)
        return validation(c, old)
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert result["eligible"] and len(reviews) == 2 and len(validations) == 1
    assert reviews[0]["candidate"]["new_text"] == reviews[1]["candidate"]["new_text"]


@pytest.mark.asyncio
async def test_same_body_and_evidence_cannot_loop_on_insufficient_review(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    backend = Backend([{"decision": "explore", "question": "Test syntax", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()},
        {"decision": "propose", "candidate": c.model_copy(update={"gist": "different words"}).model_dump()}])
    async def ask(*args):
        result = review("insufficient")
        result["tests"] = ["Supply missing proof"]
        return result
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["reason"] == "unchanged_candidate_and_evidence" and len(result["reviews"]) == 1


def test_review_expands_linked_observations_and_respects_offsets(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    store.put("observation", "after", {"app": "com.demo", "text": "actual query results", "image_ref": None})
    store.put("event", "probe", {"observation_id": "screen",
        "executor_report": "claimed success",
        "action_result": {"receipt": {"observation_id": "after", "effect_outcome": "unknown"}}})
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    c = candidate().model_copy(update={"evidence": ["event:probe@1"]})
    evidence = backend.review_evidence(c)
    assert {x["source"] for x in evidence} == {
        "source/event:probe@1", "source/observation:screen@1", "source/observation:after@1"}
    assert any(x["text"] == "actual query results" for x in evidence)
    store.put("observation", "long", {"app": "com.demo", "text": "a" * 3000 + "late field proof"})
    evidence = backend.review_evidence(c.model_copy(update={"evidence": ["observation:long@1#3000"]}))
    assert evidence[0]["text"] == "late field proof"
    assert evidence[0]["source"].endswith("#3000")




@pytest.mark.asyncio
async def test_diagnostic_history_checkpoints_complete_exchanges_and_keeps_goal(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.analysis_session import COMPACTION_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    for i in range(6):
        store.put("observation", str(i), {"app": "com.demo", "text": str(i) * 12000})
    requests = []; reads = 0; checkpoints = 0
    analysis = {"working_path": [{"statement": "An observed local state", "status": "observed", "evidence": ["observation:0@1"]}],
        "unresolved": [{"statement": "Prerequisite still unknown", "status": "unknown", "evidence": ["observation:0@1"]}]}
    async def complete(model, messages, **kwargs):
        nonlocal reads, checkpoints
        requests.append(json.loads(json.dumps(messages)))
        kwargs["attempt_meter"]("model_call_started", {})
        assert len(json.dumps(messages, ensure_ascii=False)) <= 50000
        calls = {c["id"] for m in messages for c in m.get("tool_calls", [])}
        results = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        assert calls == results
        if messages[0]["content"] == COMPACTION_SYSTEM:
            checkpoints += 1
            assert "tools" not in kwargs
            return GatewayResponse(content=json.dumps(analysis), model=model)
        assert "original exact goal" in messages[1]["content"]
        if reads < 6:
            reads += 1
            return GatewayResponse(content="", model=model, tool_calls=[ToolCall(
                id="read" + str(reads), name="read_history", arguments=json.dumps({
                    "source": "observation:" + str(reads-1) + "@1", "full": True}))])
        assert any("Prerequisite still unknown" in (m.get("content") or "") for m in messages)
        return GatewayResponse(content='{"decision":"skip","reason":"Dependencies unresolved"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(), settings=settings, library=library)
    budget = LearningBudget(max_calls=20, reserve_calls=3)
    result = await backend.diagnose("test", "learn", {"goal": "original exact goal", "context": "x" * 20500}, budget)
    assert result["decision"] == "skip" and reads == 6 and checkpoints > 0
    assert budget.calls == len(requests) == 7 + checkpoints
    assert backend.learner_conversation.compactions == checkpoints
    assert 0 < len(backend.diagnostic_reads) <= reads
    assert any("continue_source" in str(m.get("content", "")) for req in requests for m in req if m["role"] == "tool")


@pytest.mark.asyncio
async def test_repeat_validation_keeps_prior_confirmed_regression(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    backend = Backend([{"decision": "explore", "question": "Test mechanic", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()},
        {"decision": "propose", "candidate": c.model_dump(), "repeat_validation": True}])
    reviews, validations = [], []
    async def ask(self, model, system, payload, settings):
        reviews.append(payload)
        verdict = review("revise" if len(reviews) == 1 else "pass")
        verdict["tests"] = ["Repeat matched normal trial"] if len(reviews) == 1 else []
        return verdict
    async def validate(c, old):
        v = validation(c, old)
        validations.append(v)
        if len(validations) == 1:
            normal = next(t for t in v["trials"] if t["kind"] == "related_normal")
            normal.update(baseline_success=True, candidate_success=False)
        return v
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert not result["eligible"] and len(reviews) == len(validations) == 2
    assert len(reviews[1]["validation"]["trials"]) == 8
    assert reviews[1]["validation_gate"] == {"passed": False, "reason": "confirmed_regression"}


def test_action_directory_counts_attempts_not_inferred_waste(context):
    from agent.skills.learning import action_cost_directory
    _, _, _, store, _ = context
    before = store.records("event")
    for step, kind, units in [(10, "tap", 2), (2, "back", None), (3, "tap", 1), (9, "back", 1)]:
        store.put("event", str(step), {"step": step, "submitted_action": {"type": kind},
            "action_result": {"success": True, "detail": {"device_action_units": units},
                "receipt": {"visible_change": True}}})
    snapshot = store.records("event")
    index = action_cost_directory(snapshot, namespace="success/")
    assert index["submitted_attempts"] == 4 and index["reported_device_units"] == 4
    assert index["attempts_without_reported_units"] == 1
    pair = index["frequent_adjacent_types"][0]
    assert pair["types"] == ["back", "tap"] and pair["occurrences"] == 2
    assert pair["example_sources"] == ["success/event:2@1", "success/event:3@1"]
    assert "proof of waste" in index["policy"]
    assert store.records("event") == snapshot and before == []
    bounded = action_cost_directory(snapshot, character_budget=700)
    assert len(json.dumps(bounded, ensure_ascii=False)) <= 700
    assert bounded["omitted_action_types"] > 0


@pytest.mark.asyncio
async def test_negative_probe_gets_one_bounded_reassessment(context):
    settings, _, task, store, library = context
    decisions = [
        {"decision": "explore", "question": "Test route A", "hypothesis": "A saves work", "max_actions": 1},
        {"decision": "skip", "reason": "A did not work"},
        {"decision": "explore", "question": "Test strategy B", "hypothesis": "B avoids revisits", "max_actions": 1},
        {"decision": "skip", "reason": "B also not useful"}]
    backend = Backend(decisions); payloads = []
    original = backend.diagnose
    async def diagnose(model, system, payload, budget):
        payloads.append(json.loads(json.dumps(payload)))
        return await original(model, system, payload, budget)
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert backend.explored == ["Test route A", "Test strategy B"]
    assert len(payloads) == 4
    assert payloads[2]["diagnostic_read_rounds"] == 0
    assert "skip_reassessment" in payloads[2] and "skip_reassessment" not in payloads[3]
    assert result["acquisition_ledger"]["skip_reassessments"] == 1
    assert result["acquisition_ledger"]["problem_status"] == "unresolved"
    assert result["acquisition_ledger"]["probe_count"] == 2
    assert not list(library.root.rglob("*.md"))


@pytest.mark.asyncio
async def test_initial_skip_and_exhausted_probe_have_no_reassessment(context):
    settings, _, task, store, library = context
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=Backend([{"decision": "skip", "reason": "transient failure"}]))
    assert result["skipped"] and result["acquisition_ledger"]["skip_reassessments"] == 0
    budget = LearningBudget(max_actions=1)
    result = await run_task_learning(task, settings=settings, library=library, store=store, budget=budget,
        backend=Backend([{"decision": "explore", "question": "Check route", "max_actions": 1},
                         {"decision": "skip", "reason": "no actions left"}]))
    assert result["skipped"] and result["acquisition_ledger"]["skip_reassessments"] == 0


@pytest.mark.asyncio
async def test_reassessment_cannot_call_history_tools(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(kwargs.get("tools"))
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(content='{"decision":"skip","reason":"no other grounded hypothesis"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    budget = LearningBudget()
    result = await backend.diagnose("test", "learn", {"diagnostic_read_rounds": 0}, budget)
    assert result["decision"] == "skip" and calls == [None] and budget.calls == 1
    assert backend.record is None


def test_contrast_history_requires_matched_non_reference_success(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    metadata = dict(origin="candidate_assisted", oracle_success=True,
        matched_environment=True, config_hash="same-config")
    backend.add_history("success", store, task.state)
    backend.add_experience("success", **metadata)
    directory = backend.experience_directory()
    assert directory[0]["origin"] == "candidate_assisted" and "active_skills" not in directory[0]
    assert len(json.dumps(directory, ensure_ascii=False)) <= 4000
    with pytest.raises(ValueError, match="excluded"):
        backend.add_experience("success", **{**metadata, "origin": "authored_reference"})
    with pytest.raises(ValueError, match="matched"):
        backend.add_experience("success", **{**metadata, "matched_environment": False})
    with pytest.raises(ValueError, match="successful"):
        backend.add_experience("success", **{**metadata, "oracle_success": False})
    another = db.create_task("Unrelated goal", AgentState(instruction="Unrelated goal"))
    backend.add_history("other", TaskStore(db, store.artifacts, another.id), another.state)
    with pytest.raises(ValueError, match="same task"):
        backend.add_experience("other", **metadata)


def test_combined_diagnostic_handoff_keeps_goal_and_literal_probe(context):
    from agent.skills.exploration import diagnostic_payload_projection
    _, _, task, store, _ = context
    original = {"source": build_packet(task, store), "candidate_json_schema": Candidate.model_json_schema(),
        "evidence": {"question": "Does correspondence hold?", "outcome": "finish", "learning_task_id": "probe",
            "notes": {"items": [{"source": "exploration/note:test@1", "preview": "local support, global unknown"}]},
            "events": {"events": [{"executor_report": "x" * 20000}]},
            "observations": [{"source": "exploration/observation:screen@1", "preview": "v" * 1500}]},
        "prior_diagnostic_reads": [{"source": "source/note:old@1", "text": "z" * 20000}],
        "matched_experiences": [{"namespace": "success", "origin": "candidate_assisted", "notes": "w" * 3000}]}
    snapshot = json.dumps(original)
    compact = diagnostic_payload_projection(original, character_budget=6000)
    assert len(json.dumps(compact, ensure_ascii=False)) <= 6000
    assert compact["source"]["instruction"] == task.instruction
    assert compact["candidate_json_schema"] == original["candidate_json_schema"]
    assert compact["evidence"]["notes"] == original["evidence"]["notes"]
    assert compact["evidence"]["omitted_raw_events"] == 1
    assert compact["context_omissions"]["prior_diagnostic_reads"]["history_refs"] == ["source/note:old@1"]
    assert json.dumps(original) == snapshot


def test_required_diagnostic_contract_is_never_silently_truncated():
    from agent.skills.exploration import diagnostic_payload_projection
    with pytest.raises(LearningStopped, match="diagnostic_context_budget"):
        diagnostic_payload_projection({"source": {"instruction": "required" * 1000}}, character_budget=1000)


@pytest.mark.asyncio
async def test_matched_failure_can_synthesize_unproven_policy_once(context):
    from agent.skills.learning import HYPOTHESIS_SYSTEM
    settings, _, task, store, library = context
    backend = Backend([
        {"decision": "skip", "reason": "no proven shortcut"},
        {"decision": "explore", "question": "Distinguish bookkeeping hypotheses", "max_actions": 1},
        {"decision": "skip", "reason": "bounded test refuted hypothesis"}])
    backend.experiences = {"matched_success": {"origin": "no_skill"}}
    systems = [];original = backend.diagnose
    async def diagnose(model, system, payload, budget):
        systems.append(system)
        if system == HYPOTHESIS_SYSTEM:
            assert payload["diagnostic_read_rounds"] == 0
        return await original(model, system, payload, budget)
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend,
        budget=LearningBudget(max_actions=1))
    assert backend.explored == ["Distinguish bookkeeping hypotheses"]
    assert systems.count(HYPOTHESIS_SYSTEM) == 1
    assert result["acquisition_ledger"]["hypothesis_synthesis_attempted"]
    assert not list(library.root.rglob("*.md"))


@pytest.mark.asyncio
async def test_synthesis_can_stop_without_forced_probe_or_proposal(context):
    settings, _, task, store, library = context
    backend = Backend([{"decision": "skip", "reason": "no proven route"},
        {"decision": "skip", "reason": "no affordable discriminating test"}])
    backend.experiences = {"success": {}}
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["skipped"] and not backend.explored
    assert result["acquisition_ledger"]["hypothesis_synthesis_attempted"]
    backend = Backend([{"decision": "skip"}, {"decision": "propose", "candidate": candidate().model_dump()}])
    backend.experiences = {"success": {}}
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert not result["ok"] and result["reason"] == "hypothesis synthesis must explore or skip"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_preflight", [False, True])
async def test_one_inflight_reserve_continuation_preserves_final_review_capacity(context, with_preflight):
    settings, _, task, store, library = context
    backend = Backend([{"decision": "explore", "question": "Test mutation correspondence", "max_actions": 4},
        {"decision": "skip", "reason": "test still incomplete"}])
    contexts = [];budget = LearningBudget(max_calls=20, reserve_calls=8)
    final_calls = 3 if with_preflight else 2
    if with_preflight:
        async def preflight(*args): raise AssertionError("No candidate to review")
        backend.review_preflight = preflight
    original_diagnose = backend.diagnose
    async def diagnose(model, system, payload, meter):
        if not payload.get("initial_diagnosis"):
            assert payload["diagnostic_read_rounds"] == 0
            assert meter.max_calls - meter.calls == final_calls
        return await original_diagnose(model, system, payload, meter)
    backend.diagnose = diagnose
    async def explore(question, actions, meter, *, context=None):
        contexts.append(context)
        meter.action()
        if len(contexts) == 1:
            meter.calls = 12
            return {"question": question, "outcome": "model_budget_reserved", "actions_attempted": 1,
                "events": {"events": [{"submitted_action": {"type": "tap"},
                    "action_result": {"success": True, "receipt": {"dispatch_succeeded": True}}}]}}
        assert meter.reserve_calls == final_calls and actions == 3
        meter.calls = 20 - final_calls
        return {"question": question, "outcome": "model_budget_reserved", "actions_attempted": 1,
                "events": {"events": [{"submitted_action": {"type": "tap"},
                    "action_result": {"success": True, "receipt": {"dispatch_succeeded": True}}}]}}
    backend.explore = explore
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, budget=budget)
    assert len(contexts) == 2 and "partial_probe" in contexts[1]
    assert budget.calls == 20 - final_calls and budget.actions == 2
    assert result["acquisition_ledger"]["released_reserve_calls"] == 8 - final_calls
    assert result["acquisition_ledger"]["recent_probes"][0]["actions_attempted"] == 2
    assert result["skipped"] and not list(library.root.rglob("*.md"))


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [0, 1])
async def test_zero_device_progress_cannot_release_reserves(context, attempts):
    settings, _, task, store, library = context
    backend = Backend([{"decision": "explore", "question": "Test route", "max_actions": 3},
        {"decision": "skip", "reason": "no device progress"}])
    budget = LearningBudget(max_calls=20, reserve_calls=8)
    async def explore(*args, **kwargs):
        budget.calls = 12
        return {"outcome": "model_budget_reserved", "actions_attempted": attempts,
            "events": {"events": [{"submitted_action": {"type": "tap"},
                "action_result": {"success": False, "receipt": {"dispatch_succeeded": False}}}]}}
    backend.explore = explore
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, budget=budget)
    assert result["acquisition_ledger"]["released_reserve_calls"] == 0 and budget.reserve_calls == 8


@pytest.mark.asyncio
async def test_probe_plan_preserves_complete_conditions_without_silent_cut(context):
    settings, _, task, store, library = context
    parts = ["setup " + "a" * 1600, "Delete only after verified equality; preserve matching instance."]
    backend = Backend([{"decision": "explore", "question": "Test correspondence", "test": parts, "max_actions": 1},
        {"decision": "skip", "reason": "no utility"}])
    contexts = []
    async def explore(question, actions, budget, *, context=None):
        contexts.append(context);budget.action()
        return {"outcome": "finish", "actions_attempted": 1}
    backend.explore = explore
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend,
        budget=LearningBudget(max_actions=1))
    assert result["skipped"] and contexts[0]["experiment"]["test"] == "\n".join(parts)
    assert "preserve matching instance" in contexts[0]["experiment"]["test"]


@pytest.mark.asyncio
async def test_blocked_prerequisite_reassessment_is_planning_only(context):
    from agent.skills.learning import HYPOTHESIS_SYSTEM
    settings, _, task, store, library = context
    backend = Backend([
        {"decision": "explore", "question": "Test mutation", "max_actions": 1},
        {"decision": "skip", "reason": "could not establish the prerequisite correspondence"},
        {"decision": "propose", "candidate": candidate().model_dump()}])
    original = backend.diagnose
    async def diagnose(model, system, payload, budget):
        if "skip_reassessment" in payload:
            assert system == HYPOTHESIS_SYSTEM
            assert payload["diagnostic_read_rounds"] == 0
            assert payload["acquisition_ledger"]["problem_status"] == "unresolved"
            assert "prerequisite" in payload["skip_reassessment"]["prior_reason"]
        return await original(model, system, payload, budget)
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert not result["ok"]
    assert result["reason"] == "prerequisite reassessment must explore or skip"
    assert not list(library.root.rglob("*.md"))


@pytest.mark.asyncio
async def test_target_selection_receives_actual_action_contract_without_executing(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import TARGET_SYSTEM, HYPOTHESIS_SYSTEM
    from agent.session import executor_action_variants_schema
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        assert messages[0]["content"] in {TARGET_SYSTEM, HYPOTHESIS_SYSTEM}
        from agent.skills.exploration import learner_action_directory
        assert payload["executor_action_directory"] == learner_action_directory(executor_action_variants_schema())
        assert "executor_action_contract" not in payload
        from agent.skills.candidate_group import candidate_schema
        schema = candidate_schema()
        schema.pop("title", None)
        for field in schema.get("properties", {}).values():
            field.pop("title", None)
        assert payload["candidate_json_schema"] == schema
        assert payload["initial_diagnosis"] is True
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(content='{"decision":"skip","reason":"transient device failure"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result["skipped"] and result["cost"]["calls"] == 2 and result["cost"]["actions"] == 0
    assert backend.record is None


@pytest.mark.asyncio
async def test_local_mechanism_draft_with_no_ordinary_gain_cannot_be_accepted(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    c.benefit = "Predicted reduction; total utility and transfer are unmeasured"
    backend = Backend([{"decision": "explore", "question": "Test conditional mechanism", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()}])
    async def ask(*args): return review()
    monkeypatch.setattr(LearningBudget, "ask", ask)
    async def validate(c, old):
        v = validation(c, old)
        for trial in v["trials"]:
            trial["baseline_success"] = trial["candidate_success"] = True
        return v
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert result["ok"] and not result["eligible"]
    pending = get_pending(result["pending_id"], root=library.root)
    assert pending.meta["review"]["gate_reason"] == "no_marginal_benefit"
    assert not (library.root / c.target).exists()
    with pytest.raises(ValueError, match="no_marginal_benefit"):
        approve_pending(result["pending_id"], root=library.root)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_test", [
    {"setup": "Keep the complete prerequisite", "restore": "Restore only the verified same record"},
    {"setup": {"hidden": "nested unsupported object"}}])
async def test_structured_experiment_preserves_literals_or_rejects_before_device(context, structured_test):
    settings, _, task, store, library = context
    backend = Backend([{"decision": "explore", "question": "Check transition", "test": structured_test, "max_actions": 1},
        {"decision": "skip", "reason": "no useful fact"}])
    contexts = []
    original = backend.explore
    async def explore(*args, **kwargs):
        contexts.append(kwargs["context"])
        return await original(*args, **kwargs)
    backend.explore = explore
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=LearningBudget(max_actions=1))
    if isinstance(structured_test["setup"], str):
        assert result["ok"]
        assert json.loads(contexts[0]["experiment"]["test"]) == structured_test
    else:
        assert not result["ok"] and not contexts and not backend.explored
        assert result["cost"]["actions"] == 0


def test_aggregate_experiment_handoff_prunes_only_optional_literal_history():
    from agent.skills.exploration import diagnostic_payload_projection
    original = {"source": {"instruction": "unchanged full task"},
        "experiment": {"test": "required preservation condition " * 600},
        "environment_transition": {"reset": True, "fixture": "frozen-case"},
        "diagnostic_reads": [{"source": "source/note:old@1", "text": "x" * 30000}],
        "prior_experiments": [{"history_namespace": "earlier-probe", "notes": "y" * 20000}]}
    frozen = json.dumps(original)
    compact = diagnostic_payload_projection(original, character_budget=45000)
    assert len(json.dumps(compact, ensure_ascii=False)) <= 45000
    assert compact["source"] == original["source"]
    assert compact["experiment"] == original["experiment"]
    assert compact["environment_transition"] == original["environment_transition"]
    assert compact["context_omissions"]["diagnostic_reads"]["history_refs"] == ["source/note:old@1"]
    assert json.dumps(original) == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("second_verdict", ["run", "revise"])
async def test_probe_review_repairs_once_before_device_actions(context, second_verdict):
    settings, _, task, store, library = context
    backend = Backend([
        {"decision": "explore", "question": "Test unsupported transition", "max_actions": 1},
        {"decision": "explore", "question": "Test unsupported transition", "test": "Changed discrimination", "max_actions": 1},
        {"decision": "skip", "reason": "no further value"}])
    audits = iter(["revise", second_verdict])
    async def audit(experiment, actions, budget, *, context):
        assert not backend.explored
        budget.meter("model_call_started", {})
        return {"verdict": next(audits), "reasons": ["Earlier test repeated an easier known condition"]}
    backend.review_probe = audit
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=LearningBudget(max_actions=1))
    assert result["acquisition_ledger"]["probe_repairs"] == 1
    assert len(result["acquisition_ledger"]["probe_reviews"]) == 2
    if second_verdict == "run":
        assert backend.explored == ["Test unsupported transition"]
    else:
        assert result["reason"] == "probe_review_stopped" and not backend.explored
        assert result["cost"]["actions"] == 0
    assert not list(library.root.rglob("*.md"))


def test_probe_snapshot_retains_probe_time_evidence_and_rejects_wrong_task(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    backend.add_history("old_probe", store, task.state)
    evidence = {"question": "Did selection appear?", "learning_task_id": task.id,
        "actions_attempted": 1, "outcome": "finish", "notes": {"items": [
            {"source": "exploration/note:selection@1", "preview": "Opened detail, no selection"}]}}
    backend.add_probe_evidence("old_probe", evidence)
    evidence["notes"]["items"][0]["preview"] = "Later overwritten summary"
    directory = backend.probe_directory()
    assert directory["items"][0]["notes"][0]["source"] == "old_probe/note:selection@1"
    assert directory["items"][0]["notes"][0]["preview"] == "Opened detail, no selection"
    assert directory["items"][0]["probe_cost"]["status"] == "unknown"
    cost = {"status": "measured", "calls": 2, "actions": 1, "seconds": 12}
    backend.record_probe_cost({**evidence, "probe_cost": cost})
    cost["seconds"] = 900
    assert backend.probe_directory()["items"][0]["probe_cost"]["seconds"] == 12
    for _ in range(20): backend.add_probe_evidence("old_probe", evidence)
    small = backend.probe_directory(character_budget=1000)
    assert small["omitted_count"] > 0 and len(json.dumps(small, ensure_ascii=False)) <= 1000
    with pytest.raises(ValueError, match="registered native history"):
        backend.add_probe_evidence("old_probe", {**evidence, "learning_task_id": "another-task"})


@pytest.mark.asyncio
async def test_probe_review_is_fresh_tool_free_and_metered(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import PROBE_REVIEW_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    async def complete(model, messages, **kwargs):
        assert len(messages) == 2 and messages[0]["content"] == PROBE_REVIEW_SYSTEM
        assert not kwargs.get("tools")
        payload = json.loads(messages[1]["content"])
        assert payload["dispatch_capacity"]["exploration_calls_after_audit"] == 15
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(content='{"verdict":"revise","reasons":["Test repeats an easier condition"]}', model=model)
    monkeypatch.setattr("agent.skills.learning.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    budget = LearningBudget()
    result = await backend.review_probe({"question": "Compare transition", "environment_plan": {"mode": "observe", "scope": "Owned fixture navigation", "expected_condition": "Current visible comparison"}}, 2, budget, context={"source": {"instruction": task.instruction}})
    assert result["verdict"] == "revise" and budget.calls == 1 and budget.actions == 0
    assert backend.record is None


@pytest.mark.asyncio
async def test_writing_phase_reserves_native_read_window_and_keeps_goal(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LEARN_SYSTEM
    from agent.session import executor_action_variants_schema
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    store.put("observation", "large", {"app": "com.demo", "text": "o" * 20000})
    calls = []
    async def complete(model, messages, **kwargs):
        assert len(json.dumps(messages, ensure_ascii=False)) <= 50000
        payload = json.loads(messages[1]["content"])
        assert payload["source"]["instruction"] == task.instruction
        assert "executor_action_contract" not in payload
        directory = payload["executor_action_directory"]
        if isinstance(directory, dict) and directory.get("read_tool") == "read_diagnostic_context":
            directory = json.loads(backend.learner_conversation.directories[directory["ref"]])
        if isinstance(directory, dict):
            assert directory["encoding"] == "columnar_records"
            directory = [dict(zip(directory["columns"], row, strict=True)) for row in directory["rows"]]
        assert {item["type"] for item in directory} == set(executor_action_variants_schema()["properties"]["type"]["enum"])
        kwargs["attempt_meter"]("model_call_started", {})
        calls.append(messages)
        if len(calls) == 1:
            return GatewayResponse(content="", model=model, tool_calls=[ToolCall(id="read", name="read_history",
                arguments=json.dumps({"source": "observation:large@1", "full": True}))])
        assert any(m.get("tool_call_id") == "read" for m in messages)
        return GatewayResponse(content='{"decision":"skip","reason":"no grounded useful draft"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    backend.diagnostic_reads = [{"source": "source/note:old@1", "text": "x" * 20000}]
    budget = LearningBudget()
    result = await backend.diagnose("test", LEARN_SYSTEM, {"source": build_packet(task,store),
        "candidate_json_schema": Candidate.model_json_schema(), "necessary_test_conditions": "p" * 11000,
        "diagnostic_read_rounds": 1}, budget)
    assert result["decision"] == "skip" and budget.calls == 2 and budget.actions == 0
    assert backend.record is None


def test_action_cost_directory_exposes_requested_gesture_parameters_as_history(context):
    _, _, _, store, _ = context
    action = {"type": "swipe", "x": 100, "y": 900, "x2": 100, "y2": 400,
        "duration_ms": 600, "image_size": [486,1080]}
    store.put("event", "gesture", {"step": 1, "submitted_action": action, "action_result": {"success": True}})
    from agent.skills.learning import action_cost_directory
    before = json.dumps(store.records("event"))
    directory = action_cost_directory(store.records("event"), namespace="source/")
    example = directory["action_types"][0]["examples"][0]
    assert example["requested_parameters"]["duration_ms"] == 600
    assert example["requested_parameters"]["image_size"] == [486,1080]
    assert example["visible_change"] is None
    assert "not measured content displacement" in example["parameter_policy"]
    assert json.dumps(store.records("event")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("read_count", [8, 9])
async def test_batched_full_history_reads_are_explicitly_deferred_without_breaking_pairs(context, monkeypatch, read_count):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LEARN_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    for i in range(read_count): store.put("observation", str(i), {"app": "com.demo", "text": str(i) * 20000})
    snapshot = json.dumps(store.records("observation"))
    calls = []
    async def complete(model, messages, **kwargs):
        assert len(json.dumps(messages, ensure_ascii=False)) <= 50000
        kwargs["attempt_meter"]("model_call_started", {})
        calls.append(messages)
        if len(calls) == 1:
            return GatewayResponse(content="", model=model, tool_calls=[ToolCall(id="read"+str(i),
                name="read_history", arguments=json.dumps({"source":"observation:"+str(i)+"@1","full":True})) for i in range(read_count)])
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        assert set(results) == {"read" + str(i) for i in range(read_count)}
        assert results["read0"]["data"]["items"]
        deferred = {key: value for key, value in results.items() if value["status"] == "deferred"}
        assert deferred  # Exact page count varies with the system prompt size.
        for key, value in deferred.items():
            assert value["requested_source"] == "observation:" + key[4:] + "@1"
            assert "no absence" in value["policy"].lower()
        assert sum(len(m["content"]) for m in messages if m["role"] == "tool"
                   and json.loads(m["content"])["status"] != "deferred") <= 16000
        return GatewayResponse(content='{"decision":"skip","reason":"unsupported useful mechanism"}',model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    budget=LearningBudget()
    result=await backend.diagnose("test",LEARN_SYSTEM,{"source":build_packet(task,store),
        "candidate_json_schema":Candidate.model_json_schema(),"required_conditions":"p"*10000,
        "diagnostic_read_rounds":1},budget)
    assert result["decision"] == "skip" and budget.calls == 2 and budget.actions == 0
    assert json.dumps(store.records("observation")) == snapshot
    supplied = [item["source"] for m in calls[-1] if m["role"] == "tool"
        and json.loads(m["content"])["status"] != "deferred"
        for item in json.loads(m["content"])["data"]["items"] if item.get("text")]
    # The native conversation contains every supplied page; the optional recent
    # read projection intentionally retains only its last five excerpts.
    assert [item["source"] for item in backend.diagnostic_reads] == supplied[-5:]


@pytest.mark.asyncio
async def test_diagnostic_planning_capacity_refreshes_after_native_reads(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import TARGET_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    snapshot = json.dumps(store.records("observation"))
    balances = []
    async def complete(model, messages, **kwargs):
        payload = next(json.loads(m["content"]) for m in reversed(messages)
            if isinstance(m.get("content"),str) and '"latest_budget"' in m["content"])
        balances.append((payload["latest_budget"]["calls"], payload["exploration_calls_after_response_and_audit"]))
        kwargs["attempt_meter"]("model_call_started", {})
        if len(balances) == 1:
            return GatewayResponse(content="", model=model, tool_calls=[ToolCall(id="read", name="read_history",
                arguments=json.dumps({"source": "observation:screen@1", "full": True}))])
        return GatewayResponse(content='{"decision":"skip","reason":"remaining experiment capacity insufficient"}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    budget = LearningBudget(max_calls=20, calls=3)
    result = await backend.diagnose("test", TARGET_SYSTEM, {"source":build_packet(task,store),
        "budget":{"calls":0,"remaining_exploration_calls":99}, "diagnostic_read_rounds":1}, budget)
    assert result["decision"] == "skip" and balances == [(3,7),(4,6)]
    assert budget.calls == 5 and budget.actions == 0
    assert json.dumps(store.records("observation")) == snapshot


def test_live_handoff_separates_archived_research_from_complete_test_and_frontier():
    from agent.skills.exploration import live_experiment_context
    source = {"instruction":"g" * 6000, "execution_limits":{"device_actions":34}}
    test = "Setup and verification: " + "t" * 5400 + " Preserve both records; no speculative deletion."
    payload = {"source":source, "experiment":{"test":test},
        "environment_transition":{"reset":True},
        "prior_experiments":[{"history_namespace":"old", "text":"OLD_CONFLICTING_RULE" * 5000}],
        "previous_experiments":[{"question":"old failed probe"},
            {"question":"current probe", "notes":{"items":[{"source":"exploration/note:frontier@2",
                "preview":"Survivor checked; full coverage unresolved"}]}}],
        "diagnostic_reads":[{"source":"source/event:1@1", "text":"x" * 1000},
            {"source":"source/event:2@1", "text":"y" * 1000},
            {"source":"source/event:3@1", "text":"z" * 1000}],
        "acquisition_ledger":{"source_problem":{"failure_reason":"Unresolved coverage"},
            "problem_status":"unresolved", "recent_probes":[{"test":"OLD_CONFLICTING_RULE"}]} }
    before = json.dumps(payload)
    result = live_experiment_context(payload)
    assert result["source"] == source and result["experiment"]["test"] == test
    assert result["environment_transition"] == {"reset":True}
    assert result["previous_experiments"][0]["notes"]["items"][0]["preview"].endswith("unresolved")
    assert result["acquisition_ledger"]["problem_status"] == "unresolved"
    assert result["research_history_directory"]["prior_experiments"]["history_refs"] == ["old"]
    assert result["research_history_directory"]["diagnostic_reads"]["omitted_entries"] == 1
    assert all(item["truncated"] for item in result["diagnostic_reads"])
    assert "OLD_CONFLICTING_RULE" not in json.dumps(result)
    assert len(json.dumps(result)) < 20000 and json.dumps(payload) == before
    with pytest.raises(LearningStopped, match="context_budget"):
        live_experiment_context({"source":{"instruction":"g" * 50000}})


def test_execution_outline_is_chronological_literal_paged_and_immutable(context):
    from agent.skills.exploration import execution_outline
    _, _, _, store, _ = context
    for i in reversed(range(90)):
        store.put("event", str(i), {"step":i, "submitted_action":{"type":"swipe", "y":900,"y2":400,"duration_ms":550},
            "executor_report":"literal claim "+str(i)+" " + "x" * 2000,
            "observation_text":"FULL_UI_CONTENT_SHOULD_NOT_ENTER_OUTLINE"})
    rows = store.records("event");before = json.dumps(rows)
    result = execution_outline(rows)
    assert len(json.dumps(result)) <= 8000 and result["omitted_events"] > 0
    assert result["items"][0]["step"] == 0 and all(item["truncated"] for item in result["items"])
    assert result["items"][0]["text"].startswith("literal claim 0")
    assert result["items"][0]["requested_parameters"]["duration_ms"] == 550
    assert "FULL_UI_CONTENT_SHOULD_NOT_ENTER_OUTLINE" not in json.dumps(result)
    page2 = execution_outline(rows, start_step=result["next_step"])
    assert page2["items"][0]["step"] > result["items"][-1]["step"]
    assert json.dumps(rows) == before
    with pytest.raises(ValueError, match="nonnegative"):
        execution_outline(rows,start_step=True)


@pytest.mark.asyncio
async def test_outline_tool_reads_registered_tasks_only_without_device_or_source_mutation(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import TARGET_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    store.put("event", "1", {"step":1,"submitted_action":{"type":"swipe"},"executor_report":"Source claim"})
    other = db.create_task(task.instruction, AgentState(instruction=task.instruction))
    other_store = TaskStore(db,store.artifacts,other.id)
    other_store.put("event", "1", {"step":1,"submitted_action":{"type":"tap"},"executor_report":"Registered contrast claim"})
    before = json.dumps(store.records("event"));calls = []
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        calls.append(messages)
        if len(calls)==1:
            assert "read_execution_outline" in [item["function"]["name"] for item in kwargs["tools"]]
            return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id=namespace,name="read_execution_outline",
                arguments=json.dumps({"namespace":namespace})) for namespace in ("source","contrast")])
        results={m["tool_call_id"]:json.loads(m["content"]) for m in messages if m["role"]=="tool"}
        assert results["source"]["data"]["items"][0]["text"]=="Source claim"
        assert results["contrast"]["data"]["items"][0]["text"]=="Registered contrast claim"
        return GatewayResponse(content='{"decision":"skip","reason":"No supported cheaper route"}',model=model)
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.add_history("contrast",other_store,other.state)
    budget=LearningBudget()
    result=await backend.diagnose("test",TARGET_SYSTEM,{"source":build_packet(task,store),"diagnostic_read_rounds":1},budget)
    assert result["decision"]=="skip" and budget.calls==2 and budget.actions==0 and backend.record is None
    assert json.dumps(store.records("event"))==before
    assert {entry["source"] for entry in backend.diagnostic_reads}=={"source/event:1@1","contrast/event:1@1"}


def test_scoped_history_metadata_preserves_literals_and_continuation_refs():
    from agent.skills.exploration import scoped_history_data
    data = {"items":[{"source":"observation:screen@2", "continue_source":"observation:screen@2#3000",
        "event_sources":["event:4@1"], "text":"Literal note:screen@1 is not rewritten"}], "more_matches":3}
    before=json.dumps(data)
    result=scoped_history_data(data,"probe")
    assert result["items"][0]["source"] == "probe/observation:screen@2"
    assert result["items"][0]["continue_source"] == "probe/observation:screen@2#3000"
    assert result["items"][0]["event_sources"] == ["probe/event:4@1"]
    assert result["items"][0]["text"] == data["items"][0]["text"] and json.dumps(data)==before


@pytest.mark.asyncio
async def test_invalid_evidence_gets_one_repair_before_any_validation(context,monkeypatch):
    settings,_,task,store,library=context
    bad=candidate();bad.evidence=["observation:foreign@1"]
    good=candidate();good.evidence=["probe/observation:foreign@1"]
    backend=Backend([{"decision":"explore","question":"Test syntax","max_actions":1},
        {"decision":"propose","candidate":bad.model_dump()},
        {"decision":"propose","candidate":good.model_dump()}])
    resolved=[];validated=[]
    def resolve(ref):
        resolved.append(ref)
        if ref==bad.evidence[0]:raise ValueError("Unknown observation: foreign")
        return True
    backend.resolve_evidence=resolve
    async def validate(c,old):
        validated.append(c.evidence);return validation(c,old)
    async def ask(*args):return review()
    monkeypatch.setattr(LearningBudget,"ask",ask)
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,validator=validate)
    assert result["eligible"] and resolved==[bad.evidence[0],good.evidence[0]]
    assert validated==[good.evidence] and len(result["reviews"])==1


@pytest.mark.asyncio
async def test_qualified_history_read_and_namespace_conflict_are_checked(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import TARGET_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    other=db.create_task(task.instruction,AgentState(instruction=task.instruction))
    other_store=TaskStore(db,store.artifacts,other.id)
    other_store.put("note","literal",{"title":"literal","content":"probe fact"})
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.add_history("probe",other_store,other.state)
    calls=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        calls.append(messages)
        if len(calls)==1:
            return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id="r",name="read_history",
                arguments=json.dumps({"source":"probe/note:literal@1","full":True}))])
        data=json.loads(next(m["content"] for m in messages if m["role"]=="tool"))["data"]
        assert data["namespace"]=="probe" and data["items"][0]["source"]=="probe/note:literal@1"
        return GatewayResponse(content='{"decision":"skip","reason":"done"}',model=model)
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    await backend.diagnose("test",TARGET_SYSTEM,{"source":build_packet(task,store)},LearningBudget())
    async def conflict(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id="r",name="read_history",
            arguments=json.dumps({"source":"probe/note:literal@1","namespace":"source","full":True}))])
    monkeypatch.setattr("agent.skills.exploration.complete",conflict)
    with pytest.raises(ValueError,match="conflicting history namespaces"):
        await backend.diagnose("test",TARGET_SYSTEM,{"source":build_packet(task,store)},LearningBudget())


def test_post_review_handoff_keeps_quality_and_results_while_source_previews_become_directory():
    from agent.skills.exploration import diagnostic_payload_projection
    source={'instruction':'g'*6000,'execution_limits':{'device_actions':10},'external_evaluation':{'success':False},
        'counts':{'actions':5},'events':[{'source':'event:5@1','preview':'old UI'*800}],
        'observations':[{'source':'observation:screen@1','app':'com.demo','preview':'old UI'*400}],
        'notes':[{'source':'note:frontier@1','preview':'literal source claim '*150,'tail_preview':'old end'*100}]}
    review={'verdict':'revise','criteria':{k:{'status':'insufficient','reason':'Full reason '+k} for k in CRITERIA},
        'changes':['Compare equivalent grouped route'],'tests':['Resolve confirmed regression']}
    candidate_data={'new_text':'FULL CANDIDATE '*170,'evidence':['probe/observation:calendar@1']}
    validated={'candidate_hash':'exact','trials':[{'kind':'variant','baseline_success':True,'candidate_success':False}]}
    payload={'source':source,'candidate':candidate_data,'review_feedback':review,'validation':validated,
        'environment_transition':{'fixture_reset':True}}
    before=json.dumps(payload)
    result=diagnostic_payload_projection(payload,character_budget=14000)
    assert len(json.dumps(result))<=14000 and json.dumps(payload)==before
    assert result['candidate']==candidate_data and result['review_feedback']==review and result['validation']==validated
    assert result['source']['instruction']==source['instruction'] and result['source']['execution_limits']==source['execution_limits']
    assert result['source']['external_evaluation']==source['external_evaluation'] and result['source']['counts']==source['counts']
    assert result['source']['events']==[{'source':'event:5@1'}]
    assert result['source']['observations']==[{'source':'observation:screen@1','app':'com.demo'}]
    assert result['source']['notes'][0]['source']=='note:frontier@1' and result['source']['notes'][0]['truncated']
    assert result['environment_transition']==payload['environment_transition']
    with pytest.raises(LearningStopped,match='diagnostic_context_budget'):
        diagnostic_payload_projection({**payload,'source':{**source,'instruction':'g'*50000}},character_budget=14000)


@pytest.mark.asyncio
@pytest.mark.parametrize("large", [False, True])
async def test_full_review_feedback_and_native_read_share_bounded_window(context,monkeypatch,large):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LEARN_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    store.put('note','large',{'title':'large','content':'literal detail '*800})
    source=build_packet(task,store)
    source.update(instruction='g'*6000,events=[{'source':'event:1@1','preview':'old UI'*1000}],
        observations=[{'source':'observation:screen@1','app':'com.demo','preview':'old UI'*700}])
    full_candidate={'new_text':'FULL CANDIDATE '*(355 if large else 240),'evidence':['source/observation:screen@1']}
    full_review={'verdict':'revise','criteria':{k:{'status':'insufficient','reason':'Review reason '*(90 if large else 80)} for k in CRITERIA},
        'changes':['Compare grouped route'],'tests':['Resolve regression']}
    trials=[{'kind':kind,'goal':'requested goal','baseline_success':True,'candidate_success':False,
        'matched_environment':True,'independent_oracle':True,'config_hash':'same',
        **({'configuration_evidence':'h'*256} if large else {}),
        'baseline_execution':{'history_namespace':'baseline','last_report':'literal report '*100,'cost':{'actions':7},'execution_limits':{'device_actions':10}},
        'candidate_execution':{'history_namespace':'candidate','last_report':'literal report '*100,'failure_reason':'failed '*100,'cost':{'actions':10},'execution_limits':{'device_actions':10}}}
        for kind in ('source','variant','near_miss','related_normal')]
    calls=[]
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{})
        calls.append(messages)
        assert sum(len(m['content']) for m in messages if isinstance(m.get('content'),str))<=50000
        packet=json.loads(messages[1]['content'])
        assert packet['source']['instruction']==source['instruction'] and packet['candidate']==full_candidate and packet['review_feedback']==full_review
        assert [(t['baseline_success'],t['candidate_success']) for t in packet['validation']['trials']]==[(True,False)]*4
        assert packet['validation']['trials'][0]['candidate_execution']['history_namespace']=='candidate'
        if len(calls)==1:
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='r',name='read_history',arguments=json.dumps({'source':'source/note:large@1','full':True}))])
        tool_result=next(m for m in messages if m.get('role')=='tool' and m.get('tool_call_id')=='r')
        item=json.loads(tool_result['content'])['data']['items'][0]
        assert item['text'].startswith('large\nliteral detail ') and item['text']
        assert item['truncated'] and item['continue_source'].endswith('#'+str(len(item['text'])))
        if large:assert len(item['text']) < 12000
        return GatewayResponse(content='{"decision":"skip","reason":"bounded test"}',model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete)
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    budget=LearningBudget()
    result=await backend.diagnose('test',LEARN_SYSTEM,{'source':source,'candidate':full_candidate,'candidate_json_schema':Candidate.model_json_schema(),
        'review_feedback':full_review,'validation':{'trials':trials},'diagnostic_read_rounds':1},budget)
    assert result['decision']=='skip' and budget.calls==2 and budget.actions==0 and backend.record is None


@pytest.mark.asyncio
async def test_exploration_refreshes_device_date_without_reusing_source_date(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    task.state.current_device_date='2020-01-01'
    task.state.temporal_conventions=['Resolve now-relative dates against the device clock.']
    db.update_task(task.id,state=task.state);task=db.get_task(task.id)
    original=task.model_dump();driver=FixtureDriver();reads=[]
    async def read_date():
        reads.append(True)
        if len(reads)==3:raise OSError('unavailable')
        return '2030-07-16' if len(reads)==1 else '2030-07-17'
    driver.current_device_date=read_date
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{})
        anchor=next(json.loads(m['content'])['runtime_update'] for m in reversed(messages)
            if isinstance(m.get('content'),str) and m['content'].startswith('{"runtime_update"'))
        return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='done',name='submit_executor_step',arguments=json.dumps({
            'decision':'finish','summary':'Device context only; no app action needed','observation_id':anchor['current_observation_id']}))])
    monkeypatch.setattr('agent.session.complete',complete)
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    budget=LearningBudget(max_calls=10,reserve_calls=2)
    try:
        assert build_packet(task,store)['temporal_context']['source_device_date']=='2020-01-01'
        for expected in ('2030-07-16','2030-07-17',''):
            await backend.explore('Inspect current temporal context',1,budget)
            assert backend.state.current_device_date==expected
            assert backend.state.temporal_conventions==task.state.temporal_conventions
        measurement=backend.store.get('measurement','device_date_context')['payload']
        assert measurement=={'status':'unavailable','error_type':'OSError'}
        assert len(reads)==3 and not driver.actions and budget.actions==0
        assert db.get_task(task.id).model_dump()==original
    finally:backend.close()


@pytest.mark.asyncio
async def test_skill_review_reads_registered_trial_records_and_image_without_device(context,monkeypatch):
    import base64,hashlib
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver();before=task.model_dump()
    other=db.create_task('Ordinary isolated validation',AgentState(instruction='Validation'))
    trial=TaskStore(db,store.artifacts,other.id)
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
    ref=store.artifacts.save_bytes('history_images',png)
    trial.put('observation','editor',{'app':'com.demo','text':'Editor tree lacks the displayed body','image_ref':ref})
    trial.put('event','edit',{'observation_id':'editor','executor_report':'unverified content claim'})
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    backend.add_history('trial',trial,other.state);requests=[]
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{});requests.append(len(messages))
        assert model=='independent-reviewer'
        assert [t['function']['name'] for t in kwargs['tools']]==['read_review_evidence']
        if len(requests)==1:
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='1',name='read_review_evidence',arguments=json.dumps({'source':'trial/event:edit@1'}))])
        if len(requests)==2:
            item=json.loads(messages[-1]['content']);assert item['linked_observations']==['trial/observation:editor@1']
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='2',name='read_review_evidence',arguments=json.dumps({'source':item['linked_observations'][0],'include_image':True}))])
        image_url=messages[-1]['content'][1]['image_url']['url'];assert base64.b64decode(image_url.split(',',1)[1])==png
        assert json.loads(messages[1]['content'])=={'candidate':'unchanged','contracts':'complete'}
        return GatewayResponse(content=json.dumps(review()),model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete);budget=LearningBudget(max_calls=8)
    try:
        result=await backend.review('independent-reviewer','Review only',{'candidate':'unchanged','contracts':'complete'},budget)
        assert review_gate(result) and budget.calls==3 and budget.actions==0 and not driver.actions
        assert db.get_task(task.id).model_dump()==before
        assert result['evidence_reads'][-1]['image_sha256']==hashlib.sha256(png).hexdigest()
        assert result['evidence_reads'][-1]['image_status']=='supplied'
    finally:backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args',[
    ('tap',{'index':0}),('read_review_evidence',{'source':'unknown/observation:screen@1'}),
    ('read_review_evidence',{'source':'source/observation:screen@1','include_image':'yes'})])
async def test_skill_review_rejects_unauthorized_tools_and_bounds_bad_arguments(context,monkeypatch,name,args):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver();before=task.model_dump()
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{})
        return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='bad',name=name,arguments=json.dumps(args))])
    monkeypatch.setattr('agent.skills.exploration.complete',complete);budget=LearningBudget()
    try:
        malformed = args.get('include_image') == 'yes'
        if malformed:
            with pytest.raises(LearningStopped,match='review_read_budget'):
                await backend.review('review','Review only',{},budget)
        else:
            with pytest.raises(ValueError):
                await backend.review('review','Review only',{},budget)
        assert budget.calls==(4 if malformed else 1)
        assert budget.actions==0 and not driver.actions and db.get_task(task.id).model_dump()==before
    finally:backend.close()


@pytest.mark.asyncio
async def test_reviewer_nine_registered_reads_fit_shared_budget_without_device(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings,db,task,store,library=context
    driver=FixtureDriver()
    for i in range(9):
        store.put('observation',f'review{i}',{'app':'com.demo','text':f'Observed state {i}'})
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    calls=[]
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{})
        calls.append(messages)
        if len(calls)==1:
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id=f'read{i}',name='read_review_evidence',
                arguments=json.dumps({'source':f'source/observation:review{i}@1'})) for i in range(9)])
        results=[json.loads(m['content']) for m in messages if m['role']=='tool']
        assert len(results)==9 and all(r['status']=='success' for r in results)
        return GatewayResponse(content=json.dumps(review()),model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete)
    budget=LearningBudget(max_calls=8)
    try:
        result=await backend.review('review','Review only',{},budget)
        assert review_gate(result) and len(result['evidence_reads'])==9
        assert budget.calls==2 and budget.actions==0 and not driver.actions
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_skill_review_stops_bounded_reads_and_oversized_required_input(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver()
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library);calls=[]
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{});calls.append(kwargs['tools'])
        return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id=str(len(calls)),name='read_review_evidence',arguments=json.dumps({'source':'source/observation:screen@1'}))])
    monkeypatch.setattr('agent.skills.exploration.complete',complete);budget=LearningBudget(max_calls=8)
    try:
        with pytest.raises(LearningStopped,match='review_input_budget'):
            await backend.review('review','Review only',{'candidate':'x'*200001},budget)
        assert budget.calls==0
        with pytest.raises(LearningStopped,match='review_read_budget'):await backend.review('review','Review only',{},budget)
        assert budget.calls==4 and calls[-1] is None and not driver.actions
    finally:backend.close()


@pytest.mark.asyncio
async def test_skill_review_image_limit_deduplication_and_size_are_explicit(context,monkeypatch):
    import base64
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver()
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')
    for i in range(5):
        ref=store.artifacts.save_bytes('history_images',png+bytes([i]))
        store.put('observation','frame'+str(i),{'app':'com.demo','text':'Historical frame','image_ref':ref})
    ref=store.artifacts.save_bytes('history_images',png+b'x'*(2*1024*1024))
    store.put('observation','large',{'app':'com.demo','text':'Large frame','image_ref':ref})
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library);calls=[]
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{});calls.append(1)
        if len(calls)==1:
            keys=['frame0','frame0','frame1','frame2','frame3','frame4','large']
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id=str(i),name='read_review_evidence',arguments=json.dumps({'source':'source/observation:'+key+'@1','include_image':True})) for i,key in enumerate(keys)])
        images=[m for m in messages if isinstance(m.get('content'),list)];assert len(images)==4
        replies=[json.loads(m['content']) for m in messages if m.get('role')=='tool']
        assert replies[1]['image_status']=='already_supplied' and replies[1]['same_image_source']=='source/observation:frame0@1'
        assert 'four-image' in replies[-2]['image_status'] and 'byte limit' in replies[-1]['image_status']
        return GatewayResponse(content=json.dumps(review()),model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete);budget=LearningBudget(max_calls=2)
    try:
        result=await backend.review('review','Review only',{},budget)
        assert review_gate(result) and budget.calls==2 and budget.actions==0 and not driver.actions
    finally:backend.close()


@pytest.mark.asyncio
async def test_skill_review_cancel_after_reply_stops_before_historical_read(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver();cancel=[False]
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    def forbidden_read(*args):raise AssertionError('Cancelled review read evidence')
    backend.resolve_evidence=forbidden_read
    async def complete(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{});cancel[0]=True
        return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='1',name='read_review_evidence',arguments=json.dumps({'source':'source/observation:screen@1'}))])
    monkeypatch.setattr('agent.skills.exploration.complete',complete);budget=LearningBudget(cancelled=lambda:cancel[0])
    try:
        with pytest.raises(asyncio.CancelledError):await backend.review('review','Review only',{},budget)
        assert budget.calls==1 and not driver.actions
    finally:backend.close()


@pytest.mark.asyncio
async def test_learning_routes_to_configured_native_reviewer_and_keeps_pending_gate(context,monkeypatch):
    settings,db,task,store,library=context;c=candidate();seen=[]
    settings=settings.model_copy(update={'skill_reviewer_model':'independent-quality-model'})
    class NativeBackend(Backend):
        async def review(self,model,system,payload,budget):
            seen.append((model,payload));budget.meter('model_call_started',{})
            assert payload['candidate']==c.model_dump() and payload['validation_gate']=={'passed':True,'reason':'validated'}
            return {**review(),'evidence_reads':[{'source':'source/observation:screen@1','status':'success'}]}
    backend=NativeBackend([{'decision':'explore','question':'Test task-specific syntax','max_actions':1},
        {'decision':'propose','candidate':c.model_dump()}])
    async def forbidden(*a):raise AssertionError('Native reviewer was replaced with legacy text-only ask')
    monkeypatch.setattr(LearningBudget,'ask',forbidden)
    async def validate(c,old):return validation(c,old)
    budget=LearningBudget()
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,budget=budget,validator=validate)
    assert result['eligible'] and seen[0][0]=='independent-quality-model' and budget.calls==1
    pending=get_pending(result['pending_id'],root=library.root)
    assert pending.meta['review']['review']['evidence_reads'][0]['source']=='source/observation:screen@1'
    assert not (library.root/c.target).exists()


def test_loaded_generic_directory_is_not_learning_target_scope(context):
    _,_,task,store,_=context
    task.state.frozen_skill_dirs=['generic']
    packet=build_packet(task,store)
    assert packet['skill_scope']==['generic'] and 'not allowed learning targets' in packet['skill_scope_policy']
    from agent.skills.learning import LEARN_SYSTEM,TARGET_SYSTEM
    assert 'Historical loaded directories do not restrict targets' in LEARN_SYSTEM
    assert 'not learning target limits' in TARGET_SYSTEM


@pytest.mark.asyncio
@pytest.mark.parametrize('final_decision',['skip','propose','explore'])
async def test_exhausted_exploration_has_one_tool_free_draft_assessment(context,monkeypatch,final_decision):
    settings,_,task,store,library=context
    c=candidate();payloads=[];backend=Backend([])
    budget=LearningBudget(max_calls=10,reserve_calls=6,max_actions=2)
    async def diagnose(model,system,payload,meter):
        meter.meter('model_call_started',{});payloads.append(payload)
        if len(payloads)==1:return {'decision':'explore','question':'Test query syntax','max_actions':1}
        if len(payloads)==2:return {'decision':'skip','reason':'No exploration capacity; current source scope is generic'}
        assert len(payloads)==3 and payload['diagnostic_read_rounds']==0 and payload['final_draft_assessment']
        return {'decision':final_decision,'candidate':c.model_dump(),'reason':'No useful draft'}
    async def explore(question,max_actions,meter,*,context):
        meter.meter('model_call_started',{});meter.action();backend.explored.append(question)
        return {'question':question,'outcome':'finish','actions_attempted':1,'fact':'Observed query syntax'}
    async def ask(self,model,system,payload,settings):
        budget.meter('model_call_started',{});return review()
    backend.diagnose,backend.explore=diagnose,explore
    monkeypatch.setattr(LearningBudget,'ask',ask)
    validated=[]
    async def validate(c,old):validated.append(True);return validation(c,old)
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,budget=budget,validator=validate)
    assert len(payloads)==3 and len(backend.explored)==1 and budget.actions==1 and budget.calls<=10
    assert result['acquisition_ledger']['final_draft_assessment'] is True
    assert not (library.root/c.target).exists()
    if final_decision=='propose':assert result['eligible'] and len(result['reviews'])==1 and validated
    elif final_decision=='skip':assert result['skipped'] and not validated
    else:assert not result['ok'] and 'must propose or skip' in result['reason'] and not validated


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining',[1,2])
async def test_skill_review_reserves_last_admitted_call_for_verdict(context,monkeypatch,remaining):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;driver=FixtureDriver();before=task.model_dump()
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=driver,settings=settings,library=library)
    budget=LearningBudget(max_calls=3);budget.calls=3-remaining;requests=[]
    async def complete(model,messages,**kwargs):
        requests.append(kwargs['tools']);kwargs['attempt_meter']('model_call_started',{})
        if remaining==2 and len(requests)==1:
            assert kwargs['tools']
            return GatewayResponse(content='',model=model,tool_calls=[ToolCall(id='read',name='read_review_evidence',arguments=json.dumps({'source':'source/observation:screen@1'}))])
        assert kwargs['tools'] is None and 'missing facts remain insufficient' in messages[-1]['content']
        if remaining==2:assert any(m.get('role')=='tool' and m.get('tool_call_id')=='read' for m in messages)
        r=review('insufficient');r['criteria']['marginal_value']['status']='insufficient'
        return GatewayResponse(content=json.dumps(r),model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete)
    try:
        result=await backend.review('independent','Strict review',{'candidate':'unchanged'},budget)
        assert not review_gate(result) and len(requests)==remaining and budget.calls==3
        assert len(result['evidence_reads'])==remaining-1 and budget.actions==0 and not driver.actions
        assert db.get_task(task.id).model_dump()==before
    finally:backend.close()

@pytest.mark.parametrize("case,expected", [("return",True), ("efficient",False),
    ("content_changed",False), ("different_app",False), ("write",False),
    ("unknown_screen",False), ("necessary_short_return",False)])
def test_navigation_return_is_exact_bounded_cost_clue(context, case, expected):
    _, db, task, store, _ = context
    db.update_task(task.id,status=TaskStatus.SUCCEEDED,failure_reason="")
    task=db.get_task(task.id)
    keys=["list","detail","menu","detail","list"]
    if case=="necessary_short_return":
        keys=["list","detail","list","unique3","unique4"]
    if case=="content_changed": keys[-1]="changed"
    if case=="different_app": keys[-1]="other_app"
    if case=="unknown_screen": keys[2]="missing"
    keys += ["unique"+str(i) for i in range(5,13)]
    for key in set(keys)-{"missing"}:
        text=("list" if key=="other_app" else key)+" view "*30
        store.put("observation",key,{"app":"com.other" if key=="other_app" else "com.demo",
            "text":text,"step":0,"image_ref":None})
    count=4 if case=="efficient" else 12
    for i in range(count):
        kind="back" if i==2 else "tap"
        if case=="write" and i==1:kind="replace_text"
        store.put("event",str(i),{"step":i,"decision":"act","observation_id":keys[i],
            "executor_report":"Inspect a page; whether this is necessary is unresolved.",
            "submitted_action":{"type":kind},"action_result":{"success":True}})
    before=store.records("event")
    packet=build_packet(task,store)
    assert ("multi_step_navigation_return" in packet["signals"]) is expected
    assert bool(packet["navigation_returns"]) is expected
    assert len(json.dumps(packet,ensure_ascii=False))<=12000
    assert store.records("event")==before
    if expected:
        clue=packet["navigation_returns"][0]
        assert clue["actions_between"]==4
        assert clue["start_event"]=="event:0@1"
        assert clue["return_event"]=="event:4@1"
        assert "not proof" in clue["policy"]
    else:
        assert not packet["signals"]


@pytest.mark.asyncio
async def test_success_navigation_clue_can_skip_without_probe_or_validation(context):
    settings,db,task,store,library=context
    db.update_task(task.id,status=TaskStatus.SUCCEEDED,failure_reason="")
    task=db.get_task(task.id)
    keys=["list","detail","menu","detail","list"]+["u"+str(i) for i in range(5,13)]
    for key in set(keys):
        store.put("observation",key,{"app":"com.demo","text":key+" visible tree "*20})
    for i in range(12):
        store.put("event",str(i),{"step":i,"observation_id":keys[i],"submitted_action":
            {"type":"back" if i==2 else "tap"},"action_result":{"success":True}})
    class Backend:
        async def diagnose(self,model,system,payload,budget):
            assert "multi_step_navigation_return" in payload["source"]["signals"]
            return {"decision":"skip","reason":"Required inspection; no reusable improvement supported."}
        async def explore(self,*args,**kwargs):raise AssertionError("skip must not probe")
    async def validator(*args):raise AssertionError("skip must not validate")
    result=await run_task_learning(task,settings=settings,library=library,store=store,
        backend=Backend(),budget=LearningBudget(),validator=validator)
    assert result["skipped"] and "Required inspection" in result["reason"]






@pytest.mark.parametrize("body_changed", [False, True])
def test_description_revision_is_valid_but_version_only_is_not(context, body_changed):
    *_, library = context
    c = candidate()
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    old = c.new_text
    path.write_text(old, encoding="utf-8")
    c.new_text = old.replace("0.1.0", "0.1.1")
    with pytest.raises(ValueError, match="no knowledge change"):
        validate_candidate(c, library, {"com.demo"})
    c.new_text = c.new_text.replace("Query syntax", "Quoted hyphenated ingredient searches")
    if body_changed:
        c.new_text += "Keep the hyphen inside the quoted phrase.\n"
    assert validate_candidate(c, library, {"com.demo"}) == old
    assert path.read_text(encoding="utf-8") == old
    stale = validation(c, old)
    c.new_text = c.new_text.replace("Quoted hyphenated ingredient searches", "All searches")
    assert validation_gate(c, digest(old), stale) == (False, "stale_validation")


@pytest.mark.parametrize("field,value", [
    ("role_sections", True), ("interface_scope", "app"),
    ("device_profiles", ["androidworld_api33"]), ("source", "authored")])
def test_retrieval_metadata_permission_preserves_runtime_metadata(context, field, value):
    import yaml
    *_, library = context
    c = candidate()
    _, header, body = c.new_text.split("---", 2)
    metadata = yaml.safe_load(header)
    metadata[field] = value
    old = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    path.write_text(old, encoding="utf-8")
    metadata.pop(field)
    metadata["description"] = "Quoted hyphenated ingredient searches"
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    with pytest.raises(ValueError, match=f"existing metadata changed: {field}"):
        validate_candidate(c, library, {"com.demo"})


@pytest.mark.parametrize("field,value", [
    ("description", "Quoted ingredient searches"), ("tags", ["hyphenated"]),
    ("triggers", ["quoted query"]), ("app_aliases", ["Demo Recipes"]),
    ("capability", "search_ingredients")])
def test_retrieval_only_revision_allows_add_change_and_removal(context, field, value):
    import yaml
    *_, library = context
    c = candidate()
    _, header, body = c.new_text.split("---", 2)
    old_metadata = yaml.safe_load(header)
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    old = c.new_text
    path.write_text(old, encoding="utf-8")
    metadata = {**old_metadata, field: value}
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    assert validate_candidate(c, library, {"com.demo"}) == old
    assert path.read_text(encoding="utf-8") == old
    stale = validation(c, old)
    path.write_text(c.new_text, encoding="utf-8")
    old = c.new_text
    if field == "description":
        metadata[field] = "Query syntax"
    else:
        metadata.pop(field)
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    assert validate_candidate(c, library, {"com.demo"}) == old
    assert validation_gate(c, digest(old), stale) == (False, "stale_validation")


@pytest.mark.parametrize("field,value", [
    ("description", ["invalid"]), ("capability", {"name": "invalid"}),
    ("tags", ["valid", 3]), ("triggers", {"query": "invalid"}),
    ("app_aliases", "Demo Recipes"), ("app_aliases", [""])])
def test_retrieval_metadata_rejects_unusable_types(context, field, value):
    import yaml
    *_, library = context
    c = candidate()
    _, header, body = c.new_text.split("---", 2)
    metadata = yaml.safe_load(header)
    metadata[field] = value
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    with pytest.raises(ValueError, match=f"candidate {field}"):
        validate_candidate(c, library, {"com.demo"})


def test_retrieval_revision_changes_search_alias_and_workflow_catalog(context):
    from agent.skills.scope import scan_instruction_for_skill_apps
    import yaml
    *_, library = context
    c = candidate()
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    old = c.new_text
    path.write_text(old, encoding="utf-8")
    assert library.search("quoted") == []
    assert scan_instruction_for_skill_apps("Open Demo Recipes", library=library, alias_seed={}) == []
    _, header, body = old.split("---", 2)
    metadata = yaml.safe_load(header)
    metadata.update(tags=["quoted"], triggers=["hyphenated ingredient"], app_aliases=["Demo Recipes"])
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    assert validate_candidate(c, library, {"com.demo"}) == old
    path.write_text(c.new_text, encoding="utf-8")  # Isolated trial overlay, not publication.
    assert [pack.id for pack in library.search("quoted")] == ["demo-core"]
    assert [pack.id for pack in library.search("hyphenated ingredient")] == ["demo-core"]
    assert scan_instruction_for_skill_apps("Open Demo Recipes", library=library, alias_seed={}) == ["com.demo"]
    workflow = library.root / "apps/com.demo/workflows/search/SKILL.md"
    workflow.parent.mkdir(parents=True)
    old = serialize_skill_markdown(name="demo-search", description="Search ingredients", version="1.0.0",
        app="com.demo", kind="workflow", capability="search",
        body="## Procedure\nQuote the query.\n## Verification\nCheck returned details.\n")
    workflow.write_text(old, encoding="utf-8")
    c.target = workflow.relative_to(library.root).as_posix()
    c.new_text = old.replace("capability: search", "capability: search_ingredients")
    assert validate_candidate(c, library, {"com.demo"}) == old
    workflow.write_text(c.new_text, encoding="utf-8")
    assert library.workflow_catalog(["com.demo"])[0]["capability"] == "search_ingredients"
    c.new_text = c.new_text.replace("capability: search_ingredients", "capability: search_ingredients\ntriggers: [quoted]")
    with pytest.raises(ValueError, match="removed fields: triggers"):
        validate_candidate(c, library, {"com.demo"})


@pytest.mark.parametrize("field,value", [
    ("device_profiles", ["androidworld_api33"]), ("role_sections", True),
    ("source", "learner"), ("verified_actions", [{"id": "new-action"}])])
def test_retrieval_permission_cannot_add_runtime_metadata_to_existing_skill(context, field, value):
    import yaml
    *_, library = context
    c = candidate()
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    old = c.new_text
    path.write_text(old, encoding="utf-8")
    _, header, body = old.split("---", 2)
    metadata = yaml.safe_load(header)
    metadata.update(description="Quoted ingredients", **{field: value})
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    with pytest.raises(ValueError):
        validate_candidate(c, library, {"com.demo"})
    assert path.read_text(encoding="utf-8") == old


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["pass", "preflight_reject", "final_reject"])
@pytest.mark.parametrize("field,value", [
    ("description", "Quoted hyphenated ingredient searches"), ("tags", ["quoted"]),
    ("triggers", ["hyphenated ingredient"]), ("app_aliases", ["Demo Recipes"]),
    ("capability", "search_ingredients")])
async def test_retrieval_only_candidate_reaches_same_review_and_validation(context, outcome, field, value):
    from agent.skills.exploration import build_review_contracts
    import yaml
    settings, _, task, store, library = context
    c = candidate()
    old = c.new_text
    path = library.root / c.target
    path.parent.mkdir(parents=True)
    path.write_text(old, encoding="utf-8")
    _, header, body = old.split("---", 2)
    metadata = yaml.safe_load(header)
    metadata.update(version="0.1.1", **{field: value})
    c.new_text = "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---" + body
    backend = Backend([
        {"decision":"explore", "question":"Does quoting preserve the hyphen?", "max_actions":1},
        {"decision":"propose", "candidate":c.model_dump()}])
    backend.review_contracts = lambda draft: build_review_contracts(draft.new_text, library, settings)
    visits = []
    def check(payload):
        assert payload["candidate"]["new_text"] == c.new_text
        assert f"+{field}:" in payload["diff"]
    def verdict(rejected):
        result = review("reject" if rejected else "pass")
        if rejected:
            result["criteria"]["regression_risk"] = {
                "status":"fail", "reason":"Metadata creates unsupported retrieval scope"}
        return result
    async def preflight(model, payload, budget):
        visits.append("preflight")
        check(payload)
        return verdict(outcome == "preflight_reject")
    async def validate(draft, original):
        visits.append("validation")
        assert draft.new_text == c.new_text and original == old
        return validation(draft, original)
    async def final_review(model, system, payload, budget):
        visits.append("final")
        check(payload)
        assert "including retrieval metadata" in system
        assert payload["validation"]["candidate_hash"] == digest(c.new_text)
        return verdict(outcome == "final_reject")
    backend.review_preflight = preflight
    backend.review = final_review
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert result["ok"] and result["eligible"] == (outcome == "pass")
    assert visits == (["preflight"] if outcome == "preflight_reject" else ["preflight", "validation", "final"])
    assert path.read_text(encoding="utf-8") == old
    assert result["reviews"][-1]["candidate_hash"] == digest(c.new_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_preflight", [False, True])
async def test_review_revision_preserves_pending_when_full_cycle_cannot_fit(context, with_preflight):
    settings, _, task, store, library = context
    c = candidate()
    backend = Backend([{ "decision": "explore", "question": "Check quoting", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()}])
    budget = LearningBudget(max_calls=5, reserve_calls=0)
    async def revise(*args):
        budget.calls = budget.max_calls - (2 if with_preflight else 1)
        return review("revise")
    backend.review = revise
    if with_preflight:
        async def preflight(*args): return review()
        backend.review_preflight = preflight
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget)
    assert result["ok"] and not result["eligible"]
    assert result["pending_id"] and len(result["reviews"]) == 1
    assert result["revision_stop"] == {"reason": "revision_capacity_exhausted",
        "required_calls": 3 if with_preflight else 2,
        "remaining_calls": 2 if with_preflight else 1}
    assert not (library.root / c.target).exists()


@pytest.mark.asyncio
async def test_failed_probe_can_preserve_local_pitfall_without_whole_task_solution(context, monkeypatch):
    settings, _, task, store, library = context
    c = candidate()
    backend = Backend([{"decision": "explore", "question": "Check the unsupported transition", "max_actions": 1},
        {"decision": "propose", "candidate": c.model_dump()}])
    async def explore(*args, **kwargs):
        return {"outcome": "replan", "actions_attempted": 1, "report": "Whole task unresolved; local failure observed"}
    backend.explore = explore
    async def ask(self, model, system, payload, settings):
        assert "Do not demand a working alternative" in system
        assert payload["validation_gate"] == {"passed": False, "reason": "missing_validation"}
        return {**review("insufficient"), "changes": [], "tests": []}
    monkeypatch.setattr(LearningBudget, "ask", ask)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    pending = get_pending(result["pending_id"], root=library.root)
    assert result["ok"] and not result["eligible"]
    assert pending.meta["review"]["evidence"] == c.evidence
    assert result["acquisition_ledger"]["problem_status"] == "unresolved"
    assert not (library.root / c.target).exists()
