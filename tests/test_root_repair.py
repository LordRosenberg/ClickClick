"""Behavioral boundaries from the real-device false-success incident."""
import json
from pathlib import Path

import pytest

from agent.observation_space import make_registry_entry, transform_action, validate_surface_containment
from agent.revisable.dialogue import restore_dialogue
from agent.revisable.recall import resolve_source
from agent.revisable.session import ExecutionSession
from agent.revisable.store import TaskStore
from agent.revisable.tools import WriteNote, save_note
from agent.skills.library import SkillLibrary
from agent.tool_registry import AgentRole, ToolExecutionContext, ToolStatus
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.revisable import Plan, Stage
from shared.schemas import Action, AgentState, CanonicalUI, UIElement


@pytest.fixture
def state_store(tmp_path):
    db = Database(tmp_path / 'test.db')
    store = TaskStore(db, ArtifactStore(tmp_path / 'artifacts'), 'test')
    state = AgentState(instruction='Use the observed values, then submit the result')
    state.revisable.plan = Plan(current_stage=Stage(goal='Read values'))
    for oid in ['before', 'after']:
        store.put('observation', oid, {'text': oid, 'step': 0})
    yield state, store
    db.close()


def test_stage_skills_inject_retire_and_restore_without_executor_discovery(state_store):
    library = SkillLibrary(Path(__file__).parents[1] / 'skills')
    session = ExecutionSession('executor', 'test', library=library)
    session.state, session.store = state_store
    session.reset_lifecycle('task')
    session.freeze_allow_dirs(['generic'])
    assert session._index == []
    assert 'load_skill' not in {s.name for s in session._build_registry({}).specs_for_role('executor')}
    session.set_stage_skills('com.android.chrome', ['canvas-drawing'])
    body = library.get('canvas-drawing').section_for('executor')
    assert sum(body in m['content'] for m in session._k_wire()) == 1
    session.set_stage_skills('com.android.chrome', ['canvas-drawing'])
    assert sum(body in m['content'] for m in session._k_wire()) == 1
    with pytest.raises(ValueError, match='Unknown'):
        session.set_stage_skills('com.android.chrome', ['canvas_drawing'])
    assert any(body in m['content'] for m in session._k_wire())
    session.set_stage_skills('com.android.chrome', [])
    assert not any(body in m['content'] for m in session._k_wire())
    assert not any(m['skill_id'] == 'canvas-drawing' for m in session.active_skill_metadata)
    restored = ExecutionSession('executor', 'test', library=library)
    restored.set_stage_skills('com.android.chrome', ['canvas-drawing'])
    assert any(body in m['content'] for m in restored._k_wire())
    with pytest.raises(ValueError, match='unique'):
        session.validate_stage_skills('', ['canvas-drawing', 'canvas-drawing'])
    old = Stage.model_validate({'goal': 'Inspect', 'workflow_ids': []})
    assert old.model_dump() == {'goal': 'Inspect', 'target_app': '', 'skill_ids': []}


async def test_material_question_survives_replacement_plan_and_missing_dialogue(state_store):
    state, store = state_store
    save_note(store, WriteNote(note_key='effect', content='Dispatch occurred; result differs',
              unresolved='The observed result disagrees with the requested value', observation_ids=['before']), state)
    save_note(store, WriteNote(note_key='effect', content='Another attempt was dispatched'), state)
    state.revisable.revision += 1
    state.revisable.summary = 'An overconfident model summary says everything is complete.'
    await restore_dialogue(store, state, model='unused', settings=Settings(_env_file=None), meter=None, event_sink=None)
    for role in ['executor', 'planner', 'reviewer']:
        packet = store.context(state, 'after', role)
        assert packet['unresolved_questions'][0]['note_key'] == 'effect'
    question = packet['unresolved_questions'][0]
    # Compact context keeps the read handle; full provenance stays in storage.
    kind, note, _ = resolve_source(store, question['source'])
    assert kind == 'note'
    assert note['payload']['observation_ids'] == ['before']
    with pytest.raises(ValueError, match='referenced'):
        save_note(store, WriteNote(note_key='effect', content='Done', resolution='Assumed done'), state)
    with pytest.raises(ValueError, match='Unknown'):
        save_note(store, WriteNote(note_key='effect', content='Done', resolution='Saw result', observation_ids=['missing']), state)
    save_note(store, WriteNote(note_key='effect', content='Result now matches', resolution='The new observation shows the requested value', observation_ids=['after']), state)
    assert store.unresolved_notes() == []
    save_note(store, WriteNote(note_key='already_resolved', content='Observed result', resolution='Current evidence establishes it', observation_ids=['after']), state)
    assert store.unresolved_notes() == []
    assert store.get('note', 'effect', 1)['payload']['unresolved']


