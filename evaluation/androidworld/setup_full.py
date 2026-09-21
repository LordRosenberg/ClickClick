"""Idempotently provision and verify the full AndroidWorld device baseline.

The script repairs only missing app baselines/assets. Every invocation writes
new evidence under ``environment-repairs`` and never replaces an existing
``setup-complete.json`` from a sealed evaluation batch.
"""

from __future__ import annotations

import argparse
import shutil
import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
import struct
import re
import uuid
from xml.etree import ElementTree


EXPECTED_API_LEVEL = "33"
EXPECTED_APP_COUNT = 23
OSMAND_MAP_NAME = "Liechtenstein_europe.obf"
SETUP_ELEMENT_TIMEOUT_SECONDS = 30.0
ACTIVE_RUN_STATES = {
    "health_check", "initializing", "running", "scoring", "teardown"
}
OPTIONAL_SETUP_POSTCONDITIONS = {
    # Android may retain the notification decision outside Contacts app data.
    # Reaching Contacts' empty main screen proves onboarding completed even
    # when the optional Android 13 notification prompt is not shown.
    "Don't allow": frozenset({"No contacts yet", "Create contact"}),
    # OpenTracks' one-time Bluetooth prompt is absent after Android has
    # retained the decision.  Its main screen proves setup can continue.
    "Allow": frozenset({"Record", "Tracks"}),
    # The role picker is absent once Simple SMS is already the default app.
    "SMS Messenger": frozenset({"Settings", "About", "More options"}),
    "Set as default": frozenset({"Settings", "About", "More options"}),
}
MANAGE_EXTERNAL_STORAGE_APPS = frozenset({"markor", "simple gallery pro", "vlc"})
REQUIRED_RUNNER_FIELDS = frozenset(
    {"task", "params", "seed", "max_steps", "max_model_calls", "max_seconds", "apps"}
)
# Only measured, non-benchmark background noise belongs here. Never infer
# expendability from CPU usage or from an app not being the current task.
BACKGROUND_STOP_ALLOWLIST = frozenset({"com.google.android.apps.youtube.music"})
BACKGROUND_PROTECTED_PACKAGES = frozenset({
    "android", "com.android.systemui", "com.android.settings",
    "com.google.android.gms", "com.google.android.gsf",
    "com.android.providers.settings", "com.android.providers.media",
    "com.android.providers.downloads", "com.android.launcher3",
    "com.google.android.apps.nexuslauncher", "com.android.vending",
    "com.android.adbkeyboard", "com.google.android.inputmethod.latin",
    "ai.clickclick.collector",
})


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def required_external_resources(name, app, file_utils):
    """Resources outside app snapshots; shared by full and per-task setup."""
    files, directories = [], []
    if name == "osmand":
        if tuple(app.MAP_NAMES) != (OSMAND_MAP_NAME,):
            raise RuntimeError(f"Unexpected OsmAnd map contract: {app.MAP_NAMES!r}")
        files = [file_utils.convert_to_posix_path(app.DEVICE_MAPS_PATH, asset)
                 for asset in app.MAP_NAMES]
    elif name == "vlc":
        directories = [app.videos_path]
    return files, directories


def prepare_task_assets(task, env, output):
    """Restore only missing task-owned baseline resources before initialization.

    Official file tasks clear shared-storage files, including OsmAnd's map.
    App snapshots cannot restore those external resources. This helper never
    clears data, launches an app, or changes the official snapshot/fixture.
    """
    names = set(task.app_names) & {"osmand", "vlc"}
    if not names:
        return []
    from android_world.env import adb_utils
    from android_world.env.setup_device import setup
    from android_world.utils import file_utils

    available = {app.app_name: app for app in setup._APPS}
    report = []
    for name in sorted(names):
        app = available[name]
        files, directories = required_external_resources(name, app, file_utils)
        missing = [path for path in files
                   if not file_utils.check_file_exists(path, env.controller)]
        created = []
        if missing:
            # Reuse the official cached resource and its exact destination.
            app._copy_data_to_device(app.MAP_NAMES, app.DEVICE_MAPS_PATH, env)
            for path in missing:
                adb_utils.check_ok(adb_utils.issue_generic_request(
                    ["shell", "chcon", "u:object_r:media_rw_data_file:s0", path],
                    env.controller))
                if not file_utils.check_file_exists(path, env.controller):
                    raise RuntimeError(f"Task baseline resource missing after repair: {path}")
        for path in directories:
            if not file_utils.check_directory_exists(path, env.controller):
                file_utils.mkdir(path, env.controller)
                created.append(path)
                if not file_utils.check_directory_exists(path, env.controller):
                    raise RuntimeError(f"Task baseline directory missing after repair: {path}")
        report.append({"app": name, "restored_files": missing,
                       "created_directories": created, "ready": True})
    atomic_json(output / "external-assets-before-initialize.json", report)
    return report


