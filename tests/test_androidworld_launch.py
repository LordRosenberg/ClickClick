import json
from types import SimpleNamespace

import pytest

from evaluation.androidworld import launch_full as launch


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch, "PROJECT", tmp_path)
    monkeypatch.setattr(launch, "agent_python", lambda: "agent-python")
    monkeypatch.setattr(launch, "adb_executable", lambda: "adb")
    (tmp_path / "source-hashes.json").write_text("{}")
    (tmp_path / "matrix.json").write_text(json.dumps([
        {"task": "First", "apps": ["first-app"], "max_seconds": 900},
        {"task": "Later", "apps": ["later-app"], "max_seconds": 900},
    ]))
    (tmp_path / "model-probe.json").write_text('{"available":true}')
    events = []
    monkeypatch.setattr(launch, "upcoming_setup_window", lambda rows: {"apps": ["first-app"]})
    monkeypatch.setattr(launch, "initialize_app_scope", lambda *a, **k: events.append(("setup", k["apps"])))
    monkeypatch.setattr(launch, "restart_same_avd_and_initialize", lambda *a, **k: events.append(("restart", k["apps"])))
    def run(command, **kwargs):
        events.append(("probe" if "probe_model.py" in command[-1] else "run", command))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(launch.subprocess, "run", run)
    return tmp_path, events


@pytest.mark.parametrize("checks,expected", [
    ([False, False, False], ["probe", "setup", "run"]),
    ([True, False, False], ["probe", "restart", "run"]),
    ([False, True, False], ["probe", "setup", "restart", "run"]),
])
def test_v6_startup_scope_and_bounded_restart(harness, monkeypatch, checks, expected):
    root, events = harness
    decisions = iter(checks)
    monkeypatch.setattr(launch, "preventive_restart_check", lambda *a: {"restart_due": next(decisions)})
    assert launch.main() == 0
    assert [event[0] for event in events] == expected
    assert all(value == ["first-app"] for kind, value in events if kind in {"setup", "restart"})


def test_persistent_uptime_failure_never_starts_scored_runner(harness, monkeypatch):
    root, events = harness
    monkeypatch.setattr(launch, "preventive_restart_check", lambda *a: {"restart_due": True})
    with pytest.raises(RuntimeError, match="uptime"):
        launch.main()
    assert [event[0] for event in events] == ["probe", "restart", "restart"]


def test_failed_probe_never_initializes_apps(harness, monkeypatch):
    root, events = harness
    (root / "model-probe.json").write_text('{"available":false}')
    with pytest.raises(RuntimeError, match="Model probe"):
        launch.main()
    assert [event[0] for event in events] == ["probe"]


def test_resume_leaves_episode_boundary_recovery_to_full_runner(harness, monkeypatch):
    root, events = harness
    (root / "run-state.json").write_text('{"status":"paused_model_quota"}')
    assert launch.main(resume_after_quota=True) == 0
    assert [event[0] for event in events] == ["probe", "run"]
    assert events[-1][1][-1] == "--resume-after-quota"


def test_stop_marker_blocks_all_startup_side_effects(harness):
    root, events = harness
    (root / "STOP").touch()
    with pytest.raises(RuntimeError, match="marker"):
        launch.main()
    assert events == []
