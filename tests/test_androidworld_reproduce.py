import json
from pathlib import Path

import pytest

from evaluation.androidworld import fixture_codec, reproduce


def test_published_instances_are_complete_and_hash_pinned():
    path = reproduce.HERE / "fixtures/instances.json"
    assert reproduce.fixture_digest(path) == reproduce.FIXTURE_SHA256
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["records"]
    assert len(rows) == len({r["task"] for r in rows}) == 116
    assert all(r["max_seconds"] == 900 and r["max_steps"] > 0 for r in rows)
    assert data["upstream_commit"] == reproduce.UPSTREAM


def test_fixture_pin_accepts_windows_checkout_line_endings(tmp_path):
    content = (reproduce.HERE / "fixtures/instances.json").read_text(encoding="utf-8")
    path = tmp_path / "instances.json"
    path.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
    assert reproduce.fixture_digest(path) == reproduce.FIXTURE_SHA256


def test_fixture_codec_preserves_image_pixels_and_rejects_executable_types():
    from PIL import Image
    image = Image.new("RGB", (3, 2), (31, 44, 55))
    encoded = fixture_codec.encode({"values": (1, "text"), "image": image})
    restored = fixture_codec.decode(encoded)
    assert restored["values"] == (1, "text")
    assert restored["image"].tobytes() == image.tobytes()
    with pytest.raises(ValueError, match="Unknown fixture type"):
        fixture_codec.decode({"$type": "os.system", "command": "anything"})


def test_runtime_snapshot_excludes_env_data_and_untracked_files(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / "agent").mkdir()
    (project / "agent/main.py").write_text("pass")
    (project / "agent/.env").write_text("PRIVATE_VALUE=private")
    (project / "agent/untracked.json").write_text('{"secret":"private"}')
    (project / "agent/runtime.db").write_bytes(b"private database")
    monkeypatch.setattr(reproduce.subprocess, "check_output", lambda *a, **k: b"agent/main.py\0")
    destination = tmp_path / "runtime"
    reproduce.snapshot_runtime(project, destination)
    assert (destination / "agent/main.py").read_text() == "pass"
    assert [p.name for p in (destination / "agent").iterdir()] == ["main.py"]
    assert len(list((destination / "evaluation/androidworld/skills").rglob("SKILL.md"))) == 3


def test_shareable_summary_drops_paths_endpoints_and_raw_messages(tmp_path):
    folder = tmp_path / "episodes/ContactsAddContact/plan_executor"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({
        "score": 1, "valid": True, "budgeted_success": True,
        "host_path": str(tmp_path), "failure_reason": "private-endpoint-and-secret",
        "messages": [{"content": "private conversation"}], "episode_steps": 4,
    }))
    target = tmp_path / "summary.json"
    reproduce.export_summary(tmp_path, target)
    result = target.read_text(encoding="utf-8")
    assert "private" not in result and str(tmp_path) not in result
    assert json.loads(result)["predicate_successes"] == 1


def test_summary_rejects_string_disguised_as_numeric_result(tmp_path):
    folder = tmp_path / "episodes/ContactsAddContact/plan_executor"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text('{"score":"sensitive endpoint"}')
    with pytest.raises(ValueError, match="field type"):
        reproduce.export_summary(tmp_path, tmp_path / "summary.json")


def test_upstream_repair_is_scoped_and_fails_on_changed_source(tmp_path):
    controller = tmp_path / "android_world/env/android_world_controller.py"
    controller.parent.mkdir(parents=True)
    controller.write_text("# changed upstream\n")
    with pytest.raises(ValueError, match="controller changed"):
        reproduce.prepare_upstream(tmp_path)


def test_prepare_requires_new_batch_inside_data(tmp_path):
    with pytest.raises(ValueError, match="direct child"):
        reproduce.prepare(tmp_path, tmp_path / "outside")


def test_seal_verification_rejects_drift_and_manifest_escape(tmp_path):
    path = tmp_path / "input.json"
    path.write_text("{}")
    reproduce.write_json(tmp_path / "source-hashes.json", {"input.json": reproduce.digest(path)})
    reproduce.verify_batch(tmp_path)
    path.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="changed"):
        reproduce.verify_batch(tmp_path)
    reproduce.write_json(tmp_path / "source-hashes.json", {"../outside": "bad"})
    with pytest.raises(ValueError, match="changed"):
        reproduce.verify_batch(tmp_path)


def test_resume_delegates_to_frozen_v6_launcher_without_historical_directory(tmp_path, monkeypatch):
    import os
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "launch_full.py").write_text("# frozen launcher")
    reproduce.write_json(batch / "protocol.json", {"model": "custom/model"})
    reproduce.write_json(batch / "source-hashes.json", {"protocol.json": reproduce.digest(batch / "protocol.json")})
    monkeypatch.setattr(reproduce.os, "environ", dict(os.environ))
    monkeypatch.setattr(reproduce.importlib.util, "find_spec", lambda name: object())
    calls = []
    monkeypatch.setattr(reproduce.subprocess, "run", lambda command, **kw: calls.append((command, kw)))
    reproduce.main(["--output", str(batch), "--resume", "--model", "custom/model"])
    assert [Path(command[1]).name for command, _ in calls] == ["launch_full.py"]
    assert all(options["env"]["CLICKCLICK_EVAL_MODEL"] == "custom/model" for _, options in calls)
    assert json.loads((batch / "summary.public.json").read_text())["evaluated"] == 0
    calls.clear()
    (batch / "protocol.json").write_text('{"model":"custom/model","changed":true}')
    with pytest.raises(ValueError, match="changed"):
        reproduce.main(["--output", str(batch), "--resume", "--model", "custom/model"])
    assert calls == []


def test_portable_lock_excludes_second_process(tmp_path):
    import subprocess
    import sys
    from evaluation.androidworld.portable import lock_file
    path = tmp_path / "run.lock"
    with path.open("a+b") as handle:
        lock_file(handle)
        script = ("import sys; from evaluation.androidworld.portable import lock_file; "
                  "f=open(sys.argv[1], 'a+b'); lock_file(f)")
        result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True)
        assert result.returncode != 0
