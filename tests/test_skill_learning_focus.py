"""Failure-only hypothesis planning, focused discovery and stable job objectives."""
import copy

import pytest

from agent.skills.learning import HYPOTHESIS_SYSTEM, LearningBudget, run_task_learning
from agent.skills.library import serialize_skill_markdown
from agent.skills.snapshot import LibrarySnapshot
from test_task_skill_learning import Backend, context
from test_skill_evidence_admission import setup_backend, supported_review
from test_skill_verification_feedback import Validator


@pytest.mark.asyncio
async def test_failed_source_without_success_contrast_can_plan_one_audited_probe(context):
    settings, _, task, store, library = context
    backend = Backend([
        {'decision': 'skip', 'reason': 'No proven cause yet'},
        {'decision': 'explore', 'question': 'Test the unsupported transition', 'max_actions': 1},
        {'decision': 'skip', 'reason': 'No useful rule after this test'},
    ])
    audits = []
    async def audit(experiment, actions, budget, *, context):
        audits.append(experiment)
        return {'verdict': 'run', 'reasons': ['A distinct test of missing evidence']}
    backend.review_probe = audit
    systems = []; native = backend.diagnose
    async def diagnose(model, system, payload, budget):
        systems.append(system)
        return await native(model, system, payload, budget)
    backend.diagnose = diagnose
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=LearningBudget(max_actions=1))
    assert result['skipped'] and len(audits) == 1
    assert backend.explored == ['Test the unsupported transition']
    assert systems.count(HYPOTHESIS_SYSTEM) == 1
    assert result['acquisition_ledger']['hypothesis_synthesis_attempted']
    assert not list(library.root.rglob('SKILL.md'))


@pytest.mark.asyncio
async def test_failed_source_may_decline_probe_after_one_hypothesis_assessment(context):
    settings, _, task, store, library = context
    backend = Backend([{'decision': 'skip'}, {'decision': 'skip', 'reason': 'No discriminating test'}])
    async def audit(*args, **kwargs): raise AssertionError('No probe was proposed')
    backend.review_probe = audit
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend)
    assert result['skipped'] and not backend.explored
    assert result['acquisition_ledger']['hypothesis_synthesis_attempted']


@pytest.mark.asyncio
async def test_operator_goal_and_available_checks_survive_probe_and_trial_feedback(context, tmp_path):
    settings, _, task, store, library = context
    plan = {'checks': ['source'], 'reason': 'First measure original failure'}
    backend, frozen, draft = setup_backend(context, tmp_path, {**supported_review(), 'verification': plan})
    backend.decisions = iter([
        {'decision': 'explore', 'question': 'Test the prerequisite', 'max_actions': 1},
        {'decision': 'propose', 'candidate': draft.model_dump()},
        {'decision': 'skip', 'reason': 'Keep scoped result'},
    ])
    goal = 'Measure original completion under unchanged limits'
    received = []; native = backend.diagnose
    async def diagnose(model, system, payload, budget):
        received.append(copy.deepcopy(payload))
        return await native(model, system, payload, budget)
    backend.diagnose = diagnose
    async def final(*args): return supported_review()
    backend.review = final
    validator = Validator(frozen, tmp_path)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validator, verification_goal=goal)
    assert result['eligible'] and len(received) == 3
    assert all(p['verification_goal'] == goal for p in received)
    assert all(p['verification_checks'] == validator.validation_checks() for p in received)
    assert len(result['verification_feedback']) == 1


