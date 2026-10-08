"""Reset discards tree acquisition but keeps official reset and oracle settings."""
import ast
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('fails', [False, True])
def test_reset_restores_oracle_observer_even_on_error(fails):
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'reset_for_agent')
    namespace = {
        'android_world_controller': SimpleNamespace(A11yMethod=SimpleNamespace(NONE='none')),
        'hide_pointer_location': lambda: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    controller = SimpleNamespace(_a11y_method='uiautomator')
    calls = []

    def reset(*, go_home):
        calls.append(go_home)
        assert controller._a11y_method == 'none'
        if fails:
            raise RuntimeError('reset failed')

    env = SimpleNamespace(controller=controller, reset=reset)
    if fails:
        with pytest.raises(RuntimeError, match='reset failed'):
            namespace['reset_for_agent'](env, go_home=True)
    else:
        namespace['reset_for_agent'](env, go_home=True)
    assert calls == [True]
    assert controller._a11y_method == 'uiautomator'


def test_existing_emulator_runner_uses_bundled_ffmpeg_for_audio_fixtures():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    source = path.read_text(encoding='utf-8')

    assert 'import imageio_ffmpeg' in source
    assert 'AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()' in source


def test_existing_emulator_runner_submits_information_retrieval_answer_before_scoring():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    source = path.read_text(encoding='utf-8')

    submit = source.index("env.interaction_cache = result.get('answer', '')")
    score = source.index("result.update(score_task(task,env,out,'final'))")
    assert submit < score


def test_retro_lazy_empty_queue_is_only_tolerated_for_initial_score():
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'initial_score')

    def missing_queue(*_args, **_kwargs):
        raise sqlite3.OperationalError('no such table: playing_queue')

    namespace = {'score_task': missing_queue, 'sqlite3': sqlite3}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)

    score, note = namespace['initial_score'](object(), object(), Path('.'), 'RetroPlayingQueue')
    assert score == 0.0
    assert 'created lazily' in note

    with pytest.raises(sqlite3.OperationalError, match='playing_queue'):
        namespace['initial_score'](object(), object(), Path('.'), 'OtherTask')


def test_retro_fixture_signature_uses_actual_media_without_row_ids(monkeypatch):
    import sys, types
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'capture_rows')
    module = types.ModuleType('android_world.task_evals.single.retro_music')
    module._get_playlist_data = lambda env: []
    monkeypatch.setitem(sys.modules, module.__name__, module)
    saved = []
    namespace = {'save': lambda path, data: saved.append(data),
        'adb': lambda *args: b'Row: 18 title=Song B, duration=240000, _size=10, _display_name=b.mp3\nRow: 37 title=Song A, duration=180000, _size=11, _display_name=a.mp3\n'}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    namespace['capture_rows'](SimpleNamespace(app_names=['retro music']), None, Path('.'), 'initial')
    assert saved[0]['media'][0].startswith('title=Song A') and saved[0]['playlists'] == []
    namespace['adb'] = lambda *args: b'No result found.'
    with pytest.raises(RuntimeError, match='no media rows'):
        namespace['capture_rows'](SimpleNamespace(app_names=['retro music']), None, Path('.'), 'initial')


