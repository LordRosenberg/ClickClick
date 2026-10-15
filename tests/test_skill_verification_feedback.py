"""Reviewer choices, exact result feedback, cost boundaries and retained negatives."""
import copy
from types import SimpleNamespace

import pytest

from agent.skills.admission import check_review_contract, source_validation
from agent.skills.learning import LearningBudget, digest, run_task_learning, validation_gate
from agent.skills.pending import get_pending
from agent.skills.verification import available_checks, verification_plan
from evaluation.skill_learning import Experiment
from test_task_skill_learning import candidate, context, validation
from test_skill_evidence_admission import setup_backend, supported_review

PLAN = {"checks": ["source", "variant"], "reason": "Check actual rename completion; preserve original limits and rule, change only task file/content parameters."}


@pytest.mark.asyncio
async def test_unscored_runtime_receipt_settles_cost_without_a_task_outcome(tmp_path):
    import json
    from evaluation.skill_learning import EvaluationInfrastructureError, official_success
    e = Experiment.__new__(Experiment)
    e.root = tmp_path
    e.args = SimpleNamespace(rounds=50, seconds=2400)
    e.validation_budget = LearningBudget(max_calls=900, max_actions=280, max_seconds=10800)
    receipt = {'role_invocations':17, 'logical_action_steps':9, 'elapsed_s':80,
               'score':None, 'environment_status':'not_scored'}
    async def failed_worker(name, case, skills):
        folder = tmp_path/name
        folder.mkdir()
        (folder/'runtime-result.json').write_text(json.dumps(receipt), encoding='utf-8')
        raise EvaluationInfrastructureError('official scoring unavailable')
    e._run_episode_async = failed_worker
    with pytest.raises(EvaluationInfrastructureError, match='scoring unavailable'):
        await e._episode_async('candidate', 'case', tmp_path)
    cost = e.validation_budget.snapshot()
    assert cost['calls'] == 17 and cost['actions'] == 9
    assert not cost['calls_include_reserved_upper_bounds']
    assert cost['validation_children'][0]['seconds'] == 80
    assert official_success(receipt) is None


class Validator:
    def __init__(self, frozen, tmp_path, outcomes=None):
        self.frozen, self.tmp_path = frozen, tmp_path
        self.calls = []
        self.outcomes = outcomes or {}

    def validation_checks(self): return ["source", "variant", "near_miss", "related_normal"]

    async def __call__(self, draft, old, *, budget, checks=None):
        self.calls.append((list(checks), budget))
        child = budget.reserve_validation("selected", calls=6, actions=2)
        budget.settle_validation(child, {"role_invocations": 3, "logical_action_steps": 2})
        overlay = self.frozen.overlay(draft.target, draft.new_text, self.tmp_path / f"overlay{len(self.calls)}")
        result = validation(draft, old)
        result.update(baseline_library_hash=self.frozen.manifest["hash"], candidate_library_hash=overlay.manifest["hash"])
        result["trials"] = [row for row in result["trials"] if row["kind"] in checks]
        for row in result["trials"]:
            row.update(baseline_library_hash=self.frozen.manifest["hash"], candidate_library_hash=overlay.manifest["hash"])
            row["baseline_success"], row["candidate_success"] = self.outcomes.get(row["kind"], (False, True))
        return result


@pytest.mark.parametrize("value", [[], {}, {"checks": [], "reason": ""}, {"checks": ["source", "source"], "reason": "x"},
    {"checks": ["custom_app_route"], "reason": "x"}, {"checks": ["source"], "reason": ""},
    {"checks": ["source"], "reason": "x", "inputs": {"answer": 2}}])
def test_malformed_or_arbitrary_verification_is_rejected(value):
    with pytest.raises(ValueError): verification_plan(value)


def test_verification_is_optional_and_declared_by_host():
    assert verification_plan(None) is None
    assert verification_plan({"checks": ["variant", "source"], "reason": "x"})["checks"] == ["source", "variant"]
    with pytest.raises(ValueError, match="unavailable"):
        verification_plan(PLAN, available=["source"])
    assert available_checks(None) == []
    check_review_contract({**supported_review(), "verification": PLAN})


@pytest.mark.asyncio
async def test_explicit_no_verification_preserves_local_admission_without_dispatch(context, tmp_path):
    settings, _, task, store, library = context
    declined = {"checks": [], "reason": "Existing observations suffice; no new task benefit is proven."}
    backend, frozen, draft = setup_backend(context, tmp_path,
        {**supported_review(), "verification": declined})
    validator = Validator(frozen, tmp_path)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validator)
    assert result["ok"] and result["eligible"] and validator.calls == []
    receipt = get_pending(result["pending_id"], root=library.root).meta["review"]["validation"]
    assert receipt["mode"] == "source_evidence" and receipt["utility"] == "unmeasured"
    assert verification_plan(declined, available=[]) == declined


