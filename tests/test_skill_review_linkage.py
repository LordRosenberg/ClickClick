"""Review receives bounded native transition context, without inferred success."""
from types import SimpleNamespace

import pytest

from agent.skills.exploration import ExplorationBackend
from driver.fixture import FixtureDriver
from test_task_skill_learning import context


@pytest.mark.parametrize('matching_stage', [True, False])
def test_observation_adds_only_same_stage_recorded_transition(context, matching_stage):
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,
        driver=FixtureDriver(),settings=settings,library=library)
    for key,step in [('row',0),('receipt_view',1),('detail',1)]:
        store.put('observation',key,{'step':step,'stage_id':'inspection','visits':[{'step':step,'stage_id':'inspection'}],
            'text':key,'image_ref':None})
    store.put('event','0',{'step':0,'stage_id':'inspection' if matching_stage else 'other',
        'decision':'act','observation_id':'row','submitted_action':{'type':'tap','index':2},
        'action_result':{'success':True,'receipt':{'observation_id':'receipt_view','observation_accepted':True}}})
    try:
        result=backend.review_evidence(SimpleNamespace(evidence=['source/observation:detail@1']))
        refs={item['source'] for item in result if 'source' in item}
        assert 'source/observation:detail@1' in refs
        assert ('source/event:0@1' in refs)==matching_stage
        assert ('source/observation:row@1' in refs)==matching_stage
        assert ('source/observation:receipt_view@1' in refs)==matching_stage
        assert all('not causal proof' in item.get('policy','') for item in result if 'source' in item)
    finally:backend.close()