@pytest.mark.parametrize("prepared", [False, True])
def test_learning_lease_records_phase_and_times_out_without_scoring(tmp_path, prepared):
    import json
    path = Path(__file__).resolve().parents[1] / "evaluation/androidworld/run_emulator.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "hold_learning_fixture")
    ticks = iter([0, 0, 2])
    def save(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    ns = {"save": save, "time": SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    args = SimpleNamespace(learning_boundary=tmp_path / "learning.done", learning_wait_seconds=1)
    result = {"case": "SyntheticCase", "valid": False, "score": None}
    with pytest.raises(RuntimeError, match="timed out"):
        ns["hold_learning_fixture"](args, tmp_path / "out", result, prepared=prepared)
    ready = json.loads(args.learning_boundary.with_suffix(".ready.json").read_text())
    assert ready["phase"] == ("initialized_unscored_fixture" if prepared else "scored_source_fixture")
    recorded = json.loads((tmp_path / "out" / ("learning-fixture.json" if prepared else "result.json")).read_text())
    assert recorded["score"] is None and not recorded["valid"]
    assert not args.learning_boundary.exists()


def test_validator_reentry_prepares_one_unscored_fixture_and_releases_it(tmp_path, monkeypatch):
    import json
    from evaluation.skill_learning import AndroidWorldExperiment
    experiment = AndroidWorldExperiment.__new__(AndroidWorldExperiment)
    experiment.root = tmp_path
    experiment.empty = tmp_path / "no-skills"
    experiment.baseline = experiment.empty
    experiment.args = SimpleNamespace(source="SyntheticCase", learn_seconds=30)
    experiment._episode_launch = lambda directory, task, skills: (["runner"], {})
    calls = []
    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)
            self.done = Path(command[command.index("--learning-boundary") + 1])
            self.done.with_suffix(".ready.json").write_text(json.dumps({
                "case": "SyntheticCase", "ready": True, "phase": "initialized_unscored_fixture"}))
        def poll(self): return None
        def wait(self, timeout):
            assert self.done.exists()
            return 0
    monkeypatch.setattr("evaluation.skill_learning.subprocess.Popen", Process)
    transition = experiment.ensure_source_fixture()
    assert transition["fixture_reset"] and "historical" in transition["policy"]
    assert "--prepare-learning-only" in calls[0]
    reused = experiment.ensure_source_fixture()
    assert reused["fixture_reset"] is False and len(calls) == 1
    assert reused["generation"] == transition["generation"]
    assert "no initializer ran" in reused["policy"]
    experiment.release_source()
    assert experiment._source_process is None and experiment._source_log.closed


def test_tasks_fixture_signature_matches_actual_semantics_without_generated_ids(monkeypatch):
    import dataclasses, json, sys, types
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == 'capture_rows')
    package = types.ModuleType('android_world.task_evals.information_retrieval')
    utils = types.ModuleType(package.__name__ + '.task_app_utils')
    @dataclasses.dataclass
    class Row:
        title: str
        dueDate: int
        completed: int
        _id: int
        remoteId: str
    utils.list_rows = lambda env: env
    package.task_app_utils = utils
    monkeypatch.setitem(sys.modules, package.__name__, package)
    saved = []
    namespace = {'save':lambda path, data:saved.append(data), 'json':json, 'dataclasses':dataclasses}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    task = SimpleNamespace(app_names=['tasks'])
    for rows in ([Row('B',2,4,10,'random-a'),Row('A',1,3,11,'random-b')],
                 [Row('A',1,3,22,'different-a'),Row('B',2,4,21,'different-b')]):
        namespace['capture_rows'](task,rows,Path('.'),'initial')
    assert saved[0] == saved[1] and saved[0][0]['title'] == 'A'
    assert saved[0][0]['dueDate'] == 1 and saved[0][0]['completed'] == 3
    namespace['capture_rows'](task,[Row('A',1,0,22,'different-a')],Path('.'),'initial')
    assert saved[2] != saved[0]


def test_experiment_releases_source_if_baseline_result_collection_fails():
    from evaluation.skill_learning import Experiment
    experiment = Experiment.__new__(Experiment)
    experiment.args = SimpleNamespace(source='SyntheticCase')
    experiment.empty = Path('.')
    experiment.baseline = experiment.empty
    released = []
    def fail(*args):
        raise FileNotFoundError('missing initial state evidence')
    experiment.episode = fail
    experiment.release_source = lambda:released.append(True)
    with pytest.raises(FileNotFoundError,match='initial state'):
        experiment.run()
    assert released == [True]


@pytest.mark.asyncio
async def test_fresh_validation_baseline_releases_lease_before_candidate(tmp_path):
    from evaluation.skill_learning import Experiment
    experiment=Experiment.__new__(Experiment)
    experiment.root=tmp_path;experiment.empty=tmp_path/'empty';experiment.model='test'
    experiment.config={};experiment.case_baselines={};experiment.validation_runs={}
    experiment.args=SimpleNamespace(environment='androidworld',source='SyntheticCase',variant=None,near_miss=None,related_normal=None)
    calls=[]
    def release():calls.append('release')
    def episode(name,task,skills):
        calls.append(name)
        return {'score':True,'budgeted_success':True,'logical_action_steps':1,
            'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same'}
    experiment.release_source=release;experiment.episode=episode
    from agent.skills.snapshot import LibrarySnapshot
    experiment.baseline_snapshot=LibrarySnapshot.freeze(experiment.empty,experiment.root/"baseline-library");experiment.baseline=experiment.baseline_snapshot.root
    result=await experiment.validate(SimpleNamespace(new_text='candidate',target='apps/test/core/SKILL.md'),'')
    assert calls[:3]==['release','baseline-source','release'] and calls[3].endswith('-source')
    assert result['trials'][0]['matched_environment']