def test_focused_directory_prioritizes_observed_app_without_removing_other_skills(tmp_path):
    source = tmp_path / 'library'
    for i in range(40):
        p = source / f'apps/a.other{i}/core/SKILL.md'; p.parent.mkdir(parents=True)
        p.write_text(serialize_skill_markdown(name=f'other{i}', description='Unrelated',
            version='1.0.0', app=f'a.other{i}', kind='app_core', body='Other app rule'), encoding='utf-8')
    p = source / 'apps/z.target/core/SKILL.md'; p.parent.mkdir(parents=True)
    p.write_text(serialize_skill_markdown(name='target', description='Relevant',
        version='1.0.0', app='z.target', kind='app_core', body='Relevant rule'), encoding='utf-8')
    snap = LibrarySnapshot.freeze(source, tmp_path / 'frozen')
    directory = snap.directory(apps={'z.target'}, max_chars=350)
    assert directory['items'][0]['id'] == 'target' and directory['outside_observed_apps'] == 40
    assert directory['manifest_hash'] == snap.manifest['hash']
    assert 'Other app rule' in snap.read('apps/a.other0/core/SKILL.md')['text']
    assert len(snap.directory(max_chars=20000)['items']) == 41


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', ['{}', '{invalid'])
async def test_malformed_read_can_be_repaired_in_same_conversation(context, monkeypatch, arguments):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    backend.freeze_library()
    requests = []
    async def complete(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        kwargs['attempt_meter']('model_call_started', {})
        if len(requests) == 1:
            return GatewayResponse(content='', model=model, tool_calls=[
                ToolCall(id='bad', name='read_official_skill', arguments=arguments),
                ToolCall(id='good', name='read_history', arguments='{"source":"observation:screen@1","full":true}'),
            ])
        results = {m['tool_call_id']: json.loads(m['content']) for m in messages if m['role'] == 'tool'}
        assert results['bad']['status'] == 'failed'
        assert 'Search' in results['good']['data']['items'][0]['text']
        return GatewayResponse(content='{"decision":"skip","reason":"No grounded improvement"}', model=model)
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget()
    result = await backend.diagnose('test', 'learn', {}, budget)
    assert result['decision'] == 'skip' and budget.calls == 2 and budget.actions == 0
    assert backend.learner_conversation.id

@pytest.mark.asyncio
@pytest.mark.parametrize('repair_valid', [False, True])
async def test_final_analysis_reference_error_gets_one_bounded_same_session_repair(context, monkeypatch, repair_valid):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    requests = []
    wrong = {'decision': 'explore', 'question': 'Test the unresolved field meaning', 'max_actions': 1,
        'trajectory_analysis': {'working_path': [{'statement': 'The supplied outcome reports failure',
            'status': 'observed', 'evidence': ['source/external_evaluation']}]}}
    async def complete(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        kwargs['attempt_meter']('model_call_started', {})
        if len(requests) == 1:
            return GatewayResponse(content=json.dumps(wrong), model=model)
        assert len(requests) == 2 and not kwargs.get('tools')
        assert any(m.get('role') == 'assistant' and 'source/external_evaluation' in (m.get('content') or '') for m in messages)
        assert 'source/external_evaluation' in messages[-1]['content']
        value = {**wrong, 'trajectory_analysis': {'unresolved': [{'statement': 'The cause is unverified',
            'status': 'unknown', 'evidence': []}]}} if repair_valid else wrong
        return GatewayResponse(content=json.dumps(value), model=model)
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget()
    if repair_valid:
        result = await backend.diagnose('test', 'learn', {}, budget)
        assert result['decision'] == 'explore'
        assert backend.learner_conversation.analysis['unresolved'][0]['status'] == 'unknown'
    else:
        with pytest.raises(ValueError, match='Invalid analysis evidence references'):
            await backend.diagnose('test', 'learn', {}, budget)
    assert len(requests) == 2 and budget.calls == 2 and budget.actions == 0

@pytest.mark.asyncio
async def test_missing_first_environment_plan_is_blocked_before_paid_audit(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    async def forbidden(*args, **kwargs): raise AssertionError('Incomplete contract must not cost an audit')
    monkeypatch.setattr('agent.skills.learning.complete', forbidden)
    budget = LearningBudget()
    result = await backend.review_probe({'question': 'Inspect a field'}, 2, budget, context={})
    assert result['verdict'] == 'revise' and result['origin'] == 'host_contract'
    assert 'environment_plan' in result['reasons'][0]
    assert budget.calls == 0 and budget.actions == 0 and backend.record is None


@pytest.mark.asyncio
async def test_continuous_probe_can_keep_current_plan_and_still_get_independent_audit(context, monkeypatch):
    from types import SimpleNamespace
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.environment import ResearchPlan
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library, retain_probe_state=True)
    plan = ResearchPlan(scope='Owned current fixture', expected_condition='Visible detail')
    backend.environment = SimpleNamespace(status='open', plan=plan,
        receipt=lambda: {'status': 'open', 'plan': plan.model_dump()})
    async def complete(model, messages, **kwargs):
        import json
        payload = json.loads(messages[1]['content'])
        assert payload['research_session']['environment']['plan'] == plan.model_dump()
        kwargs['attempt_meter']('model_call_started', {})
        return GatewayResponse(content='{"verdict":"run","reasons":["New gap under existing scope"]}', model=model)
    monkeypatch.setattr('agent.skills.learning.complete', complete)
    budget = LearningBudget()
    result = await backend.review_probe({'question': 'New discriminating observation'}, 2, budget, context={})
    assert result['verdict'] == 'run' and budget.calls == 1 and budget.actions == 0


@pytest.mark.asyncio
async def test_final_reference_repair_can_omit_archived_raw_exchanges_at_window_boundary(context, monkeypatch):
    import json
    from agent.skills.analysis_session import LearnerConversation
    from agent.skills.exploration import ExplorationBackend, diagnostic_text_size
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    budget = LearningBudget()
    session = LearnerConversation(store.artifacts, {"task_id":task.id})
    session.history = [{"role":"user", "content":"old raw exchange " + "h"*14000}]
    session.analysis = {"unresolved":[{"statement":"Prior unresolved frontier", "status":"unknown", "evidence":[]}]}
    backend.learner_conversation, backend._conversation_budget = session, budget
    wrong = {"decision":"skip", "reason":"r"*16000,
        "trajectory_analysis":{"working_path":[{"statement":"Unverified host field", "status":"observed", "evidence":["source/external_evaluation"]}]}}
    calls = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        calls.append(copy.deepcopy(messages))
        assert diagnostic_text_size(messages) <= 50000
        assert json.loads(messages[1]["content"])["required_conditions"] == "p"*22000
        if len(calls) == 1:
            return GatewayResponse(content=json.dumps(wrong), model=model)
        assert len(calls) == 2 and kwargs.get("tools") is None
        assert "Prior unresolved frontier" in str(messages)
        assert "old raw exchange" not in str(messages)
        assert json.loads(messages[-2]["content"]) == wrong
        fixed = {**wrong, "trajectory_analysis":{"unresolved":[{"statement":"Cause remains unknown", "status":"unknown", "evidence":[]}]}}
        return GatewayResponse(content=json.dumps(fixed), model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    result = await backend.diagnose("test", "learn", {"required_conditions":"p"*22000, "candidate_json_schema":{}, "diagnostic_read_rounds":0}, budget)
    assert result["decision"] == "skip" and budget.calls == 2 and budget.actions == 0
    archived = list((store.artifacts.root / "skill-learning/conversations" / session.id).glob("*.json"))
    assert any("old raw exchange" in p.read_text(encoding="utf-8") for p in archived)


def test_format_repair_fits_by_omitting_action_schema_without_losing_decision():
    import json
    from agent.skills.exploration import analysis_repair_context, diagnostic_text_size
    candidate = {"new_text":"  significant spaces\n\n" * 35, "evidence":["source/observation:visible@2"]}
    phase = {"source":{"instruction":"g"*26000, "execution_limits":{"rounds":50},
        "external_evaluation":{"success":False,"failed_check":"URL mismatch"}},
        "candidate":candidate, "executor_action_directory":[{"schema":"a"*4000}],
        "verification_goal":"Measure full task benefit", "budget":{"remaining_calls":12}}
    checkpoint = {"unresolved":[{"statement":"Prior unknown " + "u"*2200,
        "status":"unknown","evidence":[]}]}
    parsed = {"decision":"propose", "candidate":candidate, "reason":"r"*3000,
        "trajectory_analysis":{"unresolved":[{"statement":"Unsupported reference",
            "status":"unknown","evidence":["official_skill:wrong"]}]}}
    messages = [{"role":"system","content":"s"*14500},
        {"role":"user","content":json.dumps(phase)},
        {"role":"assistant","content":json.dumps(parsed,indent=2)}]
    original = copy.deepcopy(messages)
    repaired = analysis_repair_context(messages, checkpoint, parsed,
        {"role":"user","content":"Repair references only; no tools or decision changes."})
    assert diagnostic_text_size(repaired) <= 50000
    projected = json.loads(repaired[1]["content"])
    assert "executor_action_directory" not in projected
    for key in phase.keys()-{"executor_action_directory"}:assert projected[key] == phase[key]
    assert json.loads(repaired[-2]["content"]) == parsed
    assert json.loads(repaired[2]["content"])["trajectory_analysis"] == checkpoint
    assert messages == original


def test_format_repair_cannot_drop_required_goal_to_fit_window():
    import json
    from agent.skills.exploration import analysis_repair_context
    from agent.skills.learning import LearningStopped
    messages = [{"role":"system","content":"s"*14500},
        {"role":"user","content":json.dumps({"source":{"instruction":"g"*52000},
            "executor_action_directory":[{"schema":"optional"}]})}]
    with pytest.raises(LearningStopped,match="analysis_output_repair_context_budget"):
        analysis_repair_context(messages, None, {"decision":"skip"},
            {"role":"user","content":"Repair only"})


def test_guidance_overflow_retains_index_and_exact_readable_bodies():
    import json
    from agent.skills.exploration import diagnostic_projection_with_reads,read_diagnostic_context
    guidance={"items":[{"id":"calendar-flow","kind":"workflow","app":"com.demo",
        "content_hash":"exact-role-hash","content":"Observed rule body "*600}],
        "omitted_ids":["unavailable-rule"],"policy":"Existing guidance, not actual app proof"}
    payload={"source":{"instruction":"Preserve this complete goal","counts":{"actions":50},
        "execution_limits":{"rounds":50},"official_skill_directory":{}},
        "existing_active_guidance":guidance,"verification_goal":"Assess actual task utility"}
    directories={}
    projected=diagnostic_projection_with_reads(payload,character_budget=2000,directories=directories,
        pinned_messages=[{"role":"system","content":"System policy"}],wire_budget=6000)
    index=projected["existing_active_guidance"]
    assert index["items"]==[{k:v for k,v in guidance["items"][0].items() if k!="content"}]
    assert index["omitted_ids"]==guidance["omitted_ids"] and index["policy"]==guidance["policy"]
    recovered=json.loads(directories[index["ref"]])
    assert recovered==guidance
    assert read_diagnostic_context(directories,index["ref"],length=4000)["content_hash"]==index["ref"]
    assert projected["source"]["instruction"]==payload["source"]["instruction"]
    assert projected["source"]["counts"]==payload["source"]["counts"]
    assert projected["source"]["execution_limits"]==payload["source"]["execution_limits"]
    assert projected["verification_goal"]==payload["verification_goal"]


def test_format_repair_can_omit_rule_prose_but_keeps_identity_and_full_decision():
    import json
    from agent.skills.exploration import analysis_repair_context,diagnostic_text_size
    guidance={"items":[{"id":"route","kind":"workflow","app":"com.demo",
        "content_hash":"same-hash","content":"rule "*2600}],
        "omitted_ids":["missing"],"policy":"Existing guidance, not observation"}
    phase={"source":{"instruction":"g"*25000,"external_evaluation":{"success":False}},
        "existing_active_guidance":guidance,"budget":{"remaining_calls":10}}
    parsed={"decision":"propose","candidate":{"new_text":" unchanged\n"},
        "trajectory_analysis":{"unresolved":[{"statement":"Unknown","evidence":[]}]}}
    messages=[{"role":"system","content":"s"*14500},
        {"role":"user","content":json.dumps(phase)}]
    original=copy.deepcopy(messages)
    repair=analysis_repair_context(messages,None,parsed,{"role":"user","content":"Repair references only"})
    assert diagnostic_text_size(repair)<=50000
    projected=json.loads(repair[1]["content"])
    assert projected["source"]==phase["source"] and projected["budget"]==phase["budget"]
    index=projected["existing_active_guidance"]
    assert index["items"]==[{k:v for k,v in guidance["items"][0].items() if k!="content"}]
    assert index["omitted_ids"]==guidance["omitted_ids"] and index["policy"]==guidance["policy"]
    assert json.loads(repair[-2]["content"])==parsed
    assert messages==original


@pytest.mark.asyncio
async def test_analysis_format_repair_cannot_change_candidate_or_decision(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,
        driver=FixtureDriver(),settings=settings,library=library)
    wrong={"decision":"skip","reason":"No supported mechanism",
        "trajectory_analysis":{"working_path":[{"statement":"Unsupported reference",
            "status":"observed","evidence":["source/external_evaluation"]}]}}
    requests=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{});requests.append(messages)
        if len(requests)==1:return GatewayResponse(model=model,content=json.dumps(wrong))
        assert len(requests)==2 and not kwargs.get("tools")
        return GatewayResponse(model=model,content=json.dumps({**wrong,"decision":"explore",
            "trajectory_analysis":{"unresolved":[{"statement":"Still unknown","status":"unknown","evidence":[]}]}}))
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget()
    with pytest.raises(ValueError,match="analysis format repair changed the decision"):
        await backend.diagnose("test","learn",{},budget)
    assert budget.calls==2 and budget.actions==0


@pytest.mark.asyncio
async def test_probe_revise_preserves_required_contract_at_existing_protected_ceiling(context, monkeypatch):
    import json
    from agent.session import executor_action_variants_schema
    from agent.skills.exploration import ExplorationBackend, diagnostic_text_size
    from agent.skills.learning import TARGET_SYSTEM, Candidate
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    feedback = {"verdict":"revise", "reasons":["Inspect the missing native transition, not the repeated positive."]}
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        assert diagnostic_text_size(messages) <= 50000
        packet = json.loads(messages[1]["content"])
        assert packet["source"]["instruction"] == "original required goal"
        assert packet["required_conditions"] == "p"*16000
        assert packet["probe_review"] == feedback
        from agent.skills.exploration import learner_action_directory
        assert packet["executor_action_directory"] == learner_action_directory(executor_action_variants_schema())
        assert "executor_action_contract" not in packet
        return GatewayResponse(content='{"decision":"skip","reason":"No distinct check remains","trajectory_analysis":{"unresolved":[{"statement":"Mechanism is unverified","status":"unknown","evidence":[]}]}}', model=model)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget(max_calls=10)
    value = await backend.diagnose("test", TARGET_SYSTEM, {"source":{"instruction":"original required goal", "execution_limits":{"device_actions":13}},
        "required_conditions":"p"*16000, "probe_review":feedback, "candidate_json_schema":Candidate.model_json_schema(),
        "initial_diagnosis":True, "diagnostic_read_rounds":1}, budget)
    assert value["decision"] == "skip" and budget.calls == 1 and budget.actions == 0
    assert budget.max_calls == 10


def test_checkpoint_continuation_preserves_statuses_citations_and_native_archive(context):
    import json
    from agent.skills.analysis_session import LearnerConversation
    settings, db, task, store, library = context
    session = LearnerConversation(store.artifacts, {"task_id": task.id})
    progress = {"working_path": [{"statement": "Observed local transition", "status": "observed", "evidence": ["source/event/1@1"]}],
        "unresolved": [{"statement": "Downstream prerequisite is unverified", "status": "unknown", "evidence": []}]}
    old = [{"role":"system","content":"policy"},{"role":"user","content":"source"},
        {"role":"assistant","content":"original exchange, retained in archive"}]
    session.save(old, analysis=progress)
    session.save(old[:2], analysis=progress, compacted=True)
    messages = session.messages("policy", {"current_phase":"review repair"})
    packet = json.loads(messages[2]["content"])
    assert packet["trajectory_analysis"] == progress
    assert "named gaps, contradictions or review disputes" in packet["policy"]
    assert "not independent proof" in packet["policy"]
    assert "Verify cited native records" not in packet["policy"]
    archives = list((store.artifacts.root / "skill-learning/conversations" / session.id).glob("*.json"))
    assert any("original exchange, retained in archive" in p.read_text(encoding="utf-8") for p in archives)
    assert session.compactions == 1 and session.analysis == progress


@pytest.mark.asyncio
async def test_history_schema_requires_the_same_nonempty_locator_as_runtime(context):
    from jsonschema import Draft202012Validator
    from agent.revisable.tools import register_memory_tools
    from agent.tool_registry import AgentToolRegistry, ToolExecutionContext, AgentRole
    settings, db, task, store, library = context
    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", store, task.state)
    spec = next(s for s in registry.catalog_for_role("executor") if s["function"]["name"] == "read_history")
    schema = Draft202012Validator(spec["function"]["parameters"])
    for bad in ({}, {"source":" "}, {"query":""}, {"source":"event:1@1", "query":"term"}):
        assert not schema.is_valid(bad)
    store.put("observation", "visible", {"app":"com.demo", "text":"distinctive visible fact"})
    ctx = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="schema-contract", state={"artifacts":store.artifacts})
    for good in ({"source":"observation:visible@1"}, {"query":"distinctive"}, {"source":"", "query":"distinctive"}):
        assert schema.is_valid(good)
        result = await registry.execute("read_history", good, ctx)
        assert result.data["items"]


@pytest.mark.asyncio
async def test_reviewer_repairs_failed_native_read_in_existing_rounds(context, monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import REVIEW_SYSTEM, CRITERIA
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    store.put("observation","visible",{"app":"com.demo","text":"Actual observed state"})
    requests=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{});requests.append(copy.deepcopy(messages))
        pairs={c["id"] for m in messages for c in m.get("tool_calls",[])}
        assert pairs=={m["tool_call_id"] for m in messages if m["role"]=="tool"}
        if len(requests)==1:
            return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id="missing",name="read_review_evidence",arguments="{}"),ToolCall(id="guessed",name="read_review_evidence",arguments='{"source":"source/measurement:guessed@1"}')])
        if len(requests)==2:
            failed=[json.loads(m["content"]) for m in messages if m["role"]=="tool"]
            assert len(failed)==2 and all(v["status"]=="failed" for v in failed)
            return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id="fixed",name="read_review_evidence",arguments='{"source":"source/observation:visible@1"}')])
        assert "Actual observed state" in str(messages)
        return GatewayResponse(model=model,content=json.dumps({"verdict":"insufficient","criteria":{k:{"status":"insufficient","reason":"Other required claims remain unverified"} for k in CRITERIA},"changes":[],"tests":[],"acceptance":"source_evidence"}))
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget(max_calls=8)
    result=await backend.review("test",REVIEW_SYSTEM,{},budget)
    assert result["verdict"]=="insufficient" and budget.calls==3 and budget.actions==0


