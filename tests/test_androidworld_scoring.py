"""Reject stale oracle reads and narrowly accept the requested audio suffixes."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from evaluation.androidworld import scoring
from evaluation.androidworld.freeze_runner import freeze_scoring_runner


XML = '<hierarchy><node content-desc="Start" /></hierarchy>'


def response(output, status=1):
    return SimpleNamespace(status=status, error_message="", generic=SimpleNamespace(output=output.encode()))


@pytest.mark.parametrize("failure", ["idle", "status", "transport", "missing_file", "malformed", "empty"])
def test_bad_acquisition_never_returns_stale_tree(tmp_path, failure):
    calls = []
    evidence = []

    def request(args, env, timeout_sec):
        calls.append(args)
        if args[1] == "rm":
            return response("")
        if args[1] == "uiautomator":
            if failure == "idle":
                return response("ERROR: could not get idle state.")  # adb still OK!
            if failure == "status":
                return response("failure", status=2)
            if failure == "transport":
                raise TimeoutError("device timeout")
            return response(f"UI hierchary dumped to: {args[-1]}")
        if args[1] == "cat":
            if failure == "missing_file":
                return response("No such file", status=2)
            return response("broken XML" if failure == "malformed" else "<hierarchy />")
        raise AssertionError(args)

    with pytest.raises(scoring.OracleObservationError):
        scoring.fresh_uiautomator_dump(None, 30, request=request, ok_status=1,
                                      evidence=evidence, directory=tmp_path)
    assert not evidence[0]["accepted"]
    assert "error" in evidence[0]
    assert calls[-1][1] == "rm"
    assert all("/sdcard/window_dump.xml" not in call for call in calls)
    if failure in ("idle", "status", "transport"):
        assert not any(call[1] == "cat" for call in calls)


def test_new_path_per_read_and_fresh_wrong_state_is_usable(tmp_path):
    evidence = []

    def request(args, env, timeout_sec):
        if args[1] == "uiautomator":
            return response(f"UI hierchary dumped to: {args[-1]}")
        return response(XML if args[1] == "cat" else "")

    for _ in range(2):
        assert scoring.fresh_uiautomator_dump(None, 30, request=request, ok_status=1,
                                             evidence=evidence, directory=tmp_path) == XML
    assert evidence[0]["remote_path"] != evidence[1]["remote_path"]
    assert all(row["accepted"] for row in evidence)


@pytest.mark.parametrize("failure", [None, "wrong_nonce", "window_changed", "timeout", "missing_xml"])
def test_independent_noidle_dump_fails_closed_and_cleans_helper(tmp_path, failure):
    jar = tmp_path / "oracle.jar"
    jar.write_bytes(b"test helper")
    evidence, calls = [], []

    def request(args, env, timeout_sec):
        calls.append(args)
        if args[0] == "push" or args[1] == "rm":
            return response("")
        if args[1].startswith("CLASSPATH="):
            if failure == "timeout":
                return response("", status=2)
            if failure == "window_changed":
                return response("Active window changed during oracle read")
            return response("ORACLE_DUMP_OK " + ("stale" if failure == "wrong_nonce" else args[-1]))
        if args[1] == "cat":
            return response("" if failure == "missing_xml" else XML)
        raise AssertionError(args)

    if failure:
        with pytest.raises(scoring.OracleObservationError):
            scoring.fresh_uiautomator_dump(None, 30, request=request, ok_status=1,
                evidence=evidence, directory=tmp_path, oracle_jar=jar)
    else:
        assert scoring.fresh_uiautomator_dump(None, 30, request=request, ok_status=1,
            evidence=evidence, directory=tmp_path, oracle_jar=jar) == XML
    assert evidence[0]["accepted"] is (failure is None)
    assert calls[-1][1:3] == ["rm", "-f"]
    assert calls[-1][-1] == calls[0][-1]  # Always remove the per-read helper.
    assert not any("uiautomator" == token for args in calls for token in args)
    if failure in ("timeout", "window_changed", "wrong_nonce"):
        assert not any(args[1] == "cat" for args in calls)


@pytest.fixture
def oracle_modules(monkeypatch):
    class AudioRecorderRecordAudioWithFileName:
        params = {"file_name": "2023_04_26_talk.m4a"}
        create_file_task = SimpleNamespace(data_directory="/recordings")

        def is_successful(self, env):
            return float(self.params["file_name"] + ".m4a" in files)

    files = set()
    monkeypatch.setitem(sys.modules, "android_world.task_evals.single.audio_recorder",
                        SimpleNamespace(AudioRecorderRecordAudioWithFileName=AudioRecorderRecordAudioWithFileName))
    monkeypatch.setitem(sys.modules, "android_world.utils", SimpleNamespace(file_utils=SimpleNamespace(
        check_file_or_folder_exists=lambda name, directory, controller: name in files)))

    @contextmanager
    def observations(env, directory, evidence):
        yield

    monkeypatch.setattr(scoring, "fresh_oracle_observations", observations)
    return AudioRecorderRecordAudioWithFileName, files


@pytest.mark.parametrize("name,expected,raw", [
    ("2023_04_26_talk.m4a", 0, 0),
    ("2023_04_26_talk.m4a.m4a", 1, 1),
    ("2023_04_26_talk.m4a.m4a.m4a", 0, 0),
    ("2023_04_26_talk", 0, 0),
    ("2023_04_27_talk.m4a", 0, 0),
])
def test_audio_uses_unmodified_official_predicate(tmp_path, oracle_modules, name, expected, raw):
    cls, files = oracle_modules
    files.add(name)
    result = scoring.score_task(cls(), SimpleNamespace(controller=None), tmp_path, "final")
    assert result["score"] == expected
    assert result["raw_official_score"] == raw
    assert json.loads((tmp_path / "final-oracle/audit.json").read_text())["score"] == expected


def test_real_zero_and_acquisition_error_stay_distinct(tmp_path, oracle_modules):
    class Incomplete:
        def is_successful(self, env):
            return 0.0

    class Unobservable:
        def is_successful(self, env):
            raise scoring.OracleObservationError("idle timeout")

    assert scoring.score_task(Incomplete(), None, tmp_path, "incomplete")["score"] == 0
    with pytest.raises(scoring.OracleObservationError):
        scoring.score_task(Unobservable(), None, tmp_path, "unobservable")
    audit = json.loads((tmp_path / "unobservable-oracle/audit.json").read_text())
    assert "score" not in audit
    assert "idle timeout" in audit["error"]


def test_freeze_replaces_old_scoring_before_sealing_only(tmp_path):
    runtime = Path(__file__).resolve().parents[1]
    batch = tmp_path / "batch"
    (batch / "runner").mkdir(parents=True)
    (batch / "runner/run_emulator.py").write_text("stale runner")
    freeze_scoring_runner(runtime, batch)
    for name in ("scoring.py", "run_emulator.py", "agent_worker.py"):
        assert (batch / "runner" / name).read_bytes() == (runtime / "evaluation/androidworld" / name).read_bytes()
    assert (batch / "run_full.py").read_bytes() == (runtime / "evaluation/androidworld/run_full.py").read_bytes()
    (batch / "source-hashes.json").write_text("{}")
    with pytest.raises(RuntimeError, match="fresh, unsealed"):
        freeze_scoring_runner(runtime, batch)


def test_continue_only_for_excluded_oracle_failure_with_successful_cleanup():
    result = dict(infrastructure_failure='oracle_observation_failed', score=None,
                  valid=False, teardown_ok=True)
    assert scoring.excluded_oracle_error(result)
    for change in [dict(score=0), dict(valid=True), dict(teardown_ok=False),
                   dict(teardown_error='failed'), dict(infrastructure_failure='model_unavailable')]:
        assert not scoring.excluded_oracle_error({**result, **change})
