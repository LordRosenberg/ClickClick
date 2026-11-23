"""Prepared Android fixtures must bind native build and before/after action facts."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.revisable.store import TaskStore
from agent.skills.script_schema import SemanticSelector
from evaluation import gui_script_case
from evaluation.gui_script_case import AndroidSnapshotFixture, CaseDefinition
from evaluation.gui_skill_regression import FixtureContext
from evaluation.skill_learning import EvaluationInfrastructureError
from perception.observation import ObservationPackage
from shared.protocol import AppBuild
from shared.schemas import CanonicalUI, ObservationMode, TaskStatus, UIElement


APP = "com.example.notebook"
NOTE_PATH = "/storage/emulated/0/Documents/Markor/SkillQA-Handbook.md"
NOTE_HASH = "a" * 64


@pytest.mark.parametrize("state", [b"offline", b"device"])
async def test_private_snapshot_recovers_selected_transport_after_wait_stalls(monkeypatch, state):
    serial = "emulator-5610"
    commands = []
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")
    monkeypatch.setattr(gui_script_case, "_PRIVATE_RESTORE_WAIT_SECONDS", 0.01)

    async def adb(executable, target, *arguments):
        assert (executable, target) == ("adb", serial)
        commands.append(arguments)
        if arguments == ("emu", "avd", "snapshot", "load", "clean"):
            return 0, b"OK\r\n"
        if arguments == ("wait-for-device",) and commands.count(arguments) == 1:
            await asyncio.Future()
        if arguments == ("devices",):
            return 0, b"List of devices attached\r\nemulator-5610\t" + state + b"\r\n\r\n"
        return 0, b""

    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    await gui_script_case.restore_snapshot("adb", serial, "clean")

    expected = [("emu", "avd", "snapshot", "load", "clean"), ("wait-for-device",), ("devices",)]
    if state == b"offline":
        expected.append(("reconnect", "offline"))
    assert commands == [*expected, ("wait-for-device",)]


@pytest.mark.parametrize("devices", [
    b"List of devices attached\nemulator-5610\toffline\nemulator-5554\tdevice\n",
    b"List of devices attached\nemulator-5610\tunauthorized\n",
    b"List of devices attached\nemulator-5554\toffline\n",
    b"List of devices attached\n",
    b"emulator-5610\toffline\n",
])
async def test_private_snapshot_never_reconnects_an_unproven_inventory(monkeypatch, devices):
    commands = []
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")

    async def adb(executable, target, *arguments):
        commands.append(arguments)
        if arguments == ("wait-for-device",):
            raise TimeoutError
        if arguments == ("devices",):
            return 0, devices
        return 0, b"OK\n"

    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    with pytest.raises(EvaluationInfrastructureError, match="single selected emulator"):
        await gui_script_case.restore_snapshot("adb", "emulator-5610", "clean")
    assert commands == [("emu", "avd", "snapshot", "load", "clean"), ("wait-for-device",), ("devices",)]


@pytest.mark.parametrize("socket,serial", [
    ("", "emulator-5610"), ("tcp:127.0.0.1:5037", "emulator-5610"),
    ("tcp:192.0.2.1:5062", "emulator-5610"), ("tcp:127.0.0.1:5062", "physical-device"),
])
async def test_snapshot_never_runs_private_recovery_on_an_ordinary_route(monkeypatch, socket, serial):
    commands = []
    monkeypatch.setenv("ADB_SERVER_SOCKET", socket)

    async def adb(executable, target, *arguments):
        commands.append(arguments)
        if arguments == ("wait-for-device",):
            raise TimeoutError
        return 0, b"OK\n"

    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    with pytest.raises(TimeoutError):
        await gui_script_case.restore_snapshot("adb", serial, "clean")
    assert commands == [("emu", "avd", "snapshot", "load", "clean"), ("wait-for-device",)]


@pytest.mark.parametrize("code,output", [(0, b"KO\n"), (1, b"OK\n")])
async def test_private_snapshot_still_requires_exact_console_ack(monkeypatch, code, output):
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")

    async def adb(executable, serial, *arguments):
        assert arguments == ("emu", "avd", "snapshot", "load", "clean")
        return code, output

    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    with pytest.raises(EvaluationInfrastructureError, match="snapshot restore failed"):
        await gui_script_case.restore_snapshot("adb", "emulator-5610", "clean")


def condition(text):
    return {"type": "target_present", "target": {"text": text}}


def observation(*labels, app=APP, activity=".Main", complete=True):
    nodes = [UIElement(index=-1, interactable=False, text=label, role="android.widget.TextView",
                       bounds=[0, 0, 20, 20]) for label in labels]
    ui = CanonicalUI(app_id=app, activity=activity, semantic_tree=nodes, capture_complete=complete)
    return ObservationPackage(ui, ObservationMode.TREE_ONLY, "", None, None, [], accepted=True)


class Driver:
    async def read_app_build(self, app_id) -> AppBuild:
        return {"package": app_id, "version_code": "146", "version_name": "2.10.9"}


@pytest.fixture
def snapshot_fixture(tmp_path, monkeypatch):
    definition = CaseDefinition.model_validate({
        "goal": "Rename the note", "build": "146",
        "setup": {
            "script_id": "rename", "app_id": APP,
            "initial": [{"type": "app_is", "app_id": APP}],
            "steps": [{"id": "setup", "before": [condition("Notes")],
                       "action": {"type": "tap", "target": {"text": "Open"}},
                       "after": [condition("Ready")]}],
            "final": [condition("Ready")],
        },
        "result": [condition("Renamed")], "guards": [condition("Preserved")],
        "branch": [condition("Rename here")], "branch_action": "tap",
        "branch_after": [condition("Name field")], "branch_expected": True,
    })
    output = tmp_path / "trial"
    output.mkdir()
    restored = []

    async def restore(adb, serial, snapshot):
        restored.append((serial, snapshot))

    async def with_driver(settings, serial, run, *, full_resolution=False):
        assert not full_resolution
        return await run(Driver())

    async def setup(*args, **kwargs):
        return {"status": "succeeded", "device_reconciliation_required": False}

    monkeypatch.setattr(gui_script_case, "restore_snapshot", restore)
    monkeypatch.setattr(gui_script_case, "with_android_driver", with_driver)
    monkeypatch.setattr(gui_script_case, "replay_owned", setup)
    fixture = AndroidSnapshotFixture([definition], adb="not-used", serial="emulator-5554", avd="fixture",
                                     snapshot="clean", env_file=tmp_path / "no-env", rounds=3, seconds=30)
    return fixture, FixtureContext(definition.prepared_case(), "arm", output, "model"), restored


async def test_build_reader_accepts_native_dict_and_requires_exact_package(snapshot_fixture):
    fixture, context, _ = snapshot_fixture
    assert await fixture._build(Driver(), context) == "146"

    class DifferentPackage(Driver):
        async def read_app_build(self, app_id):
            return {"package": "com.example.other", "version_code": "146", "version_name": "2.10.9"}

    with pytest.raises(EvaluationInfrastructureError, match="differs"):
        await fixture._build(DifferentPackage(), context)


@pytest.mark.parametrize("path,sha256", [
    ("/data/local/tmp/note.md", NOTE_HASH),
    ("/storage/emulated/0", NOTE_HASH),
    ("/storage/emulated/1/Documents/note.md", NOTE_HASH),
    ("/storage/emulated/0/../../data/note.md", NOTE_HASH),
    ("/storage/emulated/0/Documents/./note.md", NOTE_HASH),
    ("/storage/emulated/0/Documents//note.md", NOTE_HASH),
    ("/storage/emulated/0/Documents/$(touch note)", NOTE_HASH),
    ("/storage/emulated/0/Documents/note.md;id", NOTE_HASH),
    ("/storage/emulated/0/Documents/note.md\n", NOTE_HASH),
    (NOTE_PATH, NOTE_HASH.upper()),
])
def test_preserved_files_reject_noncanonical_paths_and_hashes(snapshot_fixture, path, sha256):
    fixture, context, _ = snapshot_fixture
    case = fixture._case(context).model_dump(mode="json")
    assert "preserved_files" not in case
    assert CaseDefinition.model_validate({**case, "preserved_files": {}}).model_dump(mode="json") == case
    with pytest.raises(ValueError, match="exact Android shared storage paths and lowercase SHA-256"):
        CaseDefinition.model_validate({**case, "preserved_files": {path: sha256}})


@pytest.mark.parametrize("final_response,observed,passed", [
    ((0, f"{NOTE_HASH}  {NOTE_PATH}\r\n".encode()), NOTE_HASH, True),
    ((0, f"{'b' * 64}  {NOTE_PATH}\n".encode()), "b" * 64, False),
    ((1, b""), None, False),
    ((0, f"{NOTE_HASH}  /storage/emulated/0/other.md\n".encode()), None, False),
    (TimeoutError(), None, False),
])
async def test_file_preservation_is_an_independent_pair_guard(
    snapshot_fixture, monkeypatch, final_response, observed, passed,
):
    fixture, context, _ = snapshot_fixture
    case = fixture._case(context)
    case.preserved_files[NOTE_PATH] = NOTE_HASH
    assert case.model_dump(mode="json")["preserved_files"] == {NOTE_PATH: NOTE_HASH}
    observations = iter([observation("Ready"), observation("Renamed", "Preserved")])
    replies = iter([(0, f"{NOTE_HASH}  {NOTE_PATH}\n".encode()), final_response])

    async def observe(driver, context):
        return next(observations)

    async def adb(executable, serial, *arguments):
        assert (executable, serial, arguments) == ("not-used", "emulator-5554", ("shell", "sha256sum", NOTE_PATH))
        response = next(replies)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")
    monkeypatch.setattr(fixture, "_observe", observe)
    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)

    preparation = await fixture.prepare(context)
    initial_report = json.loads((context.output / "initial-check.json").read_text())
    assert initial_report["file_preservation"] == {"expected": {NOTE_PATH: NOTE_HASH},
                                                   "observed": {NOTE_PATH: NOTE_HASH}, "passed": True}
    assert "file_preservation" not in initial_report["facts"]
    assessment = await fixture.assess(context, preparation, None)
    assert assessment.score and assessment.guards_passed is passed
    final_report = json.loads((context.output / "independent-check.json").read_text())
    assert assessment.file_preservation == final_report["file_preservation"] == {
        "expected": {NOTE_PATH: NOTE_HASH}, "before": {NOTE_PATH: NOTE_HASH},
        "after": {NOTE_PATH: observed}, "passed": passed,
    }


async def test_changed_file_after_preparation_prevents_ordinary_execution(snapshot_fixture, monkeypatch, tmp_path):
    fixture, context, _ = snapshot_fixture
    fixture._case(context).preserved_files[NOTE_PATH] = NOTE_HASH
    hashes = iter([NOTE_HASH, "b" * 64])

    async def observe(driver, context):
        return observation("Ready")

    async def adb(*args):
        return 0, f"{next(hashes)}  {NOTE_PATH}\n".encode()

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")
    monkeypatch.setattr(fixture, "_observe", observe)
    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    monkeypatch.setattr(gui_script_case, "create_orchestrator",
                        lambda *args, **kw: pytest.fail("ordinary work must not start after a file changed"))

    await fixture.prepare(context)
    with pytest.raises(EvaluationInfrastructureError, match="preserved files changed before ordinary execution"):
        await fixture.execute(context, tmp_path / "isolated-skills", 3, 30)


async def test_file_hashing_checks_the_private_transport_and_quotes_a_single_path(monkeypatch):
    path = "/storage/emulated/0/Documents/Markor/QA 笔记.md"
    calls = []

    async def adb(executable, serial, *arguments):
        calls.append((executable, serial, arguments))
        return 0, f"{NOTE_HASH}  {path}\n".encode()

    monkeypatch.setattr(gui_script_case, "_snapshot_adb", adb)
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5037")
    with pytest.raises(EvaluationInfrastructureError, match="selected private Android emulator"):
        await gui_script_case._shared_file_hashes("adb", "emulator-5554", [path])
    assert calls == []

    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:127.0.0.1:5062")
    assert await gui_script_case._shared_file_hashes("adb", "emulator-5554", [path]) == {path: NOTE_HASH}
    assert calls == [("adb", "emulator-5554", ("shell", "sha256sum", f"'{path}'"))]


@pytest.mark.parametrize("action,point,problem,expected", [
    ({"type": "tap", "index": 7}, None, None, True),
    ({"type": "tap", "index": 8}, None, None, False),
    ({"type": "tap", "index": 7}, None, "wrong_transaction", None),
    ({"type": "tap", "index": 7}, None, "duplicate_target", None),
    ({"type": "tap_xy", "x": 20, "y": 30}, (163.7, 67.8), None, True),
    ({"type": "tap_xy", "x": 20, "y": 30}, (1650, 67.8), None, False),
    ({"type": "tap_xy", "x": 20, "y": 30}, (163.7, 107), None, False),
    ({"type": "tap_xy", "x": 20, "y": 30}, (163.7, 67.8), "wrong_space", None),
    ({"type": "tap_xy", "x": 20, "y": 30}, (163.7, 67.8), "missing_driver_coords", None),
])
def test_explicit_touch_branch_requires_same_target_and_native_basis(snapshot_fixture, action, point, problem, expected):
    fixture, context, _ = snapshot_fixture
    source = fixture._case(context).model_dump(mode="json")
    assert "branch_touch_target" not in source
    selector = {"resource_id": "toolbar", "role": "ViewGroup"}
    with pytest.raises(ValueError, match="unique semantic selector"):
        CaseDefinition.model_validate({**source, "branch_action": "long_press", "branch_touch_target": selector})
    case = CaseDefinition.model_validate({**source, "branch_action": "tap_xy", "branch_touch_target": selector})
    before, after = observation("Ready"), observation("Name field")
    target = UIElement(index=7, role="ViewGroup", resource_id="toolbar", clickable=True,
                       bounds=[0, 29, 1700, 106])
    other = UIElement(index=8, role="ImageView", desc="More options", clickable=True,
                      bounds=[1642, 38, 1690, 96])
    before.ui.elements = [target, other]
    if problem == "duplicate_target":
        before.ui.elements.append(target.model_copy(update={"index": 9}))
    before.ui.semantic_tree = before.ui.elements.copy()
    receipt = {"transaction_id": "tx", "dispatch_succeeded": True, "observation_accepted": True,
               "effect_observation_id": after.observation_id, "effect_outcome": "confirmed"}
    native = {**receipt, "transaction_id": "wrong"} if problem == "wrong_transaction" else receipt
    tick = {"action": action, "action_result": {"receipt": native, "success": True}}
    if point is not None:
        coords = dict(zip(("x", "y"), point))
        tick["action"] = {**action, **coords}
        tick["action_pipeline"] = {
            "basis_observation_id": before.observation_id,
            "coordinate_space_id": "wrong" if problem == "wrong_space" else before.coordinate_space_id,
            "driver_coordinates": coords,
            "stages": [
                {"stage": "submitted", "observation_id": before.observation_id, "action": action},
                {"stage": "dispatched", "coordinate_space": "device", "action": tick["action"]},
            ],
        }
        if problem != "missing_driver_coords":
            tick["action_result"]["detail"] = {"dispatched_coordinates": coords}

    assert gui_script_case._branch_touch_matches(case, before, after, action, receipt, tick) is expected


@pytest.mark.parametrize("after,action_type,expected,touch_target", [
    ("Name field", "tap", True, False),
    ("File list", "tap", False, False),
    (None, "tap", None, False),
    ("Name field", "scroll", False, False),
    ("Name field", "tap", True, True),
])
async def test_branch_requires_matching_action_and_linked_post_before_scoring(
    snapshot_fixture, monkeypatch, tmp_path, after, action_type, expected, touch_target,
):
    fixture, context, _ = snapshot_fixture
    if touch_target:
        case = fixture._case(context)
        case.branch_action = "tap_xy"
        case.branch_touch_target = SemanticSelector(resource_id="toolbar")
    observations = iter([observation("Ready"), observation("Ready"), observation("Renamed", "Preserved")])

    async def observe(driver, context):
        return next(observations)

    monkeypatch.setattr(fixture, "_observe", observe)
    preparation = await fixture.prepare(context)
    assert preparation.conditions_met is True and not preparation.goal_already_met

    class OrdinaryOrchestrator:
        def __init__(self, db, artifacts):
            self.db = db
            self.artifacts = artifacts
            db.artifacts = artifacts

        @staticmethod
        def _persist_observation_files(package):
            return None

        async def run_task(self, task_id):
            before = observation("Rename here")
            post = observation(after) if after is not None else None
            action = {"type": action_type}
            receipt = {"dispatch_succeeded": True, "observation_accepted": True,
                       "observation_id": post.observation_id if post else "unavailable"}
            if touch_target:
                control = UIElement(index=7, resource_id="toolbar", clickable=True, bounds=[0, 0, 20, 20])
                before.ui.elements = [control]
                before.ui.semantic_tree.append(control)
                action["index"] = control.index
                receipt.update(transaction_id="dispatch", effect_outcome="confirmed")
                gui_script_case.TraceWriter(self.db, self.artifacts).write(
                    task_id, kind="executor_tick", message="tap finished", payload={
                        "decision": "act", "action": action, "observation_accepted": True,
                        "basis_observation_id": before.observation_id, "observation_id": before.observation_id,
                        "action_result": {"success": True, "receipt": receipt},
                    },
                )
            self._persist_observation_files(before)
            if post is not None:
                self._persist_observation_files(post)
            TaskStore(self.db, self.artifacts, task_id).put("event", "1", {
                "step": 1, "observation_id": before.observation_id, "submitted_action": action,
                "action_result": {"receipt": receipt},
            })
            self.db.update_task(task_id, status=TaskStatus.SUCCEEDED)

    monkeypatch.setattr(gui_script_case, "create_orchestrator",
                        lambda db, *args, artifacts, **kwargs: OrdinaryOrchestrator(db, artifacts))
    monkeypatch.setattr(gui_script_case, "needs_reconciliation", lambda *args: False)
    skills = tmp_path / "isolated-skills"
    skills.mkdir()
    execution = await fixture.execute(context, skills, 3, 30)
    assessment = await fixture.assess(context, preparation, execution)

    assert execution.completed and assessment.score and assessment.guards_passed
    assert assessment.branch_observed is expected


async def test_pre_execution_recheck_rejects_changed_observed_initial_activity(snapshot_fixture, monkeypatch, tmp_path):
    fixture, context, _ = snapshot_fixture
    observations = iter([observation("Ready"), observation("Ready", activity=".Unexpected")])

    async def observe(driver, context):
        return next(observations)

    monkeypatch.setattr(fixture, "_observe", observe)
    monkeypatch.setattr(gui_script_case, "create_orchestrator",
                        lambda *args, **kw: pytest.fail("ordinary work must not start after state mismatch"))
    await fixture.prepare(context)
    with pytest.raises(EvaluationInfrastructureError, match="initial state changed"):
        await fixture.execute(context, tmp_path / "isolated-skills", 3, 30)


async def test_unknown_setup_effect_quarantines_fixture_before_any_second_restore(snapshot_fixture, monkeypatch):
    fixture, context, restored = snapshot_fixture

    async def setup(*args, **kwargs):
        return {"status": "failed", "device_reconciliation_required": True}

    monkeypatch.setattr(gui_script_case, "replay_owned", setup)
    with pytest.raises(EvaluationInfrastructureError, match="setup did not complete independently"):
        await fixture.prepare(context)
    cleanup = await fixture.cleanup(context)
    assert cleanup.verified_clean is False
    assert restored == [("emulator-5554", "clean")]
    with pytest.raises(EvaluationInfrastructureError, match="requires reconciliation"):
        await fixture.prepare(context)