@pytest.mark.asyncio
async def test_reviewer_unknown_history_namespace_is_still_rejected(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import REVIEW_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id="foreign",name="read_review_evidence",arguments='{"source":"foreign/observation:secret@1"}')])
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget(max_calls=8)
    with pytest.raises(ValueError,match="unknown evidence namespace"):
        await backend.review("test",REVIEW_SYSTEM,{},budget)
    assert budget.calls==1 and budget.actions==0


def test_diagnostic_budget_omits_only_per_request_usage_bookkeeping():
    from agent.skills.exploration import diagnostic_payload_projection
    usage={"responses":300,"available_responses":250,"unknown_responses":50,
        "records":[{"input_tokens":100,"output_tokens":20}]*250,"missing_policy":"Unknown, never zero"}
    budget={"calls":300,"remaining_calls":18,"actions":7,"remaining_actions":12,
        "seconds":42,"validation_children":[{"calls":5,"actions":3}],"provider_usage":usage}
    packet={"budget":budget,"review_feedback":{"verdict":"insufficient","tests":["Inspect missing final observation"]}}
    projected=diagnostic_payload_projection(packet,character_budget=3000,compact=True)
    assert projected["review_feedback"]==packet["review_feedback"]
    assert {k:v for k,v in projected["budget"].items() if k!="provider_usage"}=={k:v for k,v in budget.items() if k!="provider_usage"}
    kept=projected["budget"]["provider_usage"]
    assert all(kept[k]==usage[k] for k in usage if k!="records")
    assert kept["omitted_request_records"]==250 and "records" not in kept
    assert len(usage["records"])==250  # Original cost audit is unchanged.


