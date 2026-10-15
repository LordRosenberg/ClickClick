"""Local admission survives budgeted, source-local task recovery."""
import copy
import hashlib

import pytest

from agent.skills.learning import LearningBudget, LearningStopped, digest, run_task_learning
from agent.skills.pending import get_pending
from agent.skills.task_goal import TaskGoal
from test_task_skill_learning import context
from test_skill_evidence_admission import setup_backend, supported_review
from test_skill_verification_feedback import Validator
from test_skill_candidate_group import setup, group_review

SOURCE = {"checks": ["source"], "reason": "Measure the newly supported source-task breakthrough"}


class NativeCappedBudget(LearningBudget):
    native_limit = 4

    def remaining_calls(self, *, exploration=False):
        native_used = sum(row.get('calls', 0) for row in self.stage_costs.values())
        return min(super().remaining_calls(exploration=exploration),
            self.native_limit - native_used - (self.reserve_calls if exploration else 0))


def test_child_allowance_cannot_authorize_native_reads_or_consume_native_reserve():
    budget = NativeCappedBudget(max_calls=200, reserve_calls=2)
    for _ in range(2):
        budget.exploration_meter('model_call_started', {})
    with pytest.raises(LearningStopped, match='model_budget_reserved'):
        budget.exploration_meter('model_call_started', {})
    child = budget.reserve_validation('ordinary', calls=72, actions=12)
    budget.settle_validation(child, {'role_invocations':15, 'logical_action_steps':10})
    assert budget.snapshot()['remaining_calls'] == 2
    assert budget.snapshot()['remaining_exploration_calls'] == 0
    for _ in range(2):
        budget.meter('model_call_started', {})
    with pytest.raises(LearningStopped, match='model_budget'):
        budget.meter('model_call_started', {})
    assert budget.calls == 19 and budget.remaining_calls() == 0


@pytest.mark.asyncio
async def test_native_diagnosis_reserves_review_slots_under_separate_child_ceiling(context, monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    store.put('observation', 'state', {'text':'Distinct live evidence'})
    budget = NativeCappedBudget(max_calls=200, reserve_calls=2)
    requests = []
    async def complete(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        kwargs['attempt_meter']('model_call_started', {})
        if len(requests) == 1:
            assert kwargs.get('tools')
            return GatewayResponse(content='', model=model, tool_calls=[ToolCall(id='read',
                name='read_history', arguments=json.dumps({'source':'observation:state@1'}))])
        assert not kwargs.get('tools')
        return GatewayResponse(content=json.dumps({'decision':'skip', 'reason':'No supported candidate',
            'trajectory_analysis':{'unresolved':[{'statement':'A useful rule is unknown',
                'status':'unknown','evidence':[]}]}}), model=model)
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    result = await backend.diagnose('test', 'learn', {}, budget)
    assert result['decision'] == 'skip' and len(requests) == 2
    assert budget.remaining_calls() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('variant_success', [True, False])
async def test_reviewer_can_select_variant_after_source_success_without_new_learner_write(context, tmp_path, variant_success):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path)
    seen = meter_diagnosis(backend)
    async def preflight(*args):
        return {**supported_review(), 'verification':SOURCE}
    backend.review_preflight = preflight
    reviews = []
    async def final(model, system, payload, budget):
        budget.meter('model_call_started', {})
        reviews.append(copy.deepcopy(payload))
        if len(reviews) == 1:
            assert [t['kind'] for t in payload['validation']['trials']] == ['source']
            return {**supported_review(), 'verification':{'checks':['variant'],
                'reason':'Source succeeded; test unchanged rule with different task parameters'}}
        assert [t['kind'] for t in payload['validation']['trials']] == ['source', 'variant']
        return {**supported_review(), 'verification':{'checks':[],
            'reason':'Retain source success; record the independent variant outcome'}}
    backend.review = final
    validator = Validator(frozen, tmp_path, {'source':(False, True), 'variant':(False, variant_success)})
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validator, task_success_goal=True)
    assert len(seen) == 1 and [row[0] for row in validator.calls] == [['source'], ['variant']]
    assert len(reviews) == 2
    assert result['eligible'] and result['task_goal']['status'] == 'verified_success'
    assert result['verification_feedback'][0]['origin'] == 'independent_reviewer'
    assert result['verification_feedback'][0]['validation']['trials'][0]['candidate_success'] is variant_success


