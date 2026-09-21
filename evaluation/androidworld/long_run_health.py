"""Fail-closed, per-episode health gates for long AndroidWorld runs.

The three observation channels intentionally remain independent:

* ClickClick Accessibility Collector (agent tree)
* raw ADB screencap (pixel fallback)
* the frozen native UiAutomation forest (official scoring)

Targeted recovery is attempted only for a failing channel.  A caller must
close AndroidEnv before acting on :class:`FullRestartRequired`.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import time


COLLECTOR_COMPONENT = 'ai.clickclick.collector/.CollectorService'
EXPECTED_AVD_NAME = 'AndroidWorldAvd'
# The API 33 rear virtualscene pipeline can accept a shutter action without
# producing media. Use the SDK's synthetic rear camera, as already used by
# this AVD's front camera; keep the AVD data and front/back identities intact.
CAMERA_BACKEND_POLICY = {'rear': 'emulated', 'version': 'synthetic-rear-v1'}
REQUIRED_SPEC_FIELDS = frozenset({
    'task', 'params', 'seed', 'max_steps', 'max_model_calls', 'max_seconds', 'apps'})
DEFAULT_THRESHOLDS_S = {'collector': 10.0, 'pixels': 5.0, 'native_forest': 15.0}
PREVENTIVE_RESTART_POLICY = {
    'version': 'guest-uptime-boundary-v1',
    'max_uptime_s': 3600,
    'preparation_cleanup_reserve_s': 300,
}
SETUP_WINDOW_POLICY = {'version': 'task-dependency-window-v1', 'budget_seconds': 3600}


def upcoming_setup_window(records: list[dict]) -> dict:
    """Prefetch a bounded budget horizon; per-task checks cover faster execution."""
    cases, apps, seconds = [], {'android world'}, 0.0
    for spec in records:
        duration = float(spec['max_seconds'])
        names = spec['apps']
        if (not math.isfinite(duration) or duration <= 0 or not isinstance(names, (list, tuple)) or not names
                or any(not isinstance(name, str) or not name for name in names)):
            raise ValueError(f"Invalid task setup dependencies/budget: {spec.get('task')}")
        cases.append(spec['task'])
        apps.update(names)
        seconds += duration
        if seconds >= SETUP_WINDOW_POLICY['budget_seconds']:
            break
    if not cases:
        raise ValueError('Cannot prepare an empty task window')
    return {'policy': dict(SETUP_WINDOW_POLICY), 'cases': cases,
            'apps': sorted(apps), 'task_budget_seconds': seconds}


class FullRestartRequired(RuntimeError):
    def __init__(self, report):
        super().__init__('Observation channels remain degraded after targeted recovery')
        self.report = report


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode(errors='replace').strip()
    return str(value).strip()


def preventive_restart_check(adb, spec: dict) -> dict:
    """Read guest uptime only at a clean boundary; never dispatch task actions.

    Guest uptime survives runner restarts and ignores AndroidWorld's wall-clock
    changes. The reserve is an estimate, not a hard guarantee for slow setup.
    """
    policy = PREVENTIVE_RESTART_POLICY
    task_seconds = float(spec['max_seconds'])
    required = task_seconds + policy['preparation_cleanup_reserve_s']
    if not math.isfinite(task_seconds) or task_seconds <= 0 or required >= policy['max_uptime_s']:
        raise RuntimeError('Task budget cannot fit preventive maintenance policy; inspect before starting')
    raw = _text(adb('shell', 'cat', '/proc/uptime'))
    try:
        uptime = float(raw.split()[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f'Cannot establish emulator uptime: {raw!r}') from exc
    if not math.isfinite(uptime) or uptime < 0:
        raise RuntimeError(f'Invalid emulator uptime: {raw!r}')
    return {'case': spec['task'], 'checked_at': time.time(), 'policy': dict(policy),
            'uptime_s': uptime, 'task_max_seconds': task_seconds,
            'projected_uptime_s': uptime + required,
            'restart_due': uptime + required >= policy['max_uptime_s']}


def validate_runner_contract(root: Path) -> dict:
    matrix = json.loads((root / 'matrix.json').read_text(encoding='utf-8'))
    if len(matrix) != 116:
        raise RuntimeError(f'Expected 116 frozen cases, got {len(matrix)}')
    bad = [
        {'ordinal': ordinal, 'missing': sorted(REQUIRED_SPEC_FIELDS - set(row))}
        for ordinal, row in enumerate(matrix)
        if REQUIRED_SPEC_FIELDS - set(row)
    ]
    if bad:
        raise RuntimeError(f'Frozen runner records are incomplete: {bad[:3]}')
    required = [
        root / 'runner/oracle/oracle.jar', root / 'frozen-params.pkl',
        root / 'observation-contract.json', root / 'setup-complete.json']
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f'Frozen runner files are missing: {missing}')
    return {'case_count': len(matrix), 'required_fields': sorted(REQUIRED_SPEC_FIELDS)}


def png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or payload[:8] != b'\x89PNG\r\n\x1a\n':
        raise RuntimeError('ADB screencap did not return a PNG')
    width, height = struct.unpack('>II', payload[16:24])
    if width < 1 or height < 1:
        raise RuntimeError(f'Invalid screenshot dimensions: {width}x{height}')
    return width, height


def tree_node_count(node) -> int:
    if not isinstance(node, dict):
        return 0
    return 1 + sum(tree_node_count(child) for child in node.get('children') or [])


def probe_pixels(adb, _env, _root, ordinal: int) -> dict:
    started = time.monotonic()
    payload = adb('exec-out', 'screencap', '-p')
    width, height = png_dimensions(payload)
    return {'ready': True, 'latency_s': time.monotonic() - started,
            'width': width, 'height': height, 'bytes': len(payload),
            'attempt': ordinal, 'provider': 'adb_screencap'}


async def _probe_collector_async(runtime: Path, timeout_s: float) -> dict:
    runtime_text = str(runtime)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    from driver.accessibility import AccessibilityCollectorClient

    client = AccessibilityCollectorClient('emulator-5554')
    started = time.monotonic()
    try:
        health = await client.health(timeout=min(timeout_s, 3.0))
        if not health.get('ready'):
            raise RuntimeError(f'collector health: {health}')
        warm = await client.warm(timeout=min(timeout_s, 5.0))
        if not warm.get('ready'):
            raise RuntimeError(f'collector warm: {warm}')
        snapshot = await client.fetch_primary(timeout=timeout_s)
        raw = snapshot.to_raw_tree(elapsed_ms=(time.monotonic() - started) * 1000)
        capture = raw.get('_capture') or {}
        if capture.get('complete') is not True:
            raise RuntimeError(f'collector snapshot incomplete: {capture}')
        nodes = tree_node_count(raw)
        if nodes <= 1:
            raise RuntimeError('collector snapshot has no window/content nodes')
        return {'ready': True, 'latency_s': time.monotonic() - started,
                'node_count': nodes, 'capture': capture}
    finally:
        await client.close()


def probe_collector(_adb, _env, root: Path, ordinal: int) -> dict:
    result = asyncio.run(_probe_collector_async(root / 'runtime', DEFAULT_THRESHOLDS_S['collector']))
    result['attempt'] = ordinal
    return result


def probe_native_forest(_adb, env, root: Path, ordinal: int) -> dict:
    from scoring import read_fresh_forest
    directory = root / 'health' / f'native-{time.time_ns()}-{ordinal}'
    directory.mkdir(parents=True, exist_ok=False)
    evidence = []
    started = time.monotonic()
    forest = read_fresh_forest(env.controller, directory, evidence)
    latency = time.monotonic() - started
    atomic_json(directory / 'health.json', {'latency_s': latency, 'observations': evidence})
    if not forest.windows:
        raise RuntimeError('Native scoring forest has no windows')
    return {'ready': True, 'latency_s': latency, 'windows': len(forest.windows),
            'attempt': ordinal, 'provider': 'frozen_native_uiautomation'}


def _probe(name, function, adb, env, root, ordinal, threshold_s) -> dict:
    started = time.monotonic()
    try:
        result = function(adb, env, root, ordinal)
        result.setdefault('latency_s', time.monotonic() - started)
        result['ready'] = bool(result.get('ready')) and result['latency_s'] <= threshold_s
        if result['latency_s'] > threshold_s:
            result['error'] = f'latency {result["latency_s"]:.3f}s exceeds {threshold_s:.3f}s'
    except Exception as exc:
        result = {'ready': False, 'latency_s': time.monotonic() - started,
                  'error': f'{type(exc).__name__}: {exc}', 'attempt': ordinal}
    result['channel'] = name
    result['threshold_s'] = threshold_s
    return result


def recover_collector(adb, _env) -> dict:
    existing = _text(adb('shell', 'settings', 'get', 'secure',
                         'enabled_accessibility_services'))
    values = [value for value in existing.split(':') if value]
    without = [value for value in values if value != COLLECTOR_COMPONENT]
    adb('shell', 'settings', 'put', 'secure', 'enabled_accessibility_services',
        ':'.join(without))
    adb('shell', 'settings', 'put', 'secure', 'enabled_accessibility_services',
        ':'.join([*without, COLLECTOR_COMPONENT]))
    adb('shell', 'settings', 'put', 'secure', 'accessibility_enabled', '1')
    time.sleep(0.5)
    confirmed = _text(adb('shell', 'settings', 'get', 'secure',
                          'enabled_accessibility_services'))
    if COLLECTOR_COMPONENT not in confirmed.split(':'):
        raise RuntimeError('Collector service setting was not retained')
    return {'operation': 'toggle_collector_service_only',
            'preserved_services': without}


def recover_pixels(_adb, _env) -> dict:
    # A fresh screencap subprocess is itself the pixel-channel reset.  Do not
    # restart ADB or touch the independent scoring/collector channels here.
    return {'operation': 'discard_failed_capture_and_open_fresh_adb_screencap'}


def recover_native_forest(adb, _env) -> dict:
    # Every oracle probe launches a fresh native helper.  HOME only stabilizes
    # the pre-initialization window; it does not touch app data or Collector.
    adb('shell', 'input', 'keyevent', 'KEYCODE_HOME')
    time.sleep(0.25)
    return {'operation': 'stabilize_home_and_launch_fresh_native_helper'}


PROBES = {'collector': probe_collector, 'pixels': probe_pixels,
          'native_forest': probe_native_forest}
RECOVERIES = {'collector': recover_collector, 'pixels': recover_pixels,
              'native_forest': recover_native_forest}


def ensure_channels(env, root: Path, adb, *, allow_full_restart: bool = True,
                    probes=None, recoveries=None, thresholds=None) -> dict:
    """Probe all channels, recover only degraded ones, then fail closed."""
    probes = probes or PROBES
    recoveries = recoveries or RECOVERIES
    thresholds = {**DEFAULT_THRESHOLDS_S, **(thresholds or {})}
    report = {'started_at': time.time(), 'channels': {}, 'recoveries': [],
              'allow_full_restart': allow_full_restart}
    for name in ('collector', 'pixels', 'native_forest'):
        report['channels'][name] = [_probe(
            name, probes[name], adb, env, root, 1, thresholds[name])]
    degraded = [name for name, rows in report['channels'].items()
                if not rows[-1]['ready']]
    for name in degraded:
        recovery = {'channel': name, 'started_at': time.time()}
        try:
            recovery.update(recoveries[name](adb, env))
            recovery['succeeded'] = True
        except Exception as exc:
            recovery.update(succeeded=False, error=f'{type(exc).__name__}: {exc}')
        recovery['finished_at'] = time.time()
        report['recoveries'].append(recovery)
        report['channels'][name].append(_probe(
            name, probes[name], adb, env, root, 2, thresholds[name]))
    # Collector recovery toggles only its accessibility service, but the
    # scoring path is too important to assume independence: prove the native
    # forest still works after any other channel was repaired.
    if report['recoveries'] and 'native_forest' not in degraded:
        native = _probe('native_forest', probes['native_forest'], adb, env,
                        root, 2, thresholds['native_forest'])
        native['integrity_recheck_after_other_recovery'] = True
        report['channels']['native_forest'].append(native)
        if not native['ready']:
            recovery = {'channel': 'native_forest', 'started_at': time.time(),
                        'reason': 'post-recovery integrity recheck'}
            try:
                recovery.update(recoveries['native_forest'](adb, env))
                recovery['succeeded'] = True
            except Exception as exc:
                recovery.update(succeeded=False,
                                error=f'{type(exc).__name__}: {exc}')
            recovery['finished_at'] = time.time()
            report['recoveries'].append(recovery)
            report['channels']['native_forest'].append(_probe(
                'native_forest', probes['native_forest'], adb, env, root, 3,
                thresholds['native_forest']))
    remaining = [name for name, rows in report['channels'].items()
                 if not rows[-1]['ready']]
    report.update(finished_at=time.time(), ready=not remaining,
                  remaining_degraded=remaining)
    health_dir = root / 'health'
    health_dir.mkdir(parents=True, exist_ok=True)
    report_path = health_dir / f'pre-episode-{time.time_ns()}.json'
    atomic_json(report_path, report)
    report['evidence'] = str(report_path)
    if remaining:
        if allow_full_restart:
            raise FullRestartRequired(report)
        raise RuntimeError(f'Full restart did not restore channels: {remaining}')
    return report


def write_episode_boundary(root: Path, case: str, packages: list[str]) -> None:
    atomic_json(root / 'episode-boundary.json', {
        'case': case, 'packages': packages, 'cleanup_complete': False,
        'written_before_initialize': True, 'updated_at': time.time()})


def mark_episode_boundary_clean(root: Path, case: str) -> None:
    path = root / 'episode-boundary.json'
    value = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    if value.get('case') != case:
        raise RuntimeError(f'Episode cleanup marker mismatch: {value.get("case")!r} != {case!r}')
    value.update(cleanup_complete=True, updated_at=time.time())
    atomic_json(path, value)


def recover_interrupted_boundary(root: Path, adb) -> dict | None:
    """Force-stop only a previously interrupted task, before next initialize."""
    path = root / 'episode-boundary.json'
    if not path.is_file():
        return None
    marker = json.loads(path.read_text(encoding='utf-8'))
    if marker.get('cleanup_complete'):
        return None
    packages = marker.get('packages') or []
    if not packages or any(not isinstance(value, str) or not value for value in packages):
        raise RuntimeError(f'Invalid interrupted episode marker: {marker}')
    events = []
    for package in packages:
        started = time.time()
        adb('shell', 'am', 'force-stop', package)
        events.append({'package': package, 'started_at': started, 'finished_at': time.time()})
    marker.update(cleanup_complete=True, recovered_after_interruption=True,
                  recovery_events=events, updated_at=time.time())
    atomic_json(path, marker)
    recovery = root / 'health' / f'interrupted-cleanup-{time.time_ns()}.json'
    atomic_json(recovery, marker)
    return marker


def restart_same_avd_and_initialize(root: Path, adb_path: Path,
                                    *, python: Path | None = None,
                                    timeout_s: float = 240.0,
                                    apps: list[str] | None = None) -> dict:
    """Restart without wiping data, then prepare the explicit dependency scope."""
    python = python or Path(sys.executable)
    command = [str(adb_path), '-s', 'emulator-5554']
    avd_discovery_error = None
    try:
        avd = subprocess.check_output(
            [*command, 'emu', 'avd', 'name'], timeout=15,
            text=True).splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        # AndroidWorld's official installation contract fixes this name.  The
        # fallback keeps a crashed ADB console recoverable without guessing a
        # different AVD or enumerating/deleting host state.
        avd = EXPECTED_AVD_NAME
        avd_discovery_error = f'{type(exc).__name__}: {exc}'
    if not avd:
        raise RuntimeError('Cannot determine the connected AVD name')
    emulator = adb_path.resolve().parent.parent / 'emulator' / ('emulator.exe' if os.name == 'nt' else 'emulator')
    if not emulator.is_file():
        raise RuntimeError(f'Emulator executable is missing: {emulator}')
    subprocess.run([*command, 'emu', 'kill'], capture_output=True, timeout=20)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        devices = subprocess.run([str(adb_path), 'devices'], capture_output=True,
                                 text=True, timeout=10).stdout
        if 'emulator-5554\tdevice' not in devices:
            break
        time.sleep(0.5)
    boot_id = time.time_ns()
    log_path = root / 'health' / f'emulator-restart-{boot_id}.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # NEVER mode avoids the blocking report-consent dialog. Give each boot a
    # fresh database so the emulator never scans/deletes an earlier boot's
    # reports; crash detection remains enabled and uploads remain disabled.
    crash_directory = root / 'health' / f'crash-reports-{boot_id}'
    crash_directory.mkdir()
    launch_env = os.environ.copy()
    launch_env['ANDROID_EMU_CRASH_REPORTING_DATABASE'] = str(crash_directory.resolve())
    with log_path.open('wb') as log:
        # This emulator build documents ``-grpc`` as disabling its default
        # JWT authentication.  AndroidEnv's pinned gRPC client has no token.
        subprocess.Popen([str(emulator), '-avd', avd, '-no-snapshot',
                          '-grpc', '8554', '-crash-report-mode', 'never',
                          '-camera-back', CAMERA_BACKEND_POLICY['rear']],
                         stdout=log, stderr=subprocess.STDOUT,
                         env=launch_env,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            probe = subprocess.run(
                [*command, 'shell', 'getprop', 'sys.boot_completed'],
                capture_output=True, text=True, timeout=10)
            if probe.returncode == 0 and probe.stdout.strip() == '1':
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(2)
    else:
        raise RuntimeError(f'AVD {avd} did not boot; inspect {log_path}')
    setup_report = initialize_app_scope(root, adb_path, python=python, apps=apps)
    return {'avd': avd, 'emulator': str(emulator), 'log': str(log_path),
            'camera_backend': dict(CAMERA_BACKEND_POLICY),
            'crash_report_mode': 'never', 'crash_report_directory': str(crash_directory),
            **setup_report, 'avd_discovery_error': avd_discovery_error}


def initialize_app_scope(root: Path, adb_path: Path, *, python: Path | None = None,
                         apps: list[str] | None = None) -> dict:
    """Caller must close AndroidEnv first and be outside any scored episode."""
    if apps is not None and (not apps or any(not isinstance(name, str) or not name for name in apps)):
        raise ValueError('Scoped initialization requires explicit app names')
    command = [str(python or sys.executable), '-u', str(root / 'setup_full.py'),
               '--root', str(root), '--adb', str(adb_path)]
    if apps is not None:
        for name in sorted(set(apps)):
            command.extend(['--app', name])
    setup = subprocess.run(command, cwd=root, timeout=900)
    if setup.returncode:
        raise RuntimeError('Environment initialization failed closed')
    latest_path = root / 'environment-repair-latest.json'
    latest = json.loads(latest_path.read_text(encoding='utf-8'))
    result = latest.get('result') or {}
    required = {
        'ready': result.get('ready') is True,
        'api33': result.get('api_level') == 33,
        'pointer0': result.get('pointer_location') == 0,
        'app_scope': (len(result.get('apps') or []) == 23 if apps is None else
                      {row.get('app') for row in result.get('apps') or [] if row.get('ready') is True}
                      == set(apps) | {'android world'}),
        'collector': (result.get('collector') or {}).get('ready') is True,
        'pixels': bool((result.get('pixels') or {}).get('width')),
        'native_forest': (result.get('scoring_forest_windows') or 0) > 0,
        'runner116': (result.get('runner_contract') or {}).get('case_count') == 116,
    }
    if latest.get('status') != 'ready' or not all(required.values()):
        raise RuntimeError(f'Full setup returned without the complete contract: {required}')
    return {'setup_returncode': setup.returncode, 'setup_apps': apps,
            'setup_evidence': latest.get('evidence'), 'contract': required}
