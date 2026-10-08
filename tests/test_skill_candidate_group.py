"""Multi-app admission boundaries; failed tasks never stand in for local effects."""
import json
from types import SimpleNamespace

import pytest

from agent.revisable.store import TaskStore
from agent.skills.candidate_group import (CandidateGroup, candidate_files, candidate_hash,
    check_group_receipt, check_trial_binding, dependency_admission, group_utility_gate,
    run_candidate_group, validate_group)
from agent.skills.learning import Candidate, CRITERIA, LearningBudget, digest, run_task_learning
from agent.skills.library import SkillLibrary, serialize_skill_markdown
from agent.skills.pending import approve_pending, approve_pending_group, get_pending
from agent.skills.snapshot import LibrarySnapshot, library_manifest
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus


def patch(app):
    return Candidate(target=f"apps/{app}/core/SKILL.md", new_text=serialize_skill_markdown(
        name=app.replace('.', '-'), description="Scoped query escaping", version="1",
        app=app, kind="app_core", body="## Procedure\nQuote hyphenated exact-match queries; confirm the returned detail.\n"),
        gist="Avoid incorrect token splitting", evidence=[f"source/observation:{app}@1"],
        scope="Exact hyphenated queries", benefit="Avoid a known unmatched query", risks="Check returned detail")


def local_review(verdict="pass", effect="unassessed", app="com.first"):
    return {"verdict": verdict, "criteria": {k: {"status": "pass" if verdict == "pass" else "insufficient", "reason": "literal app evidence"} for k in CRITERIA},
        "changes": [], "tests": [], "acceptance": "source_evidence",
        "effect": {"status": effect, "reason": "scope checked against native evidence",
            "evidence": [] if effect == "unassessed" else [f"source/event:{app}@1", f"source/observation:{app}@1"]}}


def group_review(group, verdicts=None, effects=None, checks=None):
    return {"patch_reviews": {p.target: local_review((verdicts or {}).get(p.target, "pass"),
        (effects or {}).get(p.target, "unassessed"), p.target.split('/')[1]) for p in group.patches},
        "verification": {"checks": checks or [], "reason": "One joint check only if the local effect needs it"}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path/'data')
    monkeypatch.setattr('shared.config.get_settings', lambda: settings)
    db = Database(settings.db_path)
    task = db.create_task("Find an exact hyphenated query in two apps", AgentState(instruction="Find queries"))
    db.update_task(task.id, status=TaskStatus.FAILED, failure_reason="second constraint unresolved")
    task = db.get_task(task.id)
    store = TaskStore(db, ArtifactStore(settings.artifacts_dir), task.id)
    for app in ('com.first', 'com.second'):
        store.put('observation', app, {'app': app, 'text': 'Quoted exact query returns the matching detail'})
        store.put('event', app, {'step': 1, 'submitted_action': {'type': 'type_text', 'text': '"a-b"'}, 'observation_id': app})
    library = SkillLibrary(tmp_path/'skills')
    library.root.mkdir()
    frozen = LibrarySnapshot.freeze(library.root, tmp_path/'frozen')
    group = CandidateGroup(patches=[patch('com.first'), patch('com.second')])
    class Backend:
        claim_admission = True
        def __init__(self):
            self.reviews = []
            self.feedback = []
        def freeze_library(self): return frozen
        def observed_apps(self): return {'com.first', 'com.second'}
        def resolve_evidence(self, ref):
            literal = ref.removeprefix('source/')
            kind, keyversion = literal.split(':', 1)
            key, version = keyversion.split('@')
            row = store.get(kind, key, int(version))
            if row is None: raise ValueError('unknown native reference')
            return kind, row, 0
        def review_evidence(self, candidate):
            return [{'source': ref, 'text': json.dumps(self.resolve_evidence(ref)[1]['payload'])} for ref in candidate.evidence]
        def review_contracts(self, candidate):
            from agent.skills.exploration import build_review_contracts
            return build_review_contracts(candidate.new_text, library, settings)
        async def diagnose(self, *args): return {'decision':'propose', 'candidate':group.model_dump()}
        async def review(self, model, system, payload, budget):
            budget.meter('model_call_started', {})
            return self.reviews.pop(0)
        def record_completed_verification_feedback(self, value, budget): self.feedback.append(value)
    backend = Backend()
    yield SimpleNamespace(settings=settings, task=task, store=store, library=library, frozen=frozen,
        backend=backend, group=group, tmp=tmp_path)
    db.close()