@pytest.mark.asyncio
async def test_post_probe_diagnosis_retains_review_read_capacity(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path)
    decisions = iter([{'decision':'explore','question':'Test a prerequisite','max_actions':1},
        {'decision':'propose','candidate':draft.model_dump(),'verification':SOURCE}])
    seen = []
    async def diagnose(model, system, payload, budget):
        seen.append(copy.deepcopy(payload))
        rounds = payload.get('diagnostic_read_rounds', 0)
        if len(seen) == 2:
            assert rounds == 1
        for _ in range(rounds + 1):
            budget.meter('model_call_started', {})
        return next(decisions)
    async def audit(*args, **kwargs):
        args[2].meter('model_call_started', {})
        return {'verdict':'run','reasons':['A bounded discriminating test']}
    async def explore(question, actions, budget, **kwargs):
        budget.meter('model_call_started', {}); budget.action()
        return {'outcome':'advance','actions_attempted':1}
    async def preflight(model, payload, budget):
        for _ in range(4): budget.meter('model_call_started', {})
        return {**supported_review(),'verification':SOURCE}
    async def final(model, system, payload, budget):
        for _ in range(3): budget.meter('model_call_started', {})
        return {**supported_review(),'verification':{'checks':[],'reason':'Source established'}}
    backend.diagnose, backend.review_probe, backend.explore = diagnose, audit, explore
    backend.review_preflight, backend.review = preflight, final
    budget = NativeCappedBudget(max_calls=200, reserve_calls=8)
    budget.native_limit = 12
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget, validator=Validator(frozen,tmp_path), task_success_goal=True)
    assert result['eligible'] and result['task_goal']['status'] == 'verified_success'
    assert budget.remaining_calls() == 0 and budget.calls == 15


@pytest.mark.asyncio
async def test_preflight_read_capacity_preserves_final_adjudication(context, monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    store.put('observation','state',{'text':'A resulting state'})
    budget = NativeCappedBudget(max_calls=200)
    budget.native_limit = 3
    requests = []
    async def complete(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages)); kwargs['attempt_meter']('model_call_started',{})
        if len(requests) == 1:
            assert kwargs.get('tools')
            return GatewayResponse(content='', model=model, tool_calls=[ToolCall(id='read',
                name='read_review_evidence', arguments=json.dumps({'source':'source/observation:state@1'}))])
        assert not kwargs.get('tools')
        return GatewayResponse(content=json.dumps(supported_review()), model=model)
    monkeypatch.setattr('agent.skills.exploration.complete',complete)
    result = await backend.review_preflight('test',
        {'phase':'draft_preflight','verification_checks':['source']},budget)
    assert result['verdict'] == 'pass' and len(requests) == 2 and budget.remaining_calls() == 1
    budget.meter('model_call_started',{})


def meter_diagnosis(backend):
    original = backend.diagnose
    seen = []
    async def diagnose(model, system, payload, budget):
        budget.meter('model_call_started', {})
        seen.append(copy.deepcopy(payload))
        return await original(model, system, payload, budget)
    backend.diagnose = diagnose
    return seen


@pytest.mark.asyncio
async def test_local_pass_continues_probe_then_one_selected_source_success(context, tmp_path):
    settings, _, task, store, library = context
    b, frozen, first = setup_backend(context, tmp_path)
    revised = first.model_copy(update={'new_text':first.new_text+'Use the observed detail entry before searching.\n'})
    b.decisions = iter([{'decision':'propose','candidate':first.model_dump()},
        {'decision':'explore','question':'Test the remaining detail prerequisite','max_actions':1},
        {'decision':'propose','candidate':revised.model_dump()}])
    seen = meter_diagnosis(b)
    async def preflight(model, payload, budget):
        budget.meter('model_call_started', {})
        assert payload['task_success_goal'] is True
        return {**supported_review(), **({'verification':SOURCE} if payload['candidate']==revised.model_dump() else {})}
    b.review_preflight = preflight
    async def final(model, system, payload, budget):
        budget.meter('model_call_started', {})
        return {**supported_review(), 'verification':{'checks':[], 'reason':'Source success established; no new check'}}
    b.review = final
    validator = Validator(frozen, tmp_path)
    budget = LearningBudget(max_calls=24, max_actions=6)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=b, budget=budget, validator=validator, task_success_goal=True)
    assert result['task_goal']['status']=='verified_success' and result['eligible']
    assert b.explored==['Test the remaining detail prerequisite'] and len(seen)==3
    assert len(validator.calls)==1 and validator.calls[0]==(['source'],budget)
    assert seen[1]['task_goal_feedback']['retained_candidates']==[]  # Current body already supplied.
    assert seen[2]['task_goal_feedback']['retained_candidates']==[first.model_dump()]
    assert get_pending(result['retained_pending_ids'][0]['id'],root=library.root).meta['review']['eligible']
    assert len(list((library.root/'_pending').glob('*.json')))==2
    assert not (library.root/first.target).exists() and result['cost']['actions']==3


