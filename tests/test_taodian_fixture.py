import json
import sqlite3

from evaluation.mobileworld import taodian_fixture as fixture
from evaluation.mobileworld.long_run_health import MobileWorldTarget


CONFIG = {"requireLogin": False, "defaultUserId": "fixture-user", "mockOrders": []}
READY = {"app_config": CONFIG, "hadLogin": True, "autoLoginProcessed": True,
         "loginResult": {"userId": "fixture-user"}}


def test_requires_delivered_configuration_and_correct_automatic_session():
    assert fixture.configuration_applied(CONFIG, READY)
    for key in READY:
        assert not fixture.configuration_applied(CONFIG, {**READY, key: None})
    assert not fixture.configuration_applied(CONFIG, {**READY, "loginResult": {"userId": "other"}})
    manual = {**CONFIG, "requireLogin": True}
    assert fixture.configuration_applied(manual, {"app_config": manual})


def test_reads_typed_values_without_returning_other_storage(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE DC_123_storage (key TEXT, value TEXT)')
    db.executemany('INSERT INTO DC_123_storage VALUES (?,?)', [
        ('app_config', json.dumps(CONFIG)),
        ('hadLogin', '{"type":"boolean","data":true}'),
        ('Token', 'not-for-reports'),
    ])
    raw = db.serialize(); db.close()
    monkeypatch.setattr(fixture, 'adb', lambda *a, **k: raw)
    assert fixture._read_storage(MobileWorldTarget()) == {'app_config': CONFIG, 'hadLogin': True}


def _install(monkeypatch, states):
    from types import SimpleNamespace
    monkeypatch.setattr(fixture.requests, 'get', lambda *a, **k: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: CONFIG))
    sequence = iter(states)
    monkeypatch.setattr(fixture, '_read_storage', lambda *a: next(sequence))
    monkeypatch.setattr(fixture.time, 'sleep', lambda *a: None)
    calls = []
    monkeypatch.setattr(fixture, 'adb', lambda *a, **k: calls.append(a[1:]))
    return calls


def test_healthy_session_does_not_restart(monkeypatch):
    calls = _install(monkeypatch, [READY])
    assert fixture.ensure_taodian_configuration(MobileWorldTarget()) == {'ready': True, 'restarted': False}
    assert not calls


def test_relaunches_once_and_lets_app_apply_config(monkeypatch):
    calls = _install(monkeypatch, [{}, {}, READY])
    assert fixture.ensure_taodian_configuration(MobileWorldTarget()) == {'ready': True, 'restarted': True}
    assert calls == [('shell', 'am', 'force-stop', fixture.PACKAGE),
                     ('shell', 'monkey', '-p', fixture.PACKAGE, '1')]


def test_blocks_unrepaired_initialization_without_leaking_state(monkeypatch):
    calls = _install(monkeypatch, [{}]*5)
    result = fixture.ensure_taodian_configuration(MobileWorldTarget())
    assert not result['ready']
    assert result['failure'] == 'taodian_configuration_not_applied'
    assert len(calls) == 2
    assert 'fixture-user' not in json.dumps(result)
