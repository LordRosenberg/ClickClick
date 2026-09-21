"""Focused tests for fail-closed AndroidWorld environment provisioning."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.androidworld import setup_full


@pytest.mark.parametrize("repair,unknown", [(False, False), (True, True)])
def test_contacts_rejects_unverified_overlay_without_action(tmp_path, repair, unknown):
    calls = []
    text = 'Unknown prompt' if unknown else 'Now you can find Settings and Help &amp; feedback here'
    xml = ('<hierarchy><node package="com.google.android.contacts" '
           'resource-id="com.google.android.contacts:id/og_tooltip_scrim_view" '
           f'text="{text}"/></hierarchy>').encode()
    def adb(*args):
        calls.append(args)
        if args[:3] == ('shell', 'dumpsys', 'package'):
            return b'android.permission.POST_NOTIFICATIONS: granted=true'
        return xml if args[:2] == ('shell', 'cat') else b''
    with pytest.raises(RuntimeError, match='onboarding not verified'):
        setup_full.verify_contacts_launcher(adb, tmp_path, allow_onboarding=repair)
    assert not any(x[:3] == ('shell', 'input', 'keyevent') for x in calls)


def test_contacts_missing_external_permission_fails_before_launch(tmp_path):
    calls = []
    def adb(*args):
        calls.append(args)
        return b'android.permission.POST_NOTIFICATIONS: granted=false'
    with pytest.raises(RuntimeError, match='notification permission missing'):
        setup_full.verify_contacts_launcher(adb, tmp_path, allow_onboarding=False)
    assert not any(x[:2] == ('shell', 'monkey') for x in calls)
    assert not any(x[:3] == ('shell', 'pm', 'grant') for x in calls)


def test_scoped_setup_keeps_all_declared_dependencies_and_support_app():
    all_apps = {'android world', 'markor', 'vlc', 'tasks'}
    assert setup_full.select_setup_apps(all_apps, ['markor', 'vlc']) == {
        'android world', 'markor', 'vlc'}
    assert setup_full.select_setup_apps(all_apps, None) == all_apps
    for names in ([], ['unknown'], ['']):
        with pytest.raises(ValueError):
            setup_full.select_setup_apps(all_apps, names)


def test_baseline_preflight_reads_device_and_catches_missing_secondary_app(monkeypatch):
    import sys
    snapshots = {'/snapshot/android world', '/snapshot/markor'}
    directories = set(snapshots) | {'/videos'}
    available = [SimpleNamespace(app_name=name, package_name=lambda n=name: n,
                                 videos_path='/videos') for name in ['android world', 'markor', 'vlc']]
    monkeypatch.setitem(sys.modules, 'android_world.env.setup_device', SimpleNamespace(
        setup=SimpleNamespace(_APPS=available, is_package_installed=lambda *args: True)))
    monkeypatch.setitem(sys.modules, 'android_world.utils', SimpleNamespace(
        app_snapshot=SimpleNamespace(_snapshot_path=lambda name: '/snapshot/'+name),
        file_utils=SimpleNamespace(check_directory_exists=lambda path, env: path in directories,
                                   check_file_exists=lambda *args: True)))
    env = SimpleNamespace(controller=None)
    assert setup_full.missing_task_baselines(['markor', 'vlc'], env) == ['vlc']
    directories.add('/snapshot/vlc')
    assert setup_full.missing_task_baselines(['markor', 'vlc'], env) == []
    directories.remove('/videos')
    assert setup_full.missing_task_baselines(['markor', 'vlc'], env) == ['vlc']
    with pytest.raises(RuntimeError, match='Unknown task'):
        setup_full.missing_task_baselines(['unknown'], env)


def _opentracks_xml(*, page=None, unknown=False):
    from xml.etree.ElementTree import Element, SubElement, tostring
    root = Element("hierarchy")
    package = "de.dennisguse.opentracks"
    ids = ("introduction_view_pager", "next_button") if page is not None else (
        "track_list_toolbar", "aggregated_stats_button", "track_list_fab_action")
    for resource in ids:
        SubElement(root, "node", {"package": package, "resource-id": package + ":id/" + resource,
                   "text": "", "clickable": "true", "enabled": "true", "bounds": "[10,20][30,40]"})
    if page is not None:
        texts = ["OpenTracks is a sport tracking application that completely respects your privacy.",
                 "OpenTracks itself does not provide a map. Please install OSMDashboard to view your recordings on a map."]
        SubElement(root, "node", {"package": package, "text": "Unknown" if unknown else texts[page]})
    return tostring(root)


@pytest.mark.parametrize("check_only,unknown", [(True, False), (False, True)])
def test_opentracks_setup_refuses_unapproved_page_without_tapping(tmp_path, check_only, unknown):
    calls = []
    def adb(*args):
        calls.append(args)
        return _opentracks_xml(page=0, unknown=unknown) if args[:2] == ("shell", "cat") else b""
    with pytest.raises(RuntimeError, match="onboarding missing|Unknown OpenTracks"):
        setup_full.verify_opentracks_launcher(adb, tmp_path, allow_onboarding=not check_only)
    assert not any(call[:3] == ("shell", "input", "tap") for call in calls)
    assert calls[-1] == ("shell", "am", "force-stop", "de.dennisguse.opentracks")


def test_opentracks_setup_advances_known_pages_once_then_is_idempotent(tmp_path, monkeypatch):
    calls, page = [], [0]
    monkeypatch.setattr(setup_full.time, "sleep", lambda _: None)
    def adb(*args):
        calls.append(args)
        if args[:3] == ("shell", "input", "tap"):
            page[0] += 1
        return _opentracks_xml(page=page[0] if page[0] < 2 else None) if args[:2] == ("shell", "cat") else b""
    result = setup_full.verify_opentracks_launcher(adb, tmp_path, allow_onboarding=True)
    assert result["ready"] and result["onboarding_clicks"] == 2
    calls.clear()
    result = setup_full.verify_opentracks_launcher(adb, tmp_path, allow_onboarding=False)
    assert result["ready"] and result["onboarding_clicks"] == 0
    assert not any(call[:3] == ("shell", "input", "tap") for call in calls)


@pytest.mark.parametrize("already_present", [False, True])
def test_task_resources_restore_only_missing_official_assets(tmp_path, monkeypatch, already_present):
    import posixpath
    import sys
    calls, files, directories = [], set(), set()
    map_root = "/storage/emulated/0/Android/data/net.osmand/files/"
    map_path = posixpath.join(map_root, setup_full.OSMAND_MAP_NAME)
    video_path = "/storage/emulated/0/VLCVideos"
    if already_present:
        files.add(map_path)
        directories.add(video_path)

    def copy(names, path, env):
        calls.append(("copy", names, path))
        files.update(posixpath.join(path, name) for name in names)

    apps = [SimpleNamespace(app_name="osmand", MAP_NAMES=(setup_full.OSMAND_MAP_NAME,),
                            DEVICE_MAPS_PATH=map_root, _copy_data_to_device=copy),
            SimpleNamespace(app_name="vlc", videos_path=video_path)]
    utilities = SimpleNamespace(convert_to_posix_path=posixpath.join,
        check_file_exists=lambda path, env: path in files,
        check_directory_exists=lambda path, env: path in directories,
        mkdir=lambda path, env: calls.append(("mkdir", path)) or directories.add(path))
    adb = SimpleNamespace(issue_generic_request=lambda args, env:
                          calls.append(tuple(args)) or "ok", check_ok=lambda reply: None)
    monkeypatch.setitem(sys.modules, "android_world.env", SimpleNamespace(adb_utils=adb))
    monkeypatch.setitem(sys.modules, "android_world.env.setup_device", SimpleNamespace(
        setup=SimpleNamespace(_APPS=apps)))
    monkeypatch.setitem(sys.modules, "android_world.utils", SimpleNamespace(file_utils=utilities))
    report = setup_full.prepare_task_assets(SimpleNamespace(app_names=["osmand", "vlc"]),
        SimpleNamespace(controller=None), tmp_path)
    assert all(row["ready"] for row in report)
    if already_present:
        assert calls == []
    else:
        assert calls == [("copy", (setup_full.OSMAND_MAP_NAME,), map_root),
                         ("shell", "chcon", "u:object_r:media_rw_data_file:s0", map_path),
                         ("mkdir", video_path)]
    calls.clear()
    assert setup_full.prepare_task_assets(SimpleNamespace(app_names=["tasks"]),
        None, tmp_path) == []
    assert calls == []


def test_write_json_once_preserves_existing_batch_evidence(tmp_path):
    path = tmp_path / "setup-complete.json"
    path.write_text('{"historical": true}', encoding="utf-8")
    assert not setup_full.write_json_once(path, {"historical": False})
    assert path.read_text(encoding="utf-8") == '{"historical": true}'


def test_refuse_active_batch_but_allow_paused_repair(tmp_path):
    state = tmp_path / "run-state.json"
    state.write_text('{"status": "running"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="while evaluation is 'running'"):
        setup_full.refuse_active_batch(tmp_path)
    state.write_text('{"status": "paused_infrastructure"}', encoding="utf-8")
    setup_full.refuse_active_batch(tmp_path)


def test_refuse_repair_while_runner_is_in_health_gate(tmp_path):
    (tmp_path / "run-state.json").write_text(
        '{"status": "health_check"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="health_check"):
        setup_full.refuse_active_batch(tmp_path)


def test_device_readiness_requires_api_services_and_settings(monkeypatch):
    values = {
        ("get-state",): "device",
        ("shell", "getprop", "ro.build.version.sdk"): "33",
        ("shell", "getprop", "sys.boot_completed"): "1",
        ("shell", "pidof", "system_server"): "123",
        ("shell", "settings", "get", "system", "pointer_location"): "0",
        ("shell", "service", "check", "activity"): "Service activity: found",
        ("shell", "service", "check", "package"): "Service package: found",
        ("shell", "service", "check", "window"): "Service window: found",
    }

    def adb(*args):
        return values[args].encode()

    result = setup_full.wait_device_ready(adb, timeout_seconds=0.1, poll_seconds=0)
    assert result["api_level"] == "33"
    assert all(result["services"].values())

    values[("shell", "service", "check", "window")] = "Service window: not found"
    monkeypatch.setattr(setup_full.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="did not become ready"):
        setup_full.wait_device_ready(adb, timeout_seconds=0, poll_seconds=0)


def test_scoped_setup_click_uses_one_ui_dump_and_restores_original():
    calls = []
    bounds = SimpleNamespace(center=(101.9, 202.1))
    element = SimpleNamespace(
        bbox_pixels=bounds, text="NEXT", content_description=None
    )

    class Controller:
        def __init__(self, env):
            self._env = env

        def click_element(self, _text):
            raise AssertionError("original should be replaced inside scope")

    original = Controller.click_element
    tools = SimpleNamespace(AndroidToolController=Controller)
    actuation = SimpleNamespace(
        _find_target_element=lambda elements, text, case: (0, 0)
    )
    adb_utils = SimpleNamespace(
        tap_screen=lambda x, y, env: calls.append((x, y, env))
    )
    env = SimpleNamespace(get_ui_elements=lambda: [element])
    controller = Controller(env)

    with setup_full.stable_official_setup_clicks(tools, actuation, adb_utils):
        controller.click_element("NEXT")

    assert calls == [(101, 202, env)]
    assert Controller.click_element is original


def test_contacts_optional_notification_prompt_requires_main_screen(monkeypatch):
    monkeypatch.setattr(setup_full, "SETUP_ELEMENT_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(setup_full.time, "sleep", lambda _seconds: None)
    element = SimpleNamespace(
        bbox_pixels=None, text="No contacts yet", content_description=None
    )

    class Controller:
        def __init__(self):
            self._env = SimpleNamespace(get_ui_elements=lambda: [element])

        def click_element(self, _text):
            raise AssertionError

    tools = SimpleNamespace(AndroidToolController=Controller)
    actuation = SimpleNamespace(_find_target_element=lambda *_args: (0, 99))
    adb_utils = SimpleNamespace(tap_screen=lambda *_args: None)
    with setup_full.stable_official_setup_clicks(tools, actuation, adb_utils):
        Controller().click_element("Don't allow")
        with pytest.raises(ValueError, match="NEXT"):
            Controller().click_element("NEXT")


def test_prepare_full_uses_current_setup_script_not_baseline_copy():
    source = (
        Path(__file__).resolve().parents[1]
        / "evaluation/androidworld/prepare_full.py"
    ).read_text(encoding="utf-8")
    assert 'with_name("setup_full.py")' in source
    assert 'for name in ("setup_full.py", "probe_model.py"' not in source


def test_chrome_combined_onboarding_keeps_no_account_flow_scoped(monkeypatch):
    monkeypatch.setattr(setup_full, "SETUP_ELEMENT_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(setup_full.time, "sleep", lambda _: None)
    pages = [["Welcome to Chrome", "Use without an account", "Add account to device"],
             ["Chrome notifications make things easier", "No thanks", "Continue",
              "Search or type web address", "Switch or close tabs", "More options"],
             ["Search or type web address", "Switch or close tabs", "More options"]]
    taps = []
    def elements():
        return [SimpleNamespace(text=text, content_description=None,
                bbox_pixels=SimpleNamespace(center=(index, len(taps))))
                for index, text in enumerate(pages[len(taps)])]
    class Controller:
        _env = SimpleNamespace(get_ui_elements=elements)
        def click_element(self, text):
            raise AssertionError("must be patched only in scope")
    def find(rows, text, _case):
        return next(((i, 0) for i, row in enumerate(rows) if row.text == text), (0, 99))
    def tap(index, page, _env):
        taps.append(pages[page][index])
    tools = SimpleNamespace(AndroidToolController=Controller)
    actuation = SimpleNamespace(_find_target_element=find)
    adb = SimpleNamespace(tap_screen=tap)
    with setup_full.stable_official_setup_clicks(tools, actuation, adb, lambda: "chrome"):
        controller = Controller()
        for text in ["Accept & continue", "No thanks", "No thanks"]:
            controller.click_element(text)
    assert taps == ["Use without an account", "No thanks"]
    with setup_full.stable_official_setup_clicks(tools, actuation, adb, lambda: "other"):
        with pytest.raises(ValueError, match="No thanks"):
            Controller().click_element("No thanks")


def test_all_files_appop_is_reset_and_must_be_allowed():
    calls = []
    modes = {"package": "allow", "uid": "allow"}

    def adb(*args):
        calls.append(args)
        if "set" in args:
            modes["uid" if "--uid" in args else "package"] = args[-1]
        if "get" in args:
            return ("MANAGE_EXTERNAL_STORAGE: " +
                    ("allow" if "allow" in modes.values() else "default")).encode()
        return b""

    setup_full.reset_manage_external_storage(adb, "example.package")
    assert modes == {"package": "default", "uid": "default"}
    with pytest.raises(RuntimeError, match="was not granted"):
        setup_full.verify_manage_external_storage(adb, "example.package")
    modes["uid"] = "allow"  # Android Settings grants the permission again.
    setup_full.verify_manage_external_storage(adb, "example.package")
    assert calls[0][-3:] == (
        "example.package", "MANAGE_EXTERNAL_STORAGE", "default"
    )

    with pytest.raises(RuntimeError, match="was not granted"):
        setup_full.verify_manage_external_storage(lambda *_args: b"default", "bad")


def test_full_setup_pins_frozen_environment_contract():
    assert setup_full.EXPECTED_API_LEVEL == "33"
    assert setup_full.EXPECTED_APP_COUNT == 23
    assert setup_full.OSMAND_MAP_NAME == "Liechtenstein_europe.obf"
    assert setup_full.SETUP_ELEMENT_TIMEOUT_SECONDS == 30.0


def test_pixel_contract_requires_decodable_png_dimensions():
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (1080).to_bytes(4, "big") + (2400).to_bytes(4, "big")
    result = setup_full.verify_pixels(lambda *args: payload)
    assert (result["width"], result["height"]) == (1080, 2400)
    with pytest.raises(RuntimeError, match="PNG"):
        setup_full.verify_pixels(lambda *args: b"black frame")


def test_setup_runner_contract_requires_all_fields_and_native_oracle(tmp_path):
    fields = setup_full.REQUIRED_RUNNER_FIELDS
    (tmp_path / "matrix.json").write_text(
        __import__("json").dumps([{key: None for key in fields} for _ in range(116)]))
    for name in ("frozen-params.pkl", "observation-contract.json", "runner/oracle/oracle.jar"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert setup_full.verify_runner_contract(tmp_path)["case_count"] == 116


def test_settings_residue_cleanup_is_scoped_and_never_clears_data():
    calls = []
    setup_full.dismiss_settings_residue(lambda *args: calls.append(args) or b"")
    assert calls == [
        ("shell", "am", "force-stop", "com.android.settings"),
        ("shell", "input", "keyevent", "KEYCODE_HOME"),
    ]
    source = Path(setup_full.__file__).read_text(encoding="utf-8").lower()
    assert "pm" + " clear" not in source
