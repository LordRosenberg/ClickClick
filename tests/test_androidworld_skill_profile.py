"""Evaluation overlays follow the selected runtime, not a copied runner."""
from pathlib import Path
import shutil

import pytest

from evaluation.androidworld import agent_worker
from agent.session import AgentSession
from agent.skills.library import SkillLibrary


def test_old_runner_cannot_drop_frozen_runtime_guidance(tmp_path, monkeypatch):
    branch = tmp_path/'runtime'
    output = tmp_path/'episode'
    project_skills = Path(__file__).resolve().parents[1]/'skills'
    shutil.copytree(project_skills, branch/'skills')
    (branch/'skills/base.txt').write_text('normal skill assets')
    source = Path(__file__).resolve().parents[1]/'evaluation/androidworld/skills'
    shutil.copytree(source, branch/'evaluation/androidworld/skills')
    stale_runner = tmp_path/'old-runner'
    (stale_runner/'skills').mkdir(parents=True)
    (stale_runner/'skills/obsolete.txt').write_text('old profile')
    monkeypatch.setattr(agent_worker, '__file__', str(stale_runner/'agent_worker.py'))

    root = agent_worker.prepare_skill_root(branch, output, 'androidworld')
    assert (root/'base.txt').read_text() == 'normal skill assets'
    assert not (root/'obsolete.txt').exists()
    for src in source.rglob('SKILL.md'):
        assert (root/src.relative_to(source)).read_bytes() == src.read_bytes()
    lib = SkillLibrary(root, device_profiles=['androidworld_api33'])
    assert lib.app_core('com.dimowner.audiorecorder').id == 'androidworld-audio-recorder-naming'
    markor = lib.app_core('net.gsantner.markor')
    assert markor.id == 'markor-file-management'
    assert 'upper-left toolbar title is the current folder name' in markor.body
    assert 'path shown under `..` identifies that parent' in markor.body
    assert 'Documents/Markor' not in markor.body
    androidworld = lib.get('androidworld-markor-output-directory')
    assert androidworld is not None and androidworld.kind == 'workflow'
    assert 'Documents/Markor' in androidworld.body
    assert 'do not create another `Markor`' in androidworld.body
    assert 'top-toolbar Rename' not in androidworld.body

    session = AgentSession('executor', 'm', library=lib)
    session.reset_lifecycle('task:androidworld-markor')
    session.set_stage_skills('net.gsantner.markor', [androidworld.id])
    assert {item['skill_id'] for item in session.active_skill_metadata} == {
        'markor-file-management', 'androidworld-markor-output-directory',
    }
    wire = '\n'.join(item['content'] for item in session._k_wire())
    assert 'upper-left toolbar title is the current folder name' in wire
    assert 'Documents/Markor' in wire
    assert agent_worker.prepare_skill_root(branch, output, 'app') == branch/'skills'
    assert SkillLibrary(branch/'skills').get('androidworld-audio-recorder-naming') is None


def test_markor_guidance_is_normal_app_skill():
    root = Path(__file__).resolve().parents[1]/'skills'
    lib = SkillLibrary(root)
    skill = lib.app_core('net.gsantner.markor')
    assert skill.id == 'markor-file-management'
    assert 'upper-left toolbar title is the current folder name' in skill.body
    assert 'path shown under `..` identifies that parent' in skill.body
    assert 'Do not add reopening the file as a' in skill.body
    assert 'unless the task explicitly asks to reopen it' in skill.body
    assert 'evaluator checks that directory' not in skill.body


def test_documentsui_androidworld_guidance_is_device_scoped_normal_skill():
    root = Path(__file__).resolve().parents[1]/'skills'
    assert SkillLibrary(
        root, device_profiles=['xiaomi_15_cn_android15'],
    ).get('documentsui-manage-local-files') is None
    lib = SkillLibrary(root, device_profiles=['androidworld_api33'])
    assert lib.get('documentsui-manage-local-files') is not None


@pytest.mark.parametrize('empty_directory', [False, True])
def test_missing_frozen_profile_fails_before_creating_episode(tmp_path, empty_directory):
    branch = tmp_path/'runtime'
    (branch/'skills').mkdir(parents=True)
    if empty_directory:
        (branch/'evaluation/androidworld/skills').mkdir(parents=True)
    output = tmp_path/'episode'
    with pytest.raises(RuntimeError, match='missing AndroidWorld skill profile'):
        agent_worker.prepare_skill_root(branch, output, 'androidworld')
    assert not output.exists()
