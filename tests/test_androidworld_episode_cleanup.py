import json
import sys
from types import SimpleNamespace

import pytest

from evaluation.androidworld.episode_cleanup import (
    active_location_registration_lines, teardown_task)


@pytest.mark.parametrize('app', ['chrome', 'clock'])
@pytest.mark.parametrize('failure', [None, 'official', 'stop', 'observe'])
def test_cleanup_order_scope_and_failure_reporting(tmp_path, monkeypatch, app, failure):
    events = []
    package = 'com.google.android.deskclock' if app == 'clock' else 'com.android.chrome'

    def official(env):
        events.append('official')
        if failure == 'official':
            raise RuntimeError('teardown failed')

    def request(args, controller):
        assert args[:3] == ['shell', 'am', 'force-stop']
        events.append(args[-1])
        if failure == 'stop':
            raise RuntimeError('force-stop failed')
        return 'OK'

    def observe(controller, directory, evidence):
        events.append('observe')
        if failure == 'observe':
            raise RuntimeError('tree unavailable')
        return '<hierarchy/>'

    adb = SimpleNamespace(get_adb_activity=lambda app: package + '/Activity',
        extract_package_name=lambda activity: activity.split('/')[0], issue_generic_request=request,
        check_ok=lambda response: None, press_home_button=lambda env: events.append('home'))
    monkeypatch.setitem(sys.modules, 'android_world.env', SimpleNamespace(adb_utils=adb))
    monkeypatch.setitem(sys.modules, 'scoring', SimpleNamespace(
        read_fresh_forest=observe))
    task = SimpleNamespace(app_names=[app], tear_down=official)
    expected_failure = failure in ('official', 'stop') or (failure == 'observe' and app == 'clock')
    if expected_failure:
        with pytest.raises(RuntimeError, match='Episode cleanup failed'):
            teardown_task(task, SimpleNamespace(controller=None), tmp_path)
    else:
        teardown_task(task, SimpleNamespace(controller=None), tmp_path)
    assert events[:2] == ['official', package]
    report = json.loads((tmp_path/'episode-cleanup.json').read_text())
    assert bool(report['errors']) == expected_failure
    if app == 'clock':
        assert events[2] == 'com.google.android.as'
        if failure is None:
            assert events[3:] == ['home', 'observe']
            assert report['home_tree_verified']
    else:
        assert 'com.google.android.as' not in events
        assert 'home' not in events


def test_opentracks_cleanup_proves_process_and_gps_registration_are_gone(
        tmp_path, monkeypatch):
    events = []
    adb = SimpleNamespace(
        get_adb_activity=lambda app: 'de.dennisguse.opentracks/Activity',
        extract_package_name=lambda activity: activity.split('/')[0],
        issue_generic_request=lambda args, controller: events.append(args) or 'OK',
        check_ok=lambda response: None,
    )
    monkeypatch.setitem(sys.modules, 'android_world.env', SimpleNamespace(adb_utils=adb))
    monkeypatch.setitem(sys.modules, 'scoring', SimpleNamespace(read_fresh_forest=None))
    monkeypatch.setitem(sys.modules, 'run_emulator', SimpleNamespace(
        adb=lambda *args: b'' if args[1] == 'pidof' else b'Location Manager State:\n'))
    task = SimpleNamespace(app_names=['open tracks'], tear_down=lambda env: None)
    teardown_task(task, SimpleNamespace(controller=None), tmp_path)
    report = json.loads((tmp_path / 'episode-cleanup.json').read_text())
    assert report['opentracks']['verified']
    assert events == [['shell', 'am', 'force-stop', 'de.dennisguse.opentracks']]
    assert report['events'][0]['event'] == 'official_teardown_started'
    assert report['events'][2]['event'] == 'force_stop_started'


def test_location_parser_ignores_history_but_detects_active_registration():
    package = 'de.dennisguse.opentracks'
    dump = f'''Location Manager State:
  historical requests:
    {package} last location
  gps provider:
    registrations:
      10001/{package}/listener
    last location:
      {package}
'''
    assert active_location_registration_lines(dump, package) == [
        f'10001/{package}/listener']


def test_location_parser_supports_api33_listener_sections():
    package = 'de.dennisguse.opentracks'
    dump = f'''Location Providers:
  gps provider:
    service: ProviderRequest[@1s]
    listeners:
      10123/{package}/ABC Request[@1s HIGH_ACCURACY]
    last location=null
Historical Aggregate Location Provider Data:
  gps:
    {package}: total duration = +1m
'''
    assert active_location_registration_lines(dump, package) == [
        f'10123/{package}/ABC Request[@1s HIGH_ACCURACY]']