@pytest.mark.asyncio
async def test_reviewer_subset_reaches_validator_and_continuous_feedback_without_forcing_matrix(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path, {**supported_review(), "verification": PLAN})
    validator = Validator(frozen, tmp_path)
    received = []
    backend.decisions = iter([{"decision": "propose", "candidate": draft.model_dump(), "verification": PLAN}, {"decision": "skip", "reason": "Keep the supported rule; observed gain is limited to these trials."}])
    native = backend.diagnose
    async def diagnose(model, system, payload, budget):
        received.append(copy.deepcopy(payload))
        return await native(model, system, payload, budget)
    backend.diagnose = diagnose
    async def final(*args): return supported_review()
    backend.review = final
    budget = LearningBudget()
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, validator=validator, budget=budget)
    assert result["eligible"] and len(result["verification_feedback"]) == 1
    assert validator.calls == [(["source", "variant"], budget)]
    assert received[0]["verification_checks"] == validator.validation_checks()
    assert [t["kind"] for t in received[1]["validation"]["trials"]] == ["source", "variant"]
    assert budget.calls == 3 and budget.actions == 2
    meta = get_pending(result["pending_id"], root=library.root).meta["review"]
    assert meta["validation"]["mode"] == "source_evidence"
    assert meta["validation"]["utility"] == "unmeasured"
    ordinary = meta["validation"]["ordinary_validation"]
    assert validation_gate(draft, digest(""), ordinary, baseline_manifest=frozen.manifest) == (False, "incomplete_coverage")


@pytest.mark.asyncio
@pytest.mark.parametrize('succeeded', [True, False])
async def test_completed_review_without_followups_seals_only_successful_feedback(context, tmp_path, succeeded):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path,
        {**supported_review(), 'verification': PLAN})
    validator = Validator(frozen, tmp_path,
        {kind: (False, succeeded) for kind in PLAN['checks']})
    backend.decisions = iter([{'decision': 'propose', 'candidate': draft.model_dump(), 'verification': PLAN},
        {'decision': 'explore', 'reason': 'An extra inspection'} if succeeded else
        {'decision': 'skip', 'reason': 'Keep unresolved task outcome'}])
    received, archived = [], []
    backend.record_completed_verification_feedback = lambda feedback, budget: archived.append(copy.deepcopy(feedback))
    native = backend.diagnose
    async def diagnose(model, system, payload, budget):
        received.append(copy.deepcopy(payload))
        return await native(model, system, payload, budget)
    backend.diagnose = diagnose
    async def final(*args):
        return {**supported_review(), 'verification': {'checks': [], 'reason': 'No follow-up for local admission'}}
    backend.review = final
    async def forbidden(*args, **kwargs):
        pytest.fail('Successful sealed feedback cannot dispatch a new device probe')
    backend.explore = forbidden
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validator)
    assert result['eligible'] and len(validator.calls) == 1
    if succeeded:
        assert len(received) == 1 and len(archived) == 1
        assert result['revision_stop']['reason'] == 'completed_verification_has_no_followup'
        assert archived == result['verification_feedback']
        assert archived[0]['origin'] == 'host' and 'decision' not in archived[0]
        assert all(t['candidate_success'] is True for t in archived[0]['validation']['trials'])
        assert archived[0]['review']['verification']['checks'] == []
        assert archived[0]['candidate_hash'] == digest(draft.new_text)
    else:
        assert len(received) == 2 and archived == []
        assert received[-1]['live_exploration_disabled'] is False
        assert all(t['candidate_success'] is False for t in received[-1]['validation']['trials'])
        assert received[-1]['candidate'] == draft.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize('priority', ['high', 'low', 'unknown'])
async def test_success_retains_significant_frontier_without_optimizing_minor_detours(context, tmp_path, priority):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path, {**supported_review(), 'verification': PLAN})
    opportunity = {'statement':'Repeated transition may omit a consequential state', 'priority':priority, 'status':'unknown'}
    backend.learner_conversation = SimpleNamespace(analysis={'omission_candidates':[opportunity]})
    backend.decisions = iter([{'decision':'propose','candidate':draft.model_dump()}, {'decision':'skip','reason':'No justified additional correction'}])
    received = []
    native = backend.diagnose
    async def diagnose(*args):
        received.append(copy.deepcopy(args[2]))
        return await native(*args)
    backend.diagnose = diagnose
    async def final(*args):
        return {**supported_review(), 'verification':{'checks':[], 'reason':'Scoped source check completed'}}
    backend.review = final
    validator = Validator(frozen, tmp_path)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, validator=validator)
    assert result['eligible'] and len(validator.calls) == 1
    assert len(received) == (2 if priority == 'high' else 1)
    if priority == 'high':
        assert received[-1]['remaining_opportunities'] == [opportunity]
        assert received[-1]['candidate'] == draft.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize('gap', ['unknown_outcome', 'unmatched', 'no_oracle', 'review_change', 'review_test', 'review_check', 'undeclared_check'])