def write_json_once(path: Path, value: object) -> bool:
    """Write immutable setup evidence, returning false when it already exists."""
    if path.exists():
        return False
    atomic_json(path, value)
    return True


def verify_opentracks_launcher(adb, evidence_dir: Path, *, allow_onboarding: bool) -> dict:
    """Check the real launcher, which upstream's direct activity setup bypasses.

    Only advance the two known introduction pages, before any task fixture is
    initialized. Never tap recording, permission, or other application controls.
    The caller restores the baseline first and saves it only after this succeeds.
    """
    package = "de.dennisguse.opentracks"
    prefix = package + ":id/"
    main_ids = {prefix + name for name in (
        "track_list_toolbar", "aggregated_stats_button", "track_list_fab_action")}
    pages = (
        "OpenTracks is a sport tracking application that completely respects your privacy.",
        "OpenTracks itself does not provide a map. Please install OSMDashboard to view your recordings on a map.",
    )
    report = {"ready": False, "onboarding_clicks": 0, "captures": []}
    run_id = uuid.uuid4().hex
    try:
        adb("shell", "am", "force-stop", package)
        adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        deadline = time.monotonic() + 30
        seen_pages = set()
        while time.monotonic() < deadline:
            remote = f"/sdcard/cc-setup-{uuid.uuid4().hex}.xml"
            try:
                adb("shell", "uiautomator", "dump", remote)
                xml = adb("shell", "cat", remote)
            finally:
                adb("shell", "rm", "-f", remote)
            path = evidence_dir / f"opentracks-{run_id}-{len(report['captures'])}.xml"
            path.write_bytes(xml)
            report["captures"].append(str(path))
            nodes = [n for n in ElementTree.fromstring(xml).iter("node")
                     if n.get("package") == package]
            ids = {n.get("resource-id") for n in nodes}
            intro = prefix + "introduction_view_pager" in ids
            if main_ids.issubset(ids) and not intro:
                report["ready"] = True
                return report
            if intro:
                if not allow_onboarding:
                    raise RuntimeError("OpenTracks launcher onboarding missing from baseline")
                texts = {n.get("text") for n in nodes}
                matched = [page for page in pages if page in texts]
                buttons = [n for n in nodes if n.get("resource-id") == prefix + "next_button"
                           and n.get("clickable") == "true" and n.get("enabled") == "true"]
                if len(matched) != 1 or len(buttons) != 1:
                    raise RuntimeError("Unknown OpenTracks introduction; no setup action taken")
                page = matched[0]
                if page not in seen_pages:
                    bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", buttons[0].get("bounds", ""))
                    if not bounds:
                        raise RuntimeError("Invalid OpenTracks introduction button bounds")
                    x1, y1, x2, y2 = map(int, bounds.groups())
                    if x2 <= x1 or y2 <= y1:
                        raise RuntimeError("Empty OpenTracks introduction button bounds")
                    seen_pages.add(page)
                    adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                    report["onboarding_clicks"] += 1
            time.sleep(0.25)
        raise RuntimeError("OpenTracks launcher did not reach verified main screen")
    finally:
        atomic_json(evidence_dir / f"opentracks-launcher-{run_id}.json", report)
        adb("shell", "am", "force-stop", package)


