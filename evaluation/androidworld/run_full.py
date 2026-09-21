"""Execute a sealed full batch prepared by the reproduction entry point."""
import copy
import argparse
import json
import os
from pathlib import Path
import pickle
import random
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
RUNTIME = ROOT/'runtime'
sys.path.insert(0, str(ROOT/'runner'))
sys.path.insert(0, str(ROOT/'latest-shallow'))
from run_emulator import load_env, adb, encode, reset_for_agent
from device_settings import capture, restore
from portable import agent_python, env_file, lock_file
from result_classification import model_infrastructure_failure, accuracy_pause_reason
from scoring import POLICY, OracleObservationError, score_task, excluded_oracle_error
from quota_resume import archive_quota_attempt, quota_retry_allowed
from episode_cleanup import teardown_task, resolve_task_packages, prepare_task_environment
from long_run_health import (
    FullRestartRequired, ensure_channels, mark_episode_boundary_clean,
    recover_interrupted_boundary, restart_same_avd_and_initialize,
    validate_runner_contract, write_episode_boundary,
    PREVENTIVE_RESTART_POLICY, preventive_restart_check,
    SETUP_WINDOW_POLICY, upcoming_setup_window, initialize_app_scope,
)
from setup_full import missing_task_baselines
from android_world import registry, suite_utils
from android_world.env import adb_utils, android_world_controller
from android_world.task_evals.information_retrieval.information_retrieval import InformationRetrieval
from android_world.utils import app_snapshot, file_utils
import numpy as np
import imageio_ffmpeg
from pydub import AudioSegment

AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=encode),encoding='utf-8')
    tmp.replace(path)

def state(status, **kw):
    save(ROOT/'run-state.json', {'status':status,'pid':os.getpid(),'updated_at':time.time(), **kw})
    print(status, json.dumps(kw,default=str), flush=True)

def capture_rows(task, env, out, phase):
    # Optional diagnostics, never a substitute for the official validator.
    if callable(getattr(task, 'list_rows', None)):
        save(out/f'{phase}-rows.json', task.list_rows(env))

