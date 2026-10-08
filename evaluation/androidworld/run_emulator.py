"""Paired official AndroidWorld tasks on the existing API 33 emulator.

Official task initializers, database validators, teardown, and complexity budgets.
Uses a persistent task session and independent action/model-call limits.
"""
import argparse
import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import pickle
import random
import sqlite3
import subprocess
import time
import traceback
import imageio_ffmpeg
import numpy as np
from pydub import AudioSegment
from android_world.env import env_launcher
from android_world.env import android_world_controller, interface
from android_world import registry
from android_world.task_evals.information_retrieval.information_retrieval import InformationRetrieval
from android_env import loader
from android_env.components import config_classes
from device_settings import capture, restore
from result_classification import model_infrastructure_failure
from scoring import POLICY, OracleObservationError, score_task
from episode_cleanup import teardown_task, prepare_task_environment

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent.parent
from portable import adb_executable, agent_python, env_file
ADB = adb_executable()
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()
def encode(obj):
    if dataclasses.is_dataclass(obj): return dataclasses.asdict(obj)
    if isinstance(obj,np.generic): return obj.item()
    return str(obj)

def save(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=encode),encoding='utf-8')

def hold_learning_fixture(args, out, result, *, prepared=False):
    if not args.learning_boundary or args.learning_wait_seconds <= 0:
        raise ValueError('A learning fixture requires a done-file and positive wait limit')
    save(out / ('learning-fixture.json' if prepared else 'result.json'), result)
    save(args.learning_boundary.with_suffix('.ready.json'), {
        'case': result['case'], 'ready': True,
        'phase': 'initialized_unscored_fixture' if prepared else 'scored_source_fixture'})
    deadline = time.monotonic() + args.learning_wait_seconds
    while not args.learning_boundary.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('Learning boundary timed out; fixture will be torn down')
        time.sleep(.25)


def adb(*args):
    return subprocess.check_output([ADB,'-s','emulator-5554',*args],timeout=40)

def hide_pointer_location():
    """Keep the visible emulator free of Android's pointer-coordinate overlay."""
    adb('shell', 'settings', 'put', 'system', 'pointer_location', '0')
    observed = adb('shell', 'settings', 'get', 'system', 'pointer_location').decode().strip()
    if observed != '0':
        raise RuntimeError(f'Failed to disable system pointer_location: {observed!r}')

def load_env(a11y_method=android_world_controller.A11yMethod.NONE):
    # Official supported observer; avoids the optional forwarding server's
    # intermittent Windows connection and its replacement of enabled services.
    launch=config_classes.EmulatorLauncherConfig(emulator_console_port=5554,adb_port=5555,grpc_port=8554)
    if hasattr(launch,'connect_to_existing'): launch.connect_to_existing=True
    config=config_classes.AndroidEnvConfig(
        task=config_classes.FilesystemTaskConfig(path=android_world_controller._write_default_task_proto()),
        coordinator=config_classes.CoordinatorConfig(
            device_settings=config_classes.DeviceSettingsConfig(
                show_pointer_location=False,
            ),
        ),
        simulator=config_classes.EmulatorConfig(emulator_launcher=launch,
            adb_controller=config_classes.AdbControllerConfig(adb_path=ADB)))
    raw=loader.load(config)
    controller=android_world_controller.AndroidWorldController(raw,
        a11y_method=a11y_method,
        install_a11y_forwarding_app=False)
    env=interface.AsyncAndroidEnv(controller)
    env_launcher.setup_env(env,emulator_setup=False,freeze_datetime=True)
    return env


def reset_for_agent(env, *, go_home):
    """Reset official environment state without collecting a discarded UI tree.

    The worker obtains its own fresh observation. Restore the oracle's selected
    observer even when reset raises; scoring must never inherit NONE here.
    """
    controller = env.controller
    previous_method = controller._a11y_method
    try:
        controller._a11y_method = android_world_controller.A11yMethod.NONE
        env.reset(go_home=go_home)
    finally:
        controller._a11y_method = previous_method
    hide_pointer_location()