def verify_contacts_launcher(adb, evidence_dir: Path, *, allow_onboarding: bool) -> dict:
    """Validate restored Contacts onboarding, without opening/editing contacts.

    Runtime permissions live outside the app-data snapshot. Provision the one
    known notification permission only during baseline setup; task preflight is
    verification-only and must fail before the model budget starts.
    """
    package = "com.google.android.contacts"
    permission = "android.permission.POST_NOTIFICATIONS"
    prefix = package + ":id/"
    report = {"ready": False, "onboarding_clicks": 0, "captures": []}
    run_id = uuid.uuid4().hex
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        adb("shell", "am", "force-stop", package)
        if allow_onboarding:
            adb("shell", "pm", "grant", package, permission)
        permissions = _text(adb, "shell", "dumpsys", "package", package)
        report["notification_permission_granted"] = bool(re.search(
            re.escape(permission) + r": granted=true\b", permissions))
        if not report["notification_permission_granted"]:
            raise RuntimeError("Contacts notification permission missing from baseline")
        adb("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        dismissed = False
        for _ in range(4):
            remote = f"/sdcard/cc-setup-{uuid.uuid4().hex}.xml"
            try:
                adb("shell", "uiautomator", "dump", remote)
                xml = adb("shell", "cat", remote)
            finally:
                adb("shell", "rm", "-f", remote)
            path = evidence_dir / f"contacts-{run_id}-{len(report['captures'])}.xml"
            path.write_bytes(xml)
            report["captures"].append(str(path))
            all_nodes = list(ElementTree.fromstring(xml).iter("node"))
            nodes = [n for n in all_nodes if n.get("package") == package]
            ids = {n.get("resource-id") for n in nodes}
            tooltip = prefix + "og_tooltip_scrim_view" in ids
            foreign = {n.get("package") for n in all_nodes} - {package, "com.android.systemui", None, ""}
            main = {prefix + "open_search_bar", prefix + "floating_action_button"}.issubset(ids)
            if tooltip:
                # uiautomator may omit the secondary tooltip window's text.
                # Require its app-specific scrim over the real account/list UI.
                known = main and prefix + "selected_account_disc" in ids
                if not allow_onboarding or not known or dismissed or foreign:
                    raise RuntimeError("Contacts launcher onboarding not verified; no action taken")
                scrims = [n for n in nodes if n.get("resource-id") == prefix + "og_tooltip_scrim_view"
                          and n.get("clickable") == "true" and n.get("enabled") == "true"]
                bounds = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                                      scrims[0].get("bounds", "")) if len(scrims) == 1 else None
                if not bounds:
                    raise RuntimeError("Contacts tooltip has no verified dismissal target")
                x1, y1, x2, y2 = map(int, bounds.groups())
                if x2 <= x1 or y2 <= y1:
                    raise RuntimeError("Contacts tooltip has empty bounds")
                adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                dismissed = True
                report["onboarding_clicks"] += 1
            elif main and not foreign:
                report["ready"] = True
                return report
            time.sleep(0.25)
        raise RuntimeError("Contacts launcher did not reach verified main screen")
    finally:
        atomic_json(evidence_dir / f"contacts-launcher-{run_id}.json", report)
        adb("shell", "am", "force-stop", package)


