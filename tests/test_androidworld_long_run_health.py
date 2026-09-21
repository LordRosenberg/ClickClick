"""No-device contracts for AndroidWorld long-run recovery."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.androidworld import long_run_health as health


def _ready(name, calls):
    def probe(adb, env, root, ordinal):
        calls.append(("probe", name, ordinal))
        return {"ready": True, "latency_s": 0.01}
    return probe


def test_only_degraded_channel_is_recovered(tmp_path):
    calls = []

    def collector(adb, env, root, ordinal):
        calls.append(("probe", "collector", ordinal))
        return {"ready": ordinal == 2, "latency_s": 0.01}

    probes = {
        "collector": collector,
        "pixels": _ready("pixels", calls),
        "native_forest": _ready("native_forest", calls),
    }
    recoveries = {
        name: (lambda adb, env, name=name: calls.append(("recover", name)) or {})
        for name in probes
    }
    report = health.ensure_channels(None, tmp_path, None, probes=probes,
                                    recoveries=recoveries)
    assert report["ready"]
    assert [(row["channel"], row["succeeded"]) for row in report["recoveries"]] == [
        ("collector", True)]
    assert ("recover", "pixels") not in calls
    assert ("recover", "native_forest") not in calls
    assert ("probe", "native_forest", 2) in calls


def test_failed_targeted_recovery_requires_full_restart(tmp_path):
    probes = {
        name: (lambda adb, env, root, ordinal, name=name:
               {"ready": name != "pixels", "latency_s": 0.01})
        for name in ("collector", "pixels", "native_forest")
    }
    recoveries = {name: (lambda adb, env: {}) for name in probes}
    with pytest.raises(health.FullRestartRequired) as captured:
        health.ensure_channels(None, tmp_path, None, probes=probes,
                               recoveries=recoveries)
    assert captured.value.report["remaining_degraded"] == ["pixels"]
    with pytest.raises(RuntimeError, match="did not restore"):
        health.ensure_channels(None, tmp_path, None, allow_full_restart=False,
                               probes=probes, recoveries=recoveries)


def test_latency_threshold_is_a_fail_closed_health_contract(tmp_path):
    probes = {
        name: (lambda adb, env, root, ordinal: {"ready": True, "latency_s": 2.0})
        for name in ("collector", "pixels", "native_forest")
    }
    recoveries = {name: (lambda adb, env: {}) for name in probes}
    with pytest.raises(health.FullRestartRequired) as captured:
        health.ensure_channels(None, tmp_path, None, probes=probes,
                               recoveries=recoveries,
                               thresholds={name: 1.0 for name in probes})
    assert set(captured.value.report["remaining_degraded"]) == set(probes)


def test_interrupted_boundary_stops_only_recorded_packages_once(tmp_path):
    calls = []
    health.write_episode_boundary(tmp_path, "Case", ["pkg.one", "pkg.two"])
    recovered = health.recover_interrupted_boundary(
        tmp_path, lambda *args: calls.append(args) or b"")
    assert recovered["recovered_after_interruption"]
    assert calls == [
        ("shell", "am", "force-stop", "pkg.one"),
        ("shell", "am", "force-stop", "pkg.two"),
    ]
    assert health.recover_interrupted_boundary(tmp_path, None) is None
    marker = json.loads((tmp_path / "episode-boundary.json").read_text())
    assert marker["cleanup_complete"]


def test_normal_boundary_mark_requires_matching_case(tmp_path):
    health.write_episode_boundary(tmp_path, "CaseA", ["pkg"])
    with pytest.raises(RuntimeError, match="mismatch"):
        health.mark_episode_boundary_clean(tmp_path, "CaseB")
    health.mark_episode_boundary_clean(tmp_path, "CaseA")
    assert json.loads((tmp_path / "episode-boundary.json").read_text())[
        "cleanup_complete"]


def test_runner_contract_requires_every_field_and_frozen_artifact(tmp_path):
    rows = [{key: None for key in health.REQUIRED_SPEC_FIELDS} for _ in range(116)]
    (tmp_path / "matrix.json").write_text(json.dumps(rows))
    for path in ("runner/oracle/oracle.jar", "frozen-params.pkl",
                 "observation-contract.json", "setup-complete.json"):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    assert health.validate_runner_contract(tmp_path)["case_count"] == 116
    del rows[0]["max_seconds"]
    (tmp_path / "matrix.json").write_text(json.dumps(rows))
    with pytest.raises(RuntimeError, match="incomplete"):
        health.validate_runner_contract(tmp_path)


def test_runner_source_preserves_order_and_forbids_data_clear():
    root = Path(__file__).resolve().parents[1]
    runner_source = (root / "evaluation/androidworld/run_full.py").read_text()
    cleanup_source = (root / "evaluation/androidworld/episode_cleanup.py").read_text()
    health_source = (root / "evaluation/androidworld/long_run_health.py").read_text()
    tree = ast.parse(runner_source)
    run_one = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "run_one")
    rendered = ast.unparse(run_one)
    assert rendered.index("score_task(task, env, out, 'final')") < rendered.index(
        "teardown_task(task, env, out)")
    assert rendered.index("teardown_task(task, env, out)") < rendered.index(
        "mark_episode_boundary_clean(ROOT, spec['task'])")
    assert "write_episode_boundary(ROOT, spec['task'], packages)" in rendered
    assert rendered.index("write_episode_boundary") < rendered.index("task.initialize_task(env)")
    forbidden = "pm" + " clear"
    assert forbidden not in cleanup_source.lower()
    assert forbidden not in health_source.lower()


def test_setup_window_uses_ordered_budget_and_multi_app_dependencies():
    rows = [{'task': f'Case{i}', 'max_seconds': 900, 'apps': apps}
            for i, apps in enumerate([['markor', 'vlc'], ['tasks'], ['tasks'], ['broccoli app'], ['osmand']])]
    plan = health.upcoming_setup_window(rows)
    assert plan['cases'] == ['Case0', 'Case1', 'Case2', 'Case3']
    assert set(plan['apps']) == {'android world', 'markor', 'vlc', 'tasks', 'broccoli app'}
    assert plan['task_budget_seconds'] == 3600
    assert health.upcoming_setup_window(rows[4:])['apps'] == ['android world', 'osmand']
    for bad in ([], [{'task': 'Bad', 'apps': [], 'max_seconds': 900}]):
        with pytest.raises(ValueError):
            health.upcoming_setup_window(bad)


def test_scoped_initialization_rejects_missing_secondary_app(tmp_path, monkeypatch):
    (tmp_path/'environment-repair-latest.json').write_text(json.dumps({
        'status': 'ready', 'result': {'ready': True, 'api_level': 33, 'pointer_location': 0,
        'apps': [{'app': 'android world', 'ready': True}, {'app': 'markor', 'ready': True}],
        'collector': {'ready': True}, 'pixels': {'width': 1080},
        'scoring_forest_windows': 1, 'runner_contract': {'case_count': 116}}}))
    monkeypatch.setattr(health.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match='complete contract'):
        health.initialize_app_scope(tmp_path, tmp_path/'adb', apps=['markor', 'vlc'])


def test_new_batches_freeze_health_module_and_current_setup_script():
    root = Path(__file__).resolve().parents[1]
    freeze = (root / "evaluation/androidworld/freeze_runner.py").read_text()
    prepare = (root / "evaluation/androidworld/prepare_full.py").read_text()
    assert '"long_run_health.py"' in freeze
    assert 'with_name("setup_full.py")' in prepare


@pytest.mark.parametrize('apps', [None, ['markor', 'vlc']])
def test_full_restart_preserves_avd_and_runs_setup_contract(tmp_path, monkeypatch, apps):
    adb_path = tmp_path / "sdk/platform-tools/adb.exe"
    emulator = tmp_path / "sdk/emulator/emulator.exe"
    adb_path.parent.mkdir(parents=True)
    emulator.parent.mkdir(parents=True)
    adb_path.write_bytes(b"")
    emulator.write_bytes(b"")
    (tmp_path / "setup_full.py").write_text("# setup")
    (tmp_path / "environment-repair-latest.json").write_text(json.dumps({
        "status": "ready", "evidence": "proof", "result": {
            "ready": True, "api_level": 33, "pointer_location": 0,
            "apps": ([{}] * 23 if apps is None else
                     [{'app': name, 'ready': True} for name in ['android world', *apps]]),
            "collector": {"ready": True},
            "pixels": {"width": 1080}, "scoring_forest_windows": 1,
            "runner_contract": {"case_count": 116},
        }}))
    calls, launch_environments = [], []

    monkeypatch.setattr(health.subprocess, "check_output", lambda *a, **k: "AndroidWorldAvd\nOK\n")
    def popen(args, **kwargs):
        calls.append(("Popen", args))
        launch_environments.append(kwargs['env'])
        return SimpleNamespace()
    monkeypatch.setattr(health.subprocess, "Popen", popen)

    def run(args, **kwargs):
        calls.append(("run", args))
        if args[-2:] == ["getprop", "sys.boot_completed"]:
            return SimpleNamespace(returncode=0, stdout="1\n")
        if args[-1:] == ["devices"]:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(health.subprocess, "run", run)
    report = health.restart_same_avd_and_initialize(
        tmp_path, adb_path, python=tmp_path / "python.exe", timeout_s=.1, apps=apps)
    launch = next(args for kind, args in calls if kind == "Popen")
    assert launch[launch.index("-avd") + 1] == "AndroidWorldAvd"
    assert launch[launch.index('-camera-back') + 1] == 'emulated'
    assert '-camera-front' not in launch
    assert report['camera_backend']['rear'] == 'emulated'
    assert "-wipe-data" not in launch and "-grpc" in launch
    setup = [args for kind, args in calls if kind == "run" and "setup_full.py" in " ".join(map(str, args))]
    assert len(setup) == 1 and "--root" in setup[0]
    assert setup[0].count('--app') == (len(apps) if apps else 0)
    assert launch[launch.index('-crash-report-mode') + 1] == 'never'
    first_crash_dir = Path(launch_environments[0]['ANDROID_EMU_CRASH_REPORTING_DATABASE'])
    assert first_crash_dir == Path(report['crash_report_directory']).resolve()
    assert first_crash_dir.is_dir()
    (first_crash_dir / 'preserved.dmp').write_bytes(b'prior crash evidence')
    health.restart_same_avd_and_initialize(tmp_path, adb_path, python=tmp_path / 'python.exe', timeout_s=.1, apps=apps)
    assert Path(launch_environments[1]['ANDROID_EMU_CRASH_REPORTING_DATABASE']) != first_crash_dir
    assert (first_crash_dir / 'preserved.dmp').read_bytes() == b'prior crash evidence'


def test_full_restart_contract_rejects_partial_setup_result(tmp_path, monkeypatch):
    adb_path = tmp_path / "sdk/platform-tools/adb.exe"
    emulator = tmp_path / "sdk/emulator/emulator.exe"
    adb_path.parent.mkdir(parents=True)
    emulator.parent.mkdir(parents=True)
    adb_path.write_bytes(b"")
    emulator.write_bytes(b"")
    (tmp_path / "setup_full.py").write_text("# setup")
    (tmp_path / "environment-repair-latest.json").write_text(json.dumps({
        "status": "ready", "result": {"ready": True, "api_level": 33}}))
    monkeypatch.setattr(health.subprocess, "check_output", lambda *a, **k: "AndroidWorldAvd\n")
    monkeypatch.setattr(health.subprocess, "Popen", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(health.subprocess, "run", lambda args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=(
                            "1\n" if args[-2:] == ["getprop", "sys.boot_completed"] else "")))
    with pytest.raises(RuntimeError, match="complete contract"):
        health.restart_same_avd_and_initialize(tmp_path, adb_path,
                                               python=tmp_path / "python.exe",
                                               timeout_s=.1)


@pytest.mark.parametrize(('preventive_due', 'post_due', 'missing_before'),
                         [(False, False, False), (True, False, False),
                          (True, True, False), (False, False, True)])
def test_maintenance_pause_finishes_case_and_resumes_without_replaying(tmp_path, monkeypatch, preventive_due, post_due, missing_before):
    """Execute the real loop without Android imports; fake only its I/O edges."""
    import pickle
    import traceback
    import sys
    import time
    source = Path(__file__).resolve().parents[1] / "evaluation/androidworld/run_full.py"
    main = next(node for node in ast.parse(source.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    classes = {f"Case{i}": object() for i in range(116)}
    (tmp_path / "frozen-params.pkl").write_bytes(pickle.dumps([
        {"task": name, "apps": ["tasks"], "max_seconds": 900} for name in classes]))
    for name, value in {"source-hashes.json": {}, "setup-complete.json": {},
                        "model-probe.json": {"available": True}}.items():
        (tmp_path / name).write_text(json.dumps(value))
    events, states = [], []
    missing_apps = ['tasks'] if missing_before else []
    maintenance_checks = iter([preventive_due, post_due, preventive_due, post_due])
    monkeypatch.setitem(sys.modules, 'run_emulator', SimpleNamespace(ADB='test-adb'))

    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_one(spec, cls, env):
        events.append(("start", spec["task"]))
        (tmp_path / "PAUSE_AFTER_EPISODE").write_text("maintenance")
        events.extend([("score", spec["task"]), ("teardown", spec["task"])])
        result = {"valid": True, "budgeted_success": True, "teardown_ok": True}
        save(tmp_path / "episodes" / spec["task"] / "plan_executor/result.json", result)
        return result

    def scoped_setup(*args, **kwargs):
        assert kwargs['apps'] == ['android world', 'tasks']
        events.append(('scoped_setup', None))
        missing_apps.clear()
        return {'ready': True}

    namespace = dict(
        ROOT=tmp_path, json=json, pickle=pickle, traceback=traceback, POLICY="test",
        time=time, Path=Path, sys=sys,
        PREVENTIVE_RESTART_POLICY=health.PREVENTIVE_RESTART_POLICY,
        SETUP_WINDOW_POLICY=health.SETUP_WINDOW_POLICY,
        upcoming_setup_window=health.upcoming_setup_window,
        missing_task_baselines=lambda names, env: list(missing_apps),
        initialize_app_scope=scoped_setup,
        preventive_restart_check=lambda adb, spec: {'restart_due': next(maintenance_checks)},
        restart_same_avd_and_initialize=lambda *args, **kw:
            events.append(("restart", None)) or {'ready': True},
        lock_file=lambda handle: None, agent_python=lambda: "python",
        msvcrt=SimpleNamespace(LK_NBLCK=1, locking=lambda *args: None),
        validate_runner_contract=lambda root: None,
        adb=lambda *args: b"device" if args == ("get-state",) else b"33",
        capture=lambda adb: {}, restore=lambda adb, saved:
            events.append(("restore", None)) or {"match": True},
        load_env=lambda method: SimpleNamespace(close=lambda:
            events.append(("close", None))),
        android_world_controller=SimpleNamespace(A11yMethod=SimpleNamespace(UIAUTOMATOR=1)),
        registry=SimpleNamespace(TaskRegistry=lambda: SimpleNamespace(
            get_registry=lambda name: classes)),
        recover_interrupted_boundary=lambda *args: None,
        ensure_channels=lambda *args, **kw: events.append(("health", None)) or {},
        save=save, state=lambda status, **kw: states.append((status, kw)),
        run_one=run_one, encode=str,
    )
    exec(compile(ast.Module(body=[main], type_ignores=[]), str(source), "exec"), namespace)
    if post_due:
        with pytest.raises(RuntimeError, match='Insufficient uptime'):
            namespace['main']()
        assert events == [("close", None), ("restart", None), ("health", None),
                          ("close", None), ("restore", None)]
        assert states[-1][0] == 'error'
        assert not (tmp_path / 'episodes').exists()
        return
    namespace["main"]()
    prefix = [("close", None), ("restart", None)] if preventive_due else []
    if missing_before:
        prefix += [("health", None), ("close", None), ("scoped_setup", None)]
    assert events == prefix + [("health", None), ("start", "Case0"), ("score", "Case0"),
                      ("teardown", "Case0"), ("close", None), ("restore", None)]
    assert states[-1][0] == "paused_environment_review"
    assert states[-1][1]["restoration_ok"]
    (tmp_path / "PAUSE_AFTER_EPISODE").unlink()
    events.clear()
    namespace["main"]()
    assert [value for kind, value in events if kind == "start"] == ["Case1"]
    assert states[-1][1]["completed"] == 2


@pytest.mark.parametrize(('uptime', 'seconds', 'due'), [
    ('2399.9 100', 900, False), ('2400 100', 900, True),
    ('1800 100', 1800, True), ('120 0', 1800, False),
])
def test_preventive_restart_reserves_task_budget(uptime, seconds, due):
    calls = []
    result = health.preventive_restart_check(
        lambda *args: calls.append(args) or uptime.encode(),
        {'task': 'Case', 'max_seconds': seconds})
    assert result['restart_due'] is due
    assert calls == [('shell', 'cat', '/proc/uptime')]


@pytest.mark.parametrize('uptime', ['', 'unavailable', 'nan 0', '-1 0', 'inf 0'])
def test_preventive_restart_rejects_unknown_age(uptime):
    with pytest.raises(RuntimeError, match='uptime'):
        health.preventive_restart_check(lambda *args: uptime, {'task': 'Case', 'max_seconds': 900})


def test_preventive_restart_rejects_unfittable_task_without_reboot_or_probe():
    def forbidden(*args):
        pytest.fail('invalid budget must not trigger device operations')
    with pytest.raises(RuntimeError, match='budget'):
        health.preventive_restart_check(forbidden, {'task': 'Case', 'max_seconds': 3300})