def test_measurement_identity_survives_note_paraphrase_and_resolves_directly(state_store):
    state, store = state_store
    data = {'observation_id': 'before', 'regions': {'index:3': {'index': 3, 'median_rgb': [0, 0, 255]}}}
    source = store.save_measurement(data, state)
    assert 'source' not in data
    save_note(store, WriteNote(note_key='wrong', content='Index 3 was red', source_refs=[source]), state)
    store.put('note', source, {'title':'Shadow', 'content':'Forged measurement', 'observation_ids':[]})
    kind, row, _ = resolve_source(store, source)
    assert kind == 'measurement'
    assert row['payload']['regions']['index:3']['median_rgb'] == [0, 0, 255]
    for role in ['planner', 'reviewer', 'executor']:
        assert store.context(state, 'after', role)['tool_measurements'][0]['source'] == source
    assert store.measurements(character_budget=1) == []


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
def test_declared_surface_checks_transformed_endpoints(rotation):
    entry = make_registry_entry(observation_id='now', coordinate_space_id='now', actionable=True,
        captured_monotonic_ms=1, model_image_size=(100, 100), frame_geometry=(400, 600),
        rotation_degrees=rotation, crop_box=(100, 200, 300, 400),
        ui=CanonicalUI(elements=[UIElement(index=0, bounds=[140, 240, 260, 360], clickable=True)]))
    contained = transform_action(Action(type='drag', surface_index=0, x=40, y=40, x2=60, y2=60), entry.transform)
    assert validate_surface_containment(contained, entry) == ''
    outside = transform_action(Action(type='drag', surface_index=0, x=40, y=40, x2=99, y2=99), entry.transform)
    assert validate_surface_containment(outside, entry) == 'drag_outside_surface'
    assert validate_surface_containment(outside.model_copy(update={'surface_index': 9}), entry) == 'unknown_surface_index'
    assert validate_surface_containment(outside.model_copy(update={'surface_index': None}), entry) == ''


