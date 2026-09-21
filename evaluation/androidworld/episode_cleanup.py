"""Episode-boundary process isolation for the API 33 AndroidWorld emulator.

Task data is never cleared by these helpers. Before model execution, only
reviewed non-task background noise is stopped. After scoring, official task
teardown runs first, then task packages are stopped to prevent service leaks.
"""
import json
import subprocess
import time


OPENTRACKS_PACKAGE = 'de.dennisguse.opentracks'


def resolve_task_packages(task, adb_utils):
    """Resolve task app aliases without changing device state."""
    packages = []
    errors = []
    for app in task.app_names:
        if not app:
            continue
        try:
            activity = adb_utils.get_adb_activity(app)
            if not activity:
                raise ValueError(f'Cannot resolve task app: {app}')
            package = adb_utils.extract_package_name(activity)
            if package not in packages:
                packages.append(package)
        except Exception as exc:
            errors.append(f'app resolution: {exc!r}')
    return packages, errors


def prepare_task_environment(task, output):
    """Reapply the existing noise policy after official initialization/reset.

    Initialization can reawaken background apps after the pre-case health check.
    Call only before starting the worker, never during an active task action.
    """
    from android_world.env import adb_utils
    from run_emulator import adb
    from setup_full import cleanup_background_apps

    packages, errors = resolve_task_packages(task, adb_utils)
    if errors:
        raise RuntimeError(f'Cannot protect task packages during preparation: {errors}')
    report = cleanup_background_apps(
        adb, required_packages=set(packages),
        evidence_path=output / 'background-cleanup-before-worker.json')
    if 'com.google.android.contacts' in packages and task.start_on_home_screen:
        from setup_full import verify_contacts_launcher
        try:
            verify_contacts_launcher(adb, output, allow_onboarding=False)
        finally:
            adb('shell', 'input', 'keyevent', 'KEYCODE_HOME')
    return report


def _adb_text(*args):
    """Use the frozen runner ADB command while accepting pidof's empty exit."""
    from run_emulator import adb
    try:
        value = adb(*args)
    except subprocess.CalledProcessError as exc:
        value = exc.output or b''
    return value.decode(errors='replace').strip()


def active_location_registration_lines(dump, package):
    """Return package rows inside an active ``registrations`` dump section."""
    rows = []
    section_indent = None
    for line in dump.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.lower().rstrip(':') in {
                'registrations', 'active registrations', 'listeners'}:
            section_indent = indent
            continue
        if section_indent is not None and stripped and indent <= section_indent:
            section_indent = None
        if section_indent is not None and package in stripped:
            rows.append(stripped)
    return rows


def verify_opentracks_stopped():
    """Prove OpenTracks has no process and no active LocationManager lease."""
    pid = _adb_text('shell', 'pidof', OPENTRACKS_PACKAGE)
    location_dump = _adb_text('shell', 'dumpsys', 'location')
    registrations = active_location_registration_lines(
        location_dump, OPENTRACKS_PACKAGE)
    result = {
        'pidof': pid,
        'active_gps_registrations': registrations,
        'verified': not pid and not registrations,
    }
    if not result['verified']:
        raise RuntimeError(
            'OpenTracks remained active after force-stop: '
            f'pid={pid!r}, registrations={registrations!r}')
    return result


def teardown_task(task, env, output):
    from android_world.env import adb_utils
    from scoring import read_fresh_forest

    report = {
        'policy': 'official-score -> official-tear_down -> force-stop',
        'official_teardown_ok': False,
        'stopped_packages': [],
        'events': [],
        'errors': [],
    }
    report['events'].append({'event': 'official_teardown_started', 'at': time.time()})
    try:
        task.tear_down(env)
        report['official_teardown_ok'] = True
    except Exception as exc:
        report['errors'].append(f'official teardown: {exc!r}')
    finally:
        report['events'].append({'event': 'official_teardown_finished', 'at': time.time()})

    packages, resolution_errors = resolve_task_packages(task, adb_utils)
    report['errors'].extend(resolution_errors)

    clock_task = 'com.google.android.deskclock' in packages
    if clock_task:
        # Clearing Clock does not invalidate System Intelligence's running
        # stopwatch Smartspace card. Stop its process, preserving app data.
        packages.append('com.google.android.as')
    for package in packages:
        report['events'].append({
            'event': 'force_stop_started', 'package': package, 'at': time.time()})
        try:
            response = adb_utils.issue_generic_request(
                ['shell', 'am', 'force-stop', package], env.controller)
            adb_utils.check_ok(response)
            report['stopped_packages'].append(package)
        except Exception as exc:
            report['errors'].append(f'force-stop {package}: {exc!r}')
        finally:
            report['events'].append({
                'event': 'force_stop_finished', 'package': package, 'at': time.time()})

    if OPENTRACKS_PACKAGE in packages:
        try:
            report['opentracks'] = verify_opentracks_stopped()
        except Exception as exc:
            report['errors'].append(f'OpenTracks postcondition: {exc!r}')

    if clock_task and not report['errors']:
        directory = output / 'post-teardown-observation'
        directory.mkdir(parents=True, exist_ok=False)
        report['home_observations'] = []
        try:
            adb_utils.press_home_button(env.controller)
            read_fresh_forest(env.controller, directory, report['home_observations'])
            report['home_tree_verified'] = True
        except Exception as exc:
            report['errors'].append(f'post-clock home observation: {exc!r}')
    (output / 'episode-cleanup.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    if report['errors']:
        raise RuntimeError('Episode cleanup failed; see episode-cleanup.json')
