"""Fit complete learning requests without changing ordinary execution."""
import copy
import json

import pytest

from agent.skills.exploration import LearningSession, LearningExecutor, LearningHistoryCapacity, ExplorationBackend
from agent.skills.learning import LearningStopped, LearningBudget
from agent.revisable.session import ExecutionSession
from agent.revisable.roles import PlanExecutor
from agent.tool_registry import AgentToolRegistry, AgentToolSpec, ToolCategory
from driver.fixture import FixtureDriver
from shared.llm_gateway import GatewayError
from test_task_skill_learning import context


def settings_for(settings):
    return settings.model_copy(update={'models_json':json.dumps({'test':{'context':{
        'executor':{'history_tokens':4000},
        'skill_learner':{'history_tokens':80000,'max_input_tokens':100000}}}})})


@pytest.mark.asyncio
async def test_full_wire_overhead_rejects_before_dispatch_and_preserves_current_pixels(context,monkeypatch):
    settings,*_=context
    settings=settings_for(settings)
    session=LearningSession('executor','test',settings=settings)
    session.set_stable_system('required policy '+('x'*60000))
    monkeypatch.setattr(session,'_k_wire',lambda:[])
    registry=AgentToolRegistry()
    registry.register(AgentToolSpec(name='read',description='d'*30000,
        category=ToolCategory.KNOWLEDGE,roles=('executor',),parameters={'type':'object'}),lambda *_:None)
    monkeypatch.setattr(session,'_build_registry',lambda _:registry)
    seen=[]
    async def dispatched(self,observations,**kwargs):
        seen.append((observations,kwargs));return 'accepted'
    monkeypatch.setattr(ExecutionSession,'run',dispatched)
    observation=[{'role':'user','content':[{'type':'text','text':'current grounding'},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,a'}}]}]
    history=[{'role':'user','content':'h'*204000}]
    from agent.context_projection import estimate_context
    # The initial packet fits, but a subsequent frame/tool exchange would not.
    initial=estimate_context([*session._stable,*history,*observation])+estimate_context([
        {'role':'system','content':json.dumps(registry.catalog_for_role(session.role),ensure_ascii=False)}])
    assert initial<100000
    originals=copy.deepcopy((observation,history))
    with pytest.raises(LearningHistoryCapacity) as raised:
        await session.run(observation,history_messages=history)
    assert 0<raised.value.history_tokens<80000 and not seen
    assert (observation,history)==originals
    assert session.settings.context_policy('test','executor')['max_input_tokens']==100000
    assert await session.run(observation,history_messages=[{'role':'user','content':'closed-step summary'}])=='accepted'
    assert seen[0][0]==observation and seen[0][1]['tool_registry'] is registry
    assert settings.context_policy('test','executor')['history_tokens']==4000


@pytest.mark.asyncio
@pytest.mark.parametrize('still_too_large',[False,True])
async def test_pre_dispatch_fit_reuses_compaction_with_fresh_grounding_and_one_retry(context,monkeypatch,still_too_large):
    settings,_,_,store,_=context
    learner=LearningExecutor(FixtureDriver(),store.artifacts,model='test',settings=settings_for(settings))
    original=learner.settings;calls=[];fresh=[]
    async def observe(self,**kwargs):fresh.append(True);return 'fresh frame'
    monkeypatch.setattr('agent.skills.exploration.ActionObservationTransaction.observe_current',observe)
    async def act(self,*args,**kwargs):
        calls.append(self.settings.context_policy('test','executor'))
        if len(calls)==1:raise LearningHistoryCapacity(5000)
        assert calls[-1]['history_tokens']==5000 and calls[-1]['max_input_tokens']==100000
        assert await kwargs['observe_after_handoff']()=='fresh frame'
        if still_too_large:raise LearningHistoryCapacity(3000)
        return 'completed'
    monkeypatch.setattr(PlanExecutor,'act_once',act)
    if still_too_large:
        with pytest.raises(LearningStopped,match='learning_recent_context_capacity'):
            await learner.act_once('question','old frame')
    else:assert await learner.act_once('question','old frame')=='completed'
    assert len(calls)==2 and fresh==[True] and learner.settings is original


@pytest.mark.asyncio
@pytest.mark.parametrize('category',['context_capacity','transient'])
async def test_mid_invocation_capacity_retains_probe_evidence_without_replaying_tools(context,monkeypatch,category):
    settings,db,task,source_store,library=context
    original=task.model_dump()
    backend=ExplorationBackend(task,db=db,artifacts=source_store.artifacts,
        driver=FixtureDriver(),settings=settings,library=library)
    backend.freeze_library();calls=[]
    async def act(self,*args,**kwargs):
        calls.append(True)
        self.store.put('note','partial_probe',{'title':'Partial finding','content':'Observed route, outcome unknown'})
        self.intent_id=self.environment.intent({'type':'home'},args[1].observation_id)
        raise GatewayError('request boundary',category=category)
    monkeypatch.setattr(LearningExecutor,'act_once',act)
    try:
        if category=='transient':
            with pytest.raises(GatewayError):
                await backend.explore('Inspect the route',3,LearningBudget(max_calls=20,reserve_calls=2),context={})
        else:
            evidence=await backend.explore('Inspect the route',3,LearningBudget(max_calls=20,reserve_calls=2),context={})
            assert evidence['outcome']=='context_capacity' and evidence['actions_attempted']==0
            assert backend.store.get('note','partial_probe')['payload']['content'].endswith('outcome unknown')
            assert any(row['payload'].get('phase')=='executor_request' for row in backend.store.records('measurement'))
            assert not evidence['environment']['verified_clean']
        assert calls==[True] and db.get_task(task.id).model_dump()==original
        assert not source_store.records('measurement')
    finally:backend.close()
