from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.session import AgentSession
from agent.skills.library import SkillLibrary, parse_skill_markdown

ROOT = Path(__file__).resolve().parents[1] / 'skills'


def test_interface_ownership_preserves_shared_methods_and_app_exceptions():
    admin = SkillLibrary(ROOT)
    assert all('interface_scope' in p.path.read_text(encoding='utf-8') for p in admin.load_all())
    for profiles in [[], ['mobileworld_api34'], ['xiaomi_15_cn_android15']]:
        lib = admin.for_device(profiles)
        assert lib.get('retro-music-playlists').interface_scope == 'app'
        assert lib.get('search-engine-recovery').interface_scope == 'generic'
        assert lib.get('documentsui-location-evidence') is None
        assert lib.get('androidworld-tasks-find-by-date') is None  # Third-party exception retained.
    assert admin.for_device(['androidworld_api33']).get('documentsui-location-evidence')


@pytest.mark.parametrize('extra', [
    'interface_scope: system\n',
    'interface_scope: incorrect\n',
    'interface_scope: [app]\n',
    'interface_scope: app\n',
])
def test_invalid_interface_scope_rejected(extra):
    with pytest.raises(ValueError, match='interface_scope'):
        parse_skill_markdown('---\nname: bad\nkind: generic\n'+extra+'---\nBody')


def test_third_party_system_surface_and_invalid_edit(tmp_path):
    lib = SkillLibrary(tmp_path)
    pack = lib.create_canonical(name='attach-via-system-picker', app='com.example.mail',
        description='Attach using system picker', capability='attach_file',
        kind='workflow', interface_scope='system', device_profiles=['mobileworld_api34'],
        body='## Procedure\nUse the system picker.\n## Verification\nCheck the attachment.')
    assert lib.for_device(['mobileworld_api34']).get(pack.id)
    assert not lib.for_device(['androidworld_api33']).get(pack.id)
    before = pack.path.read_bytes()
    with pytest.raises(ValueError, match='device_profiles'):
        lib.update_skill(pack.id, extra_frontmatter={'device_profiles': []})
    assert pack.path.read_bytes() == before
    with pytest.raises(ValueError, match='device_profiles'):
        lib.create_canonical(name='unbound-settings', app='com.android.settings', kind='app_core')
    assert not (tmp_path/'apps/com.android.settings/core/SKILL.md').exists()


def test_device_catalog_and_exact_id_are_both_filtered():
    admin = SkillLibrary(ROOT)
    emulator = admin.for_device(['androidworld_api33'])
    xiaomi = admin.for_device(['xiaomi_15_cn_android15'])
    unknown = admin.for_device([])
    clock = 'miui-clock-timer-entry'
    files = 'documentsui-manage-local-files'
    assert admin.get(clock) and admin.get(files)
    assert xiaomi.get(clock) and not xiaomi.get(files)
    assert emulator.get(files) and not emulator.get(clock)
    assert not unknown.get(clock) and not unknown.get(files)
    assert not emulator.app_workflows('com.android.deskclock')
    assert xiaomi.workflow_catalog(['com.android.deskclock'])
    assert emulator.get('chrome-grid-maze')  # Shared browser content is retained.


@pytest.mark.asyncio
async def test_roles_filter_before_delivery_and_clear_previous_device():
    for role in ['planner', 'executor', 'reviewer']:
        session = AgentSession(role, 'test', library=SkillLibrary(ROOT))
        driver = SimpleNamespace(skill_profile_ids=AsyncMock(return_value=['xiaomi_15_cn_android15']))
        await session.bind_device_skills(driver)
        session.set_target_app('com.android.deskclock', ['miui-clock-timer-entry'])
        assert session._k_wire()
        driver.skill_profile_ids.return_value = ['androidworld_api33']
        await session.bind_device_skills(driver)
        assert not session._k_wire()
        with pytest.raises(ValueError):
            session.set_target_app('com.android.deskclock', ['miui-clock-timer-entry'])
        session.set_target_app('com.google.android.documentsui', ['documentsui-manage-local-files'])
        assert session.active_skill_metadata[0]['selected_device_profiles'] == ['androidworld_api33']
        driver.skill_profile_ids.side_effect = RuntimeError('identity unavailable')
        await session.bind_device_skills(driver)
        assert session.library.get('documentsui-manage-local-files') is None
        assert not session._k_wire()