def test_reviewer_contract_binds_the_single_system_policy(context):
    from agent.skills.exploration import build_review_contracts
    from agent.skills.learning import REVIEW_SYSTEM,digest
    settings,db,task,store,library=context
    text="---\nname: demo\ndescription: App routing\nversion: 1.0.0\napp: com.demo\nkind: app_core\n---\n\n# Procedure\nUse the observed app route."
    contracts=build_review_contracts(text,library,settings)
    assert contracts["skill_reviewer_system_hash"]==digest(REVIEW_SYSTEM)
    assert "skill_reviewer_system" not in contracts
    assert set(contracts["prompts"])=={"planner","reviewer","executor"}
    assert contracts["action_tool_schemas"] and contracts["executor_submission_schema"]


def test_diagnostic_trial_preserves_activation_facts_without_repeated_examples():
    from agent.skills.exploration import diagnostic_payload_projection
    activation={"executor_active_exact":True,"executor_projection_hash":"exacthash",
        "requests":{"executor":14,"unknown":0},"mechanism_adoption":"unassessed",
        "examples":[{"request_ref":"original1","content_hash":"exacthash"}]*20,
        "policy":"Activation is not adoption"}
    trial={"kind":"variant","candidate_success":True,"baseline_success":False,
        "pair_evidence_ref":"validation/measurement:paired@1",
        "candidate_execution":{"cost":{"actions":11,"model_requests":16},"candidate_activation":activation}}
    packet={"review_feedback":{"verdict":"insufficient"},"source":{},"validation":{"trials":[trial]}}
    projected=diagnostic_payload_projection(packet,compact=True)
    retained=projected["validation"]["trials"][0]
    assert retained["pair_evidence_ref"]==trial["pair_evidence_ref"]
    assert retained["candidate_success"] and not retained["baseline_success"]
    got=retained["candidate_execution"]["candidate_activation"]
    assert all(got[k]==activation[k] for k in activation if k!="examples")
    assert got["omitted_examples"]==20 and "examples" not in got
    assert len(activation["examples"])==20


