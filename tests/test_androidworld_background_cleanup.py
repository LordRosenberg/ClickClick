"""The setup cleanup may stop reviewed noise, never task/runtime dependencies."""
import json

import pytest

from evaluation.androidworld import setup_full


PACKAGE = "com.google.android.apps.youtube.music"


class FakeAdb:
    def __init__(self, *, installed=True, survives=False):
        self.installed = installed
        self.survives = survives
        self.stopped = False
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:4] == ("shell", "pm", "list", "packages"):
            return ("package:ai.clickclick.collector\n" +
                    (f"package:{PACKAGE}\n" if self.installed else "")).encode()
        if args[:2] == ("shell", "ps"):
            alive = not self.stopped or self.survives
            return ("NAME\nsystem_server\nai.clickclick.collector\n" +
                    (f"{PACKAGE}\n{PACKAGE}:worker\n" if alive else "")).encode()
        assert args == ("shell", "am", "force-stop", "--user", "0", PACKAGE)
        self.stopped = True
        return b""


def test_cleanup_stops_only_allowlist_preserves_data_and_records_evidence(tmp_path):
    adb = FakeAdb()
    path = tmp_path / "cleanup.json"
    result = setup_full.cleanup_background_apps(
        adb, required_packages={"de.dennisguse.opentracks"}, evidence_path=path)
    assert result["ready"]
    assert result["packages"][0]["before"] == [PACKAGE, PACKAGE + ":worker"]
    assert result["packages"][0]["after"] == []
    assert json.loads(path.read_text()) == result
    writes = [call for call in adb.calls if call[:2] == ("shell", "am")]
    assert writes == [("shell", "am", "force-stop", "--user", "0", PACKAGE)]
    # Idempotent even when no process is currently alive.
    assert setup_full.cleanup_background_apps(
        adb, required_packages=set(), evidence_path=path)["ready"]


def test_uninstalled_noise_is_skipped(tmp_path):
    adb = FakeAdb(installed=False)
    result = setup_full.cleanup_background_apps(
        adb, required_packages=set(), evidence_path=tmp_path / "cleanup.json")
    assert result["ready"]
    assert not adb.stopped


@pytest.mark.parametrize("conflict", [PACKAGE, "ai.clickclick.collector"])
def test_required_and_runtime_apps_are_protected_before_any_adb_call(tmp_path, monkeypatch, conflict):
    monkeypatch.setattr(setup_full, "BACKGROUND_STOP_ALLOWLIST", frozenset({conflict}))
    adb = FakeAdb()
    path = tmp_path / "cleanup.json"
    with pytest.raises(RuntimeError, match="protected"):
        setup_full.cleanup_background_apps(
            adb, required_packages={PACKAGE}, evidence_path=path)
    assert adb.calls == []
    assert json.loads(path.read_text())["ready"] is False


def test_surviving_background_process_fails_closed_with_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_full.time, "sleep", lambda _: None)
    path = tmp_path / "cleanup.json"
    with pytest.raises(RuntimeError, match="remained active"):
        setup_full.cleanup_background_apps(
            FakeAdb(survives=True), required_packages=set(), evidence_path=path)
    result = json.loads(path.read_text())
    assert result["ready"] is False
    assert result["packages"][0]["after"]


@pytest.mark.parametrize("command", ["pm", "ps", "am"])
def test_adb_failure_never_reports_ready(tmp_path, command):
    adb = FakeAdb()

    def failing_adb(*args):
        if args[1] == command:
            raise TimeoutError("adb timed out")
        return adb(*args)

    path = tmp_path / "cleanup.json"
    with pytest.raises(TimeoutError):
        setup_full.cleanup_background_apps(
            failing_adb, required_packages=set(), evidence_path=path)
    assert json.loads(path.read_text())["ready"] is False


def test_missing_process_output_is_not_mistaken_for_stopped(tmp_path):
    adb = FakeAdb()

    def empty_ps(*args):
        return b"" if args[1] == "ps" else adb(*args)

    with pytest.raises(RuntimeError, match="ps header"):
        setup_full.cleanup_background_apps(
            empty_ps, required_packages=set(), evidence_path=tmp_path / "cleanup.json")
    assert not adb.stopped
