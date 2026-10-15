"""Explicit Learner capacity retains needed history without early compression."""
import json

import pytest

from agent.context_projection import estimate_context
from agent.skills.analysis_session import COMPACTION_SYSTEM
from agent.skills.exploration import ExplorationBackend, diagnostic_context_window
from agent.skills.learning import LearningBudget
from driver.fixture import FixtureDriver
from shared.config import Settings
from shared.llm_gateway import GatewayResponse
from test_task_skill_learning import context


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer_tokens", [None, 3000, 100000])
async def test_probe_audit_uses_own_capacity_and_preserves_required_plan(context, monkeypatch, reviewer_tokens):
    from agent.skills.learning import LearningStopped
    settings, db, task, store, library = context
    roles = {'skill_learner': {'max_input_tokens': 100000}}
    if reviewer_tokens is not None:
        roles['skill_reviewer'] = {'max_input_tokens': reviewer_tokens}
    settings = settings.model_copy(update={'models_json': json.dumps({'test': {'context': roles}}),
        'skill_reviewer_model': 'test'})
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    # Required test permissions exceed the legacy payload cap independently of
    # optional history. A larger Learner window must not authorize the Reviewer.
    from agent.skills.environment import ResearchPlan
    experiment = {'question': 'Question. '*150, 'reason': 'Reason. '*750,
        'hypothesis': 'Hypothesis. '*480, 'test': 'Transition. '*480,
        'environment_plan': ResearchPlan(mode='isolated', scope='Scope. '*170,
            preparation='Prepare. '*220, expected_condition='Observed. '*120,
            cleanup='Restore. '*220, cleanup_actions=2).model_dump()}
    seen = []
    async def ask(model, system, payload, settings):
        assert payload['experiment'] == experiment
        seen.append(payload)
        return {'verdict': 'run', 'reasons': []}
    budget = LearningBudget()
    monkeypatch.setattr(budget, 'ask', ask)
    audit_context = {'source': {'instruction': task.instruction + '\nPreserve unrelated records. '*1000}}
    try:
        if reviewer_tokens == 100000:
            assert (await backend.review_probe(experiment, 3, budget, context=audit_context))['verdict'] == 'run'
            assert len(seen) == 1
        else:
            with pytest.raises(LearningStopped, match='diagnostic_context_budget'):
                await backend.review_probe(experiment, 3, budget, context=audit_context)
            assert not seen and budget.actions == 0
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_complete_required_draft_can_use_explicit_window_above_legacy_payload_cap(context, monkeypatch):
    settings,db,task,store,library=context
    settings=settings.model_copy(update={'models_json':json.dumps({'test':{'context':{
        'skill_learner':{'history_tokens':80000,'max_input_tokens':100000}}}})})
    b=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    body='Required exact draft.\n'*1900
    calls=[]
    async def fake(model,messages,**kwargs):
        kwargs['attempt_meter']('model_call_started',{})
        calls.append(messages)
        phase=json.loads(messages[1]['content'])
        assert phase['candidate']['new_text']==body
        return GatewayResponse(model=model,content='{"decision":"skip","reason":"no supported revision"}')
    monkeypatch.setattr('agent.skills.exploration.complete',fake)
    try:
        result=await b.diagnose('test','Review exact draft',{'source':{'instruction':task.instruction},
            'candidate':{'target':'apps/com.demo/core/SKILL.md','new_text':body},
            'review_feedback':{'verdict':'revise','changes':['Check a required dependency']},
            'diagnostic_read_rounds':0},LearningBudget())
        assert result['decision']=='skip' and len(calls)==1
    finally:b.close()


def test_explicit_learner_history_reaches_exploration_without_changing_ordinary(context):
    from agent.skills.exploration import LearningExecutor, LearningSession
    from agent.revisable.roles import PlanExecutor
    settings, _, _, store, _ = context
    original_json=json.dumps({'test':{'marker':'preserve provider configuration','context':{
        'executor':{'history_tokens':4000},
        'skill_learner':{'history_tokens':80000,'max_input_tokens':100000}}}})
    settings=settings.model_copy(update={'models_json':original_json})
    ordinary=PlanExecutor(FixtureDriver(),store.artifacts,model='test',settings=settings)
    learner=LearningExecutor(FixtureDriver(),store.artifacts,model='test',settings=settings)
    restored_session=LearningSession('executor','test',settings=settings)
    assert ordinary.settings.context_policy('test','executor')['history_tokens']==4000
    assert settings.models_json==original_json
    for adapted in (learner.settings,learner._session.settings,restored_session.settings):
        assert adapted.context_policy('test','executor')=={'history_tokens':80000,'max_input_tokens':100000}
        assert adapted.provider_for('test')['marker']=='preserve provider configuration'


def test_input_ceiling_alone_does_not_invent_a_learner_history_window(context):
    from agent.skills.exploration import learner_execution_settings
    settings=context[0].model_copy(update={'models_json':json.dumps({'test':{
        'context':{'max_input_tokens':100000}}})})
    assert learner_execution_settings(settings,'test') is settings
    assert settings.context_policy('test','executor')['history_tokens']==settings.executor_context_tokens


def test_request_allocation_reserves_tools_and_two_images_and_honors_role_limit():
    tools = [{"type": "function", "function": {"name": "read_history", "description": "x" * 2000}}]
    settings = Settings(models_json=json.dumps({"test": {"context": {
        "max_input_tokens": 100000, "skill_learner": {"history_tokens": 20000}}}}))
    window = diagnostic_context_window(settings, "test", tools)
    request = [{"role": "user", "content": [{"type": "text", "text": "x" * (window - 1000)},
        *[{"type": "image_url", "image_url": {"url": "data:image/png;base64,a"}} for _ in range(2)]]},
        {"role": "user", "content": json.dumps({"tools": tools})}]
    assert estimate_context(request) <= 20000
    assert diagnostic_context_window(Settings(), "test") == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_capacity", [False, True])
async def test_needed_history_uses_configured_window_and_legacy_checkpoint_stays_valid(
        context, monkeypatch, explicit_capacity):
    settings, db, task, store, library = context
    if explicit_capacity:
        settings.models_json = json.dumps({"test": {"context": {"max_input_tokens": 100000}}})
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    budget = LearningBudget(max_calls=8, reserve_calls=0)
    seen = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        seen.append(messages)
        if messages[0]["content"] == COMPACTION_SYSTEM:
            return GatewayResponse(model=model, content=json.dumps({"unresolved": [
                {"statement": "The earlier raw report remains an unverified hypothesis.", "status": "unknown"}]}))
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Unresolved prerequisite"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    try:
        await backend.diagnose("test", "initial", {"diagnostic_read_rounds": 0}, budget)
        literal = "unverified raw report " * 1600
        backend.learner_conversation.history.extend([
            {"role": "user", "content": literal}, {"role": "assistant", "content": "Unknown prerequisite"}])
        result = await backend.diagnose("test", "policy " * 3400,
            {"source": {"instruction": task.instruction}, "diagnostic_read_rounds": 1}, budget)
        assert result["decision"] == "skip"
        if explicit_capacity:
            assert backend.learner_conversation.compactions == 0 and budget.calls == 2
            assert any(m.get("content") == literal for m in seen[-1])
        else:
            assert backend.learner_conversation.compactions == 1 and budget.calls == 3
            assert backend.learner_conversation.analysis["unresolved"][0]["status"] == "unknown"
    finally:
        backend.close()