def run_one(spec, cls, env):
    out = ROOT/'episodes'/spec['task']/'plan_executor'
    if out.exists() and any(out.iterdir()):
        raise RuntimeError(f'Incomplete attempt preserved at {out}; do not overwrite or silently retry')
    out.mkdir(parents=True)
    task = cls(copy.deepcopy(spec['params']))
    result = {'case':spec['task'],'architecture':'plan_executor','valid':False,'score':None,
              'seed':spec['seed'], 'max_steps':spec['max_steps'], 'max_model_calls':spec['max_model_calls']}
    try:
        state('initializing',case=spec['task'],episode_dir=str(out))
        for name in task.app_names:
            if name and not file_utils.check_directory_exists(app_snapshot._snapshot_path(name),env.controller):
                raise RuntimeError(f'Missing snapshot: {name}')
        random.seed(spec['seed']); np.random.seed(spec['seed'])
        packages, package_errors = resolve_task_packages(task, adb_utils)
        if package_errors:
            raise RuntimeError(f'Cannot establish episode package boundary: {package_errors}')
        # Persist before official initialize.  If this process is interrupted,
        # the next runner invocation stops only these packages before it runs
        # any restore/setup/initialize for another task.
        write_episode_boundary(ROOT, spec['task'], packages)
        from setup_full import prepare_task_assets
        prepare_task_assets(task, env, out)
        task.initialize_task(env)
        reset_for_agent(env, go_home=task.start_on_home_screen)
        prepare_task_environment(task, out)
        save(out/'initialization.json', {'official_initialize_task': task.initialized,
            'env_reset': True, 'reset_tree_collection': False, 'seed': spec['seed'],
            'completed_at': time.time()})
        capture_rows(task, env, out, 'initial')
        # Diagnostic only: valid official random instances are never reselected.
        result['initial_score'] = score_task(task, env, out, 'initial')['score']
        (out/'initial.png').write_bytes(adb('exec-out','screencap','-p'))
        public = {k:spec[k] for k in ['max_steps','max_model_calls','max_seconds']}
        public['goal'] = task.goal
        if isinstance(task, InformationRetrieval):
            public['goal'] += '\n\nAnswer submission: when you complete this task, put only the requested answer in your final completion reason, using the answer format specified above. Do not add an explanation or a completion sentence. This field is submitted unchanged as your answer.'
        save(out/'request.json',public)
        save(out/'official-goal.json',{'goal':task.goal,'answer_task':isinstance(task,InformationRetrieval)})
        if (ROOT/'STOP').exists():
            result['stop_cause'] = 'operator_stop'
            return result
        state('running',case=spec['task'],episode_dir=str(out))
        worker_env = os.environ.copy()
        worker_env.update(PYTHONPATH=str(RUNTIME),PYTHONIOENCODING='utf-8')
        with (out/'worker.log').open('w',encoding='utf-8') as log:
            proc = subprocess.Popen([agent_python(),'-u',str(ROOT/'runner/agent_worker.py'),
                '--architecture','plan_executor','--request',str(out/'request.json'),'--runtime',str(RUNTIME),
                '--env-file',env_file(PROJECT),'--output-root',str(ROOT),'--history-tokens','16000'],
                stdout=log,stderr=subprocess.STDOUT,cwd=RUNTIME,env=worker_env)
            save(out/'worker-pid.json',{'pid':proc.pid})
            try:
                proc.wait(timeout=spec['max_seconds']+90)
            except subprocess.TimeoutExpired:
                proc.terminate(); proc.wait(timeout=20)
                raise RuntimeError('Worker exceeded hard watchdog; evidence preserved')
        if not (out/'worker-result.json').exists():
            raise RuntimeError(f'Worker exited {proc.returncode} without result')
        result.update(json.loads((out/'worker-result.json').read_text(encoding='utf-8')))
        if (out/'worker-cleanup-error.txt').exists():
            raise RuntimeError('Worker cleanup failed')
        if (result.get('stop_cause') == 'observation_preflight_failed'
                or str(result.get('failure_reason') or '').startswith('plan_runtime:ObservationStageError:')):
            result['infrastructure_failure'] = 'observation_unavailable'
            return result
        if failure := model_infrastructure_failure(result):
            result['infrastructure_failure'] = failure
            return result
        if result.get('stop_cause') == 'operator_stop' or result['role_invocations'] == 0:
            return result
        if str(result.get('failure_reason') or '').startswith(tuple('plan_runtime:' + name + ':' for name in ['AttributeError', 'TypeError', 'KeyError', 'ImportError', 'AssertionError'])):
            raise RuntimeError('Internal runtime exception; preserve attempt and investigate')
        if result.get('stop_cause') == 'runtime_exception':
            raise RuntimeError('Uncaught worker exception; inspect before classifying')
        if isinstance(task, InformationRetrieval):
            env.interaction_cache = result.get('answer', '')
        (out/'final.png').write_bytes(adb('exec-out','screencap','-p'))
        capture_rows(task, env, out, 'final')
        result.update(score_task(task, env, out, 'final'))
        if spec['task'] == 'AudioRecorderRecordAudioWithFileName':
            from android_world.env import device_constants
            directory = device_constants.AUDIORECORDER_DATA
            files = file_utils.get_file_list_with_metadata(directory, env.controller)
            save(out/'recording-files.json', files)
            expected = task.params['file_name']
            exact = file_utils.check_file_or_folder_exists(expected, directory, env.controller)
            result['filename_audit'] = {'requested': expected, 'exact_exists': exact,
                'audit_only': True, 'scoring_policy': POLICY}
            pulled = subprocess.run([str(__import__('run_emulator').ADB), '-s', 'emulator-5554',
                'pull', directory, str(out/'recording-evidence')], capture_output=True, timeout=60)
            (out/'recording-pull.log').write_bytes(pulled.stdout + pulled.stderr)
        result['valid'] = True
        result['false_success'] = result['status'] == 'succeeded' and result['score'] != 1
        result['budgeted_success'] = (result['score'] == 1 and result['status'] == 'succeeded'
            and not result.get('stop_cause') and result['episode_steps'] <= spec['max_steps']
            and result['role_invocations'] <= spec['max_model_calls'] and result['elapsed_s'] <= spec['max_seconds'])
    except Exception as exc:
        if isinstance(exc, OracleObservationError):
            result.update(score=None, valid=False, infrastructure_failure='oracle_observation_failed')
        result['harness_error'] = traceback.format_exc()
        print(result['harness_error'],flush=True)
    finally:
        if task.initialized:
            try:
                teardown_task(task, env, out)
                result['teardown_ok'] = True
                mark_episode_boundary_clean(ROOT, spec['task'])
            except Exception:
                result['teardown_error'] = traceback.format_exc()
        save(out/'result.json',result)
    return result

