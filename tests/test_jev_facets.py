from agent.revisable.jev_facets import facets,MAX_FACETS,detail_targets,detail_question,arithmetic_issues
from agent.revisable.jev_sources import associated_actions
import pytest

@pytest.mark.parametrize('count',range(2,30))
def test_question_bound_retains_every_adjacent_assertion(count):
    text='; '.join(f'Assertion number {i} is recorded' for i in range(count))
    result=facets(text)
    assert len(result)<=MAX_FACETS
    assert result[0]==text
    assert all(sum(f'Assertion number {i} is recorded' in s for s in result[1:])==1 for i in range(count))

def test_quotes_decimals_and_repeated_spans_keep_original_text():
    claim='The file `a; b.pdf` is 1.13 kB; '+('; '.join(['Repeated assertion alpha']*16))+'; Last distinct assertion omega.'
    result=facets(claim)
    assert result[0]==claim and len(result)<=MAX_FACETS
    assert result[1].startswith('The file `a; b.pdf` is 1.13 kB')
    assert 'Last distinct assertion omega.' in result[-1]
    assert sum(s.count('Repeated assertion alpha') for s in result[1:])==16

def test_targets_are_exact_mentions_not_model_generated_rewrites():
    claim='`alex` was selected on November 20, 2025; event date 2025-11-20.'
    assert detail_targets(claim)==[{'kind':'selected_member','value':'alex'},
        {'kind':'absolute_date','value':'November 20, 2025'}, {'kind':'absolute_date','value':'2025-11-20'}]
    assert not detail_question('id','The file is 1.13 kB.',[])

def test_arithmetic_check_rejects_internal_mismatch_without_judging_world_facts():
    text='The observed prices as 8999, 2888, and 459; their integer total is '
    assert not arithmetic_issues(text+'12346.')
    assert arithmetic_issues(text+'13346.')[0]['calculated_total']==12346
    assert not arithmetic_issues('A receipt says success is unknown; 13346 is a document ID.')

@pytest.mark.parametrize('prefix',['The model reported ','A quoted source says "'])
def test_faithfully_retained_arithmetic_report_is_not_independently_corrected(prefix):
    assert not arithmetic_issues(prefix+'prices as 8999, 2888, and 459; their integer total is 13346.')

def action(source,step):
    return {'source':source,'step':step,'content':[{'role':'assistant','tool_calls':[{'function':{
        'name':'submit_executor_step','arguments':'{"action":{"type":"tap","index":3}}'}}]}]}

def test_receipt_closure_is_unique_same_step_only():
    receipt={'source':'dialogue:receipt@1','step':4,'content':[{'role':'user','content':{'step':4,'action_result':{'success':True}}}]}
    good=action('dialogue:action@1',4)
    assert associated_actions([receipt],[action('past',3),good,action('future',5)])==[good]
    assert not associated_actions([receipt],[good,action('ambiguous',4)])
    assert not associated_actions([receipt,good],[good])