@pytest.mark.asyncio
async def test_explicit_skip_preserves_admitted_local_rule_without_forcing_trial(context, tmp_path):
    settings, _, task, store, library = context
    b, _, draft = setup_backend(context, tmp_path)
    b.decisions = iter([{'decision':'propose','candidate':draft.model_dump()},
        {'decision':'skip','reason':'No grounded affordable breakthrough remains'}])
    seen = meter_diagnosis(b)
    async def forbidden(*args, **kwargs): pytest.fail('Skip must not force exploration or ordinary execution')
    b.explore = forbidden
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=b, validator=forbidden, task_success_goal=True)
    assert result['eligible'] and len(seen)==2
    assert result['task_goal']=={'status':'unresolved','stop':{
        'reason':'learner_skip','detail':'No grounded affordable breakthrough remains'}}
    assert result['retained_pending_ids'][0]['id']==result['pending_id']


@pytest.mark.asyncio
async def test_one_skip_reassessment_after_local_admission_retains_rule_and_can_recover(context, tmp_path):
    from agent.skills.learning import HYPOTHESIS_SYSTEM
    settings, _, task, store, library = context
    b, frozen, first = setup_backend(context, tmp_path)
    revised = first.model_copy(update={'new_text': first.new_text+'A separately observed remaining transition.\n'})
    b.decisions = iter([
        {'decision':'explore', 'question':'Establish a local fact', 'max_actions':1},
        {'decision':'propose', 'candidate':first.model_dump()},
        {'decision':'skip', 'reason':'No reason to revise the admitted rule'},
        {'decision':'explore', 'question':'Test a different remaining explanation', 'max_actions':1},
        {'decision':'propose', 'candidate':revised.model_dump()}])
    systems, seen = [], []
    original = b.diagnose
    async def diagnose(model, system, payload, budget):
        budget.meter('model_call_started', {})
        systems.append(system); seen.append(copy.deepcopy(payload))
        return await original(model, system, payload, budget)
    b.diagnose = diagnose
    async def preflight(model, payload, budget):
        budget.meter('model_call_started', {})
        return {**supported_review(), **({'verification':SOURCE}
            if payload['candidate']==revised.model_dump() else {})}
    async def final(model, system, payload, budget):
        budget.meter('model_call_started', {})
        return {**supported_review(), 'verification':{'checks':[], 'reason':'Matched source established'}}
    b.review_preflight, b.review = preflight, final
    validator = Validator(frozen, tmp_path)
    budget = LearningBudget(max_calls=24, max_actions=8)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=b, budget=budget, validator=validator, task_success_goal=True)
    assert len(systems) == 5, result
    assert systems[3] == HYPOTHESIS_SYSTEM and 'skip_reassessment' in seen[3]
    assert b.explored == ['Establish a local fact', 'Test a different remaining explanation']
    assert result['acquisition_ledger']['skip_reassessments'] == 1
    assert result['task_goal']['status'] == 'verified_success' and len(validator.calls) == 1
    assert get_pending(result['retained_pending_ids'][0]['id'], root=library.root).meta['review']['eligible']
    assert budget.max_calls == 24 and budget.max_actions == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('experimental', [False, True])