@pytest.mark.asyncio
@pytest.mark.parametrize("source_candidate_succeeds", [False, True])
async def test_source_first_validation_defers_only_failed_source_and_keeps_full_gate(tmp_path,source_candidate_succeeds):
    from evaluation.skill_learning import Experiment
    from agent.skills.learning import validation_gate,digest
    e=Experiment.__new__(Experiment);e.root=tmp_path;e.empty=tmp_path/'empty';e.model='test';e.config={}
    e.case_baselines={};e.validation_runs={};e.release_source=lambda:None
    e.args=SimpleNamespace(environment='androidworld',source='S',variant='V',near_miss='M',related_normal='N',source_first_validation=True)
    calls=[]
    def episode(name,task,skills):
        calls.append((name,task));success=source_candidate_succeeds if task=='S' and not name.startswith('baseline') else task!='S'
        return {'score':success,'budgeted_success':success,'logical_action_steps':1,'environment_status':'ready','initial_state_hash':'same','configuration_hash':'same'}
    e.episode=episode;c=SimpleNamespace(new_text='candidate',target='apps/test/core/SKILL.md')
    from agent.skills.snapshot import LibrarySnapshot
    e.baseline_snapshot=LibrarySnapshot.freeze(e.empty,e.root/"baseline-library");e.baseline=e.baseline_snapshot.root
    result=await e.validate(c,'')
    if source_candidate_succeeds:
        assert len(calls)==8 and {t['kind'] for t in result['trials']}=={'source','variant','near_miss','related_normal'}
        assert validation_gate(c,digest(''),result)==(True,'validated')
    else:
        assert len(calls)==2 and [t['kind'] for t in result['trials']]==['source']
        assert result['validation_stage']=='source_rejected'
        assert result['deferred_kinds']==['variant','near_miss','related_normal']
        assert validation_gate(c,digest(''),result)==(False,'incomplete_coverage')


def test_markor_root_note_signature_matches_exact_names_and_bytes_only(tmp_path,monkeypatch):
    import contextlib,hashlib,json,sys,types
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='capture_rows')
    constants=types.ModuleType('android_world.env.device_constants');constants.MARKOR_DATA='/owned/markor'
    utils=types.ModuleType('android_world.utils.file_utils');calls=[]
    @contextlib.contextmanager
    def copy_owned(directory,controller):
        assert directory=='/owned/markor' and controller=='controller';calls.append(directory);yield tmp_path
    utils.tmp_directory_from_device=copy_owned
    monkeypatch.setitem(sys.modules,constants.__name__,constants);monkeypatch.setitem(sys.modules,utils.__name__,utils)
    env_package=types.ModuleType('android_world.env');env_package.device_constants=constants
    utils_package=types.ModuleType('android_world.utils');utils_package.file_utils=utils
    monkeypatch.setitem(sys.modules,env_package.__name__,env_package)
    monkeypatch.setitem(sys.modules,utils_package.__name__,utils_package)
    saved=[];ns={'save':lambda p,v:saved.append(v),'Path':Path,'hashlib':hashlib}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=type('MarkorAddNoteHeader',(),{'app_names':('markor',)})();env=SimpleNamespace(controller='controller')
    (tmp_path/'B.txt').write_bytes(b'body\n');(tmp_path/'A.md').write_bytes(b'header\n\nbody\n')
    ns['capture_rows'](task,env,Path('.'),'initial');ns['capture_rows'](task,env,Path('.'),'initial')
    assert saved[0]==saved[1] and [f['name'] for f in saved[0]['files']]==['A.md','B.txt']
    assert saved[0]['files'][0]['bytes']==13
    assert set(saved[0]['files'][0])=={'name','bytes','sha256'}
    (tmp_path/'A.md').write_bytes(b'header\nbody\n');ns['capture_rows'](task,env,Path('.'),'initial');assert saved[-1]!=saved[0]
    (tmp_path/'A.md').rename(tmp_path/'A.md.txt');ns['capture_rows'](task,env,Path('.'),'initial');assert saved[-1]!=saved[-2]
    assert len(calls)==4


