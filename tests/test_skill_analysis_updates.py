"""Same-source analysis updates preserve knowledge and full validation gates."""
import copy
import json

import pytest

from agent.skills.analysis_session import validate_analysis, validate_decision_analysis
from agent.skills.learning import LearningBudget
from shared.llm_gateway import GatewayResponse
from test_task_skill_learning import context
from test_learner_conversation import backend


def base_analysis(context):
    b = backend(context)
    context[3].put('note', 'dependency', {'title': 'dependency', 'content': 'Required calibration observed'})
    state = validate_analysis({
        'prerequisites': [{'statement': 'Calibration is required', 'status': 'observed',
            'evidence': ['source/note:dependency@1']}],
        'unresolved': [{'statement': 'A cheaper setup remains unknown', 'status': 'unknown'}],
        'recovery_options': [{'id': 'setup', 'mechanism': 'Alternative setup',
            'bottleneck': 'Setup consumes actions', 'route': 'Inspect an observed control',
            'cost': 'Net saving unknown', 'next_test': 'Inspect the missing transition',
            'status': 'hypothesis', 'evidence': ['source/note:dependency@1']}],
        'strategy_choice': {'selected': 'setup', 'reason': 'Only grounded alternative'},
    }, b.resolve_analysis_evidence)
    return b, state


def test_update_preserves_dependencies_options_and_does_not_mutate_base(context):
    b, before = base_analysis(context)
    original = copy.deepcopy(before)
    after = validate_decision_analysis({'trajectory_analysis_update': {'candidate_value': 'Test setup before rewriting'}},
        before, b.resolve_analysis_evidence)
    assert before == original
    assert after['prerequisites'] == before['prerequisites']
    assert after['unresolved'] == before['unresolved']
    assert after['recovery_options'] == before['recovery_options']
    assert after['strategy_choice'] == before['strategy_choice']
    assert after['candidate_value'] == 'Test setup before rewriting'


def test_explicit_empty_list_clears_only_the_named_field(context):
    b, before = base_analysis(context)
    after = validate_decision_analysis({'trajectory_analysis_update': {'unresolved': []}}, before,
        b.resolve_analysis_evidence)
    assert after['unresolved'] == [] and after['prerequisites'] == before['prerequisites']
    assert before['unresolved']
    assert validate_decision_analysis({'trajectory_analysis_update': {}}, before, b.resolve_analysis_evidence) == before
    assert validate_decision_analysis({'decision': 'skip'}, before, b.resolve_analysis_evidence) is None


@pytest.mark.parametrize('previous,update', [(None, {}), ({}, []), ({}, None)])
def test_update_requires_a_previous_validated_state_and_object(previous, update):
    with pytest.raises(ValueError, match='validated source-bound analysis'):
        validate_decision_analysis({'trajectory_analysis_update': update}, previous, lambda ref: None)


def test_complete_replacements_keep_legacy_semantics_and_conflicts_fail(context):
    b, before = base_analysis(context)
    after = validate_decision_analysis({'trajectory_analysis': {'unresolved': []}}, before, b.resolve_analysis_evidence)
    assert after['prerequisites'] == []  # Full replacement retains its old meaning.
    with pytest.raises(ValueError, match='not both'):
        validate_decision_analysis({'trajectory_analysis': {}, 'trajectory_analysis_update': {}}, before,
            b.resolve_analysis_evidence)


def test_merged_analysis_still_checks_citations_and_strategy_dependencies(context):
    b, before = base_analysis(context)
    with pytest.raises(ValueError, match='Invalid analysis evidence'):
        validate_decision_analysis({'trajectory_analysis_update': {'working_path': [{
            'statement': 'Invented route', 'status': 'observed', 'evidence': ['source/event:fiction@1']}]}},
            before, b.resolve_analysis_evidence)
    with pytest.raises(ValueError, match='viable recovery option'):
        validate_decision_analysis({'trajectory_analysis_update': {'recovery_options': []}}, before,
            b.resolve_analysis_evidence)
    after = validate_decision_analysis({'trajectory_analysis_update': {
        'recovery_options': [], 'strategy_choice': None}}, before, b.resolve_analysis_evidence)
    assert after['recovery_options'] == [] and after['strategy_choice'] is None


