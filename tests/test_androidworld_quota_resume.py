"""No-device tests for quota pauses and exact preservation across resumption."""
import ast
import json
import os
from pathlib import Path
import pickle
import time
from types import SimpleNamespace

import pytest

from evaluation.androidworld.quota_resume import archive_quota_attempt, quota_retry_allowed
from evaluation.androidworld.scoring import POLICY, excluded_oracle_error
from evaluation.androidworld.long_run_health import PREVENTIVE_RESTART_POLICY


def quota_result():
    return dict(score=None, valid=False, infrastructure_failure='model_quota_exhausted',
                teardown_ok=True)


def test_archive_keeps_all_artifacts_and_rejects_completed_results(tmp_path):
    out = tmp_path / 'episodes/Clock/plan_executor'
    out.mkdir(parents=True)
    (out / 'result.json').write_text(json.dumps(quota_result()))
    (out / 'trace.bin').write_bytes(b'original trace')
    archived = archive_quota_attempt(tmp_path, 'Clock')
    assert not out.exists()
    assert (archived / 'trace.bin').read_bytes() == b'original trace'
    assert json.loads((archived / 'result.json').read_text()) == quota_result()
    for change in [dict(score=0), dict(valid=True), dict(teardown_ok=False),
                   dict(teardown_error='failed'), dict(infrastructure_failure='model_transport_unavailable')]:
        assert not quota_retry_allowed({**quota_result(), **change})
    with pytest.raises(ValueError):
        archive_quota_attempt(tmp_path, '../outside')


def runner_main(tmp_path):
    # Execute the real batch main with isolated device/model dependencies.
    source = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_full.py'
    parsed = ast.parse(source.read_text())
    node = next(n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    classes = {f'Case{i:03}': object for i in range(116)}
    records = [dict(task=name, apps=["android world"]) for name in classes]
    (tmp_path / 'source-hashes.json').write_text('{}')
    (tmp_path / 'setup-complete.json').write_text('{}')
    (tmp_path / 'model-probe.json').write_text('{"available": true}')
    (tmp_path / 'frozen-params.pkl').write_bytes(pickle.dumps(records))

    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def state(status, **kw):
        save(tmp_path / 'run-state.json', dict(status=status, **kw))

    namespace = dict(ROOT=tmp_path, PROJECT=tmp_path, POLICY=POLICY, os=os, json=json,
        pickle=pickle, save=save, state=state, encode=str, time=time,
        PREVENTIVE_RESTART_POLICY=PREVENTIVE_RESTART_POLICY,
        SETUP_WINDOW_POLICY={"version": "test"},
        upcoming_setup_window=lambda records: {"apps": ["android world"]},
        missing_task_baselines=lambda apps, env: [],
        preventive_restart_check=lambda adb, spec: {'restart_due': False},
        lock_file=lambda handle: None, agent_python=lambda: "python",
        msvcrt=SimpleNamespace(locking=lambda *a: None, LK_NBLCK=1),
        adb=lambda *args: b'device' if args[0]=='get-state' else b'33',
        capture=lambda adb: {}, restore=lambda adb, saved: {'match': True},
        load_env=lambda *a: SimpleNamespace(close=lambda: None),
        android_world_controller=SimpleNamespace(A11yMethod=SimpleNamespace(UIAUTOMATOR=1)),
        registry=SimpleNamespace(TaskRegistry=lambda: SimpleNamespace(get_registry=lambda _: classes)),
        archive_quota_attempt=archive_quota_attempt, quota_retry_allowed=quota_retry_allowed,
        excluded_oracle_error=excluded_oracle_error,
        validate_runner_contract=lambda root: {},
        recover_interrupted_boundary=lambda root, adb: None,
        ensure_channels=lambda env, root, adb, **kwargs: {'ready': True},
        FullRestartRequired=type('FullRestartRequired', (RuntimeError,), {}),
        traceback=SimpleNamespace(format_exc=lambda: 'test exception'))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace, save


def test_pause_failed_probe_and_recovery_preserve_completed_cases(tmp_path):
    ns, save = runner_main(tmp_path)
    calls = []
    completed = tmp_path / 'episodes/Case000/plan_executor/result.json'
    save(completed, dict(case='Case000', valid=True, score=0, teardown_ok=True))
    original = completed.read_bytes()
    recovered = False

    def run_one(spec, cls, env):
        calls.append(spec['task'])
        result = dict(case=spec['task'], **(dict(valid=True, score=1, teardown_ok=True) if recovered else quota_result()))
        save(tmp_path / 'episodes' / spec['task'] / 'plan_executor/result.json', result)
        return result

    ns['run_one'] = run_one
    ns['main'](True)
    assert calls == ['Case001']
    assert json.loads((tmp_path / 'run-state.json').read_text())['status'] == 'paused_model_quota'
    assert json.loads((tmp_path / 'live-results.json').read_text())[-1]['score'] is None

    def probe_fail(*a, **kw):
        save(tmp_path / 'model-probe.json', {'available': False})
        return SimpleNamespace(returncode=1)

    ns['subprocess'] = SimpleNamespace(run=probe_fail)
    with pytest.raises(RuntimeError, match='still unavailable'):
        ns['main'](resume_after_quota=True)
    assert calls == ['Case001']
    assert not (tmp_path / 'interrupted-attempts').exists()

    def probe_success(*a, **kw):
        save(tmp_path / 'model-probe.json', {'available': True})
        return SimpleNamespace(returncode=0)

    ns['subprocess'] = SimpleNamespace(run=probe_success)
    recovered = True
    ns['main'](resume_after_quota=True)
    assert calls[:2] == ['Case001', 'Case001']
    assert len(calls) == 116  # 1 interrupted + 115 resumed, Case000 untouched.
    assert completed.read_bytes() == original
    archives = list((tmp_path / 'interrupted-attempts/Case001').glob('*/result.json'))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())['infrastructure_failure'] == 'model_quota_exhausted'
    final = json.loads((tmp_path / 'run-state.json').read_text())
    assert final['status'] == 'completed' and final['valid'] == 116


def test_accuracy_guard_stops_before_next_case_and_restores_device(tmp_path):
    from evaluation.androidworld.result_classification import accuracy_pause_reason
    ns, save = runner_main(tmp_path)
    ns['accuracy_pause_reason'] = accuracy_pause_reason
    calls=[]
    def run_one(spec, cls, env):
        calls.append(spec['task'])
        return {'case':spec['task'],'valid':True,'budgeted_success':False,'score':0,'teardown_ok':True}
    ns['run_one']=run_one
    ns['main'](accuracy_floor=.65)
    assert calls==[f'Case{i:03d}' for i in range(10)]
    state=json.loads((tmp_path/'run-state.json').read_text())
    assert state['status']=='paused_accuracy_review'
    assert state['restoration_ok']
    assert json.loads((tmp_path/'accuracy-pause.json').read_text())['success_rate']==0
    assert json.loads((tmp_path/'live-results.json').read_text())[0]['valid'] is True