async def run(s, validator=None):
    return await run_task_learning(s.task, settings=s.settings, library=s.library, store=s.store,
        backend=s.backend, budget=LearningBudget(), validator=validator)


@pytest.mark.asyncio
async def test_failed_source_accepts_independent_second_app_skill(setup):
    s = setup
    first, second = s.group.patches
    s.backend.reviews = [group_review(s.group, {first.target:'insufficient'})]
    result = await run(s)
    assert result['accepted_targets'] == [second.target]
    assert result['cost']['calls'] == 1
    assert not any((s.library.root/p.target).exists() for p in s.group.patches)
    rejected, accepted = result['pending_ids']
    assert get_pending(accepted['id'], root=s.library.root).meta['app'] == 'com.second'
    with pytest.raises(ValueError, match='not locally admitted'):
        approve_pending(rejected['id'], root=s.library.root)
    approve_pending(accepted['id'], root=s.library.root)
    assert (s.library.root/second.target).read_text() == second.new_text


@pytest.mark.asyncio
async def test_failed_dependency_blocks_only_its_dependents(setup):
    s = setup
    first, second = s.group.patches
    s.group.dependencies = {second.target:[first.target]}
    s.backend.reviews = [group_review(s.group, {first.target:'insufficient'})]
    result = await run(s)
    assert result['accepted_targets'] == []
    assert get_pending(result['pending_ids'][1]['id'], root=s.library.root).meta['review']['gate_reason'] == 'dependency_not_admitted'


@pytest.mark.asyncio
async def test_joint_failed_task_can_support_local_effect_and_one_trial(setup):
    s = setup
    first, second = s.group.patches
    s.backend.reviews = [group_review(s.group, checks=['source']),
        group_review(s.group, {second.target:'insufficient'}, {first.target:'supported'})]
    class Validator:
        supports_candidate_groups = True
        def __init__(self): self.calls = []
        def validation_checks(self): return ['source']
        async def validate(self, group, old, *, budget, checks):
            self.calls.append((group, checks))
            snapshot = s.frozen.overlay_many(candidate_files(group), s.tmp/'overlay')
            return {'candidate_hash':candidate_hash(group), 'base_hash':digest(old),
                'baseline_library_hash':s.frozen.manifest['hash'], 'candidate_library_hash':snapshot.manifest['hash'],
                'trials':[{'kind':'source','case_id':'case','config_hash':'config',
                    'matched_environment':True,'independent_oracle':True,'baseline_success':False,'candidate_success':False,
                    'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':snapshot.manifest['hash']}]}
    validator = Validator()
    result = await run(s, validator.validate)
    assert len(validator.calls) == 1 and len(validator.calls[0][0].patches) == 2
    assert result['accepted_targets'] == [first.target]
    receipt = get_pending(result['pending_ids'][0]['id'], root=s.library.root).meta['review']['group_receipt']
    check_group_receipt(receipt)
    assert receipt['validation']['trials'][0]['candidate_success'] is False


@pytest.mark.asyncio
async def test_final_reviewer_checks_run_jointly_without_repeating_source(setup):
    s = setup
    s.backend.reviews = [group_review(s.group, checks=['source']),
        group_review(s.group, checks=['source', 'variant']),
        group_review(s.group, checks=['variant'])]
    class Validator:
        supports_candidate_groups = True
        def __init__(self): self.calls = []
        def validation_checks(self): return ['source', 'variant']
        async def validate(self, group, old, *, budget, checks):
            self.calls.append(checks)
            snapshot = s.frozen.overlay_many(candidate_files(group), s.tmp/f'overlay-{len(self.calls)}')
            return {'candidate_hash':candidate_hash(group), 'base_hash':digest(old),
                'baseline_library_hash':s.frozen.manifest['hash'], 'candidate_library_hash':snapshot.manifest['hash'],
                'trials':[{'kind':kind,'case_id':kind,'config_hash':'config',
                    'matched_environment':True,'independent_oracle':True,'baseline_success':False,'candidate_success':False,
                    'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':snapshot.manifest['hash']}
                    for kind in checks]}
    validator = Validator()
    result = await run(s, validator.validate)
    assert validator.calls == [['source'], ['variant']]
    assert len(result['accepted_targets']) == 2
    receipt = get_pending(result['pending_ids'][0]['id'], root=s.library.root).meta['review']['group_receipt']
    assert [t['kind'] for t in receipt['validation']['trials']] == ['source', 'variant']
    assert [c['executed_request'] for c in receipt['verification_execution']] == [['source'], ['variant']]
    check_group_receipt(receipt)


