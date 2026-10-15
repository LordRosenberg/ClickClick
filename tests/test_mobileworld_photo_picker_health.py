from subprocess import CompletedProcess
from unittest.mock import Mock

from evaluation.mobileworld.long_run_health import MobileWorldTarget
from evaluation.mobileworld import photo_picker_health as picker


def test_preparation_avoids_other_apps_and_platforms(monkeypatch):
    read = Mock(return_value=b"35\n")
    run = Mock()
    monkeypatch.setattr(picker.subprocess, "check_output", read)
    monkeypatch.setattr(picker.subprocess, "run", run)
    assert picker.prepare_photo_picker(MobileWorldTarget(), ["Calendar"])["ready"]
    read.assert_not_called()
    assert not picker.prepare_photo_picker(MobileWorldTarget(), ["Mattermost"])["required"]
    run.assert_not_called()


def test_preparation_retries_initial_lookup_without_touching_images(monkeypatch):
    monkeypatch.setattr(picker.subprocess, "check_output", Mock(side_effect=[b"34", b"0"]))
    calls = []
    def stat(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return CompletedProcess(command, 1, b"", b"Bad address")
        return CompletedProcess(command, 0, b"directory\n", b"")
    monkeypatch.setattr(picker.subprocess, "run", stat)
    row = picker.prepare_photo_picker(MobileWorldTarget(), ["Mattermost"])
    assert row["ready"] and row["checked"][0]["attempts"] == 2
    assert len(calls) == 5
    for command in calls:
        assert command[6:10] == ["shell", "stat", "-c", "%F"]
        assert command[-1].startswith("/storage/emulated/0/.transforms/synthetic/picker")
        assert not command[-1].endswith(".jpg")


def test_persistent_lookup_error_is_invalid_environment(monkeypatch):
    monkeypatch.setattr(picker.subprocess, "check_output", Mock(side_effect=[b"34", b"0"]))
    run = Mock(return_value=CompletedProcess([], 1, b"", b"EFAULT"))
    monkeypatch.setattr(picker.subprocess, "run", run)
    row = picker.prepare_photo_picker(MobileWorldTarget(), ["Mattermost"])
    assert not row["ready"] and row["failure"] == "photo_picker_directory_unavailable"
    assert run.call_count == 2


def test_bad_picker_environment_stops_before_model_or_score(monkeypatch, tmp_path):
    import sys
    from evaluation.mobileworld import run_single as single
    events, results = [], []
    monkeypatch.setattr(sys, "argv", ["run_single", "--task", "AttachmentTask",
        "--collector-apk", "a.apk", "--ime-apk", "b.apk", "--data-dir", str(tmp_path),
        "--no-recording"])
    for name in ("_verify_device", "verify_official_session_fix", "verify_official_task_fix"):
        monkeypatch.setattr(single, name, lambda *a: None)
    for name in ("verify_mattermost_readiness_fix", "ensure_task_asset_route",
                 "inspect_initialization_errors", "inspect_task_asset_access",
                 "prepare_fixture_context"):
        monkeypatch.setattr(single, name, lambda *a: {"ready": True})
    def request(method, url, **kwargs):
        events.append(url.rsplit("/", 1)[-1])
        return {"metadata": {"apps": ["Mattermost"]}, "goal": "original"}.get(events[-1], "OK")
    def prepare(*a):
        assert events[-1] == "init"
        return {"ready": False, "failure": "photo_picker_directory_unavailable"}
    monkeypatch.setattr(single, "_request", request)
    monkeypatch.setattr(single, "prepare_photo_picker", prepare)
    monkeypatch.setattr(single, "_run_clickclick", Mock(side_effect=AssertionError("model must not start")))
    monkeypatch.setattr(single, "_write_result", lambda args, row: results.append(row))
    single.main()
    assert events[-1] == "tear_down" and "eval" not in events
    assert results[0]["score"] is None
    assert results[0]["environment_status"] == "invalid_initialization"