async def test_matched_local_rule_failure_uses_next_analysis_for_strategy_then_new_body_check(context, tmp_path, experimental):
    from agent.skills.learning import RECOVERY_SYSTEM, LEARN_SYSTEM
    settings, _, task, store, library = context
    b, frozen, first = setup_backend(context, tmp_path, {**supported_review(), 'verification':SOURCE})
    revised = first.model_copy(update={'new_text':first.new_text+'Use the newly observed replacement route.\n'})
    b.decisions = iter([{'decision':'propose','candidate':first.model_dump()},
        {'decision':'explore','question':'Test the earliest unknown replacement','max_actions':1},
        {'decision':'propose','candidate':revised.model_dump()}])
    original = b.diagnose; systems = []
    async def diagnose(model, system, payload, budget):
        budget.meter('model_call_started', {})
        systems.append(system)
        if len(systems) == 2:
            assert system == (RECOVERY_SYSTEM if experimental else LEARN_SYSTEM)
            assert payload['task_goal_feedback']['status'] == 'unresolved'
            assert payload['validation']['trials'][0]['candidate_success'] is False
            assert bool(payload.get('recovery_strategy_synthesis')) is experimental
            if experimental:
                assert payload['diagnostic_read_rounds'] == 0
        return await original(model, system, payload, budget)
    b.diagnose = diagnose
    async def final(model, system, payload, budget):
        budget.meter('model_call_started', {})
        return {**supported_review(), 'verification':{'checks':[], 'reason':'Retain supported local knowledge'}}
    b.review = final
    validator = Validator(frozen, tmp_path, {'source':(False, False)})
    old_explore = b.explore
    async def explore(*args, **kwargs):
        result = await old_explore(*args, **kwargs)
        validator.outcomes['source'] = (False, True)
        return result
    b.explore = explore
    budget = LearningBudget(max_calls=24, max_actions=8)
    result = await run_task_learning(task,settings=settings,library=library,store=store,
        backend=b,budget=budget,validator=validator,task_success_goal=True,
        experimental_recovery_synthesis=experimental)
    assert len(systems) == 3 and systems[2] == LEARN_SYSTEM
    assert len(validator.calls) == 2 and b.explored == ['Test the earliest unknown replacement']
    assert result['task_goal']['status'] == 'verified_success' and result['eligible']
    assert result['acquisition_ledger']['hypothesis_synthesis_attempted'] is experimental
    assert len(list((library.root/'_pending').glob('*.json'))) == 2
    assert not (library.root/first.target).exists()


@pytest.mark.parametrize('fault', ['none', 'body', 'base', 'library', 'overlay', 'kind', 'baseline', 'candidate', 'environment', 'oracle', 'config'])
def test_strategy_synthesis_requires_exact_matched_failed_source(fault):
    from agent.skills.snapshot import _hash
    c = {'target':'apps/demo/core/SKILL.md', 'new_text':'A local rule'}
    manifest = {'hash':'library', 'files':{}}
    overlay = _hash({c['target']:hashlib.sha256(c['new_text'].encode()).hexdigest()})
    trial = {'kind':'source','case_id':'source','baseline_success':False,'candidate_success':False,
        'matched_environment':True,'independent_oracle':True,'config_hash':'config',
        'baseline_library_hash':'library','candidate_library_hash':overlay}
    v = {'candidate_hash':'body','base_hash':'base','baseline_library_hash':'library',
        'candidate_library_hash':overlay,'trials':[trial]}
    if fault in {'body','base','library','overlay'}:
        v[{'body':'candidate_hash','base':'base_hash','library':'baseline_library_hash','overlay':'candidate_library_hash'}[fault]] = 'foreign'
    elif fault != 'none':
        key = {'kind':'kind','baseline':'baseline_success','candidate':'candidate_success',
            'environment':'matched_environment','oracle':'independent_oracle','config':'config_hash'}[fault]
        trial[key] = 'variant' if fault == 'kind' else (None if fault == 'config' else True if fault in {'baseline','candidate'} else False)
    assert TaskGoal().matched_failed_source(v,candidate_hash='body',base_hash='base',
        baseline_manifest=manifest,candidates=[c]) is (fault == 'none')