async def test_final_feedback_remains_with_learner_when_a_followup_or_result_gap_exists(context, tmp_path, gap):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path, {**supported_review(), 'verification': PLAN})
    validator = Validator(frozen, tmp_path)
    received = []
    native = backend.diagnose
    backend.decisions = iter([{'decision':'propose', 'candidate':draft.model_dump()},
        {'decision':'skip', 'reason':'Retain the unresolved evidence and follow-up'}])
    async def diagnose(*args):
        received.append(copy.deepcopy(args[2]))
        return await native(*args)
    backend.diagnose = diagnose
    async def validate(actual, old, *, budget, checks=None):
        result = await validator(actual, old, budget=budget, checks=checks)
        if gap in ('unknown_outcome', 'unmatched', 'no_oracle'):
            field = {'unknown_outcome':'candidate_success', 'unmatched':'matched_environment', 'no_oracle':'independent_oracle'}[gap]
            result['trials'][0][field] = None if gap == 'unknown_outcome' else False
        return result
    validate.validation_checks = validator.validation_checks
    async def final(*args):
        result = {**supported_review(), 'verification':{'checks':[], 'reason':'No unchanged repetition'}}
        if gap == 'review_change':
            result.update(verdict='revise', changes=['Clarify the reported applicability'])
        if gap == 'review_test':
            result.update(verdict='insufficient', tests=[{'claim':'Prerequisite is dispensable',
                'gap':'Effect disputed', 'check':'Inspect the linked observation', 'decision':'Retain only if supported'}])
        if gap == 'review_check': result['verification']['checks'] = ['source']
        if gap == 'undeclared_check': result.pop('verification')
        return result
    backend.review = final
    backend.record_completed_verification_feedback = lambda *args: pytest.fail('Must not close incomplete or disputed feedback')
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, validator=validate)
    assert len(received) == 2
    assert result['verification_feedback'][0]['decision']['decision'] == 'skip'


@pytest.mark.asyncio
async def test_learner_request_cannot_dispatch_without_independent_reviewer_choice(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path)
    backend.decisions = iter([{"decision": "propose", "candidate": draft.model_dump(), "verification": PLAN}])
    validator = Validator(frozen, tmp_path)
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, validator=validator)
    assert result["eligible"] and validator.calls == [] and result["verification_feedback"] == []


@pytest.mark.asyncio
async def test_unavailable_review_check_stops_before_device_work(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, _ = setup_backend(context, tmp_path, {**supported_review(), "verification": PLAN})
    validator = Validator(frozen, tmp_path)
    validator.validation_checks = lambda: ["source"]
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, validator=validator)
    assert not result["ok"] and "unavailable" in result["reason"] and validator.calls == []


@pytest.mark.asyncio
async def test_positive_source_admission_cannot_erase_confirmed_ordinary_regression(context, tmp_path):
    settings, _, task, store, library = context
    backend, frozen, draft = setup_backend(context, tmp_path, {**supported_review(), "verification": PLAN})
    backend.decisions = iter([{"decision": "propose", "candidate": draft.model_dump()}, {"decision": "skip", "reason": "Keep negative evidence; do not claim improvement."}])
    validator = Validator(frozen, tmp_path, {"variant": (True, False)})
    async def final(*args): return supported_review()
    backend.review = final
    result = await run_task_learning(task, settings=settings, library=library, store=store, backend=backend, validator=validator)
    meta = get_pending(result["pending_id"], root=library.root).meta["review"]
    assert not result["eligible"] and meta["gate_reason"] == "confirmed_regression"
    assert meta["validation"]["trials"][1]["candidate_success"] is False


def test_source_receipt_binds_optional_ordinary_results(context, tmp_path):
    _, _, task, _, _ = context
    backend, frozen, draft = setup_backend(context, tmp_path)
    overlay = frozen.overlay(draft.target, draft.new_text, tmp_path / "overlay")
    ordinary = validation(draft)
    ordinary.update(baseline_library_hash=frozen.manifest["hash"], candidate_library_hash=overlay.manifest["hash"])
    ordinary["trials"] = ordinary["trials"][:1]
    ordinary["trials"][0].update(baseline_library_hash=frozen.manifest["hash"], candidate_library_hash=overlay.manifest["hash"])
    r = supported_review(); contracts = backend.review_contracts(draft)
    receipt = source_validation(draft, "", review=r, evidence=backend.review_evidence(draft), source_id=task.id,
        contracts=contracts, manifest=frozen.manifest, ordinary_validation=ordinary)
    kwargs = dict(baseline_manifest=frozen.manifest, review=r, contracts_hash=digest(contracts))
    assert validation_gate(draft, digest(""), receipt, **kwargs)[0]
    receipt["ordinary_validation"]["trials"][0]["candidate_success"] = False
    assert validation_gate(draft, digest(""), receipt, **kwargs)[1] == "source_ordinary_evidence_mismatch"
    receipt["ordinary_validation_hash"] = digest(receipt["ordinary_validation"])
    receipt["ordinary_validation"]["trials"][0]["baseline_success"] = True
    receipt["ordinary_validation_hash"] = digest(receipt["ordinary_validation"])
    assert validation_gate(draft, digest(""), receipt, **kwargs)[1] == "confirmed_regression"