def test_diagnostic_projection_counts_escaped_wire_and_retains_trial_facts():
    import json
    from agent.skills.exploration import diagnostic_projection_with_reads,diagnostic_text_size,read_diagnostic_context
    from agent.skills.learning import digest
    raw={"source":{"instruction":'"required goal" '*900},
        "review_feedback":{"verdict":"insufficient","criteria":{"regression_risk":{"status":"insufficient","reason":"Read exact final observation"}}},
        "validation":{"candidate_hash":"candidate","base_hash":"base","trials":[{
            "kind":"variant","matched_environment":True,"independent_oracle":True,
            "baseline_success":False,"candidate_success":True,"pair_evidence_ref":"registered/measurement:pair@1",
            "candidate_execution":{"task_id":"candidate-task","runtime_status":"succeeded",
                "execution_limits":{"max_steps":12},"cost":{"actions":11,"model_requests":16},
                "action_types":{"verbose_archived_detail":'"literal" '*1400},
                "candidate_activation":{"executor_active_exact":True,"mechanism_adoption":"unassessed"}}}]}}
    pinned=[{"role":"system","content":"p"*14000},{"role":"user","content":"checkpoint"+"a"*5000}]
    wire=lambda value:diagnostic_text_size([*pinned,{"role":"user","content":json.dumps(value,ensure_ascii=False,separators=(",",":"))}])
    assert wire(raw)>45000
    directories={}
    value=diagnostic_projection_with_reads(raw,character_budget=30000,directories=directories,pinned_messages=pinned,wire_budget=45000)
    assert wire(value)<=45000
    assert value["source"]["instruction"]==raw["source"]["instruction"]
    assert value["review_feedback"]==raw["review_feedback"]
    trial=value["validation"]["trials"][0]
    assert trial["candidate_success"] and not trial["baseline_success"]
    assert trial["matched_environment"] and trial["independent_oracle"]
    assert trial["candidate_execution"]["cost"]=={"actions":11,"model_requests":16}
    assert trial["candidate_execution"]["candidate_activation"]["mechanism_adoption"]=="unassessed"
    ref=value["validation"]["details"]["ref"]
    assert digest(directories[ref])==ref and json.loads(directories[ref])==raw["validation"]
    page=read_diagnostic_context(directories,ref,length=4000)
    assert page["next_offset"]==4000 and page["content_hash"]==ref