@pytest.mark.asyncio
async def test_later_rejection_does_not_revoke_previous_admission(context, tmp_path):
    settings, _, task, store, library = context
    b, _, draft = setup_backend(context, tmp_path)
    changed = draft.model_copy(update={'new_text':draft.new_text+'Unsupported shortcut.\n'})
    b.decisions = iter([{'decision':'propose','candidate':draft.model_dump()},
        {'decision':'propose','candidate':changed.model_dump()}, {'decision':'skip','reason':'Shortcut refuted'}])
    meter_diagnosis(b)
    async def preflight(model, payload, budget):
        r = supported_review()
        if payload['candidate']==changed.model_dump():
            r['verdict']='reject'; r['criteria']['regression_risk']['status']='fail'
        return r
    b.review_preflight = preflight
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=b, task_success_goal=True)
    assert not result['eligible'] and result['task_goal']['status']=='unresolved'
    retained = get_pending(result['retained_pending_ids'][0]['id'],root=library.root)
    assert retained.path.with_suffix('.md').read_text('utf-8')==draft.new_text and retained.meta['review']['eligible']
    assert not get_pending(result['pending_id'],root=library.root).meta['review']['eligible']


@pytest.mark.asyncio
async def test_goal_budget_exhaustion_preserves_pending_without_unusable_diagnosis(context, tmp_path):
    settings, _, task, store, library = context
    b, _, _ = setup_backend(context, tmp_path)
    seen = meter_diagnosis(b)
    async def preflight(model, payload, budget):
        budget.meter('model_call_started', {})
        return supported_review()
    b.review_preflight = preflight
    budget=LearningBudget(max_calls=3,reserve_calls=0)
    result=await run_task_learning(task,settings=settings,library=library,store=store,
        backend=b,budget=budget,task_success_goal=True)
    assert result['eligible'] and len(seen)==1 and budget.calls==2
    assert result['task_goal']['stop']['reason']=='revision_capacity_exhausted'
    assert result['retained_pending_ids']


@pytest.mark.asyncio
async def test_successful_variant_does_not_complete_original_task_goal(context, tmp_path):
    settings, _, task, store, library=context
    plan={'checks':['variant'],'reason':'Measure a supported input variation'}
    b,frozen,draft=setup_backend(context,tmp_path,{**supported_review(),'verification':plan})
    b.decisions=iter([{'decision':'propose','candidate':draft.model_dump()},
        {'decision':'skip','reason':'Original source recovery remains unknown'}])
    seen=meter_diagnosis(b)
    async def final(*args): return {**supported_review(),'verification':{'checks':[],'reason':'No further grounded check'}}
    b.review=final
    result=await run_task_learning(task,settings=settings,library=library,store=store,
        backend=b,validator=Validator(frozen,tmp_path),task_success_goal=True)
    assert len(seen)==2 and result['eligible'] and result['task_goal']['status']=='unresolved'


@pytest.mark.asyncio
async def test_distinct_recovery_rounds_share_budget_beyond_prose_repair_limit(context, tmp_path):
    settings, _, task, store, library = context
    b, frozen, draft = setup_backend(context,tmp_path)
    drafts=[draft.model_copy(update={'new_text':draft.new_text+f'Observed prerequisite {i}.\n'}) for i in range(4)]
    b.decisions=iter({'decision':'propose','candidate':d.model_dump()} for d in drafts)
    seen=meter_diagnosis(b)
    async def preflight(*args): return {**supported_review(),'verification':SOURCE}
    b.review_preflight=preflight
    async def final(*args): return {**supported_review(),'verification':{'checks':[],'reason':'Retain knowledge separately'}}
    b.review=final
    validator=Validator(frozen,tmp_path,{'source':(False,False)})
    native=validator.__call__
    async def validate(draft, old, *, budget, checks):
        validator.outcomes={'source':(False,len(validator.calls)==3)}
        return await native(draft,old,budget=budget,checks=checks)
    validate.validation_checks=validator.validation_checks
    budget=LearningBudget(max_calls=30,max_actions=12)
    result=await run_task_learning(task,settings=settings,library=library,store=store,
        backend=b,budget=budget,validator=validate,task_success_goal=True)
    assert result['task_goal']['status']=='verified_success'
    assert len(seen)==len(validator.calls)==4 and all(row[1] is budget for row in validator.calls)
    assert len(list((library.root/'_pending').glob('*.json')))==4 and budget.actions==8