def main(continue_on_oracle_error=None, resume_after_quota=False, accuracy_floor=None):
    # OS releases this lock after normal exit or a crash.
    lock = (ROOT/'run.lock').open('a+b')
    lock_file(lock)
    if (ROOT/'STOP').exists():
        state('stopped',reason='STOP exists; no model calls started')
        return
    manifest = json.loads((ROOT/'source-hashes.json').read_text())
    import hashlib
    for name, digest in manifest.items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, f'Frozen source changed: {name}'
    assert (ROOT/'setup-complete.json').exists(), 'Environment setup must finish first'
    validate_runner_contract(ROOT)
    if not resume_after_quota:
        assert json.loads((ROOT/'model-probe.json').read_text())['available']
    previous_policy = json.loads((ROOT/'run-policy.json').read_text()) if (ROOT/'run-policy.json').exists() else {}
    if continue_on_oracle_error is None:
        continue_on_oracle_error = previous_policy.get('continue_on_oracle_error', False)
    policy = {'continue_on_oracle_error': continue_on_oracle_error, 'scoring_policy': POLICY,
              'preventive_restart': dict(PREVENTIVE_RESTART_POLICY),
              'setup_window': dict(SETUP_WINDOW_POLICY)}
    if accuracy_floor is None:
        accuracy_floor = previous_policy.get('accuracy_floor')
    if accuracy_floor is not None:
        if not 0 <= accuracy_floor <= 1:
            raise ValueError('Accuracy floor must be between 0 and 1')
        policy.update(accuracy_floor=accuracy_floor, accuracy_min_valid_samples=10,
                      decline_window=10, decline_percentage_points=20)
    if (ROOT/'run-policy.json').exists():
        assert json.loads((ROOT/'run-policy.json').read_text()) == policy, 'Run policy changed; use a fresh batch'
    save(ROOT/'run-policy.json', policy)
    if resume_after_quota:
        previous_state = json.loads((ROOT/'run-state.json').read_text())
        if previous_state.get('status') != 'paused_model_quota' or not previous_state.get('restoration_ok'):
            raise RuntimeError('Quota resume requires a quota pause and successful device restoration')
        # Fresh probe under the same batch lock. A stale available=true is insufficient.
        probe = subprocess.run([agent_python(), '-u', str(ROOT/'probe_model.py')], cwd=PROJECT)
        if probe.returncode != 0 or not json.loads((ROOT/'model-probe.json').read_text())['available']:
            raise RuntimeError('Model is still unavailable; keep all interrupted evidence and retry resume after recovery')
    assert adb('get-state').decode().strip() == 'device'
    assert adb('shell','getprop','ro.build.version.sdk').decode().strip() == '33'
    saved = capture(adb)
    if not (ROOT/'initial-device.json').exists(): save(ROOT/'initial-device.json',saved)
    env = None
    results = []
    ending = 'error'
    try:
        env = load_env(android_world_controller.A11yMethod.UIAUTOMATOR)
        classes = registry.TaskRegistry().get_registry('android_world')
        assert len(classes) == 116
        if not (ROOT/'frozen-params.pkl').exists():
            random.seed(20260914); np.random.seed(20260914)
            suite = suite_utils.create_suite(classes, n_task_combinations=1,seed=20260914,env=env)
            records = [{'task':name,'params':task.params,'seed':task.params['seed'],
                'max_steps':int(task.complexity*10),'max_model_calls':int(task.complexity*60),
                'max_seconds':900,'apps':list(task.app_names)} for name, tasks in suite.items() for task in tasks]
            (ROOT/'frozen-params.pkl').write_bytes(pickle.dumps(records))
            save(ROOT/'matrix.json', records)
        records = pickle.loads((ROOT/'frozen-params.pkl').read_bytes())
        assert len(records) == 116 and {r['task'] for r in records} == set(classes)
        for ordinal, spec in enumerate(records):
            # This runs before any initialization in the new process.  It is a
            # no-op after a normal score/tear_down/force-stop boundary.
            recover_interrupted_boundary(ROOT, adb)
            result_path = ROOT/'episodes'/spec['task']/'plan_executor/result.json'
            if result_path.exists():
                result = json.loads(result_path.read_text(encoding='utf-8'))
                if resume_after_quota and quota_retry_allowed(result):
                    archive_quota_attempt(ROOT, spec['task'])
                else:
                    if (not result['valid'] or result.get('teardown_error')) and not (
                        continue_on_oracle_error and excluded_oracle_error(result)):
                        raise RuntimeError(f'Excluded attempt requires explicit inspection before retry: {spec["task"]}')
                    results.append(result)
                    continue
            if (ROOT/'STOP').exists():
                ending = 'stopped'; break
            # The worker watches STOP and cancels immediately. Maintenance
            # instead waits for run_one's scoring/teardown and this boundary.
            if (ROOT/'PAUSE_AFTER_EPISODE').exists():
                ending = 'paused_environment_review'; break
            pending = [row for row in records[ordinal:] if not
                       (ROOT/'episodes'/row['task']/'plan_executor/result.json').exists()]
            setup_window = upcoming_setup_window(pending)
            maintenance = preventive_restart_check(adb, spec)
            maintenance['setup_window'] = setup_window
            maintenance_path = ROOT/'health'/f'preventive-{time.time_ns()}.json'
            save(maintenance_path, maintenance)
            if maintenance['restart_due']:
                state('preventive_environment_restart', case=spec['task'],
                      evidence=str(maintenance_path))
                env.close()
                env = None
                import run_emulator as frozen_runner
                restart = restart_same_avd_and_initialize(
                    ROOT, Path(frozen_runner.ADB), python=Path(sys.executable),
                    apps=setup_window['apps'])
                maintenance['restart'] = restart
                save(maintenance_path, maintenance)
                env = load_env(android_world_controller.A11yMethod.UIAUTOMATOR)
            state('health_check', case=spec['task'])
            try:
                health = ensure_channels(env, ROOT, adb,
                                         allow_full_restart=not maintenance['restart_due'])
            except FullRestartRequired as exc:
                state('recovering_environment', case=spec['task'],
                      degraded=exc.report.get('remaining_degraded'),
                      evidence=exc.report.get('evidence'))
                env.close()
                env = None
                import run_emulator as frozen_runner
                restart = restart_same_avd_and_initialize(
                    ROOT, Path(frozen_runner.ADB), python=Path(sys.executable),
                    apps=setup_window['apps'])
                save(ROOT/'health/latest-full-restart.json', restart)
                env = load_env(android_world_controller.A11yMethod.UIAUTOMATOR)
                health = ensure_channels(
                    env, ROOT, adb, allow_full_restart=False)
            save(ROOT/'health/latest-ready.json', health)
            missing = missing_task_baselines(spec['apps'], env)
            if missing:
                state('preparing_environment', case=spec['task'], missing_apps=missing,
                      setup_window=setup_window)
                env.close()
                env = None
                import run_emulator as frozen_runner
                setup_report = initialize_app_scope(
                    ROOT, Path(frozen_runner.ADB), python=Path(sys.executable),
                    apps=setup_window['apps'])
                save(ROOT/'health'/f'app-scope-{time.time_ns()}.json', setup_report)
                env = load_env(android_world_controller.A11yMethod.UIAUTOMATOR)
                if missing_task_baselines(spec['apps'], env):
                    raise RuntimeError('Current task dependencies remain missing after scoped setup')
                health = ensure_channels(env, ROOT, adb, allow_full_restart=False)
                save(ROOT/'health/latest-ready.json', health)
            # Health/setup may be slow. Recheck once and stop rather than enter
            # an unbounded reboot loop or start a case outside the policy.
            maintenance['before_initialize'] = preventive_restart_check(adb, spec)
            save(maintenance_path, maintenance)
            if maintenance['before_initialize']['restart_due']:
                raise RuntimeError('Insufficient uptime allowance after environment preparation; no task started')
            # An operator may request a pause while full setup is running.
            if (ROOT/'STOP').exists():
                ending = 'stopped'; break
            if (ROOT/'PAUSE_AFTER_EPISODE').exists():
                ending = 'paused_environment_review'; break
            result = run_one(spec, classes[spec['task']], env)
            results.append(result)
            save(ROOT/'live-results.json',results)
            print('RESULT',json.dumps(result,default=encode),flush=True)
            if not result['valid'] or result.get('teardown_error'):
                if continue_on_oracle_error and excluded_oracle_error(result):
                    state('excluded_oracle_error', case=spec['task'], episode_dir=str(result_path.parent))
                    continue
                ending = ('paused_model_quota' if quota_retry_allowed(result) else
                          'stopped' if result.get('stop_cause') == 'operator_stop' else 'paused_infrastructure')
                break
            if accuracy_floor is not None:
                accuracy_pause = accuracy_pause_reason(results, accuracy_floor)
                if accuracy_pause:
                    save(ROOT/'accuracy-pause.json', accuracy_pause)
                    ending = 'paused_accuracy_review'
                    break
        else:
            ending = 'completed'
    except Exception:
        (ROOT/'run-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
    finally:
        try:
            if env is not None: env.close()
        finally:
            restoration = restore(adb,saved)
            save(ROOT/'device-restoration.json',restoration)
            if not restoration['match']: ending = 'restoration_error'
            state(ending,completed=len(results),valid=sum(r['valid'] for r in results),
                  passed=sum(bool(r.get('budgeted_success')) for r in results),restoration_ok=restoration['match'])
            lock.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--continue-on-oracle-error', action='store_true', default=None,
        help='Exclude failed oracle captures and continue only after successful teardown; other infrastructure errors still stop.')
    parser.add_argument('--resume-after-quota', action='store_true',
        help='Probe model recovery, preserve quota-interrupted evidence, and retry that case from official initialization.')
    parser.add_argument('--accuracy-floor', type=float, default=None,
        help='Pause for operator review below this budgeted success rate, or on a 20-point decline between successive 10-case windows.')
    args = parser.parse_args()
    main(args.continue_on_oracle_error, args.resume_after_quota, args.accuracy_floor)