def test_androidworld_launch_matches_set_assigned_noise_across_processes(tmp_path,monkeypatch):
    import json,os,subprocess,sys
    from evaluation.skill_learning import AndroidWorldExperiment
    baseline=tmp_path/'prepared';(baseline/'latest-shallow').mkdir(parents=True)
    (baseline/'setup-complete.json').write_text('{}')
    e=AndroidWorldExperiment.__new__(AndroidWorldExperiment);e.model='test'
    e.args=SimpleNamespace(androidworld_baseline=str(baseline),eval_python=sys.executable,
        agent_python=None,env_file=str(tmp_path/'unused.env'),adb='adb',rounds=12,seconds=900)
    script="import json,random; random.seed(17); names=set('note_%s.md'%i for i in range(40)); print(json.dumps({n:random.getrandbits(64) for n in names},sort_keys=True))"
    snapshots=[]
    for parent_seed in ('1','2'):
        monkeypatch.setenv('PYTHONHASHSEED',parent_seed)
        command,environment=e._episode_launch(tmp_path/'arm','MarkorAddNoteHeader',tmp_path/'skills')
        snapshots.append(json.loads(subprocess.check_output([sys.executable,'-c',script],env=environment,text=True)))
        assert os.environ['PYTHONHASHSEED']==parent_seed
    assert snapshots[0]==snapshots[1]
    control={**environment,'PYTHONHASHSEED':'2'}
    assert json.loads(subprocess.check_output([sys.executable,'-c',script],env=control,text=True))!=snapshots[0]


@pytest.mark.asyncio
async def test_source_first_validation_stops_unmatched_success_without_guard_credit(tmp_path):
    from evaluation.skill_learning import Experiment
    from agent.skills.learning import validation_gate,digest
    e=Experiment.__new__(Experiment);e.root=tmp_path;e.empty=tmp_path/'empty';e.model='test';e.config={}
    e.case_baselines={};e.validation_runs={};e.release_source=lambda:None
    e.args=SimpleNamespace(environment='androidworld',source='S',variant='V',near_miss='M',related_normal='N',source_first_validation=True)
    calls=[]
    def episode(name,task,skills):
        calls.append(task)
        return {'score':True,'budgeted_success':True,'logical_action_steps':1,'environment_status':'ready',
            'initial_state_hash':'baseline' if name.startswith('baseline') else 'different','configuration_hash':'same'}
    e.episode=episode;c=SimpleNamespace(new_text='candidate',target='apps/test/core/SKILL.md')
    from agent.skills.snapshot import LibrarySnapshot
    e.baseline_snapshot=LibrarySnapshot.freeze(e.empty,e.root/"baseline-library");e.baseline=e.baseline_snapshot.root
    result=await e.validate(c,'')
    assert calls==['S','S'] and result['validation_stage']=='unmatched_source'
    assert result['deferred_kinds']==['variant','near_miss','related_normal']
    assert result['trials'][0]['candidate_success'] is True and result['trials'][0]['matched_environment'] is False
    assert validation_gate(c,digest(''),result)==(False,'unmatched_or_unverified_trial')


def test_calendar_ir_fixture_signature_preserves_titles_times_and_repeat_rules(monkeypatch):
    import dataclasses,json,sys,types
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='capture_rows')
    @dataclasses.dataclass
    class Row:
        title:str
        start_ts:int
        end_ts:int
        repeat_rule:int
        id:int
    calendar=types.ModuleType('android_world.task_evals.single.calendar')
    calendar.calendar_utils=SimpleNamespace(EVENTS_TABLE='events',DB_PATH='/calendar/events.db')
    utils=types.ModuleType('android_world.task_evals.utils')
    calls=[]
    def read(table,path,row_type,env):
        assert (table,path,row_type)==('events','/calendar/events.db',Row);calls.append(True);return env
    utils.sqlite_schema_utils=SimpleNamespace(CalendarEvent=Row)
    utils.sqlite_utils=SimpleNamespace(get_rows_from_remote_device=read)
    monkeypatch.setitem(sys.modules,calendar.__name__,calendar);monkeypatch.setitem(sys.modules,utils.__name__,utils)
    saved=[];ns={'save':lambda path,value:saved.append(value),'json':json,'dataclasses':dataclasses}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=SimpleNamespace(app_names=['simple calendar pro'])
    ns['capture_rows'](task,[Row('B',3,4,1,10),Row('A',1,2,0,11)],Path('.'),'initial')
    ns['capture_rows'](task,[Row('A',1,2,0,99),Row('B',3,4,1,98)],Path('.'),'initial')
    assert saved[0]==saved[1] and saved[0][0]['title']=='A' and 'id' not in saved[0][0]
    for changed in [Row('A clipped',1,2,0,99),Row('A',1,3,0,99),Row('A',1,2,2,99)]:
        ns['capture_rows'](task,[changed,Row('B',3,4,1,98)],Path('.'),'initial');assert saved[-1]!=saved[0]
    assert len(calls)==5