def test_group_followup_merge_retains_regression_and_rejects_foreign_binding():
    from agent.skills.candidate_group import merge_group_validation
    binding = dict(candidate_hash='body', base_hash='base', baseline_library_hash='empty', candidate_library_hash='overlay')
    source = {**binding, 'trials':[{'kind':'source','baseline_success':True,'candidate_success':False}], 'deferred_kinds':['variant']}
    variant = {**binding, 'trials':[{'kind':'variant','baseline_success':False,'candidate_success':True}]}
    merged = merge_group_validation(source, variant)
    assert merged['trials'] == [*source['trials'], *variant['trials']]
    assert merged['deferred_kinds'] == []
    with pytest.raises(ValueError, match='differently bound'):
        merge_group_validation(source, {**variant, 'candidate_hash':'other'})


def test_group_contract_projection_removes_only_exact_repeated_fields():
    from agent.skills.candidate_group import group_review_contracts
    contracts = {'a':{'prompts':{'executor':'shared policy'}, 'related_skills':[{'id':'a','text':'A rule'}]},
                 'b':{'prompts':{'executor':'shared policy'}, 'related_skills':[{'id':'b','text':'B rule'}]}}
    projected = group_review_contracts(contracts)
    assert projected['shared'] == {'prompts':{'executor':'shared policy'}}
    for target, original in contracts.items():
        assert {**projected['shared'], **projected['patches'][target]} == original
        assert projected['complete_contract_hashes'][target] == digest(original)


@pytest.mark.asyncio
async def test_exact_group_repair_reuses_source_for_new_reviewer_selected_variant(setup):
    s = setup
    second = s.group.patches[1]
    disputed = group_review(s.group, {second.target:'revise'})
    disputed['patch_reviews'][second.target]['changes'] = ['Resolve applicability through an available variant']
    s.backend.reviews = [group_review(s.group, checks=['source']), disputed,
        group_review(s.group, checks=['source','variant']), group_review(s.group)]
    decisions = iter([{'decision':'propose','candidate':s.group.model_dump()},
        {'decision':'propose','candidate':s.group.model_dump(),
         'verification':{'checks':['variant'],'reason':'Check the unresolved applicability boundary'}}])
    async def diagnose(*args): return next(decisions)
    s.backend.diagnose = diagnose
    class Validator:
        supports_candidate_groups = True
        def __init__(self): self.calls = []
        def validation_checks(self): return ['source','variant']
        async def validate(self, group, old, *, budget, checks):
            self.calls.append(checks)
            snapshot = s.frozen.overlay_many(candidate_files(group), s.tmp/f'repair-overlay-{len(self.calls)}')
            return {'candidate_hash':candidate_hash(group), 'base_hash':digest(old),
                'baseline_library_hash':s.frozen.manifest['hash'], 'candidate_library_hash':snapshot.manifest['hash'],
                'trials':[{'kind':kind,'case_id':kind,'config_hash':'config',
                    'matched_environment':True,'independent_oracle':True,'baseline_success':False,'candidate_success':False,
                    'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':snapshot.manifest['hash']}
                    for kind in checks]}
    validator = Validator()
    result = await run(s, validator.validate)
    assert validator.calls == [['source'], ['variant']]
    assert result['accepted_targets'] == sorted(candidate_files(s.group))
    receipt = get_pending(result['pending_ids'][-1]['id'], root=s.library.root).meta['review']['group_receipt']
    assert [t['kind'] for t in receipt['validation']['trials']] == ['source','variant']
    check_group_receipt(receipt)


@pytest.mark.asyncio
async def test_harmful_local_effect_does_not_pass_with_good_criteria(setup):
    s = setup
    first, second = s.group.patches
    s.backend.reviews = [group_review(s.group, effects={first.target:'contradicted'})]
    result = await run(s)
    assert result['accepted_targets'] == [second.target]


