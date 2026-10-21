"""Exercise the private continuation's exact-source reuse without a device."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.skills.learning import Candidate, digest
from agent.skills.candidate_group import candidate_hash
from agent.skills.snapshot import _hash


def continuation_type():
    # Execute the exact injected validator body, rather than a test copy. The
    # preparer is never imported: imports would read an active runtime snapshot.
    path = Path(__file__).resolve().parents[1] / 'data/androidworld-skill-discovery-20261011/prepare-recovery8.py'
    tree = ast.parse(path.read_text('utf-8'))
    injection = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
        and isinstance(node.value, str) and node.value.startswith('        from agent.skills.candidate_group import candidate_hash, candidate_files'))
    recorded = []
    class Parent:
        async def validate(self, candidate, old, *, budget=None, checks=None):
            self.dispatched.append(checks)
            return {**self.binding, 'validation_stage':'complete', 'trials':[
                {'kind':kind, 'case_id':self.args.variant if kind == 'variant' else self.args.source,
                 'candidate_success':True} for kind in checks]}
    env = {'Parent':Parent, 'digest':digest, 'ROOT':Path('.'),
        'controller':SimpleNamespace(read=lambda _:env['protocol'],write=lambda *args:recorded.append(args))}
    code = 'class Continuation(Parent):\n    async def validate(self, candidate, old, *, budget=None, checks=None):\n' + injection
    code += '        return await super().validate(candidate, old, budget=budget, checks=checks)\n'
    exec(compile(code,str(path),'exec'),env)
    return env['Continuation'], env, recorded


def setup():
    kind, env, receipts = continuation_type()
    candidate = Candidate(target='apps/example/workflows/statistics/SKILL.md',new_text='learned statistics rule',
        gist='scoped statistics',evidence=['exploration/note:scope@1'],scope='category and date',
        benefit='avoid unscoped values',risks='precision is separate')
    binding = {'candidate_hash':candidate_hash(candidate),'base_hash':digest(''),
        'baseline_library_hash':_hash({}), 'candidate_library_hash':_hash({candidate.target:digest(candidate.new_text)})}
    cached = {**binding, 'validation_stage':'complete','trials':[{'kind':'source','case_id':'distance',
        'matched_environment':True,'independent_oracle':True,'config_hash':'frozen',
        'baseline_success':False,'candidate_success':False}]}
    env['protocol'] = {'prior_attempts':{'distance':{'previous_ordinary_validation':cached}}}
    value = kind()
    value.args = SimpleNamespace(source='distance',variant='count')
    value.root = Path('.')
    value.baseline_snapshot = SimpleNamespace(manifest={'files':{},'hash':_hash({})})
    value.binding, value.dispatched = binding, []
    value.validation_checks = lambda:['source','variant']
    return value, candidate, cached, receipts


def observed_transfer_type():
    path = Path(__file__).resolve().parents[1] / 'data/androidworld-skill-discovery-20261011/acquire-count-observed-transfer9.py'
    tree = ast.parse(path.read_text('utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'CountTransfer')
    class Parent:
        async def validate(self, candidate, old, *, budget=None, checks=None):
            self.dispatched.append(checks)
            return {'fresh_dispatch':True}
    env = {'frozen':SimpleNamespace(DiscoveryExperiment=Parent), 'CASE':'count',
        'PROTOCOL':Path('protocol'), 'controller':SimpleNamespace(read=lambda p:
            env['protocol'] if p == Path('protocol') else env['validation'])}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),env)
    return env['CountTransfer'], env


def feedback_continuation_type():
    path = Path(__file__).resolve().parents[1] / 'data/androidworld-skill-discovery-20261011/continue-count-native11.py'
    tree = ast.parse(path.read_text('utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Continuation')
    class Parent:
        async def validate(self,candidate,old,*,budget=None,checks=None):
            self.dispatched.append(checks)
            return {'fresh_dispatch':True}
    env = {'q':SimpleNamespace(prior_module=SimpleNamespace(CountTransfer=Parent)),
        'CASE':'count','PROTOCOL':Path('protocol'),
        'c':SimpleNamespace(read=lambda path:env['protocol'] if path==Path('protocol') else env['validation'])}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),env)
    return env['Continuation'],env


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['none','changed_body','foreign_case','missing_oracle','changed_library','missing_config','changed_base'])
async def test_feedback_continuation_preserves_negative_evidence_and_binding(mutation):
    _,candidate,cached,_ = setup()
    kind,env = feedback_continuation_type()
    cached['trials'][0]['case_id'] = 'count'
    value = kind()
    value.baseline_snapshot = SimpleNamespace(manifest={'files':{},'hash':_hash({})})
    value.dispatched = []
    env['protocol'] = {'completed_validations':['validation']}
    env['validation'] = cached
    if mutation == 'changed_body':
        candidate = candidate.model_copy(update={'new_text':'New native body'})
    elif mutation == 'foreign_case':
        cached['trials'][0]['case_id'] = 'distance'
    elif mutation == 'missing_oracle':
        cached['trials'][0]['independent_oracle'] = False
    elif mutation == 'changed_library':
        cached['candidate_library_hash'] = 'foreign'
    elif mutation == 'missing_config':
        cached['trials'][0]['config_hash'] = ''
    elif mutation == 'changed_base':
        cached['base_hash'] = 'foreign'
    result = await value.validate(candidate,'',checks=['source','variant'])
    if mutation == 'none':
        assert not value.dispatched and result['reused_negative_receipts']
        assert result['trials'][0]['candidate_success'] is False
    else:
        assert value.dispatched==[['source','variant']] and result['fresh_dispatch']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['none','changed_body','foreign_case','missing_oracle','changed_library'])
async def test_new_observation_handoff_never_retries_an_exact_known_failed_body(mutation):
    _, candidate, cached, _ = setup()
    kind, env = observed_transfer_type()
    cached['trials'][0]['case_id'] = 'count'
    value = kind()
    value.args = SimpleNamespace(source='count')
    value.baseline_snapshot = SimpleNamespace(manifest={'files':{},'hash':_hash({})})
    value.dispatched = []
    env['protocol'] = {'validation_file':Path('validation')}
    env['validation'] = cached
    if mutation == 'changed_body':
        candidate = candidate.model_copy(update={'new_text':'new native evidence, changed body'})
    elif mutation == 'foreign_case':
        cached['trials'][0]['case_id'] = 'distance'
    elif mutation == 'missing_oracle':
        cached['trials'][0]['independent_oracle'] = False
    elif mutation == 'changed_library':
        cached['candidate_library_hash'] = 'foreign'
    result = await value.validate(candidate,'',checks=['source','variant'])
    if mutation == 'none':
        assert not value.dispatched
        assert result['reused_source_receipt'] and result['deferred_kinds'] == ['variant']
        assert result['trials'][0]['candidate_success'] is False
    else:
        assert value.dispatched == [['source','variant']] and result['fresh_dispatch']


@pytest.mark.asyncio
async def test_exact_failed_source_is_reused_without_replaying_a_child():
    value, candidate, cached, receipts = setup()
    result = await value.validate(candidate,'',checks=['source'])
    assert not value.dispatched and result['reused_source_receipt']
    assert result['trials'] == cached['trials'] and result['trials'][0]['candidate_success'] is False
    assert len(receipts) == 1


@pytest.mark.asyncio
async def test_new_count_check_preserves_failed_source_and_distinguishes_cross_template():
    value, candidate, cached, _ = setup()
    result = await value.validate(candidate,'',checks=['source','variant'])
    assert value.dispatched == [['variant']]
    assert result['trials'][0] == cached['trials'][0]
    assert result['trials'][1]['case_id'] == 'count'
    assert result['trials'][1]['variation_kind'] == 'cross_template_same_app_not_a_seed_variant'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['body','library','foreign_source','unmatched','missing_oracle'])
async def test_changed_binding_or_invalid_source_cannot_suppress_a_fresh_check(invalid):
    value, candidate, cached, _ = setup()
    if invalid == 'body': candidate = candidate.model_copy(update={'new_text':'different rule'})
    if invalid == 'library': value.baseline_snapshot.manifest['hash'] = 'changed-library'
    if invalid == 'foreign_source': cached['trials'][0]['case_id'] = 'foreign'
    if invalid == 'unmatched': cached['trials'][0]['matched_environment'] = False
    if invalid == 'missing_oracle': cached['trials'][0]['independent_oracle'] = None
    result = await value.validate(candidate,'',checks=['source'])
    assert value.dispatched == [['source']]
    assert not result.get('reused_source_receipt')