@pytest.mark.parametrize('architecture', ['plan_executor', 'plan_reviewer'])
@pytest.mark.parametrize('explicit_resolution', [False, True, 'already_closed'])
async def test_complete_cannot_override_recorded_question(tmp_path, monkeypatch, architecture, explicit_resolution):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus
    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture=architecture, default_model='test')
    db = Database(settings.db_path); artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver)
    state = AgentState(instruction='Inspect the current state')
    if architecture == 'plan_reviewer': state.revisable.next_role = 'reviewer'
    task = db.create_task(state.instruction, state, device_serial='fixture')
    store = TaskStore(db, artifacts, task.id)
    save_note(store, WriteNote(note_key='result', content='Result account', unresolved='' if explicit_resolution == 'already_closed' else 'Requested effect has not been established'), state)
    invocations = 0
    async def reply(model, messages, *, tools, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations > 1 and not explicit_resolution:
            assert 'Recorded material questions remain unresolved' in str(messages)
        name = next(t['function']['name'] for t in tools if t['function']['name'].startswith('submit_'))
        args = {'decision': 'complete' if invocations == 1 else 'inconclusive', 'reason': 'No supported way to settle the effect'}
        if explicit_resolution:
            packet = next(json.loads(m['content']) for m in messages if isinstance(m.get('content'), str) and m['content'].startswith('{"original_instruction"'))
            measurement = store.save_measurement({'observation_id':packet['current_observation_id'],'regions':{}},state)
            args.update(decision='complete', resolved_questions=['result'], source_refs=[packet['current_observation_id'], measurement], reason='Current observation settles the supplied result question')
        return GatewayResponse(content='', model='test', stop_reason='tool_calls', tool_calls=[ToolCall(id=f'call_{invocations}', name=name, arguments=json.dumps(args))])
    monkeypatch.setattr('agent.session.complete', reply)
    try:
        assert await runtime.run_task(task.id) == (TaskStatus.SUCCEEDED if explicit_resolution else TaskStatus.FAILED)
        assert invocations == (1 if explicit_resolution else 2)
        assert driver.actions == []
        assert bool(store.unresolved_notes()) == (explicit_resolution != 'already_closed')
    finally: db.close()


async def test_free_regions_cannot_claim_an_index_and_invalid_calls_do_not_consume_budget():
    from io import BytesIO
    from PIL import Image
    from perception.observation import ObservationPackage
    from shared.schemas import ObservationMode
    from agent.read_tools import make_inspect_image_regions_handler
    out = BytesIO(); Image.new('RGB', (20, 20), 'blue').save(out, format='PNG')
    package = ObservationPackage(ui=CanonicalUI(elements=[UIElement(index=7, bounds=[0, 0, 20, 20], clickable=True)]),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm='', gap_reasons=[], clean_png=out.getvalue(),
        image_for_llm=out.getvalue(), annotated_png=None, observation_id='now', frame_width=20, frame_height=20,
        model_image_width=20, model_image_height=20, index_actionable=True)
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, 'test', {'active_observation_id':'now','active_package':package})
    handler = make_inspect_image_regions_handler()
    for wrong in [
        {'regions':[{'bounds':[0,0,10,10], 'label':'index:3'}]},
        {'regions':[{'bounds':[0,0,10,10], 'index':3}]},
        {'targets':[{'index':7, 'bounds':[0,0,10,10]}]},
    ]:
        with pytest.raises(ValueError):
            await handler({'observation_id':'now','metrics':['median_rgb'],**wrong},ctx)
    assert not ctx.state.get('image_region_inspection_calls')
    result = await handler({'observation_id':'now','metrics':['median_rgb'],
        'targets':[{'index':7,'inset_ratio':.2}], 'regions':[{'bounds':[4,4,16,16]}],
        'pairs':[['index:7','region:1']]},ctx)
    assert result.data['regions']['index:7']['index'] == 7
    assert 'index' not in result.data['regions']['region:1']
    assert result.data['delta_e_2000'] == [['index:7','region:1',0.0]]


async def test_runtime_handoff_replaces_injected_skill_before_next_executor(monkeypatch, tmp_path):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus
    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture='plan_reviewer', default_model='test')
    db = Database(settings.db_path); artifacts = ArtifactStore(settings.artifacts_dir); driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver)
    task = db.create_task('Inspect two stages', AgentState(instruction='Inspect two stages'), device_serial='fixture')
    planned = 0; executed = 0
    body = SkillLibrary(Path(__file__).parents[1] / 'skills').get('canvas-drawing').section_for('executor')
    async def reply(model, messages, *, tools, **kwargs):
        nonlocal planned, executed
        names = [t['function']['name'] for t in tools]
        name = next(n for n in names if n.startswith('submit_'))
        if name == 'submit_planner_decision':
            from agent.session import _delivered_text
            repairing = any(m.get('role') == 'tool' and 'stage_guidance' in str(m.get('content')) for m in messages)
            if repairing:
                assert body in _delivered_text(messages)
            else:
                planned += 1
            args = {'decision':'complete','reason':'Both stages have been inspected'} if planned == 3 else {
                'decision':'execute','reason':'Current stage only','plan':{'current_stage':{
                    'goal':f'Inspect stage {planned}', 'skill_ids':['canvas-drawing'] if planned == 1 else []},
                    'assumption_roadmap':['Possible later work']}}
        elif name == 'submit_executor_step':
            executed += 1
            assert planned == executed
            assert 'load_skill' not in names
            assert sum(body in str(m.get('content')) for m in messages) == (1 if executed == 1 else 0)
            packet = next(json.loads(m['content'])['runtime_update'] for m in reversed(messages)
                if isinstance(m.get('content'), str) and m['content'].startswith('{"runtime_update"'))
            assert packet['latest_plan']['assumption_roadmap'] == ['Possible later work']
            args = {'decision':'advance','summary':'Observed the current stage result',
                    'observation_id':packet['current_observation_id'],'completed_stage_id':packet['stage_id']}
        else: args = {'decision':'complete','reason':'The original requested result is observed'}
        return GatewayResponse(content='',model='test',stop_reason='tool_calls',tool_calls=[ToolCall(id=f'c{planned}_{executed}',name=name,arguments=json.dumps(args))])
    monkeypatch.setattr('agent.session.complete', reply)
    try:
        assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
        assert planned == 3 and executed == 2
        assert not driver.actions
    finally: db.close()