def capture_rows(task, env, out, phase):
    """Persist optional task-specific diagnostics when the task exposes them."""
    list_rows = getattr(task, 'list_rows', None)
    if callable(list_rows):
        save(out/f'{phase}-rows.json', list_rows(env))
    elif 'tasks' in task.app_names:
        # InformationRetrieval has no list_rows method. Compare actual task
        # records, excluding generated database/sync identities only; never
        # expose this evaluator-only snapshot to the task or Learner.
        from android_world.task_evals.information_retrieval import task_app_utils
        rows = [{key: value for key, value in dataclasses.asdict(row).items()
                 if key not in {'_id', 'remoteId'}}
                for row in task_app_utils.list_rows(env)]
        save(out/f'{phase}-rows.json', sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)))
    elif 'open tracks sports tracker' in task.app_names:
        from android_world.task_evals.information_retrieval import activity_app_utils
        # Keep actual type, dates and measured totals, excluding generated identities.
        rows = [{key:value for key,value in dataclasses.asdict(row).items() if key not in {'_id', 'uuid'}}
                for row in activity_app_utils.list_rows(env)]
        save(out/f'{phase}-rows.json', sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)))
    elif 'simple calendar pro' in task.app_names:
        # IR tasks lack SQLiteApp.list_rows. Match actual calendar semantics,
        # keeping all fields except the generated primary key; evaluator-only.
        from android_world.task_evals.single.calendar import calendar_utils
        from android_world.task_evals.utils import sqlite_schema_utils, sqlite_utils
        rows = [{key:value for key,value in dataclasses.asdict(row).items() if key != 'id'}
                for row in sqlite_utils.get_rows_from_remote_device(
                    calendar_utils.EVENTS_TABLE, calendar_utils.DB_PATH,
                    sqlite_schema_utils.CalendarEvent, env)]
        save(out/f'{phase}-rows.json', sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)))
    elif ('markor' in task.app_names and type(task).__name__ in {
            'MarkorAddNoteHeader', 'MarkorChangeNoteContent', 'MarkorEditNote', 'MarkorDeleteNote'}):
        # Selected root-note tasks use names and exact bytes, not timestamps or
        # nested-folder state. Snapshot through the official file accessor;
        # evaluator hashes never expose fixture contents to agents.
        from android_world.env import device_constants
        from android_world.utils import file_utils
        with file_utils.tmp_directory_from_device(device_constants.MARKOR_DATA, env.controller) as copied:
            files = [{"name":p.name, "bytes":p.stat().st_size,
                "sha256":hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted(Path(copied).iterdir()) if p.is_file()]
        save(out/f'{phase}-rows.json', {"scope":"markor_root_note_files", "files":files})
    elif 'osmand' in task.app_names and type(task).__name__ == 'OsmAndFavorite':
        # This official task is file-backed, not SQLiteApp. Match the actual
        # favorites, map coverage and saved search/map context before launch.
        # Keep hashes evaluator-only; never send GPX/prefs or oracle targets to agents.
        from android_world.task_evals.single import osmand
        from android_world.utils import file_utils
        files = []
        map_path = '/data/media/0/Android/data/net.osmand/files/Liechtenstein_europe.obf'
        for remote in (osmand._FAVORITES_PATH, osmand._LEGACY_FAVORITES_PATH, map_path):
            exists = file_utils.check_file_exists(remote, env.controller)
            if remote == map_path and not exists:
                raise RuntimeError('OsmAnd offline map missing; refuse unmatched evaluation')
            entry = {'path': remote, 'exists': exists}
            if exists:
                with file_utils.tmp_file_from_device(remote, env.controller) as copied:
                    data = Path(copied).read_bytes()
                entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            files.append(entry)
        with file_utils.tmp_directory_from_device('/data/data/net.osmand/shared_prefs', env.controller) as copied:
            preferences = [{'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                           for p in sorted(Path(copied).iterdir()) if p.is_file()]
        if not preferences:
            raise RuntimeError('OsmAnd preference baseline missing; refuse unmatched evaluation')
        save(out/f'{phase}-rows.json', {'scope': 'osmand_favorite_files_and_preferences',
                                     'files': files, 'preferences': preferences})
    elif 'retro music' in task.app_names:
        # Retro's official evaluator has no list_rows; match actual media and
        # playlist state across arms without feeding fixture answers to agents.
        from android_world.task_evals.single.retro_music import _get_playlist_data
        media = adb('shell', 'content', 'query', '--uri',
            'content://media/external/audio/media', '--projection',
            'title:duration:_size:_display_name').decode('utf-8')
        rows = sorted(line.split(maxsplit=2)[2] for line in media.splitlines() if line.startswith('Row: '))
        if not rows:
            raise RuntimeError('Retro fixture has no media rows; refuse unmatched evaluation')
        captured = {'media': rows, 'playlists': _get_playlist_data(env)}
        if type(task).__name__ == 'RetroPlayingQueue':
            from android_world.task_evals.single.retro_music import _get_playing_queue
            try:
                captured['playing_queue'] = _get_playing_queue(env)
            except sqlite3.OperationalError as exc:
                if 'no such table: playing_queue' not in str(exc):
                    raise
                captured['playing_queue'] = []
        if type(task).__name__ == 'RetroSavePlaylist':
            from android_world.env import device_constants
            from android_world.utils import file_utils
            with file_utils.tmp_directory_from_device(device_constants.DOWNLOAD_DATA, env.controller) as copied:
                captured['download_files'] = [
                    {'name':p.name, 'bytes':p.stat().st_size, 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in sorted(Path(copied).iterdir()) if p.is_file()]
            provider = adb('shell','content','query','--uri','content://media/external/downloads',
                '--projection','_display_name:_data:_size').decode('utf-8')
            captured['download_provider'] = sorted(line.split(maxsplit=2)[2]
                for line in provider.splitlines() if line.startswith('Row: '))
        save(out/f'{phase}-rows.json', captured)

def prepare_retro_export_storage(task, env, out):
    """Complete official storage cleanup so deleted files have no ghost download rows."""
    if type(task).__name__ != 'RetroSavePlaylist':
        return
    from android_world.task_evals.utils import user_data_generation
    user_data_generation._clear_external_downloads(env)
    save(out/'storage-normalization.json', {'scope':'RetroSavePlaylist official Downloads provider',
        'operation':'official _clear_external_downloads before ordinary initialization',
        'policy':'Applies equally to baseline/candidate; official initializer separately deletes shared-storage files. No app route or fixture answers reach agents.'})


def initial_score(task, env, out, task_name):
    """Return the official initial score, allowing Retro's lazy empty queue DB."""
    try:
        return score_task(task, env, out, 'initial')['score'], None
    except sqlite3.OperationalError as exc:
        if task_name == 'RetroPlayingQueue' and 'no such table: playing_queue' in str(exc):
            return 0.0, 'playing_queue table is created lazily after the first queue operation'
        raise

def regenerate_selected_params(records, classes, seed_offset, selected_cases):
    """Use official generators for selected holdouts; row-based tasks reject empty rows."""
    if not seed_offset:
        return
    for spec in records:
        if selected_cases and spec['task'] not in selected_cases:
            continue
        cls = classes[spec['task']]
        require_rows = 'row_objects' in spec['params']
        seed = spec['seed'] + seed_offset
        for _ in range(100):
            random.seed(seed)
            np.random.seed(seed)
            params = cls.generate_random_params()
            if not require_rows or params.get('row_objects'):
                break
            seed += 1000
        else:
            raise RuntimeError('No nonempty holdout instance')
        params['seed'] = seed
        spec.update(seed=seed, params=params, goal=cls(params).goal)


def main(args):
    global ROOT, PROJECT, ADB
    ROOT, PROJECT, ADB = args.output.resolve(), args.project.resolve(), str(args.adb)
    ROOT.mkdir(parents=True, exist_ok=True)
    records=pickle.loads((args.baseline/'frozen-params.pkl').read_bytes())
    classes=registry.TaskRegistry().get_registry('android_world')
    assert (args.baseline/'setup-complete.json').exists()
    regenerate_selected_params(records, classes, args.seed_offset, args.case)
    for spec in records:
        for field in ('max_steps', 'max_model_calls', 'max_seconds'):
            override = getattr(args, field, None)
            if override is not None:
                if override <= 0: raise ValueError('Episode budget must be positive')
                spec[field] = min(spec[field], override)
    selected=[s for s in records if not args.case or s['task'] in args.case]
    if not selected: raise ValueError('No selected cases')
    if getattr(args, 'prepare_learning_only', False) and (
            len(selected) != 1 or args.arm != 'plan_executor' or not args.learning_boundary):
        raise ValueError('Learning-only initialization requires one case, plan_executor and an explicit done-file')
    runtimes = {'plan_reviewer': args.runtime.resolve(),
                'plan_executor': (args.executor_runtime or args.runtime).resolve()}
    protocol = {
        'baseline':str(args.baseline.resolve()),'runtime':str(args.runtime.resolve()),
        'runtime_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=args.runtime,text=True).strip(),
        'runner_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).resolve().parents[2],text=True).strip(),
        'runtimes': {arm: {'path': str(path), 'commit': subprocess.check_output(
            ['git','rev-parse','HEAD'], cwd=path, text=True).strip()} for arm, path in runtimes.items()},
        'purpose': 'learning_fixture' if getattr(args, 'prepare_learning_only', False) else 'ordinary_evaluation',
        'profile':args.profile,'seed_offset':args.seed_offset,
        'scoring_policy': POLICY,
        'history_tokens':args.history_tokens,
        'budget_overrides': {k: getattr(args, k, None) for k in ('max_steps', 'max_model_calls', 'max_seconds')},
        'device':'emulator-5554','model':os.environ.get('CLICKCLICK_EVAL_MODEL','chatgpt/gpt-5.6-sol'),
        'budgets':'AndroidWorld agent steps <= complexity*10; model calls <= complexity*60; wall time <= 900s. One submitted replace_text counts as one step even when its indexed input primitive focuses the field internally; handoffs cost none.',
        'session':'One run_task per episode; official initialize/is_successful/tear_down; not the legacy per-call adapter.',
        'caveat':'Skill-adapted regression results and changed budget accounting are separate from the frozen original benchmark.'}
    for name, value in [('matrix.json',selected),('protocol.json',protocol)]:
        path=ROOT/name
        normalized=json.loads(json.dumps(value,default=encode))
        if path.exists() and json.loads(path.read_text(encoding='utf-8'))!=normalized:
            raise RuntimeError(f'Refuse mixed evaluation conditions at {path}; choose a fresh output directory')
        save(path,value)
    if adb('get-state').decode().strip()!='device' or adb('shell','getprop','ro.build.version.sdk').decode().strip()!='33':
        raise RuntimeError('Requires the existing API 33 emulator-5554')
    captured=capture(adb)
    save(ROOT/'initial-device.json',captured)
    env=None
    results=[]
    try:
        env=load_env()
        # Pinned AndroidEnv's loader currently drops CoordinatorConfig and
        # applies DeviceSettingsConfig defaults (pointer_location=1). Enforce
        # the user-visible setting after AndroidEnv has initialized.
        hide_pointer_location()
        for spec in records:
            if args.case and spec['task'] not in args.case: continue
            for arm in spec.get('order') or ['plan_executor', 'plan_reviewer']:
                if args.arm and arm!=args.arm: continue
                runtime = runtimes[arm]
                if (ROOT/'STOP').exists(): return
                out=ROOT/'episodes'/spec['task']/arm
                if (out/'result.json').exists():
                    results.append(json.loads((out/'result.json').read_text(encoding='utf-8')))
                    continue
                if out.exists() and any(out.iterdir()): raise RuntimeError(f'Incomplete evidence at {out}; preserve before retry')
                out.mkdir(parents=True,exist_ok=True)
                cls=classes[spec['task']]
                task=cls(copy.deepcopy(spec['params']))
                result={'case':spec['task'],'architecture':arm,'score':None,'valid':False}
                initialized=False
                try:
                    print('INITIALIZE', spec['task'],arm,flush=True)
                    from android_world.utils import app_snapshot,file_utils
                    for app_name in task.app_names:
                        if not file_utils.check_directory_exists(app_snapshot._snapshot_path(app_name),env.controller):
                            raise RuntimeError(f'Missing official baseline snapshot: {app_name}; refuse unmatched initialization')
                    random.seed(spec['seed']); np.random.seed(spec['seed'])
                    from setup_full import prepare_task_assets
                    prepare_task_assets(task, env, out)
                    prepare_retro_export_storage(task, env, out)
                    task.initialize_task(env); initialized=True
                    reset_for_agent(env, go_home=task.start_on_home_screen)
                    prepare_task_environment(task, out)
                    capture_rows(task, env, out, 'initial')
                    result['initial_score'], initial_note = initial_score(
                        task, env, out, spec['task'])
                    if initial_note:
                        result['initial_score_note'] = initial_note
                    if result['initial_score']!=0:
                        raise RuntimeError('Initial state already passes official oracle; invalid comparison instance')
                    (out/'initial.png').write_bytes(adb('exec-out','screencap','-p'))
                    public={
                        'goal': task.goal,
                        **{k:spec[k] for k in ['max_steps','max_model_calls','max_seconds']},
                    }
                    save(out/'request.json',public)
                    if getattr(args, 'prepare_learning_only', False):
                        # No ordinary agent run or final score is claimed here.
                        # The same official finally/teardown remains mandatory.
                        result['phase'] = 'initialized_unscored_fixture'
                        hold_learning_fixture(args, out, result, prepared=True)
                    else:
                        print('RUN',spec['task'],arm,flush=True)
                        hide_pointer_location()
                        worker_env=os.environ.copy(); worker_env['PYTHONIOENCODING']='utf-8'
                        worker_env['PYTHONPATH']=str(runtime)
                        with (out/'worker.log').open('w',encoding='utf-8') as log:
                            proc=subprocess.Popen([agent_python(),'-u',str(Path(__file__).with_name('agent_worker.py')),
                                '--architecture',arm,'--request',str(out/'request.json'),
                                '--runtime',str(runtime),'--env-file',env_file(PROJECT),
                                '--output-root',str(ROOT),'--profile',args.profile,
                                '--history-tokens',str(args.history_tokens)],stdout=log,stderr=subprocess.STDOUT,
                                cwd=runtime,env=worker_env)
                            try: proc.wait(timeout=spec['max_seconds']+90)
                            except subprocess.TimeoutExpired:
                                proc.terminate(); proc.wait(timeout=20)
                                result['harness_error']='worker_hard_timeout'
                        if not (out/'worker-result.json').exists():
                            raise RuntimeError(f'Worker exited {proc.returncode} without result; see worker.log')
                        result.update(json.loads((out/'worker-result.json').read_text(encoding='utf-8')))
                        if (out/'worker-cleanup-error.txt').exists():
                            result['infrastructure_failure']='worker_cleanup_failed'
                            raise RuntimeError('Worker resource cleanup failed; preserve diagnostics and stop the batch')
                        if (result.get('stop_cause') == 'observation_preflight_failed'
                                or str(result.get('failure_reason') or '').startswith('plan_runtime:ObservationStageError:')):
                            result['infrastructure_failure']='observation_unavailable'
                            raise RuntimeError('Observation infrastructure unavailable; preserve this unscored attempt for investigation')
                        model_failure=model_infrastructure_failure(result)
                        if model_failure:
                            result['infrastructure_failure']=model_failure
                            raise RuntimeError('Model service unavailable; stop the batch and exclude this episode from task accuracy')
                        if result['role_invocations']==0 or result.get('stop_cause')=='operator_stop':
                            raise RuntimeError('Agent did not enter model execution or was operator-cancelled; infrastructure/calibration result excluded')
                        if isinstance(task, InformationRetrieval):
                            env.interaction_cache = result.get('answer', '')
                        (out/'final.png').write_bytes(adb('exec-out','screencap','-p'))
                        capture_rows(task, env, out, 'final')
                        result.update(score_task(task,env,out,'final'))
                        result['valid']=True
                        result['false_success']=result['status']=='succeeded' and result['score']!=1
                        result['budgeted_success']=(result['score']==1 and result['status']=='succeeded'
                            and not result.get('stop_cause') and result['episode_steps']<=spec['max_steps']
                            and result['elapsed_s']<=spec['max_seconds'])
                        if getattr(args, 'learning_boundary', None):
                            # Keep official fixture and oracle alive until Learner finishes.
                            # Every path still reaches the normal teardown below.
                            hold_learning_fixture(args, out, result)
                except Exception as exc:
                    if isinstance(exc, OracleObservationError):
                        result.update(score=None, valid=False, infrastructure_failure='oracle_observation_failed')
                    result['harness_error']=traceback.format_exc()
                    print('HARNESS_ERROR',result['harness_error'],flush=True)
                finally:
                    if initialized:
                        try: teardown_task(task, env, out); result['teardown_ok']=True
                        except Exception: result['teardown_error']=traceback.format_exc()
                    save(out/'result.json',result)
                    results.append(result); save(ROOT/'live-results.json',results)
                print('RESULT',json.dumps(result,default=encode),flush=True)
                prepared_ok = result.get('phase') == 'initialized_unscored_fixture'
                if (not result['valid'] and not prepared_ok) or result.get('harness_error') or result.get('teardown_error'):
                    raise RuntimeError('Stop on harness/environment error; do not score as architecture failure')
    finally:
        try:
            if env is not None: env.close()
        finally:
            restoration=restore(adb,captured)
            hide_pointer_location()
            save(ROOT/'device-restoration.json',restoration)
            if not restoration['match']:
                raise RuntimeError('Device settings restoration mismatch; inspect device-restoration.json')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline',required=True,type=Path,help='Trusted frozen AndroidWorld evaluation directory')
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--runtime',required=True,type=Path)
    parser.add_argument('--executor-runtime',type=Path,help='Optional frozen Executor branch checkout for that arm')
    parser.add_argument('--project',required=True,type=Path,help='Project with .env and agent .venv')
    parser.add_argument('--adb',required=True,type=Path)
    parser.add_argument('--profile',choices=['app','androidworld'],default='androidworld')
    parser.add_argument('--history-tokens',type=int,choices=[16000,24000,32000],default=16000)
    parser.add_argument('--seed-offset',type=int,default=0)
    parser.add_argument('--case',nargs='+')
    parser.add_argument('--arm',choices=['plan_reviewer','plan_executor'])
    parser.add_argument('--max-steps', type=int)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--max-seconds', type=float)
    parser.add_argument('--learning-boundary', type=Path, help='Experimental done-file; retain scored fixture only until this signal')
    parser.add_argument('--prepare-learning-only', action='store_true', help='Initialize one matched fixture for bounded exploration; run no ordinary agent and produce no task score')
    parser.add_argument('--learning-wait-seconds', type=float, default=900)
    main(parser.parse_args())