def new_evidence_dir(root: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    base = root / "environment-repairs" / f"{stamp}-{os.getpid()}"
    candidate = base
    suffix = 1
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def refuse_active_batch(root: Path) -> None:
    state_path = root / "run-state.json"
    if not state_path.is_file():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") in ACTIVE_RUN_STATES:
        raise RuntimeError(
            f"Refuse environment repair while evaluation is {state['status']!r}"
        )


def _text(adb: Callable[..., bytes], *args: str) -> str:
    return adb(*args).decode(errors="replace").strip()


def cleanup_background_apps(
    adb: Callable[..., bytes], *, required_packages: set[str],
    evidence_path: Path,
) -> dict[str, object]:
    """Stop a reviewed allowlist at an idle setup boundary; preserve app data.

    Both setup and check-only maintenance use this policy. Capture evidence even
    on failure, and refuse readiness if any targeted process remains alive.
    """
    report: dict[str, object] = {
        "policy": "allowlist_force_stop_user_0_no_data_clear",
        "ready": False, "packages": [], "started_at": time.time(),
    }
    try:
        conflict = BACKGROUND_STOP_ALLOWLIST & (
            BACKGROUND_PROTECTED_PACKAGES | set(required_packages)
        )
        if conflict:
            raise RuntimeError(f"Background cleanup targets protected apps: {sorted(conflict)}")
        installed = {
            line.removeprefix("package:").strip()
            for line in _text(adb, "shell", "pm", "list", "packages", "--user", "0").splitlines()
            if line.startswith("package:")
        }
        if not installed:
            raise RuntimeError("Cannot verify installed packages for background cleanup")

        def processes(package: str) -> list[str]:
            names = _text(adb, "shell", "ps", "-A", "-o", "NAME").splitlines()
            if not names or names[0].strip() != "NAME":
                raise RuntimeError("Cannot verify background processes: missing ps header")
            return [name.strip() for name in names[1:]
                    if name.strip() == package or name.strip().startswith(package + ":")]

        for package in sorted(BACKGROUND_STOP_ALLOWLIST):
            row = {"package": package, "installed": package in installed}
            report["packages"].append(row)
            if package not in installed:
                continue
            row["before"] = processes(package)
            adb("shell", "am", "force-stop", "--user", "0", package)
            row["force_stopped"] = True
            for attempt in range(3):
                row["after"] = processes(package)
                if not row["after"]:
                    break
                if attempt < 2:
                    time.sleep(0.25)
            if row["after"]:
                raise RuntimeError(f"Background app remained active after force-stop: {package}")
        report["ready"] = True
        return report
    except Exception as exc:
        report["error"] = repr(exc)
        raise
    finally:
        report["finished_at"] = time.time()
        atomic_json(evidence_path, report)


def wait_device_ready(
    adb: Callable[..., bytes], timeout_seconds: float = 180.0,
    poll_seconds: float = 2.0,
) -> dict[str, object]:
    """Wait for API 33 and services required by setup/scoring, or fail closed."""
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            services = {
                name: "found" in _text(
                    adb, "shell", "service", "check", name
                ).lower()
                for name in ("activity", "package", "window")
            }
            settings_value = _text(
                adb, "shell", "settings", "get", "system", "pointer_location"
            )
            last = {
                "adb_state": _text(adb, "get-state"),
                "api_level": _text(
                    adb, "shell", "getprop", "ro.build.version.sdk"
                ),
                "boot_completed": _text(
                    adb, "shell", "getprop", "sys.boot_completed"
                ),
                "system_server_pid": _text(
                    adb, "shell", "pidof", "system_server"
                ),
                "services": services,
                "settings_provider": settings_value != "",
            }
            if (
                last["adb_state"] == "device"
                and last["api_level"] == EXPECTED_API_LEVEL
                and last["boot_completed"] == "1"
                and bool(last["system_server_pid"])
                and all(services.values())
                and last["settings_provider"]
            ):
                return last
        except Exception as exc:  # device may disappear briefly while booting
            last = {"error": repr(exc)}
        time.sleep(poll_seconds)
    raise RuntimeError(f"Android device services did not become ready: {last}")


def reset_manage_external_storage(adb: Callable[..., bytes], package: str) -> None:
    """Restore the pristine AppOp expected by official onboarding flows."""
    # A package-level grant survives resetting only the UID mode. It makes
    # Android skip the permission page that official onboarding must visit.
    # Reset this one operation at both scopes; setup grants and verifies it
    # again before saving a baseline or starting any scored task.
    adb(
        "shell", "cmd", "appops", "set", package,
        "MANAGE_EXTERNAL_STORAGE", "default",
    )
    adb(
        "shell", "cmd", "appops", "set", "--uid", package,
        "MANAGE_EXTERNAL_STORAGE", "default",
    )


def verify_manage_external_storage(
    adb: Callable[..., bytes], package: str
) -> None:
    observed = _text(
        adb, "shell", "cmd", "appops", "get", package,
        "MANAGE_EXTERNAL_STORAGE",
    ).lower()
    if "manage_external_storage: allow" not in observed:
        raise RuntimeError(
            f"All-files access was not granted for {package}: {observed!r}"
        )


def dismiss_settings_residue(adb: Callable[..., bytes]) -> None:
    """Close special-access Settings pages that can stay above later apps."""
    adb("shell", "am", "force-stop", "com.android.settings")
    adb("shell", "input", "keyevent", "KEYCODE_HOME")


def verify_runner_contract(root: Path) -> dict[str, object]:
    matrix = json.loads((root / "matrix.json").read_text(encoding="utf-8"))
    if len(matrix) != 116:
        raise RuntimeError(f"Expected 116 runner records, got {len(matrix)}")
    incomplete = [
        {"ordinal": ordinal, "missing": sorted(REQUIRED_RUNNER_FIELDS - set(row))}
        for ordinal, row in enumerate(matrix)
        if REQUIRED_RUNNER_FIELDS - set(row)
    ]
    if incomplete:
        raise RuntimeError(f"Runner records are missing required fields: {incomplete[:3]}")
    required_files = (
        root / "frozen-params.pkl",
        root / "observation-contract.json",
        root / "runner/oracle/oracle.jar",
    )
    missing = [str(path.relative_to(root)) for path in required_files if not path.is_file()]
    if missing:
        raise RuntimeError(f"Runner contract files are missing: {missing}")
    return {"case_count": len(matrix), "required_fields": sorted(REQUIRED_RUNNER_FIELDS)}


def verify_pixels(adb: Callable[..., bytes]) -> dict[str, object]:
    started = time.monotonic()
    payload = adb("exec-out", "screencap", "-p")
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("ADB pixel capture did not return a PNG")
    width, height = struct.unpack(">II", payload[16:24])
    if width < 1 or height < 1:
        raise RuntimeError(f"Invalid screenshot size: {width}x{height}")
    return {
        "provider": "adb_screencap",
        "width": width,
        "height": height,
        "bytes": len(payload),
        "latency_s": time.monotonic() - started,
    }


def tree_node_count(node: object) -> int:
    if not isinstance(node, dict):
        return 0
    return 1 + sum(tree_node_count(child) for child in node.get("children") or [])


async def _verify_collector(root: Path) -> dict[str, object]:
    runtime = str(root / "runtime")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from driver.accessibility import AccessibilityCollectorClient

    client = AccessibilityCollectorClient("emulator-5554")
    started = time.monotonic()
    try:
        health = await client.health(timeout=3.0)
        if not health.get("ready"):
            raise RuntimeError(f"Collector health failed: {health}")
        warm = await client.warm(timeout=5.0)
        if not warm.get("ready"):
            raise RuntimeError(f"Collector warm failed: {warm}")
        snapshot = await client.fetch_primary(timeout=10.0)
        tree = snapshot.to_raw_tree(elapsed_ms=(time.monotonic() - started) * 1000)
        capture = tree.get("_capture") or {}
        if capture.get("complete") is not True:
            raise RuntimeError(f"Collector snapshot incomplete: {capture}")
        nodes = tree_node_count(tree)
        if nodes <= 1:
            raise RuntimeError("Collector snapshot has no window/content nodes")
        return {
            "ready": True,
            "latency_s": time.monotonic() - started,
            "node_count": nodes,
            "capture": capture,
        }
    finally:
        await client.close()


def verify_collector(root: Path) -> dict[str, object]:
    return asyncio.run(_verify_collector(root))


@contextmanager
def stable_official_setup_clicks(
    tools_module, actuation_module, adb_utils_module, current_app=None,
) -> Iterator[None]:
    """Keep official onboarding clicks tied to the UI dump that chose them.

    Upstream ``find_and_click_element`` finds an index in one UI dump and then
    obtains another dump before executing it. On animated onboarding screens
    that can reuse or remove the index. This scoped replacement preserves the
    official text matching but taps the bounds from the same observation.
    """
    controller_type = tools_module.AndroidToolController
    original = controller_type.click_element

    def click_element(self, element_text: str):
        # A cold app launch can exceed upstream's ten-second lookup window on a
        # loaded Windows host. Keep the retry bounded, but allow the UI to settle.
        last_distance = None
        last_texts: list[str] = []
        for launch_attempt in range(2):
            deadline = time.monotonic() + SETUP_ELEMENT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                elements = self._env.get_ui_elements()
                last_texts = [
                    value
                    for element in elements
                    for value in (element.text, element.content_description)
                    if value
                ]
                app_name = current_app() if callable(current_app) else None
                target_text = element_text
                if app_name == "chrome":
                    # Chrome 109 can use the combined welcome/sign-in page.
                    # Preserve official no-account/no-notifications semantics.
                    if (element_text == "Accept & continue"
                            and "Welcome to Chrome" in last_texts
                            and "Use without an account" in last_texts):
                        target_text = "Use without an account"
                    elif (element_text == "No thanks"
                          and "No thanks" not in last_texts
                          and {"Search or type web address", "Switch or close tabs",
                               "More options"}.issubset(last_texts)):
                        print("SETUP_OPTIONAL_PROMPT_ABSENT", "chrome", element_text,
                              "postcondition", "new_tab_page", flush=True)
                        return
                index, distance = actuation_module._find_target_element(
                    elements, target_text, False
                )
                last_distance = distance
                if distance <= 1 and 0 <= index < len(elements):
                    bounds = elements[index].bbox_pixels
                    if bounds is None:
                        raise ValueError(f'Bbox is missing for target "{element_text}"')
                    x, y = bounds.center
                    adb_utils_module.tap_screen(int(x), int(y), self._env)
                    return
                time.sleep(0.25)
            app_name = current_app() if callable(current_app) else None
            launcher_visible = any(
                marker in last_texts for marker in ("Google app", "Voice search", "Predicted app: Markor")
            )
            if launch_attempt == 0 and app_name and launcher_visible:
                print("SETUP_RELAUNCH", app_name, "for", element_text, flush=True)
                adb_utils_module.launch_app(app_name, self._env)
                continue
            break
        markers = OPTIONAL_SETUP_POSTCONDITIONS.get(element_text, frozenset())
        if markers.intersection(last_texts):
            print(
                "SETUP_OPTIONAL_PROMPT_ABSENT", element_text,
                "postcondition", sorted(markers.intersection(last_texts)),
                flush=True,
            )
            return
        raise ValueError(
            f'Target text "{element_text}" not found (distance={last_distance}, '
            f"visible_text={last_texts!r})"
        )

    controller_type.click_element = click_element
    try:
        yield
    finally:
        controller_type.click_element = original


def select_setup_apps(all_required: set[str], requested: list[str] | None) -> set[str]:
    """Select explicit official dependencies; None retains the full setup mode."""
    if requested is None:
        return set(all_required)
    if not requested or any(not isinstance(name, str) or not name for name in requested):
        raise ValueError("Scoped setup requires nonempty official app names")
    unknown = set(requested) - all_required
    if unknown:
        raise ValueError(f"Unknown scoped setup apps: {sorted(unknown)}")
    return set(requested) | {"android world"}


def missing_task_baselines(app_names, env) -> list[str]:
    """Read actual device state before initialize; never trust an old ready file."""
    from android_world.env.setup_device import setup
    from android_world.utils import app_snapshot, file_utils

    available = {app.app_name: app for app in setup._APPS}
    required = set(app_names) | {"android world"}
    unknown = required - set(available)
    if unknown:
        raise RuntimeError(f"Unknown task app dependencies: {sorted(unknown)}")
    missing = []
    for name in sorted(required):
        app = available[name]
        files, directories = required_external_resources(name, app, file_utils)
        if (not setup.is_package_installed(app.package_name(), env)
                or not file_utils.check_directory_exists(app_snapshot._snapshot_path(name), env.controller)
                or any(not file_utils.check_file_exists(path, env.controller) for path in files)
                or any(not file_utils.check_directory_exists(path, env.controller) for path in directories)):
            missing.append(name)
    return missing


def main(root: Path, adb_path: Path, *, check_only: bool = False,
         apps: list[str] | None = None) -> Path:
    root = root.resolve()
    refuse_active_batch(root)
    evidence_dir = new_evidence_dir(root)
    print("ENVIRONMENT_EVIDENCE", evidence_dir, flush=True)
    sys.path[:0] = [str(root / "runner"), str(root / "latest-shallow")]

    import run_emulator as runner
    from device_settings import capture, restore
    from scoring import read_fresh_forest
    from android_world import registry
    from android_world.env import actuation, adb_utils, android_world_controller, tools
    from android_world.env.setup_device import setup
    from android_world.task_evals.information_retrieval.information_retrieval import (
        InformationRetrieval,
    )
    from android_world.utils import app_snapshot, file_utils

    runner.ADB = shutil.which(str(adb_path)) or str(adb_path.resolve())
    env = None
    saved = None
    report: list[dict[str, object]] = []
    ready: dict[str, object] | None = None
    failure: BaseException | None = None
    failure_traceback = None
    restoration: object = {"attempted": False}
    try:
        ready = wait_device_ready(runner.adb)
        atomic_json(evidence_dir / "device-ready.json", ready)
        runner_contract = verify_runner_contract(root)
        atomic_json(evidence_dir / "runner-contract.json", runner_contract)
        saved = capture(runner.adb)
        atomic_json(evidence_dir / "device-before.json", saved)

        env = runner.load_env(android_world_controller.A11yMethod.UIAUTOMATOR)
        # AndroidEnv initialization can reapply DeviceSettingsConfig defaults.
        runner.hide_pointer_location()

        classes = registry.TaskRegistry().get_registry("android_world")
        required = {
            app
            for cls in classes.values()
            for app in (
                cls({}).app_names
                if issubclass(cls, InformationRetrieval)
                else cls.app_names
            )
        }
        required.add("android world")
        if len(required) != EXPECTED_APP_COUNT:
            raise RuntimeError(
                f"Expected {EXPECTED_APP_COUNT} official app baselines, got "
                f"{len(required)}: {sorted(required)}"
            )
        available = {app.app_name: app for app in setup._APPS}
        unknown = sorted(required - set(available))
        if unknown:
            raise RuntimeError(f"No official setup definition for: {unknown}")

        required = select_setup_apps(required, apps)
        atomic_json(evidence_dir / "setup-scope.json", {
            "mode": "full" if apps is None else "scoped",
            "requested_apps": apps, "required_apps": sorted(required),
        })

        required_packages = {available[name].package_name() for name in required}
        cleanup_background_apps(
            runner.adb, required_packages=required_packages,
            evidence_path=evidence_dir / "background-cleanup-before.json",
        )
        setup_context = {"app": None}
        with stable_official_setup_clicks(
            tools, actuation, adb_utils,
            current_app=lambda: setup_context["app"],
        ):
            for name in sorted(required):
                setup_context["app"] = name
                app = available[name]
                package = app.package_name()
                installed = setup.is_package_installed(package, env)
                snapshot_path = app_snapshot._snapshot_path(name)
                snapshot_ready = file_utils.check_directory_exists(
                    snapshot_path, env.controller
                )
                required_assets, required_directories = required_external_resources(
                    name, app, file_utils)
                assets_ready = all(
                    file_utils.check_file_exists(path, env.controller)
                    for path in required_assets
                ) and all(
                    file_utils.check_directory_exists(path, env.controller)
                    for path in required_directories
                )
                repair_needed = (
                    not installed or not snapshot_ready or not assets_ready
                )
                print(
                    "SETUP_CHECK", name, "installed", installed,
                    "snapshot", snapshot_ready, "assets", assets_ready,
                    "repair", repair_needed, flush=True,
                )

                if check_only and repair_needed:
                    raise RuntimeError(f"Environment check requires repair: {name}")
                if not installed:
                    setup.maybe_install_app(app, env)
                if repair_needed:
                    # setup.setup_app() swallows onboarding ValueError and can save
                    # a partial snapshot. Run official setup directly and fail.
                    if name in MANAGE_EXTERNAL_STORAGE_APPS:
                        reset_manage_external_storage(runner.adb, package)
                    dismiss_settings_residue(runner.adb)
                    try:
                        app.setup(env)
                        if name in MANAGE_EXTERNAL_STORAGE_APPS:
                            verify_manage_external_storage(runner.adb, package)
                    finally:
                        # Several official setup flows open Android Settings;
                        # closing only the task package can leave that foreign
                        # screen in front of the next app's onboarding.
                        try:
                            adb_utils.close_app(name, env.controller)
                        finally:
                            dismiss_settings_residue(runner.adb)
                    for path in required_assets:
                        if not file_utils.check_file_exists(path, env.controller):
                            raise RuntimeError(f"Missing required {name} asset: {path}")
                    app_snapshot.save_snapshot(name, env.controller)

                if not setup.is_package_installed(package, env):
                    raise RuntimeError(f"Package missing after setup: {package}")
                if not file_utils.check_directory_exists(
                    snapshot_path, env.controller
                ):
                    raise RuntimeError(f"Snapshot missing after setup: {name}")
                app_snapshot.restore_snapshot(name, env.controller)
                launcher_report = None
                if name == "open tracks sports tracker":
                    launcher_report = verify_opentracks_launcher(
                        runner.adb, evidence_dir, allow_onboarding=not check_only)
                    if launcher_report["onboarding_clicks"]:
                        # Save only restored baseline data plus completed setup;
                        # no task has been initialized at this boundary.
                        app_snapshot.save_snapshot(name, env.controller)
                        app_snapshot.restore_snapshot(name, env.controller)
                        launcher_report["after_restore"] = verify_opentracks_launcher(
                            runner.adb, evidence_dir, allow_onboarding=False)
                if name == "contacts":
                    launcher_report = verify_contacts_launcher(
                        runner.adb, evidence_dir, allow_onboarding=not check_only)
                    if not check_only:
                        app_snapshot.save_snapshot(name, env.controller)
                        app_snapshot.restore_snapshot(name, env.controller)
                        launcher_report["after_restore"] = verify_contacts_launcher(
                            runner.adb, evidence_dir, allow_onboarding=False)
                for path in required_assets:
                    if not file_utils.check_file_exists(path, env.controller):
                        raise RuntimeError(f"Required asset missing after restore: {path}")
                for path in required_directories:
                    if not file_utils.check_directory_exists(path, env.controller):
                        raise RuntimeError(f"Required directory missing after restore: {path}")
                dismiss_settings_residue(runner.adb)

                row = {
                    "app": name,
                    "package": package,
                    "snapshot": snapshot_path,
                    "required_assets": required_assets,
                    "required_directories": required_directories,
                    "repaired": repair_needed,
                    "ready": True,
                }
                if launcher_report is not None:
                    row["launcher"] = launcher_report
                report.append(row)
                atomic_json(evidence_dir / "apps-progress.json", report)
                print("SETUP_READY", name, flush=True)

        background_cleanup = cleanup_background_apps(
            runner.adb, required_packages=required_packages,
            evidence_path=evidence_dir / "background-cleanup-after.json",
        )
        adb_utils.press_home_button(env.controller)
        pixels = verify_pixels(runner.adb)
        atomic_json(evidence_dir / "pixels.json", pixels)
        collector = verify_collector(root)
        atomic_json(evidence_dir / "collector.json", collector)
        with tempfile.TemporaryDirectory(
            prefix="androidworld-oracle-health-"
        ) as temp:
            oracle_evidence: list[dict[str, object]] = []
            forest = read_fresh_forest(
                env.controller, Path(temp), oracle_evidence
            )
            windows = len(forest.windows)
            if windows < 1:
                raise RuntimeError("Official oracle returned an empty forest")
            atomic_json(evidence_dir / "oracle-attempts.json", oracle_evidence)

        runner.hide_pointer_location()
        pointer = _text(
            runner.adb, "shell", "settings", "get", "system", "pointer_location"
        )
        if pointer != "0":
            raise RuntimeError(f"pointer_location was not disabled: {pointer!r}")
        payload = {
            "schema_version": 4,
            "ready": True,
            "mode": "check" if check_only else "repair",
            "completed_at": time.time(),
            "device": "emulator-5554",
            "device_ready": ready,
            "api_level": int(EXPECTED_API_LEVEL),
            "pointer_location": 0,
            "scoring_forest_windows": windows,
            "collector": collector,
            "pixels": pixels,
            "runner_contract": runner_contract,
            "background_cleanup": background_cleanup,
            "setup_scope": "full" if apps is None else "scoped",
            "required_apps": sorted(required),
            "expected_app_count": len(required),
            "official_app_count": EXPECTED_APP_COUNT,
            "apps": report,
        }
        atomic_json(evidence_dir / "result.json", payload)
        # Initial setup creates the immutable batch prerequisite. A later repair
        # retains that historical file and publishes only new repair evidence.
        write_json_once(root / "setup-complete.json", payload)
        atomic_json(
            root / "environment-repair-latest.json",
            {"status": "ready", "evidence": str(evidence_dir), "result": payload},
        )
        print(
            "ENVIRONMENT_READY", len(report), "apps", windows,
            "forest windows", flush=True,
        )
    except BaseException as exc:
        failure = exc
        failure_traceback = exc.__traceback__
        (evidence_dir / "error.txt").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        atomic_json(
            root / "environment-repair-latest.json",
            {"status": "failed", "evidence": str(evidence_dir), "error": repr(exc)},
        )
    finally:
        cleanup_errors: list[str] = []
        if env is not None:
            try:
                env.close()
            except Exception:
                cleanup_errors.append(traceback.format_exc())
        if saved is not None:
            try:
                restoration = restore(runner.adb, saved)
            except Exception:
                cleanup_errors.append(traceback.format_exc())
        try:
            runner.hide_pointer_location()
        except Exception:
            cleanup_errors.append(traceback.format_exc())
        atomic_json(evidence_dir / "restoration.json", restoration)
        if cleanup_errors:
            (evidence_dir / "cleanup-error.txt").write_text(
                "\n---\n".join(cleanup_errors), encoding="utf-8"
            )
            atomic_json(
                root / "environment-repair-latest.json",
                {
                    "status": "failed",
                    "evidence": str(evidence_dir),
                    "error": "environment setup cleanup/restoration failed",
                },
            )
            if failure is None:
                failure = RuntimeError(
                    f"Environment setup cleanup failed; inspect {evidence_dir}"
                )
                failure_traceback = failure.__traceback__

    if failure is not None:
        raise failure.with_traceback(failure_traceback)
    return evidence_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent,
        help="Prepared batch containing runner/ and latest-shallow/.",
    )
    parser.add_argument(
        "--adb", type=Path, default=Path("adb")
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Verify every invariant without provisioning missing baselines.",
    )
    parser.add_argument(
        "--app", action="append", dest="apps",
        help="Official app name to prepare; repeat for all task dependencies. Omit for all apps.",
    )
    args = parser.parse_args()
    main(args.root, args.adb, check_only=args.check_only, apps=args.apps)