@pytest.mark.asyncio
@pytest.mark.parametrize("source_outcomes,stage,expected_pairs", [((False,True),"complete",2),((False,False),"source_rejected",1),((True,True),"source_without_gain",1)])
async def test_harness_runs_only_selected_pairs_and_stops_without_source_gain(tmp_path, source_outcomes, stage, expected_pairs):
    from agent.skills.snapshot import LibrarySnapshot, library_manifest
    ex = Experiment.__new__(Experiment)
    ex.root = tmp_path / "ex"; ex.root.mkdir()
    empty = tmp_path / "empty"; empty.mkdir()
    ex.baseline_snapshot = LibrarySnapshot.freeze(empty, ex.root / "baseline-library")
    ex.baseline = ex.baseline_snapshot.root
    ex.args = SimpleNamespace(environment="androidworld",source="Task",variant="Task",variant_seed_offset=7,
        near_miss="Guard",related_normal="Normal",source_first_validation=False,rounds=2,seconds=10)
    ex.rows=[];ex.config={};ex.case_baselines={};ex.validation_runs={};ex.release_source=lambda:None
    visits=[]
    def episode(name, case, skills):
        visits.append(name)
        is_candidate=not name.startswith("baseline-")
        success=source_outcomes[1 if is_candidate else 0] if name.endswith("source") else True
        return {"score":success,"budgeted_success":success,"logical_action_steps":1,"role_invocations":1,
                "elapsed_s":1,"environment_status":"ready","initial_state_hash":"same", "configuration_hash":"same",
                "library_hash":library_manifest(skills)["hash"],"goal":"declared task"}
    ex.episode=episode
    budget=LearningBudget(max_calls=60,max_actions=10,max_seconds=60,reserve_calls=4)
    result=await ex.validate(candidate(),"",budget=budget,checks=["source","variant"])
    assert result["validation_stage"]==stage and len(result["trials"])==expected_pairs
    assert len(visits)==expected_pairs*2 and not any("miss" in x or "normal" in x for x in visits)
    assert budget.calls==len(visits) and budget.actions==len(visits)
    if expected_pairs==1:assert result["deferred_kinds"]==["variant"]
    ex.args.variant_seed_offset=None
    assert "variant" not in ex.validation_checks()


@pytest.mark.asyncio
async def test_same_candidate_cannot_repeat_a_check_by_changing_the_combination(context,tmp_path):
    settings, _, task, store, library=context
    backend, frozen, draft=setup_backend(context,tmp_path)
    plans=iter([{**supported_review(),"verification":PLAN},
                {**supported_review(),"verification":{"checks":["source"],"reason":"Repeat source only"}}])
    async def preflight(*args):return next(plans)
    backend.review_preflight=preflight
    backend.decisions=iter([{"decision":"propose","candidate":draft.model_dump()},
        {"decision":"propose","candidate":draft.model_dump(),"verification":{"checks":["source"],"reason":"Repeat source only"}}])
    async def final(*args):return supported_review()
    backend.review=final
    validator=Validator(frozen,tmp_path)
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,validator=validator)
    assert len(validator.calls)==1 and result["reason"]=="unchanged_candidate_and_evidence"

