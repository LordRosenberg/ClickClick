import os
from types import SimpleNamespace
import sys

import pytest

from evaluation.skill_fixture_fingerprint import directory_fingerprint,capture_owned_file_fixture,recording_settings_fingerprint


def test_fixture_fingerprint_preserves_nested_identity_bytes_and_empty_folders(tmp_path):
    (tmp_path/'empty').mkdir();(tmp_path/'notes').mkdir()
    (tmp_path/'notes'/'中文.md').write_bytes(b'first\x00second')
    original=directory_fingerprint(tmp_path)
    assert original['directories']==['empty','notes']
    assert original['files'][0]['name']=='notes/中文.md'
    assert original['files'][0]['bytes']==12
    os.utime(tmp_path/'notes'/'中文.md',(1,1))
    assert directory_fingerprint(tmp_path)==original
    (tmp_path/'notes'/'中文.md').write_bytes(b'first\x00changed')
    assert directory_fingerprint(tmp_path)!=original
    (tmp_path/'empty').rmdir()
    assert directory_fingerprint(tmp_path)['directories']==['notes']


def test_empty_owned_fixture_is_measured_not_an_absent_snapshot(tmp_path):
    assert directory_fingerprint(tmp_path)=={'directories':[],'files':[]}
    with pytest.raises(FileNotFoundError):directory_fingerprint(tmp_path/'missing')
    file=tmp_path/'file';file.write_text('data')
    with pytest.raises(ValueError,match='not a directory'):directory_fingerprint(file)


def test_composite_or_unknown_apps_do_not_gain_an_incomplete_binding(monkeypatch):
    # Unsupported scopes return before importing device helpers or touching data.
    monkeypatch.setitem(sys.modules,'android_world',None)
    for apps in (['markor','clipper'],['markor','simple sms messenger'],['files'],['audio recorder','markor']):
        assert capture_owned_file_fixture(SimpleNamespace(app_names=apps),SimpleNamespace()) is None


def test_fixture_snapshot_rejects_link_even_when_it_points_within_owned_root(tmp_path,monkeypatch):
    file=tmp_path/'file';file.write_text('data')
    real=type(file).is_symlink
    monkeypatch.setattr(type(file),'is_symlink',lambda p:p.name=='file' or real(p))
    with pytest.raises(ValueError,match='cannot follow links'):directory_fingerprint(tmp_path)


def test_recording_format_changes_cannot_hide_behind_an_empty_media_directory(tmp_path):
    path=tmp_path/'preferences.xml'
    path.write_text('<map><string name="record_format">aac</string><int name="last_record_id" value="1"/></map>')
    before=recording_settings_fingerprint(tmp_path)
    assert [row['key'] for row in before]==['record_format']
    assert 'aac' not in str(before)
    path.write_text('<map>\n  <string name="record_format">aac</string>\n  <int value="1" name="last_record_id"/>\n</map>')
    assert recording_settings_fingerprint(tmp_path)==before
    path.write_text('<map><string name="record_format">aac</string><int name="last_record_id" value="2"/></map>')
    assert recording_settings_fingerprint(tmp_path)==before
    path.write_text('<map><string name="record_format">wav</string><int name="last_record_id" value="2"/></map>')
    assert recording_settings_fingerprint(tmp_path)!=before
