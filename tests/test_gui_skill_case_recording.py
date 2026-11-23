"""Verify recording itself stays isolated and registration requires a witnessed initial state."""

from __future__ import annotations

from copy import deepcopy

import pytest

from agent.skills.script_runner import ScriptRunResult
from agent.skills.script_schema import GuiScript, script_fingerprint
from evaluation import gui_skill_case_recording as recording
from evaluation.gui_script_case import CaseDefinition
from perception.observation import ObservationPackage
from shared.schemas import CanonicalUI, ObservationMode, TaskStatus, UIElement


APP = "com.example.countdown"


def present(text):
    return {"type": "target_present", "target": {"text": text}}


def definitions(branch_expected=True):
    script = GuiScript.model_validate({
        "script_id": "prepare-countdown", "app_id": APP, "initial": [present("Timer")],
        "steps": [{"id": "open", "action": {"type": "tap", "target": {"text": "Timer"}},
                   "before": [present("Timer")], "after": [present("Keypad")]}],
        "final": [present("Keypad")],
    })
    request = recording.RecordingCaseRequest.model_validate({
        "app_id": APP, "build": "47", "setup_goal": "Open the timer keypad.",
        "goal": "Set a two-minute timer without starting it.",
        "result": [present("02:00")], "guards": [present("Start")],
        "branch": [present("00:02")], "branch_action": "tap", "branch_after": [present("02:00")],
        "branch_expected": branch_expected,
    })
    return request, CaseDefinition(setup=script, **request.model_dump(exclude={"setup_goal", "app_id"}))