def test_opentracks_ir_fixture_signature_preserves_types_dates_and_totals(monkeypatch):
    import dataclasses,json,sys,types
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='capture_rows')
    @dataclasses.dataclass
    class Row:
        name:str
        category:str
        starttime:int
        totaltime:int
        _id:int
        uuid:bytes=b"generated-identity"
    package=types.ModuleType('android_world.task_evals.information_retrieval')
    package.activity_app_utils=SimpleNamespace(list_rows=lambda env:env)
    monkeypatch.setitem(sys.modules,package.__name__,package)
    saved=[];ns={'save':lambda path,value:saved.append(value),'json':json,'dataclasses':dataclasses}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=SimpleNamespace(app_names=['open tracks sports tracker'])
    ns['capture_rows'](task,[Row('B','running',3,4,10),Row('A','walking',1,2,11)],Path('.'),'initial')
    ns['capture_rows'](task,[Row('A','walking',1,2,99,b'different-a'),Row('B','running',3,4,98,b'different-b')],Path('.'),'initial')
    assert saved[0]==saved[1] and '_id' not in saved[0][0] and 'uuid' not in saved[0][0]
    for changed in [Row('A','hiking',1,2,99),Row('A','walking',2,2,99),Row('A','walking',1,3,99)]:
        ns['capture_rows'](task,[changed,Row('B','running',3,4,98)],Path('.'),'initial');assert saved[-1]!=saved[0]

@pytest.mark.parametrize('queue,error', [(['Song A','Song B'],None),
    ([],sqlite3.OperationalError('no such table: playing_queue')),
    ([],sqlite3.OperationalError('database is locked'))])
def test_retro_queue_signature_retains_order_and_rejects_unknown_state(monkeypatch,queue,error):
    import sys,types
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
        if isinstance(n,ast.FunctionDef) and n.name=='capture_rows')
    module=types.ModuleType('android_world.task_evals.single.retro_music')
    module._get_playlist_data=lambda env:[]
    def get_queue(env):
        if error:raise error
        return list(queue)
    module._get_playing_queue=get_queue
    monkeypatch.setitem(sys.modules,module.__name__,module)
    saved=[]
    ns={'save':lambda path,data:saved.append(data),'sqlite3':sqlite3,
        'adb':lambda *args:b'Row: 3 title=Song A, duration=180000, _size=11, _display_name=a.mp3\n'}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=type('RetroPlayingQueue',(),{'app_names':['retro music']})()
    if error and 'no such table: playing_queue' not in str(error):
        with pytest.raises(sqlite3.OperationalError,match='locked'):ns['capture_rows'](task,None,Path('.'),'initial')
        assert not saved
    else:
        ns['capture_rows'](task,None,Path('.'),'initial')
        assert saved[-1]['playing_queue']==queue
        if queue:
            queue.reverse();ns['capture_rows'](task,None,Path('.'),'initial')
            assert saved[0]!=saved[1]