@pytest.mark.asyncio
async def test_dependent_publication_requires_closed_subset(setup):
    s = setup
    first, second = s.group.patches
    s.group.dependencies = {second.target:[first.target]}
    s.backend.reviews = [group_review(s.group)]
    result = await run(s)
    with pytest.raises(ValueError, match='together'):
        approve_pending(result['pending_ids'][1]['id'], root=s.library.root)
    assert not (s.library.root/second.target).exists()
    approve_pending_group([i['id'] for i in result['pending_ids']], root=s.library.root)
    assert all((s.library.root/p.target).exists() for p in s.group.patches)


@pytest.mark.asyncio
async def test_same_app_dependency_can_be_published_after_exact_prerequisite(setup):
    s = setup
    first = s.group.patches[0]
    second = first.model_copy(update={'target':'apps/com.first/workflows/exact-query/SKILL.md',
        'new_text':first.new_text.replace('name: com-first', 'name: exact-query').replace('kind: app_core','kind: workflow\ncapability: Exact query search') + '\n## Verification\nConfirm the returned detail.\n'})
    s.group.patches = [first, second]
    s.group.dependencies = {second.target:[first.target]}
    s.backend.reviews = [group_review(s.group)]
    result = await run(s)
    assert 'pending_ids' in result, result
    approve_pending(result['pending_ids'][0]['id'], root=s.library.root)
    approve_pending(result['pending_ids'][1]['id'], root=s.library.root)
    assert all((s.library.root/p.target).read_text() == p.new_text for p in s.group.patches)


