"""Evidence delivery efficiency without weaker admission or source mutation."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_task_skill_learning import context, candidate, Backend, review, validation
from agent.skills.exploration import ExplorationBackend, execution_overview
from agent.skills.learning import LearningBudget
from shared.llm_gateway import GatewayResponse, ToolCall


def test_diagnostic_contract_prefix_survives_phase_changes_without_omitting_facts():
    from agent.skills.analysis_session import diagnostic_payload_content
    from os.path import commonprefix
    first = {'source': {'instruction': '当前任务', 'execution_overview': ['old rows']},
        'budget': {'calls': 12}, 'trajectory_analysis_format': {'required': ['native evidence']},
        'executor_action_directory': [{'type': 'tap', 'parameters': ['index']}],
        'existing_active_guidance': {'content': 'Frozen app knowledge'}, 'candidate': {'body': 'Original'}}
    second = {**first, 'source': {**first['source'], 'execution_overview': {'read_tool': 'outline'}},
        'budget': {'calls': 13}, 'candidate': {'body': 'Revised'}, 'new_feedback': {'unknown': True}}
    originals = copy.deepcopy((first, second))
    encoded = [diagnostic_payload_content(value) for value in (first, second)]
    assert tuple(json.loads(value) for value in encoded) == originals
    assert (first, second) == originals
    original = [json.dumps(value, ensure_ascii=False, separators=(',', ':')) for value in originals]
    assert len(commonprefix(encoded)) > len(commonprefix(original))
    assert [len(value) for value in encoded] == [len(value) for value in original]


def test_source_active_guidance_preserves_actual_request_role_even_for_same_hash(context):
    from agent.skills.learning import build_packet
    from shared.schemas import TraceEvent
    _, db, task, store, _ = context
    before = copy.deepcopy(store.records('event'))
    for role in ('planner', 'reviewer', 'executor', None):
        request = {'active_skills':[{'skill_id':'same-skill', 'content_hash':'same-projection', 'version':'1.0'}]}
        if role:
            request['role'] = role
        ref = store.artifacts.save_json('llm', request)
        db.add_trace(TraceEvent(task_id=task.id, kind='llm', payload={'request_ref':ref}))
    packet = build_packet(task, store)
    assert {row['role'] for row in packet['active_skills']} == {'planner', 'reviewer', 'executor', None}
    assert all(row['content_hash'] == 'same-projection' for row in packet['active_skills'])
    assert store.records('event') == before and len(json.dumps(packet, ensure_ascii=False)) <= 12000


def test_host_review_closure_is_archived_in_continuous_conversation_without_fabricating_a_decision(context):
    from agent.skills.analysis_session import LearnerConversation
    b = backend(context)
    session = LearnerConversation(b.artifacts, {'task_id':b.task.id, 'source_hash':'sealed', 'library_hash':'fixed'})
    before = [{'role':'system', 'content':'Bound Learner policy'}, {'role':'user', 'content':'Original pinned input'},
        {'role':'assistant', 'content':'{"decision":"propose"}'}]
    old_ref = session.save(before)
    original = b.artifacts.read_text(old_ref)
    feedback = {'origin':'host', 'candidate_hash':'exact', 'review':{'verification':{'checks':[], 'reason':'No further check; transfer remains unknown'}},
        'validation':{'trials':[{'candidate_success':True, 'mechanism_adoption':'unassessed'}]}}
    new_ref = session.deliver_host_feedback(feedback)
    archived = json.loads(b.artifacts.read_text(new_ref))
    assert new_ref != old_ref and b.artifacts.read_text(old_ref) == original
    assert archived['source_binding'] == session.binding and archived['previous'] == old_ref
    assert archived['history'][:-1] == before[2:]
    delivered = json.loads(archived['history'][-1]['content'])['completed_verification_feedback']
    assert delivered == feedback and 'decision' not in delivered
    assert archived['phase_input'] == before[:2] and not archived['analysis_current']
    assert archived['compactions'] == 0 and session.stages == 0


@pytest.mark.asyncio
async def test_independent_reviewer_reads_cross_app_frozen_guidance_on_demand_within_existing_budget(context, monkeypatch):
    from agent.skills.library import serialize_skill_markdown
    from test_task_skill_learning import review
    b = backend(context)
    path = 'apps/com.android.chrome/core/SKILL.md'
    target = b.library.root/path; target.parent.mkdir(parents=True)
    text = serialize_skill_markdown(name='chrome-navigation', description='Browser navigation',
        version='1.0.13', app='com.android.chrome', kind='app_core', body='Frozen browser guidance. '*250)
    target.write_text(text, 'utf8'); b.freeze_library()
    before = copy.deepcopy(b.source_store.records('event'))
    calls, pages = [], []
    async def complete(model, messages, **kwargs):
        kwargs['attempt_meter']('model_call_started', {})
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            assert [t['function']['name'] for t in kwargs['tools']] == ['read_review_evidence','read_official_skill']
            assert json.loads(messages[1]['content'])['contracts']['related_skills'] == []
            return GatewayResponse(model=model, content='', tool_calls=[
                ToolCall(id='guide', name='read_official_skill', arguments=json.dumps({'path':path, 'length':256})),
                ToolCall(id='private', name='read_official_skill', arguments=json.dumps({'path':'../../.env'}))])
        if len(calls) == 2:
            results = {m['tool_call_id']:json.loads(m['content']) for m in messages if m['role']=='tool'}
            page = results['guide']; pages.append(page)
            assert page['status']=='success' and page['source_kind']=='official_skill'
            assert page['text']==text[:256] and page['offset']==0 and page['truncated']
            assert page['source']==f"official_skill:{path}@{b.snapshot.manifest['files'][path]}"
            from agent.skills.library import parse_skill_markdown
            from agent.tool_registry import stable_hash
            assert page['executor_projection_hash']==stable_hash(parse_skill_markdown(text).section_for('executor'))
            assert page['executor_projection_hash']!=page['hash'] and page['hash_scope']=='complete_SKILL.md_utf8'
            assert 'not evidence' in page['policy'] and results['private']['status']=='failed'
            return GatewayResponse(model=model, content='', tool_calls=[ToolCall(id='next', name='read_official_skill',
                arguments=json.dumps({'path':path,'offset':page['next_offset'],'length':256}))])
        page = json.loads(next(m['content'] for m in messages if m.get('tool_call_id')=='next'))
        pages.append(page)
        assert page['text']==text[256:512] and page['hash']==pages[0]['hash']
        return GatewayResponse(model=model, content=json.dumps(review()))
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget(max_calls=8)
    result = await b.review('test','Independent review',{'contracts':{'related_skills':[]}},budget)
    assert budget.calls==3 and budget.actions==0 and len(calls)==3
    assert len(result['evidence_reads'])==3 and b.source_store.records('event')==before
    assert target.read_text('utf8')==text


@pytest.mark.asyncio
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
async def test_learner_and_deferred_pages_expose_same_frozen_executor_identity(context, monkeypatch, newline):
    from agent.skills.library import parse_skill_markdown
    from agent.tool_registry import stable_hash
    b = backend(context)
    path = 'apps/example/core/SKILL.md'
    target = b.library.root/path
    target.parent.mkdir(parents=True)
    text = candidate().new_text
    target.write_bytes(text.replace('\n', newline).encode('utf8')); b.freeze_library()
    call = ToolCall(id='page', name='read_official_skill', arguments=json.dumps({'path':path, 'length':256}))
    queued, attachments = await b._queued_diagnostic_read(call, {}, {}, 256)
    assert not attachments
    assert queued['executor_projection_hash'] == stable_hash(parse_skill_markdown(text).section_for('executor'))
    assert queued['role_projection_hashes'] == {role:stable_hash(parse_skill_markdown(text).section_for(role))
        for role in ('planner', 'reviewer', 'executor', 'decision')}
    assert queued['hash'] != queued['executor_projection_hash']
    before = copy.deepcopy(b.source_store.records('event'))
    requests = []
    async def complete(model, messages, **kwargs):
        kwargs['attempt_meter']('model_call_started', {})
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return GatewayResponse(model=model, content='', tool_calls=[call])
        supplied = json.loads(next(m['content'] for m in messages if m.get('tool_call_id') == 'page'))
        assert supplied == queued and supplied['hash_scope'] == 'complete_SKILL.md_utf8'
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No missing correspondence"}')
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget(max_calls=12, reserve_calls=3)
    await b.diagnose('test', 'learn', {}, budget)
    assert budget.calls == 2 and budget.actions == 0 and b.source_store.records('event') == before
    (b.snapshot.root/path).write_text(text+'changed', 'utf8')
    with pytest.raises(ValueError, match='frozen library file changed'):
        b.official_skill_page(path, 0, 256)
    with pytest.raises(ValueError, match='not in frozen'):
        b.official_skill_page('../../.env', 0, 256)


def test_format_repair_replaces_only_acquisition_policy_when_required_fields_fit():
    from agent.skills.exploration import ANALYSIS_REPAIR_SYSTEM, analysis_repair_context, diagnostic_text_size
    checkpoint = {'unresolved':[{'statement':'Prior uncertainty '*380, 'status':'unknown', 'evidence':[]}]}
    phase = {'source':{'instruction':'Keep complete original task', 'execution_limits':{'rounds':50}},
        'review_feedback':{'reason':'Unresolved review dispute '*850}, 'budget':{'remaining_calls':12},
        'candidate':{'new_text':'Exact candidate\n'*200}, 'existing_active_guidance':{'items':[], 'omitted_ids':['unknown']}}
    parsed = {'decision':'propose', 'candidate':phase['candidate'], 'reason':'Keep scoped unknowns '*120,
        'trajectory_analysis':{'unresolved':[{'statement':'Current uncertainty '*300, 'status':'unknown', 'evidence':[]}]}}
    messages = [{'role':'system','content':'Full acquisition policy '*380},
        {'role':'user','content':json.dumps(phase,ensure_ascii=False,separators=(',',':'))}]
    original = copy.deepcopy(messages)
    repaired = analysis_repair_context(messages, checkpoint, parsed, {'role':'user','content':'Repair references only'})
    assert repaired[0]['content'] == ANALYSIS_REPAIR_SYSTEM and diagnostic_text_size(repaired) <= 50000
    assert json.loads(repaired[1]['content']) == phase
    assert json.loads(repaired[2]['content'])['trajectory_analysis'] == checkpoint
    assert json.loads(repaired[-2]['content']) == parsed and messages == original


@pytest.mark.asyncio
async def test_explicit_read_only_validation_uses_short_policy_without_losing_current_facts(context, monkeypatch):
    from agent.skills.learning import LEARN_SYSTEM, READONLY_SYSTEM
    b = backend(context)
    goal = 'Assess the unchanged candidate against the bound completed baseline'
    current = {'new_text': 'Complete candidate body', 'evidence': ['source/event:1@1']}
    seen = []
    async def complete(model, messages, **kwargs):
        kwargs['attempt_meter']('model_call_started', {})
        seen.append(copy.deepcopy(messages))
        assert messages[0]['content'] == READONLY_SYSTEM
        payload = json.loads(messages[1]['content'])
        assert payload['verification_goal'] == goal and payload['candidate'] == current
        assert payload['live_exploration_disabled'] is True
        assert payload['validation']['trials'][0]['candidate_success'] is False
        assert payload['review_feedback']['verdict'] == 'pass'
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Keep the unresolved utility separate"}')
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    await b.diagnose('test', LEARN_SYSTEM, {'live_exploration_disabled': True,
        'verification_goal': goal, 'candidate': current, 'review_feedback': {'verdict': 'pass'},
        'validation': {'trials': [{'candidate_success': False}]}}, LearningBudget())
    assert len(seen) == 1 and len(READONLY_SYSTEM) < len(LEARN_SYSTEM)


def test_source_bound_resume_retains_reads_from_before_latest_job(context):
    from agent.skills.analysis_session import LearnerConversation
    b = backend(context)
    binding = {"task_id":b.task.id, "source_hash":"sealed", "library_hash":"fixed"}
    first = LearnerConversation(b.artifacts, binding)
    old = {"data":{"items":[{"source":"source/event:4@1", "text":"Earlier dependency"}]}}
    recent = {"data":{"items":[{"source":"source/event:8@1", "text":"Latest dependency"}]}}
    assert first.observe_read_result(old) and first.observe_read_result(recent)
    ref = first.save([{"role":"system","content":"Same learner"},{"role":"user","content":"Same source"}])
    archive = json.loads(b.artifacts.read_text(ref))
    resumed = LearnerConversation(b.artifacts, binding)
    resumed.restore_read_progress(archive)
    assert not resumed.observe_read_result(old) and not resumed.observe_read_result(recent)
    assert resumed.observe_read_result({"data":{"items":[{"source":"source/event:4@2", "text":"Earlier dependency"}]}})
    assert len(archive["read_progress"]["fragments"]) == 2  # No mutation of old bookkeeping.
    foreign = LearnerConversation(b.artifacts, {**binding, "source_hash":"different"})
    with pytest.raises(ValueError, match="different source binding"):
        foreign.restore_read_progress(archive)


def test_checkpoint_defer_initial_outline_preserves_current_constraints():
    from agent.skills.exploration import checkpoint_phase_input
    source = {"instruction": "Inspect all events and preserve unrelated files",
        "execution_limits": {"device_actions": 50}, "counts": {"events": 50},
        "outcome": "failed", "external_evaluation": {"success": False},
        "execution_overview": {"rows": [[i, f"source/event:{i}@1", "tap", "claim " * 80]
                                         for i in range(50)]}}
    payload = {"initial_diagnosis": True, "source": source,
               "candidate": {"new_text": "Keep full rule"},
               "review_feedback": {"tests": ["Verify retained file membership"]}}
    messages = [{"role": "system", "content": "Same Learner"},
                {"role": "user", "content": json.dumps(payload)}]
    original = copy.deepcopy(messages)
    compacted = checkpoint_phase_input(messages)
    new = json.loads(compacted[1]["content"])
    assert messages == original
    assert {k: v for k, v in new["source"].items() if k != "execution_overview"} == {
        k: v for k, v in source.items() if k != "execution_overview"}
    assert new["candidate"] == payload["candidate"] and new["review_feedback"] == payload["review_feedback"]
    assert new["source"]["execution_overview"]["read_tool"] == "read_execution_outline"
    assert len(compacted[1]["content"]) < len(messages[1]["content"]) / 5


@pytest.mark.asyncio
async def test_screenshot_batch_delivers_unseen_images_before_evicting(context, monkeypatch):
    b = backend(context)
    store = context[3]
    for i in range(3):
        ref = b.artifacts.save_bytes("images", b"\x89PNG\r\n\x1a\n" + bytes([i]), ".png")
        store.put("observation", f"image-{i}", {"app": "com.demo", "text": f"State {i}",
            "image_ref": ref, "step": i})
    original = copy.deepcopy(store.records("observation"))
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        kwargs["attempt_meter"]("model_call_started", {})
        images = [m for m in messages if isinstance(m.get("content"), list)]
        assert len(images) <= 2
        if len(calls) == 1:
            return GatewayResponse(model=model, content="", tool_calls=[
                ToolCall(id=f"image-{i}", name="read_history", arguments=json.dumps({
                    "source": f"source/observation:image-{i}@1", "view": "image"})) for i in range(3)])
        if len(calls) == 2:
            assert all(f"image-{i}" in str(images) for i in (0, 1))
            assert "image-2" not in str(images)
            results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
            assert results["image-2"]["status"] == "deferred"
            return GatewayResponse(model=model, content="", tool_calls=[
                ToolCall(id="gap", name="read_history", arguments='{"source":"observation:screen@1"}')])
        assert "image-2" in str(images)
        deliveries = [json.loads(m["content"]).get("deferred_read_delivery") for m in messages
                      if m["role"] == "user" and isinstance(m.get("content"), str)]
        assert any(v and v["original_tool_call_id"] == "image-2" for v in deliveries)
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No useful draft"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    result = await b.diagnose("test", "learn", {}, LearningBudget())
    assert result["decision"] == "skip" and len(calls) == 3
    assert store.records("observation") == original


@pytest.mark.asyncio
@pytest.mark.parametrize('valid_checkpoint', [True, False])
async def test_read_response_checkpoint_keeps_new_evidence_and_rejects_invalid_eviction(context, monkeypatch, valid_checkpoint):
    from agent.skills.analysis_session import LearnerConversation
    b = backend(context)
    b.source_store.put('note', 'old', {'content':'Old dependency still applies'})
    b.source_store.put('note', 'new', {'content':'New result has not been summarized'})
    source_before = copy.deepcopy(b.source_store.records('note'))
    good = {'prerequisites':[{'statement':'Old dependency still applies', 'status':'observed',
                             'evidence':['source/note:old@1']}]}
    value = good if valid_checkpoint else {'prerequisites':[{'statement':'Invented dependency', 'status':'observed',
                                                          'evidence':['foreign/note:missing@1']}]}
    messages = [{'role':'system','content':'Same Learner'},
        {'role':'user','content':json.dumps({'source':{'instruction':'Keep the original constraint'},
                                          'candidate':{'new_text':'Keep only the scoped decision rule'}})},
        {'role':'assistant','content':None,'tool_calls':[{'id':'old-read', 'type':'function',
            'function':{'name':'read_history','arguments':'{"source":"note:old@1"}'}}]},
        {'role':'tool','tool_call_id':'old-read','content':'Old dependency still applies'},
        {'role':'assistant','content':json.dumps({'trajectory_analysis':value}), 'tool_calls':[{'id':'new-read','type':'function',
            'function':{'name':'read_history','arguments':'{"source":"note:new@1"}'}}]},
        {'role':'tool','tool_call_id':'new-read','content':'New result has not been summarized'}]
    original = copy.deepcopy(messages)
    session = LearnerConversation(b.artifacts, {'task_id':b.task.id})
    budget = LearningBudget()
    calls = []
    async def complete(model, supplied, **kwargs):
        calls.append(copy.deepcopy(supplied))
        assert not valid_checkpoint, 'A valid native checkpoint should not pay for another summary'
        assert 'old-read' in str(supplied) and 'New result has not been summarized' in str(supplied)
        kwargs['attempt_meter']('model_call_started', {})
        return GatewayResponse(model=model, content=json.dumps(good))
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    compacted = await b._compact_diagnosis('test', messages, session, budget, 0)
    if valid_checkpoint:
        assert compacted[2:] == messages[4:]
        assert not session.analysis_current  # The new result still needs analysis.
        assert session.reused_checkpoints == 1 and budget.calls == 0
    else:
        assert len(calls) == 1 and budget.calls == 1
        assert session.analysis['prerequisites'][0]['evidence'] == ['source/note:old@1']
    assert compacted[:2] == messages[:2]
    assert messages == original and b.source_store.records('note') == source_before


def test_candidate_native_evidence_error_identifies_guidance_not_source_prefix(context):
    b = backend(context)
    b.source_store.put('observation', 'native', {'app':'com.demo', 'text':'Visible fact'})
    assert b.resolve_evidence('source/observation:native@1') == b.resolve_evidence('observation:native@1')
    b.freeze_library()
    ref = 'official_skill:apps/com.demo/core/SKILL.md@' + ('0'*64)
    with pytest.raises(ValueError, match='not official_skill guidance') as error:
        b.resolve_evidence(ref)
    assert ref in str(error.value) and 'source/ prefixes' in str(error.value)
    with pytest.raises(ValueError, match='unknown evidence namespace'):
        b.resolve_evidence('foreign/observation:native@1')


@pytest.mark.asyncio
async def test_valid_repair_removes_old_draft_before_review_feedback(context, monkeypatch):
    from agent.skills.learning import run_task_learning
    settings, _, task, store, library = context
    bad = candidate().model_copy(update={'evidence':['official_skill:apps/com.demo/core/SKILL.md@bad']})
    good = candidate()
    b = Backend([{'decision':'propose', 'candidate':bad.model_dump()},
        {'decision':'propose', 'candidate':good.model_dump()},
        {'decision':'propose', 'candidate':good.model_dump()}])
    def resolve(ref):
        if ref.startswith('official_skill:'):
            raise ValueError('Candidate evidence requires native task records, not official_skill guidance')
    b.resolve_evidence = resolve
    observed = []
    diagnose = b.diagnose
    async def capture(*args):
        observed.append(copy.deepcopy(args[2]))
        return await diagnose(*args)
    b.diagnose = capture
    reviews = []
    async def ask(*args):
        reviews.append(1)
        value = review()
        if len(reviews) == 1:
            value.update(verdict='insufficient', changes=['Check the supported omission dependency.'])
            value['criteria']['root_cause']['status'] = 'insufficient'
        return value
    monkeypatch.setattr(LearningBudget, 'ask', ask)
    async def validate(c, old):
        return validation(c, old)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=b, validator=validate, manual=True)
    assert result['reason'] == 'unchanged_candidate_and_evidence' and len(observed) == 3
    assert observed[1]['invalid_candidate'] == bad.model_dump()
    assert observed[2]['candidate'] == good.model_dump()
    assert observed[2]['review_feedback']['verdict'] == 'insufficient'
    assert not {'invalid_candidate', 'candidate_error', 'repair_instruction'} & observed[2].keys()


@pytest.mark.asyncio
async def test_reviewer_delivers_requested_deferred_records_without_repeat_calls(context, monkeypatch):
    b = backend(context)
    for i in range(4):
        b.source_store.put('observation', str(i), {'app':'com.demo', 'text':('"literal"\n'*800)+str(i)})
    requests = []
    async def complete(model, messages, **kwargs):
        kwargs['attempt_meter']('model_call_started', {})
        requests.append(copy.deepcopy(messages))
        if len(requests) == 1:
            return GatewayResponse(model=model, content='', tool_calls=[ToolCall(id=f'read-{i}',
                name='read_review_evidence', arguments=json.dumps({'source':f'source/observation:{i}@1'})) for i in range(4)])
        replies = [json.loads(m['content']) for m in messages if m.get('role') == 'tool']
        assert len(replies) == 4 and any(r['status'] == 'deferred' for r in replies)
        deliveries = [json.loads(m['content'])['deferred_review_delivery'] for m in messages
            if m.get('role') == 'user' and isinstance(m.get('content'), str) and 'deferred_review_delivery' in m['content']]
        supplied = {r['source'] for r in replies if r['status'] == 'success'}
        for d in deliveries:
            assert d['arguments']['source'] == d['result']['source']
            assert d['original_tool_call_id'] == 'read-'+d['result']['source'].split(':')[1].split('@')[0]
            expected, _ = b._read_review_record(d['arguments'], {}, 0)
            assert d['result'] == expected
            supplied.add(d['result']['source'])
        assert supplied == {f'source/observation:{i}@1' for i in range(4)}
        assert len(json.dumps(deliveries)) <= 16000
        return GatewayResponse(model=model, content=json.dumps(review()))
    monkeypatch.setattr('agent.skills.exploration.complete', complete)
    budget = LearningBudget(max_calls=2)
    result = await b.review('review', 'Review', {'candidate':'complete'}, budget)
    assert result['verdict'] == 'pass' and budget.calls == 2 and budget.actions == 0
    assert len([m for m in requests[-1] if m.get('role') == 'assistant' and m.get('tool_calls')]) == 1


def backend(context):
    settings, db, task, store, library = context
    return ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=SimpleNamespace(), settings=settings, library=library)


def test_overview_preserves_complete_literal_order_versions_and_unknowns():
    rows = [{"key":str(i), "version":2, "payload":{"step":i,
        "submitted_action":{"type":"tap", "index":i},
        "executor_report":"reported, not verified " + str(i)}} for i in range(50)]
    original = copy.deepcopy(rows)
    result = execution_overview(list(reversed(rows)))
    assert rows == original
    assert result["omitted_events"] == 0 and result["next_step"] is None
    assert [entry[0] for entry in result["rows"]] == list(range(50))
    assert all(entry[1] == f"source/event:{i}@2" for i,entry in enumerate(result["rows"]))
    assert all(entry[3] == rows[i]["payload"]["executor_report"] for i,entry in enumerate(result["rows"]))
    assert "verified effects" in result["policy"]
    page = execution_overview(rows, character_budget=900)
    assert page["omitted_events"] > 0 and page["next_step"] == len(page["rows"])
    assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":"))) <= 900


@pytest.mark.asyncio
async def test_initial_index_replaces_duplicate_previews_without_changing_goal_or_source(context, monkeypatch):
    b = backend(context)
    store = context[3]
    for step in range(50):
        store.put("event", str(step), {"step":step, "submitted_action":{"type":"tap"},
            "executor_report":f"Action claim {step}"})
    before = copy.deepcopy(store.records("event"))
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        packet = json.loads(messages[1]["content"])["source"]
        assert packet["instruction"] == b.task.instruction
        assert len(packet["execution_overview"]["rows"]) == 50
        assert packet["execution_limits"] == {"device_actions":50}
        assert packet["events"][0] == {"source":"event:49@1"}
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No valuable supported rule"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    await b.diagnose("test", "learn", {"initial_diagnosis":True,
        "source":{"instruction":b.task.instruction,"execution_limits":{"device_actions":50},
                  "events":[{"source":"event:49@1","preview":"large duplicate preview"}]}}, LearningBudget())
    assert store.records("event") == before


@pytest.mark.asyncio
async def test_resumed_source_index_is_readable_without_repeating_rows(context, monkeypatch):
    b = backend(context)
    store = context[3]
    for i in range(50):
        store.put("event", str(i), {"step": i, "decision": "act", "executor_report": f"Claim {i}"})
    before = copy.deepcopy(store.records("event"))
    calls = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            assert len(json.loads(messages[1]["content"])["source"]["execution_overview"]["rows"]) == 50
            return GatewayResponse(model=model, content=json.dumps({"decision":"skip", "reason":"No useful draft",
                "trajectory_analysis":{"unresolved":[{"statement":"Mechanism unknown", "status":"unknown", "evidence":[]}]}}))
        if len(calls) == 2:
            source = json.loads(messages[1]["content"])["source"]
            assert source["instruction"] == b.task.instruction and source["execution_limits"] == {"device_actions":50}
            assert source["execution_overview"]["read_tool"] == "read_execution_outline"
            assert "rows" not in source["execution_overview"]
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(id="gap", name="read_execution_outline",
                arguments='{"namespace":"source","start_step":40}')])
        result = next(json.loads(m["content"]) for m in messages if m.get("tool_call_id") == "gap")
        assert any(item["source"] == "source/event:40@1" for item in result["data"]["items"])
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No new rule"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget()
    payload = {"initial_diagnosis":True, "source":{"instruction":b.task.instruction,"execution_limits":{"device_actions":50}}}
    await b.diagnose("test", "learn", payload, budget)
    await b.diagnose("test", "learn", payload, budget)
    assert len(calls) == 3 and store.records("event") == before


@pytest.mark.asyncio
async def test_deferred_read_delivered_without_model_retry_and_tool_pair_preserved(context, monkeypatch):
    b = backend(context)
    paths = []
    for i in range(4):
        path = f"apps/example/workflows/reading-{i}/SKILL.md"
        file = b.library.root/path
        file.parent.mkdir(parents=True)
        file.write_text(candidate().new_text + "\n" + str(i)*6000, 'utf-8')
        paths.append(path)
    b.freeze_library()
    seen = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        seen.append(copy.deepcopy(messages))
        if len(seen) == 1:
            return GatewayResponse(model=model, content="", tool_calls=[
                ToolCall(id=f"page-{i}", name="read_official_skill", arguments=json.dumps({"path":path}))
                for i,path in enumerate(paths)])
        tool_results = [m for m in messages if m.get("role") == "tool"]
        assert {m['tool_call_id'] for m in tool_results} == {f'page-{i}' for i in range(4)}
        assert any(json.loads(m['content']).get('status') == 'deferred' for m in tool_results)
        deliveries = [json.loads(m['content'])['deferred_read_delivery'] for m in messages
            if m.get('role') == 'user' and isinstance(m.get('content'), str)
            and 'deferred_read_delivery' in json.loads(m['content'])]
        assert len(deliveries) == 1
        delivered = deliveries[0]
        assert delivered['original_tool_call_id'] == 'page-3'
        assert delivered['arguments'] == {'path':paths[3]}
        assert delivered['result']['text'] == (b.snapshot.root/paths[3]).read_text('utf-8')[:4000]
        assert delivered['result']['source'].endswith('@'+b.snapshot.manifest['files'][paths[3]])
        assert 'trajectory_analysis' not in delivered
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Scope remains unknown"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget(max_calls=12, reserve_calls=3)
    value = await b.diagnose("test", "learn", {}, budget)
    assert value['decision'] == 'skip' and budget.calls == 2 and budget.actions == 0
    # Deferred work is confined to that phase; a new phase does not replay it.
    async def next_phase(model, messages, **kwargs):
        kwargs['attempt_meter']('model_call_started', {})
        assert not kwargs.get('tools')
        assert sum('deferred_read_delivery' in str(m.get('content','')) for m in messages) == 1
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No new evidence"}')
    monkeypatch.setattr('agent.skills.exploration.complete', next_phase)
    await b.diagnose('test', 'next', {'diagnostic_read_rounds':0}, budget)
    assert budget.calls == 3


@pytest.mark.asyncio
async def test_queued_read_never_falls_back_to_source_for_unknown_namespace(context):
    b = backend(context)
    call = ToolCall(id='bad', name='read_history', arguments=json.dumps({
        'namespace':'unregistered', 'source':'event:1@1', 'full':True}))
    with pytest.raises(ValueError, match='unknown history namespace'):
        await b._queued_diagnostic_read(call, {'source':object()}, {}, 4000)


@pytest.mark.asyncio
async def test_queued_context_read_preserves_exact_offset_and_hash(context):
    b = backend(context)
    text = 'literal evidence; a report is not proof'*100
    call = ToolCall(id='page', name='read_diagnostic_context',
        arguments=json.dumps({'ref':'frozen','offset':40,'length':3000}))
    result, attachments = await b._queued_diagnostic_read(call, {}, {'frozen':text}, 700)
    assert result['text'] == text[40:740] and result['offset'] == 40
    assert result['next_offset'] == 740 and result['total_characters'] == len(text)
    assert attachments == []


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity', [100, 400, 4000])
async def test_outline_capacity_failure_is_deferred_not_empty_evidence(context, capacity):
    b = backend(context)
    b.source_store.put('event', '1', {'step':1, 'submitted_action':{'type':'tap', 'index':2},
        'executor_report':'Literal report '*10})
    call = ToolCall(id='outline', name='read_execution_outline', arguments='{"start_step":1}')
    result, attachments = await b._queued_diagnostic_read(call, {'source':object()}, {}, capacity)
    assert attachments == []
    if capacity < 500:
        assert result['status'] == 'deferred'
    else:
        assert result['status'] == 'success' and result['data']['items'][0]['source'] == 'source/event:1@1'


@pytest.mark.asyncio
async def test_undelivered_queue_does_not_cross_feedback_phase_or_exceed_window(context, monkeypatch):
    from agent.skills.exploration import diagnostic_text_size
    b = backend(context)
    paths = []
    for i in range(8):
        path = f'apps/example/workflows/pending-{i}/SKILL.md'
        file = b.library.root/path
        file.parent.mkdir(parents=True)
        file.write_text(candidate().new_text + '\n' + str(i)*6000, 'utf-8')
        paths.append(path)
    b.freeze_library()
    calls = []
    async def complete(model, messages, **kwargs):
        assert diagnostic_text_size(messages) <= 50000
        kwargs['attempt_meter']('model_call_started', {})
        calls.append(copy.deepcopy(messages))
        if len(calls) == 1:
            return GatewayResponse(model=model, content='',tool_calls=[
                ToolCall(id=f'pending-{i}',name='read_official_skill',arguments=json.dumps({'path':path}))
                for i,path in enumerate(paths)])
        return GatewayResponse(model=model,content='{"decision":"skip","reason":"Remaining pages unknown; no further useful work"}')
    monkeypatch.setattr('agent.skills.exploration.complete',complete)
    budget = LearningBudget(max_calls=12,reserve_calls=3)
    await b.diagnose('test','initial',{},budget)
    delivered_before = sum('deferred_read_delivery' in str(m.get('content','')) for m in calls[-1])
    assert any('queued_reads' in str(m.get('content','')) for m in calls[-1])
    assert 0 < delivered_before < 5
    await b.diagnose('test','feedback',{'diagnostic_read_rounds':0},budget)
    assert sum('deferred_read_delivery' in str(m.get('content','')) for m in calls[-1]) == delivered_before
    assert budget.calls == 3 and budget.actions == 0


def test_shifted_and_cross_page_reads_are_not_new_progress(tmp_path):
    from agent.skills.analysis_session import LearnerConversation
    from shared.artifacts import ArtifactStore
    session = LearnerConversation(ArtifactStore(tmp_path), {"source": "same"})
    def page(offset, text, **metadata):
        return {"data": {"items": [{"source": "source/observation:record@1",
            "offset": offset, "text": text, **metadata}]}}
    assert session.observe_read_result(page(0, "abcdefgh", observation_id="record"))
    assert session.observe_read_result(page(8, "ijklmnop"))
    assert not session.observe_read_result(page(3, "defghijklm", event_sources=["source/event:2@1"]))
    assert session.observe_read_result(page(13, "nopQR"))
    assert not session.observe_read_result(page(16, "QR", truncated=True))
    assert session.observe_read_result(page(18, "abcdefgh"))  # Same text at a new position.
    packet = page(3, "def")
    packet["data"]["items"][0]["source"] = "source/observation:record@2"
    assert session.observe_read_result(packet)
    session.save(session.messages("Read progress", {"source":"same"}))
    archive = json.loads(session.artifacts.read_text(session.ref))
    resumed = LearnerConversation(session.artifacts, session.binding)
    resumed.restore_read_progress(archive)
    assert not resumed.observe_read_result(page(3, "defghijklm"))


@pytest.mark.asyncio
async def test_live_executor_sees_probe_elapsed_separately_from_job_elapsed(monkeypatch):
    from agent.skills import learning
    from agent.skills.exploration import LearningExecutor, PlanExecutor
    clock = [1000.0]
    monkeypatch.setattr(learning.time, "monotonic", lambda: clock[0])
    budget = LearningBudget(max_seconds=5000, started=0, job_started=0, phase_started=0)
    budget.begin_probe()
    clock[0] += 20
    captured = []
    async def call(self, obs, history, **kwargs):
        messages, _ = kwargs["context_state"]["render_observation_bucket"](None)
        captured.append(json.loads(messages[-1]["content"]))
        return None, SimpleNamespace(llm_rounds=[])
    monkeypatch.setattr(PlanExecutor, "_call_with_session", call)
    executor = object.__new__(LearningExecutor)
    executor.budget = budget
    executor.store = SimpleNamespace(
        get=lambda *args: (_ for _ in ()).throw(ValueError("new context")),
        put=lambda kind, key, payload: {"key": key, "version": 1})
    await executor._call_with_session([], [], context_state={
        "render_observation_bucket": lambda package: ([], [])})
    assert captured[0]["budget"]["seconds"] == 1020
    assert captured[0]["probe_cost"]["seconds"] == 20
    assert captured[0]["probe_cost"]["status"] == "running"
    assert "Cumulative" in captured[0]["budget_scope"]


@pytest.mark.asyncio
async def test_live_executor_keeps_capacity_and_usage_audit_without_resending_request_records(monkeypatch):
    from agent.skills import learning
    from agent.skills.exploration import LearningExecutor, PlanExecutor
    monkeypatch.setattr(learning.time, "monotonic", lambda: 100.0)
    budget = LearningBudget(max_seconds=1000, started=0, job_started=0, phase_started=0)
    for index in range(80):
        budget.record_usage({"input_tokens": 30000 + index, "cached_read_tokens": 8192,
            "output_tokens": 200, "cache_hit": True})
    budget.record_usage(None)
    budget.calls, budget.actions = 81, 14
    budget.begin_probe()
    original = copy.deepcopy(budget.snapshot())
    captured = []
    async def call(self, obs, history, **kwargs):
        messages, names = kwargs["context_state"]["render_observation_bucket"](None)
        assert names == ["grounding", "research_runtime"]
        assert messages[0] == {"role":"user", "content":"Live grounding"}
        captured.append(json.loads(messages[-1]["content"]))
        return None, SimpleNamespace(llm_rounds=[])
    monkeypatch.setattr(PlanExecutor, "_call_with_session", call)
    executor = object.__new__(LearningExecutor)
    executor.budget = budget
    executor.store = SimpleNamespace(
        get=lambda *args: (_ for _ in ()).throw(ValueError("new context")),
        put=lambda kind, key, payload: {"key":key, "version":1})
    await executor._call_with_session([], [], context_state={"render_observation_bucket":
        lambda package: ([{"role":"user", "content":"Live grounding"}], ["grounding"])})
    actual = captured[0]["budget"]
    assert {k:v for k,v in actual.items() if k != "provider_usage"} == {
        k:v for k,v in original.items() if k != "provider_usage"}
    usage = actual["provider_usage"]
    assert {k:v for k,v in usage.items() if k in original["provider_usage"]} == {
        k:v for k,v in original["provider_usage"].items() if k != "records"}
    assert "records" not in usage and usage["omitted_request_records"] == 81
    assert usage["unknown_responses"] == 1
    assert len(json.dumps(actual)) < len(json.dumps(original)) / 3
    assert budget.snapshot() == original  # Full immutable audit remains available.


def test_legacy_read_fragment_hashes_stay_usable_when_spans_are_missing(tmp_path):
    from agent.skills.analysis_session import LearnerConversation
    from shared.artifacts import ArtifactStore
    first = LearnerConversation(ArtifactStore(tmp_path), {"source": "same"})
    packet = {"data": {"items": [{"source": "source/observation:legacy@1",
        "observation_id": "legacy", "event_sources": ["source/event:3@1"], "text": "abcdefgh"}]}}
    assert first.observe_read_result(packet)
    first.save(first.messages("Read progress", {"source":"same"}))
    archive = json.loads(first.artifacts.read_text(first.ref))
    archive["read_progress"].pop("spans")  # Prior format had only opaque fragment hashes.
    resumed = LearnerConversation(first.artifacts, first.binding)
    resumed.restore_read_progress(archive)
    assert not resumed.observe_read_result(packet)
    shifted = copy.deepcopy(packet)
    shifted["data"]["items"][0].update(offset=3, text="def")
    assert not resumed.observe_read_result(shifted)


def test_archived_probe_details_keep_incremental_cost_inline():
    from agent.skills.exploration import diagnostic_projection_with_reads
    receipt = {"status": "measured", "calls": 7, "actions": 5, "seconds": 123.063}
    payload = {"source": {"instruction": "Preserve source constraints"},
        "evidence": {"question": "Inspect a transition", "probe_cost": receipt,
            "budget_scope": "Cumulative job", "notes": {"items": [{"text": "Large literal transcript " * 1000}]}}}
    before = copy.deepcopy(payload)
    directories = {}
    projected = diagnostic_projection_with_reads(payload, character_budget=3000, directories=directories)
    assert projected["evidence"]["probe_cost"] == receipt
    assert projected["evidence"]["budget_scope"] == "Cumulative job"
    assert directories and payload == before