@pytest.mark.parametrize('task_name,expected',[('RetroSavePlaylist',True),('RetroCreatePlaylist',False)])
def test_export_fixture_normalizes_official_download_provider_only_for_export(monkeypatch,task_name,expected):
    import sys,types
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
        if isinstance(n,ast.FunctionDef) and n.name=='prepare_retro_export_storage')
    module=types.ModuleType('android_world.task_evals.utils.user_data_generation');calls=[];saved=[]
    module._clear_external_downloads=lambda env:calls.append(env)
    package=types.ModuleType('android_world.task_evals.utils');package.user_data_generation=module
    monkeypatch.setitem(sys.modules,package.__name__,package)
    monkeypatch.setitem(sys.modules,module.__name__,module)
    ns={'save':lambda path,data:saved.append(data)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=type(task_name,(),{})();env=object()
    ns['prepare_retro_export_storage'](task,env,Path('.'))
    assert bool(calls) is expected and bool(saved) is expected
    if expected:assert calls==[env]


def test_export_signature_includes_actual_download_bytes_and_provider_rows(monkeypatch,tmp_path):
    import sys,types,contextlib,hashlib
    path=Path(__file__).resolve().parents[1]/'evaluation/androidworld/run_emulator.py'
    node=next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
        if isinstance(n,ast.FunctionDef) and n.name=='capture_rows')
    music=types.ModuleType('android_world.task_evals.single.retro_music');music._get_playlist_data=lambda env:[]
    constants=types.ModuleType('android_world.env.device_constants');constants.DOWNLOAD_DATA='/sdcard/Download'
    files=types.ModuleType('android_world.utils.file_utils')
    @contextlib.contextmanager
    def copied(*args):yield tmp_path
    files.tmp_directory_from_device=copied
    env_package=types.ModuleType('android_world.env');env_package.device_constants=constants
    utils_package=types.ModuleType('android_world.utils');utils_package.file_utils=files
    for module in (music,constants,files,env_package,utils_package):monkeypatch.setitem(sys.modules,module.__name__,module)
    saved=[];provider=[]
    def adb(*args):
        if 'content://media/external/downloads' in args:return ('\n'.join(provider)).encode()
        return b'Row: 9 title=Song A, duration=180000, _size=11, _display_name=a.mp3\n'
    ns={'save':lambda path,data:saved.append(data),'adb':adb,'hashlib':hashlib,'Path':Path}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
    task=type('RetroSavePlaylist',(),{'app_names':['retro music']})();env=SimpleNamespace(controller=object())
    ns['capture_rows'](task,env,Path('.'),'initial')
    assert saved[-1]['download_files']==[] and saved[-1]['download_provider']==[]
    provider.append('Row: 3 _display_name=Ghost.m3u, _data=/sdcard/Download/Ghost.m3u, _size=3')
    ns['capture_rows'](task,env,Path('.'),'initial')
    assert saved[0]!=saved[1] and saved[1]['download_files']==[]
    (tmp_path/'Ghost.m3u').write_bytes(b'abc');ns['capture_rows'](task,env,Path('.'),'initial')
    assert saved[1]!=saved[2] and saved[2]['download_files'][0]['sha256']==hashlib.sha256(b'abc').hexdigest()