def test_checkpoint_receives_current_variant_intent_and_review_dispute():
    import json
    from agent.skills.exploration import checkpoint_phase_context
    payload={"verification_goal":"Resolve missing final rename observation without more ordinary trials",
        "source":{"instruction":"Rename B to C, preserving A"},
        "candidate":{"new_text":"Only skip redundant navigation after B is identified; preserve A", "scope":"Known B identity"},
        "validation":{"trials":[{"kind":"variant","goal":"Rename B to C",
            "candidate_success":True,"candidate_execution_namespace":"variant_candidate",
            "pair_evidence_ref":"variant_candidate/measurement:pair@1"}]},
        "review_feedback":{"criteria":{"root_cause":{"status":"pass","reason":"already checked"},
            "regression_risk":{"status":"insufficient","reason":"Final list excerpt omitted C"}},"tests":["Inspect final screenshot"]},
        "irrelevant_full_contract":"x"*40000}
    value=checkpoint_phase_context([{"role":"system","content":"checkpoint"},{"role":"user","content":json.dumps(payload)}])
    assert value["verification_goal"]==payload["verification_goal"]
    assert value["original_instruction"]==payload["source"]["instruction"]
    assert value["candidate"]==payload["candidate"]
    assert value["validation_targets"][0]["goal"]=="Rename B to C"
    assert value["validation_targets"][0]["candidate_execution_namespace"]=="variant_candidate"
    assert value["review_gaps"]=={"regression_risk":payload["review_feedback"]["criteria"]["regression_risk"]}
    assert value["review_tests"]==["Inspect final screenshot"]
    assert "candidate_success" not in value["validation_targets"][0]
    assert "irrelevant_full_contract" not in value