@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls,expected_pairs",[(15,0),(30,1)])
async def test_optional_check_budget_deferral_preserves_prior_evidence_and_local_admission(context,tmp_path,max_calls,expected_pairs):
    from agent.skills.snapshot import library_manifest
    settings,_,task,store,library=context
    backend,frozen,draft=setup_backend(context,tmp_path)
    ex=Experiment.__new__(Experiment);ex.root=tmp_path/'ex';ex.root.mkdir()
    ex.baseline_snapshot=frozen;ex.baseline=frozen.root;ex.config={};ex.rows=[];ex.case_baselines={};ex.validation_runs={};ex.release_source=lambda:None
    ex.args=SimpleNamespace(environment='androidworld',source='Task',variant='Task',variant_seed_offset=7,
        near_miss=None,related_normal=None,rounds=2,seconds=10)
    visited=[]
    def episode(name,case,skills):
        visited.append(name);learned=not name.startswith('baseline-')
        return {'score':learned,'budgeted_success':learned,'logical_action_steps':1,'role_invocations':12,
            'elapsed_s':1,'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same',
            'library_hash':library_manifest(skills)['hash']}
    ex.episode=episode
    budget=LearningBudget(max_calls=max_calls,max_actions=10,max_seconds=60,reserve_calls=4)
    ordinary=await ex.validate(draft,'',budget=budget,checks=['source','variant'])
    assert ordinary['validation_stage']=='budget_deferred' and len(ordinary['trials'])==expected_pairs
    assert budget.calls==expected_pairs*24 and len(visited)==expected_pairs*2
    r=supported_review();contracts=backend.review_contracts(draft)
    receipt=source_validation(draft,'',review=r,evidence=backend.review_evidence(draft),source_id=task.id,
        contracts=contracts,manifest=frozen.manifest,ordinary_validation=ordinary)
    assert validation_gate(draft,digest(''),receipt,baseline_manifest=frozen.manifest,review=r,contracts_hash=digest(contracts))[0]


def test_longer_native_evidence_list_is_preserved_under_total_checkpoint_capacity():
    from agent.skills.analysis_session import validate_analysis
    references=[f'source/event:{i}@1' for i in range(9)]
    resolved=[]
    def resolve(ref):resolved.append(ref);return ('event',{},0)
    result=validate_analysis({'working_path':[{'statement':'Recorded route','status':'observed','evidence':references}]},resolve)
    assert result['working_path'][0]['evidence']==references and resolved==references
    with pytest.raises(ValueError,match='checkpoint capacity'):
        validate_analysis({'working_path':[{'statement':'Recorded route','status':'observed','evidence':references*100}]},resolve)


@pytest.mark.asyncio
async def test_optional_feedback_revision_receives_fresh_independent_review(context,tmp_path):
    settings,_,task,store,library=context
    backend,frozen,draft=setup_backend(context,tmp_path)
    changed=draft.model_copy(update={'new_text':draft.new_text.replace('Query syntax','Quoted ingredient lookup')})
    choices=[]
    async def preflight(model,payload,budget):
        choices.append(payload['candidate']['new_text'])
        return {**supported_review(),**({'verification':PLAN} if len(choices)==1 else {})}
    backend.review_preflight=preflight
    backend.decisions=iter([{'decision':'propose','candidate':draft.model_dump()}, {'decision':'propose','candidate':changed.model_dump()}])
    async def final(*args):return supported_review()
    backend.review=final
    validator=Validator(frozen,tmp_path)
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,validator=validator)
    assert result['eligible'] and len(choices)==2 and len(validator.calls)==1
    meta=get_pending(result['pending_id'],root=library.root).meta['review']
    assert meta['candidate_hash']==digest(changed.new_text)
    assert 'ordinary_validation' not in meta['validation'] # Exact old-body trials cannot become revised-body results.


@pytest.mark.asyncio
async def test_host_outcome_receipt_is_citable_without_mutating_reused_source(context,tmp_path):
    from shared.config import Settings
    from shared.db import Database
    from shared.schemas import AgentState
    from shared.artifacts import ArtifactStore
    from agent.skills.snapshot import library_manifest
    settings,db,task,store,library=context
    _,frozen,draft=setup_backend(context,tmp_path)
    child_settings=Settings(_env_file=None,data_dir=tmp_path/'child-runtime')
    child_db=Database(child_settings.db_path)
    child=child_db.create_task(task.instruction,AgentState(instruction=task.instruction))
    before=store.records('measurement')
    histories={}
    async def release():pass
    backend=SimpleNamespace(task=task,release_device=release,owned_history_dbs=[],
        add_history=lambda name,history,state:histories.update({name:history}))
    ex=Experiment.__new__(Experiment);ex.root=tmp_path/'ex';ex.root.mkdir()
    ex.baseline_snapshot=frozen;ex.baseline=frozen.root;ex.config={};ex.rows=[];ex.validation_runs={}
    ex.release_source=lambda:None;ex.learning_backend=backend
    ex.args=SimpleNamespace(environment='androidworld',source='Task',variant=None,
        near_miss=None,related_normal=None,rounds=2,seconds=10)
    def episode(name,case,skills):
        learned=not name.startswith('baseline-')
        return {'score':learned,'budgeted_success':learned,'logical_action_steps':1,'role_invocations':1,
            'elapsed_s':1,'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same',
            'library_hash':library_manifest(skills)['hash'],'goal':task.instruction,
            'runtime_directory':str(child_settings.data_dir if learned else settings.data_dir),
            'clickclick_task_id':child.id if learned else task.id}
    ex.episode=episode
    ex.case_baselines={'Task':{**episode('baseline-source','Task',frozen.root),
        'score':True,'role_invocations':None,'elapsed_s':None}}
    try:
        result=await ex.validate(draft,'',budget=LearningBudget(max_calls=30,max_actions=10,max_seconds=60),checks=['source'])
        trial=result['trials'][0]
        namespace,reference=trial['pair_evidence_ref'].split('/',1)
        kind,key=reference.split('@')[0].split(':')
        receipt=histories[namespace].get(kind,key,1)['payload']
        assert receipt['baseline_success'] is False and receipt['candidate_success'] is True
        assert receipt['baseline_oracle_success'] is True and receipt['candidate_oracle_success'] is True
        assert receipt['overlay_hash']==digest(draft.new_text)
        assert receipt['baseline_cost']=={'actions':1,'model_requests':None,'seconds':None}
        assert receipt['candidate_cost']=={'actions':1,'model_requests':1,'seconds':1}
        assert receipt['candidate_activation']['executor_active_exact'] is None
        assert receipt['candidate_activation']['mechanism_adoption']=='unassessed'
        assert 'examples' not in receipt['candidate_activation']
        assert namespace==trial['candidate_execution_namespace']
        assert store.records('measurement')==before
    finally:
        for opened in backend.owned_history_dbs:opened.close()
        child_db.close()


