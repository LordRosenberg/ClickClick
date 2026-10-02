from agent.session import AgentSession
from agent.skills.library import SkillLibrary


def write_core(root, name, profiles, variant=False):
    path = root / 'apps/com.google.android.documentsui/core'
    if variant:
        path /= profiles[0]
    path.mkdir(parents=True, exist_ok=True)
    (path / 'SKILL.md').write_text(f'''---
name: {name}
description: Profile-specific picker guidance.
version: 1.0.0
app: com.google.android.documentsui
interface_scope: system
device_profiles: [{', '.join(profiles)}]
kind: app_core
source: authored
---
## Hints
- {name} only.
''', encoding='utf8')


def test_unexpected_picker_loads_only_matching_core(tmp_path):
    write_core(tmp_path, 'aw-core', ['androidworld_api33'])
    write_core(tmp_path, 'mw-core', ['mobileworld_api34'], variant=True)
    assert SkillLibrary(tmp_path).lint_authored_modules() == []
    for profile, expected in [('androidworld_api33', 'aw-core'), ('mobileworld_api34', 'mw-core')]:
        session = AgentSession('executor', 'm', library=SkillLibrary(tmp_path, device_profiles=[profile]))
        session.set_target_app('com.mattermost.rnbeta')
        session.set_foreground_app('com.google.android.documentsui')
        assert {row['skill_id'] for row in session.active_skill_metadata} == {expected}
        session.set_foreground_app('com.mattermost.rnbeta')
        assert session.active_skill_metadata == []
    assert SkillLibrary(tmp_path, device_profiles=[]).app_core('com.google.android.documentsui') is None


def test_overlapping_profile_cores_are_rejected(tmp_path):
    write_core(tmp_path, 'original-core', ['androidworld_api33', 'mobileworld_api34'])
    write_core(tmp_path, 'conflicting-core', ['mobileworld_api34'], variant=True)
    assert any('already has app_core' in e for e in SkillLibrary(tmp_path).lint_authored_modules())
    assert SkillLibrary(tmp_path, device_profiles=['mobileworld_api34']).app_core('com.google.android.documentsui') is None