@pytest.mark.asyncio
async def test_latest_capacity_does_not_reintroduce_usage_records(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    budget=LearningBudget(max_calls=500)
    budget.usage=[{"input_tokens":100,"output_tokens":20}]*200
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        capacities=[json.loads(m["content"])["latest_budget"] for m in messages if m["role"]=="user" and isinstance(m["content"],str) and '"latest_budget"' in m["content"]]
        assert capacities and all("records" not in c["provider_usage"] for c in capacities)
        assert capacities[-1]["provider_usage"]["omitted_request_records"]==200
        return GatewayResponse(content='{"decision":"skip","reason":"No supported candidate","trajectory_analysis":{"unresolved":[{"statement":"No app evidence supplied","status":"unknown","evidence":[]}]}}',model=model)
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    result=await backend.diagnose("test","learn",{"diagnostic_read_rounds":0},budget)
    assert result["decision"]=="skip" and len(budget.usage)==201


@pytest.mark.asyncio
async def test_capacity_replacement_preserves_native_tool_exchanges(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    for key in ("first","second"):
        store.put("observation",key,{"text":key+" distinct observed state"})
    budget=LearningBudget(max_calls=12,reserve_calls=2)
    requests=[]
    async def complete(model,messages,**kwargs):
        capacities=[json.loads(m["content"])["latest_budget"] for m in messages
                    if m["role"]=="user" and isinstance(m.get("content"),str) and '"latest_budget"' in m["content"]]
        assert len(capacities)==1 and capacities[0]["calls"]==len(requests)
        paired={m["tool_call_id"] for m in messages if m["role"]=="tool"}
        assert paired=={c["id"] for m in messages for c in m.get("tool_calls",[])}
        requests.append(copy.deepcopy(messages));kwargs["attempt_meter"]("model_call_started",{})
        if len(requests)<3:
            key=("first","second")[len(requests)-1]
            return GatewayResponse(content="",model=model,tool_calls=[ToolCall(id=key,name="read_history",arguments=json.dumps({"source":"observation:"+key+"@1"}))])
        assert all(key+" distinct observed state" in str(messages) for key in ("first","second"))
        return GatewayResponse(content='{"decision":"skip","reason":"No decision-changing rule","trajectory_analysis":{"unresolved":[{"statement":"Task mechanism remains unknown","status":"unknown","evidence":[]}]}}',model=model)
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    result=await backend.diagnose("test","learn",{"diagnostic_read_rounds":3},budget)
    assert result["decision"]=="skip" and len(requests)==3 and budget.calls==3
    assert backend.learner_conversation.compactions==0


@pytest.mark.parametrize("feedback",[None,[],[{"verdict":"revise","criteria":{"root_cause":{"status":"insufficient","reason":"Inspect prerequisite"}},"tests":["Read predecessor effect"]}]])
def test_checkpoint_accepts_post_probe_review_history(feedback):
    import json
    from agent.skills.exploration import checkpoint_phase_context
    payload={"verification_goal":"Continue failed-task research", "review_feedback":feedback}
    value=checkpoint_phase_context([{"role":"system","content":"checkpoint"},{"role":"user","content":json.dumps(payload)}])
    if feedback:
        assert value["review_gaps"]==feedback[-1]["criteria"]
        assert value["review_tests"]==feedback[-1]["tests"]
    else:
        assert value["review_gaps"]=={} and value["review_tests"]==[]


def test_feedback_pages_passed_reasons_but_keeps_blockers_and_exact_review():
    import json
    from agent.skills.exploration import diagnostic_projection_with_reads,diagnostic_text_size
    from agent.skills.learning import digest
    feedback={"verdict":"revise","acceptance":"source_evidence","criteria":{
        "root_cause":{"status":"pass","reason":"qualified independent assessment "*150},
        "regression_risk":{"status":"insufficient","reason":"Keep this exact necessary blocking explanation"}},
        "changes":["Preserve completed local work"],"tests":[{"gap":"missing survivor","check":"read final observation"}]}
    packet={"source":{"instruction":"original required goal"},"review_feedback":feedback}
    original=copy.deepcopy(packet);pinned=[{"role":"system","content":"p"*14000},{"role":"user","content":"c"*5000}]
    directories={}
    projected=diagnostic_projection_with_reads(packet,character_budget=6000,directories=directories,pinned_messages=pinned,wire_budget=22000)
    retained=projected["review_feedback"]
    assert retained["criteria"]["regression_risk"]==feedback["criteria"]["regression_risk"]
    assert retained["changes"]==feedback["changes"] and retained["tests"]==feedback["tests"]
    assert retained["verdict"]=="revise" and retained["acceptance"]=="source_evidence"
    assert retained["criteria"]["root_cause"]["status"]=="pass"
    ref=retained["review_details"]["ref"]
    assert digest(directories[ref])==ref and json.loads(directories[ref])==feedback
    assert packet==original
    assert diagnostic_text_size([*pinned,{"role":"user","content":json.dumps(projected,ensure_ascii=False)}])<=22000



def test_required_feedback_falls_back_to_smallest_fitting_exact_projection():
    import json
    from agent.skills.exploration import diagnostic_projection_with_reads,diagnostic_text_size
    feedback={"verdict":"revise","criteria":{"root_cause":{"status":"pass","reason":"qualified passed assessment "*200},
        "regression_risk":{"status":"insufficient","reason":"necessary blocker "*50}},"changes":["Keep required work"],"tests":["Inspect missing effect"]}
    packet={"source":{"instruction":"required goal "*200},"review_feedback":feedback}
    pinned=[{"role":"system","content":"p"*14000},{"role":"user","content":"c"*5000}]
    directories={}
    value=diagnostic_projection_with_reads(packet,character_budget=12000,directories=directories,pinned_messages=pinned,wire_budget=22000,fallback_wire_budget=25000)
    wire=diagnostic_text_size([*pinned,{"role":"user","content":json.dumps(value,ensure_ascii=False)}])
    assert 22000<wire<=25000
    assert value["source"]["instruction"]==packet["source"]["instruction"]
    assert value["review_feedback"]["criteria"]["regression_risk"]==feedback["criteria"]["regression_risk"]
    ref=value["review_feedback"]["review_details"]["ref"]
    assert json.loads(directories[ref])==feedback



def test_semantic_read_progress_ignores_shortened_outline_and_paging_prose(tmp_path):
    from agent.skills.analysis_session import LearnerConversation
    from shared.artifacts import ArtifactStore
    session=LearnerConversation(ArtifactStore(tmp_path), {"source":"bound"})
    items=[{"source":"source/event:"+str(i)+"@1","step":i,"type":"tap","text":"Distinct action "+str(i),"truncated":False} for i in range(5)]
    assert session.observe_read_result({"status":"success","data":{"items":items,"omitted_events":0,"next_step":None}})
    assert not session.observe_read_result({"status":"success","summary":"different envelope","data":{"items":items[:2],"omitted_events":3,"next_step":2}})
    smaller={**items[0],"text":"Distinct", "truncated":True}
    assert not session.observe_read_result({"status":"success","data":{"items":[smaller]}})
    richer={**items[0],"text":"Distinct action 0 with a newly supplied parameter"}
    assert session.observe_read_result({"status":"success","data":{"items":[richer]}})
    assert not session.observe_read_result({"status":"success","data":{"items":[items[0]]}})
    assert session.observe_read_result({"status":"success","data":{"items":[{**items[0],"source":"source/event:0@2"}]}})


def test_semantic_read_progress_preserves_native_pages_versions_and_images(tmp_path):
    from agent.skills.analysis_session import LearnerConversation
    from shared.artifacts import ArtifactStore
    session=LearnerConversation(ArtifactStore(tmp_path), {})
    def read(item):return {"status":"succeeded","data":{"items":[item]}}
    first={"source":"source/observation:visible@1","observation_id":"visible","text":"abcdefgh","truncated":True,"continue_source":"source/observation:visible@1#8"}
    assert session.observe_read_result(read(first))
    assert not session.observe_read_result(read({**first,"text":"abc","continue_source":"source/observation:visible@1#3"}))
    assert session.observe_read_result(read({**first,"text":"abcdefgh","continue_source":"source/observation:visible@1#16"}))
    whole={k:v for k,v in first.items() if k not in {"truncated","continue_source"}}
    whole["text"]="abcdefghabcdefgh"
    assert not session.observe_read_result(read(whole))  # Both positions were already delivered.
    assert not session.observe_read_result(read(first))
    image={"source":first["source"],"observation_id":"visible"}
    assert session.observe_read_result(read(image))
    assert not session.observe_read_result(read(image))
    assert session.observe_read_result(read({**image,"source":"source/observation:visible@2"}))
    assert not session.observe_read_result({"status":"deferred","data":{"items":[whole]}})
    assert not session.observe_read_result({"status":"failed","data":{"items":[whole]}})
    assert not session.observe_read_result({"status":"succeeded","data":{"items":[],"more_matches":99}})


def test_frozen_read_progress_preserves_offset_and_binding(tmp_path):
    from agent.skills.analysis_session import LearnerConversation
    from shared.artifacts import ArtifactStore
    session=LearnerConversation(ArtifactStore(tmp_path), {})
    page={"source":"official_skill:app/SKILL.md@hash","hash":"hash","text":"observed text","offset":0,"truncated":True,"next_offset":13}
    assert session.observe_read_result(page)
    assert not session.observe_read_result({**page,"text":"observed","next_offset":8})
    assert session.observe_read_result({**page,"offset":13,"truncated":False,"next_offset":None})
    assert session.observe_read_result({**page,"source":"official_skill:app/SKILL.md@newhash","hash":"newhash"})
    assert session.observe_read_result({"ref":"archive","content_hash":"exact","offset":0,"text":"new archived facts","next_offset":18})
    assert not session.observe_read_result({"ref":"archive","content_hash":"exact","offset":0,"text":"new archived","next_offset":12})


def test_learner_intent_directory_retains_atomic_kinds_and_parameter_constraints():
    from agent.session import executor_action_variants_schema
    from agent.skills.exploration import learner_action_directory
    import json
    schema=executor_action_variants_schema();original=copy.deepcopy(schema)
    directory=learner_action_directory(schema)
    assert schema==original
    assert len(json.dumps(directory))<len(json.dumps(schema))
    for variant,item in zip(schema["anyOf"],directory):
        assert item["type"]==variant["properties"]["type"]["enum"][0]
        assert item["required"]==variant.get("required",[])
        assert set(item["parameters"])==set(variant["properties"])-{"type"}
        def compare(raw,compact):
            if isinstance(raw,dict):
                assert set(compact)==set(raw)-{"description","title","default"}
                for key in compact:compare(raw[key],compact[key])
            elif isinstance(raw,list):
                assert len(raw)==len(compact)
                for a,b in zip(raw,compact):compare(a,b)
            else:assert raw==compact
        for key,value in item["parameters"].items():compare(variant["properties"][key],value)


@pytest.mark.asyncio
async def test_shortened_duplicate_outlines_end_reads_under_existing_no_progress_rule(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    items=[{"source":"event:"+str(i)+"@1","step":i,"type":"tap","text":"Observed action "+str(i)} for i in range(6)]
    supplied=[]
    def outline(*a,**k):
        n=6-len(supplied)*2;supplied.append(n)
        return {"items":items[:n],"omitted_events":6-n,"next_step":n if n<6 else None}
    monkeypatch.setattr("agent.skills.exploration.execution_outline",outline)
    requests=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{});requests.append(copy.deepcopy(messages))
        if len(requests)<4:
            assert kwargs["tools"]
            return GatewayResponse(model=model,content="",tool_calls=[ToolCall(id=str(len(requests)),name="read_execution_outline",arguments="{}")])
        assert kwargs["tools"] is None
        assert all(any(m.get("tool_call_id")==str(i) for m in messages) for i in range(1,4))
        return GatewayResponse(model=model,content='{"decision":"skip","reason":"Unresolved hypothesis retained; repeated pages added no facts"}')
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget(max_calls=20,reserve_calls=2)
    value=await backend.diagnose("test","learn",{},budget)
    assert value["decision"]=="skip" and budget.calls==4 and budget.actions==0
    assert supplied==[6,4,2] and backend.learner_conversation.stalled_rounds==2



def test_feedback_fallback_measures_after_whole_optional_entry_removal():
    import json
    from agent.skills.exploration import diagnostic_projection_with_reads,diagnostic_text_size
    from agent.skills.learning import LearningStopped
    packet={"source":{"instruction":"g"*21000},"review_feedback":{"verdict":"pass"},
        "matched_experiences":[{"namespace":"own_trial","text":"x"*6000}]}
    pinned=[{"role":"system","content":"p"*22000}]
    result=diagnostic_projection_with_reads(packet,character_budget=30000,directories={},
        pinned_messages=pinned,wire_budget=40000,fallback_wire_budget=46000)
    wire=diagnostic_text_size([*pinned,{"role":"user","content":json.dumps(result,ensure_ascii=False,separators=(",", ":"))}])
    assert 40000<wire<=46000
    assert result["source"]==packet["source"] and result["review_feedback"]==packet["review_feedback"]
    assert not result["matched_experiences"]
    assert result["context_omissions"]["matched_experiences"]=={"count":1,"history_refs":["own_trial"]}
    assert len(packet["matched_experiences"])==1
    with pytest.raises(LearningStopped,match="diagnostic_context_budget"):
        diagnostic_projection_with_reads({**packet,"source":{"instruction":"g"*27000}},
            character_budget=30000,directories={},pinned_messages=pinned,
            wire_budget=40000,fallback_wire_budget=46000)



def test_checkpoint_moves_initial_previews_to_native_directory_without_losing_constraints():
    import json
    from agent.skills.exploration import checkpoint_phase_input
    packet={"initial_diagnosis":True,"source":{"instruction":"Preserve existing content",
        "execution_limits":{"max_steps":12},"outcome":{"success":False},"counts":{"events":3},
        "events":[{"source":"event:1@1","step":1,"preview":"old preview "*800}],
        "observations":[{"source":"observation:visible@2","app":"com.demo","preview":"UI "*900}],
        "notes":[{"source":"note:gap@1","title":"Unknown dependency","preview":"x"*900}]},
        "candidate_json_schema":{"required":["evidence"]},"verification_goal":"Measure benefit",
        "verification_checks":["source"],"verification_conditions":{"source":"Keep ordinary limits"},
        "budget":{"remaining_calls":17},"existing_active_guidance":{"items":[{"content":"existing constraint"}]}}
    messages=[{"role":"system","content":"system"},{"role":"user","content":json.dumps(packet)},
        {"role":"assistant","content":"Original history remains archived"}]
    original=copy.deepcopy(messages);result=checkpoint_phase_input(messages);projected=json.loads(result[1]["content"])
    assert messages==original and len(result)==2
    assert len(result[1]["content"])<len(messages[1]["content"])
    for key in ("instruction","execution_limits","outcome","counts"):
        assert projected["source"][key]==packet["source"][key]
    assert projected["source"]["events"]==[{"source":"event:1@1","step":1}]
    assert projected["source"]["observations"]==[{"source":"observation:visible@2","app":"com.demo"}]
    assert projected["source"]["notes"][0]["truncated"]
    for key in packet.keys()-{"source"}:assert projected[key]==packet[key]
    assert checkpoint_phase_input(result)==result


@pytest.mark.asyncio
async def test_reviewer_frozen_guidance_identifier_is_a_repairable_tool_mismatch(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import REVIEW_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    requests=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{});requests.append(copy.deepcopy(messages))
        if len(requests)==1:
            return GatewayResponse(model=model,content="",tool_calls=[ToolCall(id="mistake",name="read_review_evidence",arguments='{"source":"official_skill:apps/com.demo/core/SKILL.md@1.0.1"}')])
        tool=next(m for m in messages if m.get("tool_call_id")=="mistake")
        data=json.loads(tool["content"]);assert data["status"]=="failed" and "contracts.related_skills" in data["reason"]
        assert "No evidence supplied" in data["policy"]
        return GatewayResponse(model=model,content='{"verdict":"insufficient","criteria":{},"changes":[],"tests":[]}')
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget(max_calls=8)
    result=await backend.review("test",REVIEW_SYSTEM,{},budget)
    assert result["verdict"]=="insufficient" and budget.calls==2 and budget.actions==0