@pytest.mark.asyncio
async def test_actual_diagnosis_materializes_update_and_new_job_cannot_inherit_it(context, monkeypatch):
    b, state = base_analysis(context)
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        kwargs['attempt_meter']('model_call_started', {})
        if len(calls) == 1:
            value = {'decision': 'explore', 'question': 'Inspect setup', 'trajectory_analysis': state}
        elif len(calls) == 2:
            assert any('Calibration is required' in str(m.get('content')) for m in messages)
            value = {'decision': 'explore', 'question': 'Inspect the next transition',
                'trajectory_analysis_update': {'candidate_value': 'Compare observed setup cost'}}
        else:
            assert not any('Calibration is required' in str(m.get('content')) for m in messages)
            value = {'decision': 'skip', 'reason': 'No source-bound evidence'}
        return GatewayResponse(model=model, content=json.dumps(value))
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget(max_calls=12)
    await b.diagnose('test', 'initial', {'diagnostic_read_rounds': 0}, budget)
    session = b.learner_conversation
    result = await b.diagnose('test', 'next', {'diagnostic_read_rounds': 0}, budget)
    assert result['trajectory_analysis']['candidate_value'] == 'Compare observed setup cost'
    assert result['trajectory_analysis']['prerequisites'] == state['prerequisites']
    assert 'trajectory_analysis_update' not in result
    archived = json.loads(b.artifacts.read_text(session.ref))
    assert archived['analysis'] == result['trajectory_analysis'] and archived['analysis_current']
    assert 'trajectory_analysis_update' in archived['history'][-1]['content']  # Literal output stays archived.
    compacted = await b._compact_diagnosis('test', session.messages('next', {}), session, budget, 2)
    assert budget.calls == 2 and session.reused_checkpoints == 1
    assert 'Calibration is required' in compacted[2]['content']
    await b.diagnose('test', 'new job', {'diagnostic_read_rounds': 0}, LearningBudget(max_calls=12))
    assert b.learner_conversation.id != session.id and b.learner_conversation.analysis is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change_decision', [False, True])
async def test_invalid_update_repair_cannot_modify_decision(context, monkeypatch, change_decision):
    b, state = base_analysis(context)
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages)
        kwargs['attempt_meter']('model_call_started', {})
        if len(calls) == 1:
            value = {'decision': 'explore', 'question': 'Inspect setup', 'trajectory_analysis': state}
        elif len(calls) == 2:
            value = {'decision': 'explore', 'question': 'Inspect next', 'trajectory_analysis_update': {
                'working_path': [{'statement': 'Invented fact', 'status': 'observed', 'evidence': ['event:fiction@1']}]}}
        else:
            assert kwargs['tools'] is None
            value = {'decision': 'skip' if change_decision else 'explore', 'question': 'Inspect next',
                'trajectory_analysis_update': {'candidate_value': 'Still untested'}}
        return GatewayResponse(model=model, content=json.dumps(value))
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget(max_calls=12)
    await b.diagnose('test', 'initial', {'diagnostic_read_rounds': 0}, budget)
    if change_decision:
        with pytest.raises(ValueError, match='repair changed the decision'):
            await b.diagnose('test', 'next', {'diagnostic_read_rounds': 0}, budget)
        assert b.learner_conversation.analysis == state
    else:
        result = await b.diagnose('test', 'next', {'diagnostic_read_rounds': 0}, budget)
        assert result['trajectory_analysis']['prerequisites'] == state['prerequisites']
    assert budget.calls == 3 and budget.actions == 0
