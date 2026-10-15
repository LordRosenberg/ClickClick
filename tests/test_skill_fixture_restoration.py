"""Known-absence restoration stays confined and preserves exact state binding."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from evaluation.skill_fixture_restoration import absent_owned_files, restore_recorded_absence
from evaluation.skill_learning import AndroidWorldExperiment
from agent.skills.learning import digest


PATH = '/data/media/0/Android/data/com.example.app/files/fixture/item.gpx'


def test_only_recorded_absence_is_selected_and_deduplicated():
    assert absent_owned_files({'files':[{'path':PATH,'exists':False},
        {'path':PATH,'exists':False},{'path':PATH+'.other','exists':True}]}) == [PATH]
    assert restore_recorded_absence('unused',{'rows':[]}) == {
        'checked_files':0,'removed_files':0,'empty_scope_restored':False}


@pytest.mark.parametrize('path', ['/data/media/0/DCIM/item',
    '/data/media/0/Android/data/com.example.app/files/../../other',
    '/data/data/com.example.app/files/./item', PATH+'\nother', '/data/data/com.example.app/files/'])
def test_invalid_scope_fails_before_any_device_call(monkeypatch,path):
    calls=[];monkeypatch.setattr(subprocess,'check_output',lambda *a,**k:calls.append(a))
    with pytest.raises(ValueError):
        restore_recorded_absence('adb',{'files':[{'path':PATH,'exists':False},{'path':path,'exists':False}]})
    assert not calls


def test_absence_cannot_carry_an_existing_file_hash():
    with pytest.raises(ValueError):absent_owned_files({'files':[{'path':PATH,'exists':False,'sha256':'bytes'}]})


@pytest.mark.parametrize('identity,root', [(b'OtherAvd\nOK',b'0'),(b'AndroidWorldAvd\nOK',b'2000')])
def test_wrong_device_or_unrooted_device_never_deletes(monkeypatch,identity,root):
    calls=[]
    def adb(command,**kwargs):
        calls.append(command)
        return identity if command[3]=='emu' else root
    monkeypatch.setattr(subprocess,'check_output',adb)
    with pytest.raises(ValueError):restore_recorded_absence('adb',{'files':[{'path':PATH,'exists':False}]})
    assert all('rm -f' not in ' '.join(c) for c in calls)


def test_quoted_literal_unlink_receipt_and_failure_propagation(monkeypatch):
    replies=iter([b'AndroidWorldAvd\nOK',b'0',b'removed']);calls=[]
    def adb(command,**kwargs):calls.append(command);return next(replies)
    monkeypatch.setattr(subprocess,'check_output',adb)
    literal=PATH+" quote's item"
    assert restore_recorded_absence('adb',{'files':[{'path':literal,'exists':False}]})['removed_files']==1
    shell=calls[-1][-1]
    assert 'rm -rf' not in shell and 'exit 65' in shell and 'exit 66' in shell
    assert "'\"'\"'" in shell  # Apostrophe is a literal, never a new shell command.
    def failed(*a,**k):raise subprocess.CalledProcessError(65,a[0])
    monkeypatch.setattr(subprocess,'check_output',failed)
    with pytest.raises(subprocess.CalledProcessError):restore_recorded_absence('adb',{'files':[{'path':PATH,'exists':False}]})


def test_initially_empty_scope_never_recursively_deletes(monkeypatch):
    replies=iter([b'AndroidWorldAvd\nOK',b'0',b'empty']);calls=[]
    def adb(command,**kwargs):calls.append(command);return next(replies)
    monkeypatch.setattr(subprocess,'check_output',adb)
    result=restore_recorded_absence('adb',{'schema':'official-task-owned-files-v1',
        'scope':PATH.rsplit('/',1)[0],'files':[],'directories':[]})
    assert result['empty_scope_restored'] and '-maxdepth 1 -type f -exec rm -f' in calls[-1][-1]
    assert '! -type f' in calls[-1][-1] and 'rm -rf' not in calls[-1][-1]


def experiment(tmp_path):
    obj=AndroidWorldExperiment.__new__(AndroidWorldExperiment);obj.root=tmp_path
    obj.args=SimpleNamespace(disposable_research=True,target='emulator-5554',expected_serial='AndroidWorldAvd',adb='unused')
    return obj


def test_restoration_requires_the_bound_snapshot_and_closed_lease(tmp_path,monkeypatch):
    from evaluation import skill_fixture_restoration as restoration
    calls=[];monkeypatch.setattr(restoration,'restore_recorded_absence',lambda *a:calls.append(a) or {'checked_files':0})
    obj=experiment(tmp_path);p=tmp_path/'initial-rows.json';state={'files':[]};p.write_text(json.dumps(state))
    base={'initial_state_file':str(p),'initial_state_hash':'wrong','runtime_directory':str(tmp_path/'runtime')}
    with pytest.raises(ValueError,match='bound hash'):obj.restore_bound_absence(base)
    assert not calls
    base['initial_state_hash']=digest(state);obj._source_process=object()
    with pytest.raises(ValueError,match='lease'):obj.restore_bound_absence(base)
    assert not calls
    obj._source_process=None;assert obj.restore_bound_absence(base)=={'checked_files':0}
    assert len(calls)==1 and len(list(tmp_path.glob('fixture-file-restoration-*.json')))==1


def test_legacy_snapshots_and_user_defaults_do_not_gain_mutation_permission(tmp_path):
    obj=experiment(tmp_path);base={'runtime_directory':str(tmp_path/'runtime'),'initial_state_hash':'unknown'}
    assert obj.restore_bound_absence(base) is None
    obj.args.disposable_research=False
    assert obj.restore_bound_absence({}) is None
    obj.args.disposable_research=True;obj.args.target='another-device'
    with pytest.raises(ValueError,match='dedicated'):obj.restore_bound_absence(base)


def test_bound_trial_restores_before_child_launch_and_rejects_drift_first(tmp_path):
    obj=experiment(tmp_path);order=[];base={'bound':'state'};obj.case_baselines={'example':base}
    def restore(value):assert value is base;order.append('restore')
    def launch(*args):order.append('launch');raise RuntimeError('launch boundary')
    obj.restore_bound_absence=restore;obj._episode_launch=launch
    with pytest.raises(RuntimeError,match='launch boundary'):obj.episode('candidate','example',tmp_path)
    assert order==['restore','launch']
    def drift(value):raise ValueError('bound drift')
    obj.restore_bound_absence=drift;order.clear()
    with pytest.raises(ValueError,match='bound drift'):obj.episode('candidate','example',tmp_path)
    assert not order


@pytest.mark.parametrize('empty_snapshot',[False,True])
def test_export_scored_episode_without_snapshot_or_model_dispatch(tmp_path,empty_snapshot):
    from agent.skills.snapshot import library_manifest
    obj=experiment(tmp_path);obj.rows=[];lib=tmp_path/'empty-library';lib.mkdir()
    folder=tmp_path/'paid';episode=folder/'episodes/example/plan_executor';episode.mkdir(parents=True)
    raw={'task_id':'paid-task','status':'failed','episode_steps':2,'role_invocations':3,
        'elapsed_s':4,'score':0,'budgeted_success':False,'valid':True}
    manifest={'model':'fake','provider':'fake','runtime_hashes':{},'skill_hashes':{},
        'settings':{'architecture':'plan_executor','executor_context_tokens':1000},'library_manifest':library_manifest(lib)}
    for name,data in [('result.json',raw),('request.json',{'goal':'synthetic'}),('runtime-manifest.json',manifest)]:
        (episode/name).write_text(json.dumps(data))
    if empty_snapshot:(episode/'initial-rows.json').write_text('{}')
    result=obj.read_episode_result('paid','example',lib,folder)
    assert result['score']==0 and result['role_invocations']==3
    assert result['initial_state_hash'] is None
    assert result['initial_state_file']==(str(episode/'initial-rows.json') if empty_snapshot else None)
    assert result['initial_state_coverage']=='unavailable' and result['environment_status']=='ready'
    with pytest.raises(ValueError,match='verification unavailable'):obj.disposable_adapter(result)