@pytest.mark.parametrize('fault',['unscored','probe_only','foreign_body','foreign_base','unmatched','no_oracle','no_config','partial_admission','foreign_library','no_case','unknown_baseline'])
def test_task_success_requires_exact_admitted_overlay_and_matched_official_source(fault):
    goal=TaskGoal(); c={'target':'apps/demo/core/SKILL.md','new_text':'Rule'}
    result={'pending_id':'local','eligible':True}
    from agent.skills.snapshot import _hash
    overlay=_hash({c['target']:hashlib.sha256(c['new_text'].encode()).hexdigest()})
    manifest={'hash':'library','files':{}}
    v={'candidate_hash':'body','base_hash':'base','baseline_library_hash':'library','candidate_library_hash':overlay,
       'trials':[{'kind':'source','case_id':'source','baseline_success':False,
        'baseline_library_hash':'library','candidate_library_hash':overlay,
        'candidate_success':True,'matched_environment':True,'independent_oracle':True,'config_hash':'config'}]}
    candidates=[c]
    if fault=='unscored': v['trials'][0]['candidate_success']=None
    elif fault=='probe_only': v['trials'][0]['kind']='probe'
    elif fault=='foreign_body': v['candidate_hash']='foreign'
    elif fault=='foreign_base': v['base_hash']='foreign'
    elif fault=='unmatched': v['trials'][0]['matched_environment']=False
    elif fault=='no_oracle': v['trials'][0]['independent_oracle']=False
    elif fault=='no_config': v['trials'][0]['config_hash']=None
    elif fault=='foreign_library': v['trials'][0]['candidate_library_hash']='foreign'
    elif fault=='no_case': v['trials'][0]['case_id']=None
    elif fault=='unknown_baseline': v['trials'][0]['baseline_success']=None
    else: candidates.append({'target':'apps/other/core/SKILL.md','new_text':'Unadmitted'})
    goal.observe(result,candidates,v,candidate_hash='body',base_hash='base',baseline_manifest=manifest)
    assert goal.status=='unresolved' and goal.retained


@pytest.mark.asyncio
async def test_group_local_admission_can_continue_live_exploration_in_same_loop(setup):
    s=setup
    s.backend.reviews=[group_review(s.group)]
    decisions=iter([{'decision':'propose','candidate':s.group.model_dump()},
        {'decision':'explore','question':'Test remaining cross-app prerequisite','max_actions':1},
        {'decision':'skip','reason':'No further supported route'},
        {'decision':'skip','reason':'No different affordable discriminating test'}])
    seen=[]
    async def diagnose(model, system, payload, budget):
        budget.meter('model_call_started',{}); seen.append(copy.deepcopy(payload))
        return next(decisions)
    s.backend.diagnose=diagnose
    async def explore(question,actions,budget,*,context):
        budget.action(); return {'outcome':'replan','actions_attempted':1}
    s.backend.explore=explore
    result=await run_task_learning(s.task,settings=s.settings,library=s.library,store=s.store,
        backend=s.backend,task_success_goal=True)
    assert len(seen)==4 and seen[1]['live_exploration_disabled'] is False
    assert 'skip_reassessment' in seen[3] and result['acquisition_ledger']['skip_reassessments']==1
    assert result['eligible'] and len(result['retained_pending_ids'])==2
    assert result['task_goal']['status']=='unresolved' and result['cost']['actions']==1


@pytest.mark.asyncio
async def test_group_source_success_uses_normalized_reviewed_body_without_extra_diagnosis(setup):
    from agent.skills.candidate_group import candidate_hash, candidate_files
    s=setup
    for patch in s.group.patches:
        patch.new_text=patch.new_text.rstrip()
    s.backend.reviews=[group_review(s.group,checks=['source']),group_review(s.group)]
    seen=meter_diagnosis(s.backend)
    class Validator:
        supports_candidate_groups=True
        def validation_checks(self): return ['source']
        async def __call__(self,group,old,*,budget,checks):
            assert all(p.new_text.endswith('\n') for p in group.patches)
            overlay=s.frozen.overlay_many(candidate_files(group),s.tmp/'overlay')
            return {'candidate_hash':candidate_hash(group),'base_hash':digest(old),
                'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':overlay.manifest['hash'],
                'trials':[{'kind':'source','case_id':'case','config_hash':'config','baseline_success':False,
                    'candidate_success':True,'matched_environment':True,'independent_oracle':True,
                    'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':overlay.manifest['hash']}]}
    result=await run_task_learning(s.task,settings=s.settings,library=s.library,store=s.store,
        backend=s.backend,validator=Validator(),task_success_goal=True)
    assert result['task_goal']['status']=='verified_success' and len(seen)==1
    assert result['eligible'] and len(result['retained_pending_ids'])==2
