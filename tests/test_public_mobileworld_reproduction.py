"""Offline integrity and paired-score contracts for the public entry point."""
import importlib.util
import json
from pathlib import Path

import pytest

from evaluation.mobileworld import reproduce


def test_prepare_has_no_subprocess_device_or_model_access(tmp_path, monkeypatch):
    def prohibited(*args,**kwargs):
        pytest.fail('Offline preparation attempted a subprocess')
    monkeypatch.setattr(reproduce.subprocess,'run',prohibited)
    prepared = reproduce.prepare(tmp_path)
    reproduce.verify(prepared,reproduce.read(tmp_path/'input-seal.json'))
    profile = reproduce.read(prepared/'profile.json')
    assert len(profile['tasks']) == 117
    assert len(profile['clarified_tasks']) == 9
    assert profile['max_round'] == 50
    assert profile['compaction_attempt_notes'] is False


def test_resume_rejects_modified_frozen_source(tmp_path):
    prepared = reproduce.prepare(tmp_path)
    source = prepared/'runtime/agent/revisable/summary.py'
    source.write_text('# changed\n',encoding='utf8')
    with pytest.raises(RuntimeError,match='Frozen input changed'):
        reproduce.prepare(tmp_path)


def test_extra_python_module_cannot_enter_frozen_runtime(tmp_path):
    prepared = reproduce.prepare(tmp_path)
    (prepared/'runtime/agent/injected.py').write_text('')
    with pytest.raises(RuntimeError,match='Unexpected Python source'):
        reproduce.prepare(tmp_path)


def test_paired_substitution_keeps_clarified_failure(tmp_path):
    profile = {'tasks':{'a':{},'b':{}},'clarified_tasks':['a']}
    (tmp_path/'original').mkdir()
    (tmp_path/'clarified').mkdir()
    reproduce.write(tmp_path/'original/results.json',[
        {'task':'a','score':{'score':1}}, {'task':'b','score':{'score':1}}])
    reproduce.write(tmp_path/'clarified/results.json',[{'task':'a','score':{'score':0}}])
    summary = reproduce.summarize(tmp_path,profile)
    assert summary['original']['passed'] == 2
    assert summary['clarified']['passed'] == 1
    assert summary['clarified']['total'] == 117


def test_incomplete_variant_has_no_full_success_percentage(tmp_path):
    profile = reproduce.read(reproduce.BUNDLE/'profile.json')
    (tmp_path/'original').mkdir()
    reproduce.write(tmp_path/'original/results.json',[
        {'task':name,'score':{'score':1}} for name in profile['tasks']])
    summary = reproduce.summarize(tmp_path,profile)
    assert summary['original']['success_percent'] == 100
    assert summary['clarified']['success_percent'] is None
    assert not summary['clarified']['complete']


def test_duplicate_results_rejected(tmp_path):
    (tmp_path/'original').mkdir()
    row={'task':'a','score':{'score':1}}
    reproduce.write(tmp_path/'original/results.json',[row,row])
    with pytest.raises(RuntimeError,match='duplicate result'):
        reproduce.summarize(tmp_path,{'tasks':{'a':{}},'clarified_tasks':[]})


def test_goal_drift_fails_before_delivery():
    spec=importlib.util.spec_from_file_location('public_mobileworld_worker',reproduce.BUNDLE/'worker.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile=reproduce.read(reproduce.BUNDLE/'profile.json')
    name='CartManagementTask'
    row=profile['tasks'][name]
    assert module.deliver(profile,'original',name,row['container_goal']) == row['original_goal']
    assert module.deliver(profile,'clarified',name,row['original_goal']) == row['clarified_goal']
    with pytest.raises(RuntimeError,match='wording changed'):
        module.deliver(profile,'clarified',name,'unreviewed new task')


def test_failed_environment_is_not_scored_as_completed(tmp_path):
    (tmp_path/'original').mkdir()
    reproduce.write(tmp_path/'original/results.json',[
        {'task':'a','environment_status':'invalid_initialization','score':None}])
    summary=reproduce.summarize(tmp_path,{'tasks':{'a':{}},'clarified_tasks':[]})
    assert summary['original']['scored'] == 0
    assert summary['original']['success_percent'] is None


def test_backend_evaluator_source_drift_rejected(monkeypatch):
    from types import SimpleNamespace
    spec=importlib.util.spec_from_file_location('public_mobileworld_source_check',reproduce.BUNDLE/'worker.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile=reproduce.read(reproduce.BUNDLE/'profile.json')
    hashes={name:row['source_file_sha256'] for name,row in profile['tasks'].items()}
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout=json.dumps(hashes)))
    module.verify_backend_source(profile,'dedicated-container')
    hashes[next(iter(hashes))]='changed'
    with pytest.raises(RuntimeError,match='implementation changed'):
        module.verify_backend_source(profile,'dedicated-container')