def _holdout_regenerator():
    import random
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == 'regenerate_selected_params')
    seeds = []
    namespace = {'random': random, 'np': SimpleNamespace(random=SimpleNamespace(seed=seeds.append))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['regenerate_selected_params'], seeds


def test_holdout_generates_selected_non_row_task_and_is_deterministic():
    import copy, random
    regenerate, seeds = _holdout_regenerator()
    class Generated:
        calls = 0
        def __init__(self, params): self.goal = 'Create ' + params['title']
        @classmethod
        def generate_random_params(cls):
            cls.calls += 1
            return {'title': str(random.getrandbits(64)), 'files': ['a.mp3', 'b.mp3']}
    original = [{'task':'Selected', 'seed':17, 'params':{'files':['old.mp3']}, 'goal':'old'},
                {'task':'Unselected', 'seed':4, 'params':{'row_objects':[]}, 'goal':'keep'}]
    first, second = copy.deepcopy(original), copy.deepcopy(original)
    regenerate(first, {'Selected':Generated}, 3, ['Selected'])
    regenerate(second, {'Selected':Generated}, 3, ['Selected'])
    assert first == second and Generated.calls == 2 and seeds == [20,20]
    assert first[0]['seed'] == first[0]['params']['seed'] == 20
    assert first[0]['goal'] == 'Create ' + first[0]['params']['title']
    assert first[1] == original[1]
    regenerate(first, {}, 0, None)
    assert Generated.calls == 2


def test_row_holdout_still_rejects_empty_generation_and_retains_final_seed():
    regenerate, seeds = _holdout_regenerator()
    class Generated:
        calls = 0
        def __init__(self, params): self.goal = 'rows'
        @classmethod
        def generate_random_params(cls):
            cls.calls += 1
            return {'row_objects': [] if cls.calls == 1 else [{'value':'new'}]}
    specs = [{'task':'Selected','seed':17,'params':{'row_objects':[{'value':'old'}]}}]
    regenerate(specs, {'Selected':Generated}, 3, ['Selected'])
    assert seeds == [20,1020] and specs[0]['seed'] == specs[0]['params']['seed'] == 1020
    Generated.generate_random_params = classmethod(lambda cls: {'row_objects':[]})
    with pytest.raises(RuntimeError, match='No nonempty holdout'):
        regenerate(specs, {'Selected':Generated}, 3, ['Selected'])
    assert len(seeds) == 102


def test_androidworld_launch_carries_holdout_seed(tmp_path):
    import sys
    from evaluation.skill_learning import AndroidWorldExperiment
    baseline=tmp_path/'prepared';(baseline/'latest-shallow').mkdir(parents=True)
    (baseline/'setup-complete.json').write_text('{}')
    e=AndroidWorldExperiment.__new__(AndroidWorldExperiment);e.model='test'
    e.args=SimpleNamespace(androidworld_baseline=str(baseline),eval_python=sys.executable,
        agent_python=None,env_file=str(tmp_path/'unused.env'),adb='adb',rounds=24,seconds=900,seed_offset=37)
    command,_=e._episode_launch(tmp_path/'arm','RetroCreatePlaylist',tmp_path/'skills')
    assert command[command.index('--seed-offset')+1] == '37'

@pytest.mark.parametrize('missing_map', [False, True])
def test_osmand_favorite_matches_file_state_and_search_preferences(tmp_path, monkeypatch, missing_map):
    import hashlib, sys
    from contextlib import contextmanager
    from types import ModuleType
    path = Path(__file__).resolve().parents[1] / 'evaluation/androidworld/run_emulator.py'
    node = next(n for n in ast.parse(path.read_text(encoding='utf-8')).body
                if isinstance(n, ast.FunctionDef) and n.name == 'capture_rows')
    map_path = '/data/media/0/Android/data/net.osmand/files/Liechtenstein_europe.obf'
    map_file = tmp_path / 'map.obf'; map_file.write_bytes(b'actual offline map')
    prefs = tmp_path / 'preferences'; prefs.mkdir()
    setting = prefs / 'settings.xml'; setting.write_text('<map-center value="A"/>')
    @contextmanager
    def copy_file(remote, controller):
        assert remote == map_path
        yield map_file
    @contextmanager
    def copy_directory(remote, controller):
        assert remote == '/data/data/net.osmand/shared_prefs'
        yield prefs
    utils = ModuleType('android_world.utils')
    utils.file_utils = SimpleNamespace(
        check_file_exists=lambda remote, controller: remote == map_path and not missing_map,
        tmp_file_from_device=copy_file, tmp_directory_from_device=copy_directory)
    single = ModuleType('android_world.task_evals.single')
    single.osmand = SimpleNamespace(_FAVORITES_PATH='/favorite.gpx', _LEGACY_FAVORITES_PATH='/legacy.gpx')
    monkeypatch.setitem(sys.modules, utils.__name__, utils)
    monkeypatch.setitem(sys.modules, single.__name__, single)
    saved = []
    namespace = {'save': lambda path, value: saved.append(value), 'Path': Path, 'hashlib': hashlib}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    task = type('OsmAndFavorite', (), {'app_names': ['osmand']})()
    env = SimpleNamespace(controller=None)
    if missing_map:
        with pytest.raises(RuntimeError, match='offline map missing'):
            namespace['capture_rows'](task, env, tmp_path, 'initial')
        assert not saved
    else:
        namespace['capture_rows'](task, env, tmp_path, 'initial')
        assert saved[0]['files'][:2] == [
            {'path': '/favorite.gpx', 'exists': False}, {'path': '/legacy.gpx', 'exists': False}]
        assert saved[0]['files'][2]['sha256'] == hashlib.sha256(map_file.read_bytes()).hexdigest()
        setting.write_text('<map-center value="B"/>')
        namespace['capture_rows'](task, env, tmp_path, 'initial')
        assert saved[0] != saved[1]