@pytest.mark.parametrize('fault', ['cycle','unknown','duplicate','duplicate_id','unobserved'])
def test_structural_group_errors_precede_review_or_devices(setup, fault):
    s = setup
    a, b = s.group.patches
    if fault == 'cycle': s.group.dependencies = {a.target:[b.target], b.target:[a.target]}
    if fault == 'unknown': s.group.dependencies = {a.target:['unknown']}
    if fault == 'duplicate': s.group.patches.append(a.model_copy())
    if fault == 'duplicate_id': b.new_text = b.new_text.replace('name: com-second', 'name: com-first')
    if fault == 'unobserved': s.group.patches.append(patch('com.unseen'))
    with pytest.raises(ValueError):
        validate_group(s.group, s.library, s.backend.observed_apps(), s.backend.resolve_evidence)
    assert s.backend.reviews == []


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['body','receipt','base','manifest'])
async def test_group_publication_rejects_stale_bindings(setup, fault):
    s = setup
    s.backend.reviews = [group_review(s.group)]
    result = await run(s)
    item = get_pending(result['pending_ids'][0]['id'], root=s.library.root)
    if fault == 'body': item.path.with_suffix('.md').write_text('altered')
    if fault == 'receipt':
        item.meta['review']['group_receipt']['accepted'] = []
        item.path.write_text(json.dumps(item.meta))
    if fault in {'base','manifest'}:
        path = s.library.root/(s.group.patches[0].target if fault == 'base' else 'resources/changed.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('altered')
    with pytest.raises(ValueError): approve_pending(item.id, root=s.library.root)


@pytest.mark.parametrize('fault', ['hash','base','library'])
def test_joint_trial_requires_exact_bindings(setup, fault):
    s = setup
    old = validate_group(s.group, s.library, s.backend.observed_apps(), s.backend.resolve_evidence)
    snapshot = s.frozen.overlay_many(candidate_files(s.group), s.tmp/'overlay')
    trial = {'matched_environment':True,'independent_oracle':True,
        'baseline_library_hash':s.frozen.manifest['hash'], 'candidate_library_hash':snapshot.manifest['hash']}
    validation = {'candidate_hash':candidate_hash(s.group),'base_hash':digest(old),'trials':[trial],
        'baseline_library_hash':s.frozen.manifest['hash'], 'candidate_library_hash':snapshot.manifest['hash']}
    if fault == 'hash': validation['candidate_hash'] = 'wrong'
    if fault == 'base': validation['base_hash'] = 'wrong'
    if fault == 'library': validation['candidate_library_hash'] = 'wrong'
    with pytest.raises(ValueError): check_trial_binding(s.group, old, validation, s.frozen.manifest)


def test_overlay_many_changes_only_requested_files(setup):
    s = setup
    result = s.frozen.overlay_many(candidate_files(s.group), s.tmp/'overlay')
    assert set(result.manifest['files']) == set(candidate_files(s.group))
    assert library_manifest(s.frozen.root) == s.frozen.manifest
    with pytest.raises(ValueError): s.frozen.overlay_many({'../escape/SKILL.md':'bad'}, s.tmp/'bad')


@pytest.mark.parametrize('unknown', ['matched_environment','independent_oracle'])
def test_unmatched_task_trial_does_not_refute_independent_local_facts(setup, unknown):
    s = setup
    old = validate_group(s.group, s.library, s.backend.observed_apps(), s.backend.resolve_evidence)
    overlay = s.frozen.overlay_many(candidate_files(s.group), s.tmp/'overlay')
    trials = [{'kind':kind,'case_id':kind,'config_hash':'same', 'matched_environment':True,
        'independent_oracle':True,'baseline_success':False,'candidate_success':True,
        'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':overlay.manifest['hash']}
        for kind in ('source','variant','near_miss','related_normal')]
    trials[0][unknown] = False
    validation = {'candidate_hash':candidate_hash(s.group),'base_hash':digest(old),'trials':trials,
        'baseline_library_hash':s.frozen.manifest['hash'],'candidate_library_hash':overlay.manifest['hash']}
    check_trial_binding(s.group, old, validation, s.frozen.manifest)
    assert not group_utility_gate(s.group, old, validation, s.frozen.manifest)
    evidence = {p.target:s.backend.review_evidence(p) for p in s.group.patches}
    accepted, _ = dependency_admission(s.group, group_review(s.group)['patch_reviews'], evidence,
        validation=validation, trial_group=s.group, old=old, manifest=s.frozen.manifest)
    assert accepted == set(candidate_files(s.group))


def test_retained_context_preserves_rule_outcomes_and_exact_readable_details(setup):
    from agent.skills.analysis_session import retained_admission_context
    s = setup
    review = local_review(); review.pop('effect')
    review['criteria']['root_cause']['reason'] = 'Necessary exact qualification '*100
    payload = {'source':{'instruction':'Original goal'},'candidate':s.group.patches[0].model_dump(),
        'review_feedback':review, 'validation':{'candidate_hash':'exact', 'trials':[{
            'kind':'source','candidate_success':False,'baseline_success':False,'matched_environment':True,
            'independent_oracle':True,'pair_evidence_ref':'source/measurement:pair@1',
            'candidate_execution_namespace':'validation_candidate','candidate_execution':{
                'cost':{'actions':8},'last_report':'redundant report '*100}}]}}
    before = json.dumps(payload,sort_keys=True)
    directories = {}
    result = retained_admission_context(payload, directories)
    assert json.dumps(payload,sort_keys=True) == before
    assert result['source'] == payload['source'] and result['candidate']['new_text'] == payload['candidate']['new_text']
    assert result['validation']['trials'][0]['candidate_success'] is False
    assert result['validation']['trials'][0]['candidate_execution']['cost'] == {'actions':8}
    for key in ('candidate','review_feedback','validation'):
        ref = result[key]['details']['ref']
        assert digest(directories[ref]) == ref
        assert json.loads(directories[ref]) == payload[key]
    assert len(json.dumps(result)) < len(json.dumps(payload))


@pytest.mark.asyncio
async def test_recovered_goal_does_not_restart_failure_hypothesis_search(setup):
    s = setup
    calls = []
    async def diagnose(*args):
        calls.append(args); return {'decision':'skip','reason':'No valuable remaining opportunity'}
    s.backend.diagnose = diagnose
    s.backend.experiences = {'earlier':{'origin':'candidate_assisted'}}
    result = await run_task_learning(s.task, settings=s.settings, library=s.library, store=s.store,
        backend=s.backend, outcome_evidence={'success':False,'prior_candidate_recovered_goal':True})
    assert result['skipped'] and len(calls) == 1


@pytest.mark.asyncio
async def test_failed_group_repair_retains_already_admitted_independent_patch(setup):
    from shared.llm_gateway import GatewayError
    s = setup
    first, second = s.group.patches
    review = group_review(s.group, {second.target:'revise'})
    review['patch_reviews'][second.target]['changes'] = ['Resolve missing applicability condition']
    s.backend.reviews = [review]
    count = 0
    async def diagnose(*args):
        nonlocal count
        count += 1
        if count == 1: return {'decision':'propose','candidate':s.group.model_dump()}
        raise ValueError('read-only repair failed')
    s.backend.diagnose = diagnose
    result = await run(s)
    assert result['accepted_targets'] == [first.target] and result['eligible']
    assert result['revision_stop']['reason'] == 'read-only repair failed'