@pytest.mark.asyncio
async def test_explicit_measurement_goal_survives_local_review_and_result_feedback(context,tmp_path):
    settings,_,task,store,library=context
    backend,frozen,draft=setup_backend(context,tmp_path)
    goal='Does this rule improve completion under the original task limits?'
    seen=[]
    native=backend.diagnose
    async def diagnose(model,system,payload,budget):
        assert payload['verification_goal']==goal
        return await native(model,system,payload,budget)
    backend.diagnose=diagnose
    backend.decisions=iter([{'decision':'propose','candidate':draft.model_dump()}, {'decision':'skip','reason':'Retain local rule with limited measured evidence'}])
    async def preflight(model,payload,budget):
        seen.append(payload['verification_goal'])
        return {**supported_review(),'verification':PLAN}
    async def final(model,system,payload,budget):
        seen.append(payload['verification_goal'])
        return supported_review()
    backend.review_preflight=preflight;backend.review=final
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,
        validator=Validator(frozen,tmp_path),verification_goal=goal)
    assert result['eligible'] and len(result['verification_feedback'])==1 and seen==[goal,goal]


@pytest.mark.asyncio
@pytest.mark.parametrize('goal',['',[], 'x'*1501])
async def test_invalid_operator_measurement_goal_stops_before_model_request(context,tmp_path,goal):
    settings,_,task,store,library=context
    backend,_,_=setup_backend(context,tmp_path)
    with pytest.raises(ValueError,match='bounded operator objective'):
        await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,verification_goal=goal)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop',[False,True])
async def test_final_learning_telemetry_records_outcome_without_source_mutation(context,tmp_path,stop):
    settings,_,task,store,library=context
    backend,_,_=setup_backend(context,tmp_path)
    before=store.records('event');recorded=[]
    backend._record_research=lambda role,phase,payload,budget,**kwargs:recorded.append((phase,payload,kwargs))
    if stop:
        async def interrupted(*args):raise RuntimeError('infrastructure stopped before model result')
        backend.diagnose=interrupted
        with pytest.raises(RuntimeError):
            await run_task_learning(task,settings=settings,library=library,store=store,backend=backend)
    else:
        result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend)
        assert result['eligible']
    assert recorded[-1][0]=='learning_outcome'
    assert recorded[-1][2]['status']==('stopped' if stop else 'completed')
    assert recorded[-1][1]['result']['ok'] is not stop
    assert store.records('event')==before


@pytest.mark.asyncio
async def test_incomplete_task_cannot_hide_independent_state_regression(context,tmp_path):
    settings,_,task,store,library=context
    backend,frozen,draft=setup_backend(context,tmp_path,{**supported_review(),"verification":{"checks":["source"],"reason":"Check actual task benefit"}})
    backend.decisions=iter([{"decision":"propose","candidate":draft.model_dump()},{"decision":"skip","reason":"Retain negative evidence"}])
    validator=Validator(frozen,tmp_path,{"source":(False,False)})
    async def validate(actual,old,*,budget,checks=None):
        result=await validator(actual,old,budget=budget,checks=checks)
        result["trials"][0].update(baseline_oracle_success=True,candidate_oracle_success=False)
        return result
    validate.validation_checks=validator.validation_checks
    async def final(*args):return supported_review()
    backend.review=final
    result=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,validator=validate)
    meta=get_pending(result["pending_id"],root=library.root).meta["review"]
    assert not result["eligible"] and meta["gate_reason"]=="confirmed_regression"
    ordinary=meta["validation"]
    assert validation_gate(draft,digest(""),ordinary,baseline_manifest=frozen.manifest)[1]=="confirmed_regression"
    receipt=source_validation(draft,"",review=supported_review(),evidence=backend.review_evidence(draft),source_id=task.id,
        contracts=backend.review_contracts(draft),manifest=frozen.manifest,ordinary_validation=ordinary)
    assert validation_gate(draft,digest(""),receipt,baseline_manifest=frozen.manifest,review=supported_review(),contracts_hash=digest(backend.review_contracts(draft)))[1]=="confirmed_regression"