async def test_surface_rejection_is_returned_without_dispatch_or_pipeline_crash(state_store, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from agent.revisable.roles import PlanExecutor
    from agent.decision_context import _coordinate_surface_image_bounds
    from perception.observation import ObservationPackage
    from shared.schemas import ObservationMode, ActionPipeline
    state, store = state_store
    class Driver:
        async def act(self, action):
            raise AssertionError('Rejected trajectory reached device')
    out = BytesIO(); Image.new('RGB',(200,200),'white').save(out,format='PNG')
    target = UIElement(index=7, resource_id='canvas', role='ImageButton', bounds=[50,50,150,150], clickable=True)
    wrapper = UIElement(index=-1, resource_id='canvasContainer', role='View', bounds=[0,50,200,150])
    package = ObservationPackage(ui=CanonicalUI(app_id="com.example", elements=[target], semantic_tree=[wrapper,target]),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm='[7] ImageButton canvas', gap_reasons=[],
        clean_png=out.getvalue(), image_for_llm=out.getvalue(), annotated_png=None,
        observation_id='now', frame_width=200, frame_height=200, model_image_width=200, model_image_height=200)
    assert _coordinate_surface_image_bounds(package,[wrapper,target])[id(target)] == [50,50,150,150]
    executor = PlanExecutor(Driver(), store.artifacts, model='test', settings=Settings(_env_file=None))
    executor.store = store
    async def reply(model,messages,**kwargs):
        packet = next(json.loads(m['content'])['runtime_update'] for m in reversed(messages)
            if isinstance(m.get('content'),str) and m['content'].startswith('{"runtime_update"'))
        args={'decision':'act','summary':'Confined drag','observation_id':packet['current_observation_id'],
              'action':{'type':'drag','surface_index':7,'x':100,'y':100,'x2':190,'y2':100}}
        return GatewayResponse(content='',model='test',stop_reason='tool_calls',tool_calls=[ToolCall(id='reject',name='submit_executor_step',arguments=json.dumps(args))])
    monkeypatch.setattr('agent.session.complete',reply)
    step,result,*_ = await executor.act_once('Draw inside canvas',package,task_id='test',state=state)
    assert not result.success
    assert step.action_pipeline.dispatch_suppressed
    assert step.action_pipeline.validation_result['reason'] == 'drag_outside_surface'
    assert ActionPipeline.model_validate_json(step.action_pipeline.model_dump_json()).dispatch_suppressed


@pytest.mark.parametrize("role", ["executor", "reviewer"])
def test_non_planner_terminal_sessions_cannot_load_skills(role):
    from agent.session import AgentSession
    from agent.revisable.session import terminal_registry
    from agent.revisable.summary import StructuredSummary
    async def submit(args, ctx): pass
    registry = terminal_registry(AgentSession(role, "unused"), StructuredSummary, "save_summary", "Summarize", submit)
    assert "load_skill" not in {spec.name for spec in registry.specs_for_role(role)}