def test_invalid_scope_is_rejected_and_edits_preserve_scope(tmp_path):
    raw = (ROOT/'apps/com.android.deskclock/workflows/timer-entry/SKILL.md').read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='device_profiles'):
        parse_skill_markdown(raw.replace('[xiaomi_15_cn_android15]', 'xiaomi_15_cn_android15'))
    destination = tmp_path/'apps/com.android.deskclock/workflows/miui-clock-timer-entry/SKILL.md'
    destination.parent.mkdir(parents=True)
    destination.write_text(raw, encoding='utf-8')
    lib = SkillLibrary(tmp_path)
    lib.update_skill('miui-clock-timer-entry', description='Updated description')
    assert lib.get('miui-clock-timer-entry').device_profiles == ['xiaomi_15_cn_android15']
    assert lib.for_device(['androidworld_api33']).get('miui-clock-timer-entry') is None


@pytest.mark.asyncio
async def test_remote_driver_transmits_device_scope():
    from httpx import ASGITransport
    from agent.driver_client import DriverClient
    from driver.fixture import FixtureDriver
    from driver.rpc_server import create_driver_app

    driver = FixtureDriver()
    driver.skill_profile_ids = AsyncMock(return_value=['androidworld_api33'])
    remote = DriverClient('http://test', transport=ASGITransport(app=create_driver_app(driver)))
    session = AgentSession('planner', 'test', library=SkillLibrary(ROOT))
    await session.bind_device_skills(remote)
    assert session.library.get('documentsui-manage-local-files')
    assert session.library.get('miui-clock-stopwatch') is None


@pytest.mark.asyncio
async def test_orchestrator_binds_scope_before_executor_handoff(tmp_path, monkeypatch):
    import json
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.artifacts import ArtifactStore
    from shared.config import Settings
    from shared.db import Database
    from shared.llm_gateway import GatewayResponse, ToolCall
    from shared.revisable import Plan, Stage
    from shared.schemas import AgentState, TaskStatus

    monkeypatch.setenv('CLICKCLICK_SKILLS_DIR', str(ROOT))
    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture='plan_executor',
                        default_model='test', manager_model='test', executor_model='test')
    driver = FixtureDriver()
    driver.skill_profile_ids = AsyncMock(return_value=['androidworld_api33'])
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  artifacts=artifacts, driver=driver)
    state = AgentState(instruction='Open a file')
    state.revisable.plan = Plan(current_stage=Stage(goal='Open a file',
        target_app='com.google.android.documentsui', skill_ids=['documentsui-manage-local-files']))
    state.revisable.next_role = 'executor'
    record = db.create_task(state.instruction, state, device_serial='fixture')

    async def complete(model, messages, **kwargs):
        assert 'documentsui-manage-local-files' in str(messages)
        anchor = next(json.loads(m['content'])['runtime_update'] for m in reversed(messages)
                      if isinstance(m.get('content'), str) and m['content'].startswith('{"runtime_update"'))
        args = {'decision':'act','summary':'Go home','observation_id':anchor['current_observation_id'],
                'action':{'type':'home'}}
        return GatewayResponse(content='', model='test', stop_reason='tool_calls',
            tool_calls=[ToolCall(id='action',name='submit_executor_step',arguments=json.dumps(args))])
    monkeypatch.setattr('agent.session.complete', complete)
    try:
        assert await runtime.run_task(record.id, max_device_actions=1) == TaskStatus.RUNNING
        assert len(driver.actions) == 1
    finally:
        db.close()