from test_skill_candidate_group import setup as group_setup
from agent.skills.candidate_group import candidate_files, candidate_hash, check_trial_binding, validate_group
from agent.skills.snapshot import library_manifest


@pytest.mark.asyncio
async def test_real_validator_uses_one_joint_overlay_and_retains_failed_source(group_setup):
    from evaluation.skill_learning import Experiment
    s = group_setup
    experiment = Experiment.__new__(Experiment)
    experiment.root = s.tmp/'real-validator'; experiment.root.mkdir()
    experiment.baseline_snapshot = s.frozen; experiment.baseline = s.frozen.root
    experiment.case_baselines = {}; experiment.validation_runs = {}; experiment.config = {}
    experiment.release_source = lambda: None
    experiment.args = SimpleNamespace(environment='mobileworld', source='Source', variant=None,
        near_miss=None, related_normal=None, source_first_validation=False)
    dispatched = []
    async def episode(name, case, skills):
        manifest = library_manifest(skills)
        dispatched.append((name, manifest))
        return {'score':{'success':False}, 'logical_action_steps':5, 'role_invocations':7, 'elapsed_s':1,
            'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same',
            'library_hash':manifest['hash'],'goal':s.task.instruction}
    experiment._episode_async = episode
    old = validate_group(s.group, s.library, s.backend.observed_apps(), s.backend.resolve_evidence)
    result = await experiment.validate(s.group, old, checks=['source'])
    assert len(dispatched) == 2 and set(dispatched[1][1]['files']) == set(candidate_files(s.group))
    assert result['trials'][0]['candidate_success'] is False
    check_trial_binding(s.group, old, result, s.frozen.manifest)

from test_task_skill_learning import json, asyncio, TaskStore, Settings, Database, AgentState, TaskStatus, ArtifactStore

@pytest.mark.asyncio
async def test_experiment_does_not_force_efficient_success_to_learn(context, monkeypatch, tmp_path):
    from evaluation.skill_learning import Experiment
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    experiment = Experiment.__new__(Experiment)
    experiment.root = tmp_path / 'experiment'; experiment.root.mkdir()
    experiment.settings = settings
    experiment.model = 'unused'
    experiment.empty = library.root; experiment.empty.mkdir(); experiment.baseline = experiment.empty
    experiment.args = SimpleNamespace(target='fixture', environment='androidworld',
        learn_calls=24, learn_actions=20, learn_seconds=600)
    driver = FixtureDriver()
    async def close_provider(): pass
    driver.close_observation_provider = close_provider
    monkeypatch.setattr('evaluation.skill_learning.get_driver', lambda *args, **kwargs: driver)
    result = await experiment.learn({'runtime_directory': str(settings.data_dir),
        'clickclick_task_id': task.id, 'score': 1})
    assert result['reason'] == 'no_candidate_signal'
    assert json.loads((experiment.root / 'trigger.json').read_text())['signals'] == []
    assert not (experiment.root / 'diagnoses.json').exists()



def test_trial_evidence_is_bounded_and_does_not_mix_learning_task_events(context):
    from evaluation.skill_learning import trial_execution_evidence
    settings, db, task, store, _ = context
    for i in range(50):
        store.put("event", str(i), {"step": i, "executor_report": "long report " * 200,
            "submitted_action": {"type": "replace_text" if i == 0 else "tap",
                "text": "description phrase" if i == 0 else None}})
    other = db.create_task("unrelated", AgentState(instruction="unrelated"))
    TaskStore(db, store.artifacts, other.id).put("event", "other",
        {"step": 0, "submitted_action": {"type": "replace_text", "text": "unrelated query"}})
    result = trial_execution_evidence({"runtime_directory": str(settings.data_dir),
        "clickclick_task_id": task.id, "logical_action_steps": 50, "role_invocations": 60,
        "elapsed_s": 100, "execution_limits": {"max_steps": 34, "max_seconds": 900}},
        character_budget=2500)
    assert len(json.dumps(result, ensure_ascii=False)) <= 2500 and result["omitted_events"] > 0
    assert result["text_input_count"] == 1
    assert result["text_inputs"][0]["text"] == "description phrase"
    assert result["execution_limits"]["max_steps"] == 34 and result["cost"]["model_requests"] == 60