def package(*labels):
    nodes = [UIElement(index=-1, interactable=False, text=label, role="android.widget.TextView",
                       bounds=[0, 0, 20, 20]) for label in labels]
    return ObservationPackage(
        CanonicalUI(app_id=APP, activity=".Main", semantic_tree=nodes, capture_complete=True),
        ObservationMode.TREE_ONLY, "", None, None, [], accepted=True,
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    seen = []
    snapshots = []

    class Driver:
        async def read_app_build(self, app_id):
            assert app_id == APP
            return {"package": app_id, "version_code": "47"}

    async def restore(adb, serial, snapshot):
        snapshots.append((adb, serial, snapshot))

    async def on_driver(settings, serial, run, **options):
        seen.append((settings.data_dir, options.get("full_resolution")))
        return await run(Driver())

    monkeypatch.setattr(recording, "restore_snapshot", restore)
    monkeypatch.setattr(recording, "with_android_driver", on_driver)
    options = dict(adb="adb", serial="emulator-5556", avd="SkillQA", snapshot="clean",
                   env_file=None, model="test-model", rounds=4, seconds=8, fixture_seconds=4)
    return options, seen, snapshots


@pytest.mark.parametrize("labels,expected", [
    (["Keypad", "00:02", "Start"], True),
    (["Keypad", "00:00", "Start"], False),
])
async def test_verify_unassisted_setup_binds_the_exact_case_and_branch(tmp_path, monkeypatch, harness, labels, expected):
    options, _, snapshots = harness
    _, case = definitions(branch_expected=expected)

    async def replay(script, **kwargs):
        assert script == case.setup and kwargs["fixture"]["snapshot"] == "clean"
        assert "assist" not in kwargs
        return {"status": "succeeded", "task_id": "replay"}

    async def observe(*_):
        return package(*labels)

    monkeypatch.setattr(recording, "replay_owned", replay)
    monkeypatch.setattr(recording.AndroidSnapshotFixture, "_observe", observe)
    monkeypatch.setattr(recording, "_verified_run", lambda *_: (
        ScriptRunResult(script_hash=script_fingerprint(case.setup), script_id=case.setup.script_id,
                        revision=1, status="succeeded", final_asserted=True, reason="asserted"), {},
    ))
    evidence = await recording.verify_case(case, output=tmp_path / "verify", asset_root=tmp_path, **options)
    assert evidence.case_hash == recording.case_fingerprint(case)
    assert evidence.branch_ready is expected and evidence.guards_passed
    assert not evidence.goal_already_met and evidence.conditions_met
    assert len(snapshots) == 2


@pytest.mark.parametrize("labels", [
    ["Keypad", "00:02", "Start", "02:00"],
    ["Keypad", "00:02"],
    ["Keypad", "00:01", "Start"],
])
async def test_completed_goal_missing_guard_or_wrong_branch_cannot_be_registered(
    tmp_path, monkeypatch, harness, labels,
):
    options, _, snapshots = harness
    _, case = definitions()

    async def replay(*_, **__):
        return {"status": "succeeded", "task_id": "replay"}

    async def observe(*_):
        return package(*labels)

    monkeypatch.setattr(recording, "replay_owned", replay)
    monkeypatch.setattr(recording.AndroidSnapshotFixture, "_observe", observe)
    monkeypatch.setattr(recording, "_verified_run", lambda *_: (
        ScriptRunResult(script_hash=script_fingerprint(case.setup), script_id=case.setup.script_id,
                        revision=1, status="succeeded", final_asserted=True, reason="asserted"), {},
    ))
    with pytest.raises(recording.CaseRecordingError, match="declared regression initial state"):
        await recording.verify_case(case, output=tmp_path / "verify", asset_root=tmp_path, **options)
    assert not (tmp_path / "verify" / "verification.json").exists()
    assert len(snapshots) == 2


async def test_uncertain_setup_replay_does_not_reset_or_publish(tmp_path, monkeypatch, harness):
    options, _, snapshots = harness
    _, case = definitions()

    async def replay(*_, **__):
        return {"status": "unknown", "task_id": "replay", "device_reconciliation_required": True}

    monkeypatch.setattr(recording, "replay_owned", replay)
    with pytest.raises(recording.CaseRecordingError) as raised:
        await recording.verify_case(case, output=tmp_path / "verify", asset_root=tmp_path, **options)
    assert raised.value.device_reconciliation_required
    assert len(snapshots) == 1


async def test_recording_runs_only_setup_with_opt_in_and_preserves_review(
    tmp_path, monkeypatch, harness,
):
    options, seen, snapshots = harness
    request, expected = definitions()
    baseline = tmp_path / "baseline-skills"
    baseline.mkdir()
    calls = []

    class Orchestrator:
        def __init__(self, db):
            self.db = db

        async def run_task(self, task_id):
            task = self.db.get_task(task_id)
            assert task.generate_test_script and task.instruction == request.setup_goal
            assert task.state.revisable.limits.device_actions == options["rounds"]
            calls.append("setup")
            self.db.update_task(task_id, status=TaskStatus.SUCCEEDED)

    fingerprint = script_fingerprint(expected.setup)
    review = {"script": expected.setup.model_dump(mode="json"), "script_hash": fingerprint,
              "review": {"decision": "accept", "script_hash": fingerprint},
              "provenance": {"goal": request.setup_goal}}

    async def author(task, *, settings, store, destination, budget):
        assert task.status is TaskStatus.SUCCEEDED and store.task_id == task.id
        assert destination == tmp_path / "record" / "draft" and budget.max_actions == 0
        calls.append("author")
        return {"status": "candidate", "candidate": deepcopy(review)}

    async def verify(case, **kwargs):
        assert case == expected and kwargs["setup_goal"] == request.setup_goal
        calls.append("verify")
        return "verified"

    monkeypatch.setattr(recording, "create_orchestrator", lambda db, *_, **__: Orchestrator(db))
    monkeypatch.setattr(recording, "author_mixed_script", author)
    monkeypatch.setattr(recording, "verify_case", verify)
    recorded = await recording.record_case(request, baseline_skills=baseline, output=tmp_path / "record", **options)
    assert calls == ["setup", "author", "verify"]
    assert recorded.definition == expected and recorded.candidate == review
    assert len(seen) == 1 and seen[0][1] is True
    assert len(snapshots) == 2