@pytest.mark.asyncio
async def test_experiment_preserves_cost_on_validation_exception(context,monkeypatch,tmp_path):
    import evaluation.skill_learning as evaluation
    settings,db,task,store,library=context
    experiment=evaluation.Experiment.__new__(evaluation.Experiment)
    experiment.root=tmp_path/'experiment';experiment.root.mkdir()
    experiment.settings=settings;experiment.model='test';experiment.empty=library.root;experiment.baseline=library.root
    experiment.args=SimpleNamespace(target='test',environment='androidworld',learn_calls=12,
        learn_actions=14,learn_seconds=500,force_learning=False)
    cleanup=[]
    class Driver:
        async def close_observation_provider(self):cleanup.append('driver')
    class Backend:
        def __init__(self,task,**kwargs):
            self.source_store=TaskStore(kwargs['db'],kwargs['artifacts'],task.id)
            self.library=kwargs['library']
        async def diagnose(self,*a,**kw):raise AssertionError('native call forbidden')
        async def explore(self,*a,**kw):raise AssertionError('device call forbidden')
        async def release_device(self):cleanup.append('release')
        def close(self):cleanup.append('backend')
    async def fail(*a,**kw):
        meter=kw['budget'];meter.meter('model_call_started',{});meter.meter('model_call_started',{})
        meter.action();meter.started-=1
        raise RuntimeError('ordinary validation infrastructure failed')
    monkeypatch.setattr(evaluation,'get_driver',lambda *a,**kw:Driver())
    monkeypatch.setattr(evaluation,'ExplorationBackend',Backend)
    monkeypatch.setattr(evaluation,'run_task_learning',fail)
    with pytest.raises(RuntimeError,match='ordinary validation infrastructure failed'):
        await experiment.learn({'runtime_directory':str(settings.data_dir),'clickclick_task_id':task.id,'score':0})
    cost=json.loads((experiment.root/'phase-cost.json').read_text(encoding='utf-8'))
    assert cost['calls']==2 and cost['actions']==1 and cost['seconds']>=1
    assert cleanup==['release','backend','driver']
    assert not (experiment.root/'learning.json').exists()



@pytest.mark.parametrize('score,expected',[(0,False),(0.5,False),(1.0,True),(None,None),(2,None)])
def test_official_androidworld_partial_score_is_scored_failure(score,expected):
    from evaluation.skill_learning import official_success
    assert official_success({'evaluation_environment':'androidworld','score':score}) is expected
    if score==0.5:assert official_success({'score':score}) is None



@pytest.mark.asyncio
@pytest.mark.parametrize('same_instruction', [False, True])
async def test_validation_holdout_histories_do_not_become_original_goal_experiences(context,tmp_path,same_instruction):
    from evaluation.skill_learning import Experiment
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    e=Experiment.__new__(Experiment);e.root=tmp_path/'experiment';e.root.mkdir()
    e.empty=e.root/'empty';e.empty.mkdir();e.model='test';e.config={}
    from agent.skills.snapshot import LibrarySnapshot
    e.baseline_snapshot=LibrarySnapshot.freeze(e.empty,e.root/'baseline-library');e.baseline=e.baseline_snapshot.root
    e.case_baselines={};e.validation_runs={};e.release_source=lambda:None;e.learning_backend=backend
    e.args=SimpleNamespace(environment='androidworld',source='Source',variant=None,near_miss=None,related_normal=None,source_first_validation=False)
    before=task.model_dump()
    def episode(name,case,skills):
        hs=Settings(_env_file=None,data_dir=e.root/name/'runtime');hd=Database(hs.db_path)
        instruction=task.instruction if same_instruction else task.instruction+' with different inputs'
        ht=hd.create_task(instruction,AgentState(instruction=instruction));hd.update_task(ht.id,status=TaskStatus.SUCCEEDED)
        hd.close()
        return {'score':True,'budgeted_success':True,'logical_action_steps':1,'role_invocations':1,'elapsed_s':1,
            'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same',
            'runtime_directory':str(hs.data_dir),'clickclick_task_id':ht.id,'goal':instruction}
    e.episode=episode
    try:
        result=await e.validate(candidate(),'')
        assert result['trials'][0]['matched_environment'] and result['trials'][0]['independent_oracle']
        assert len(backend.additional_histories)==2
        assert len(backend.experiences)==(2 if same_instruction else 0)
        if not same_instruction:
            namespace=result['trials'][0]['candidate_execution_namespace']
            with pytest.raises(ValueError,match='same task instruction'):
                backend.add_experience(namespace,origin='candidate_assisted',oracle_success=True,matched_environment=True,config_hash='same')
        assert db.get_task(task.id).model_dump()==before
    finally:
        backend.close()
