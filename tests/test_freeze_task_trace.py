from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from evaluation.freeze_task_trace import (
    export_task_trace,
    verify_frozen_task_trace,
)


INCIDENTS = Path("evaluation/fixtures/incidents")


def _source_db(path: Path, *, state_json: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, instruction TEXT NOT NULL, status TEXT NOT NULL,
          current_node_id TEXT, failure_reason TEXT, state_json TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL, device_serial TEXT
        );
        CREATE TABLE steps (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          node_id TEXT, seq INTEGER NOT NULL, payload_json TEXT NOT NULL
        );
        CREATE TABLE traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
          node_id TEXT, step_seq INTEGER, kind TEXT NOT NULL, level TEXT NOT NULL,
          message TEXT NOT NULL, payload_ref TEXT, payload_json TEXT, ts REAL NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        ("task-1", "instruction", "failed", None, None, state_json, 1, 2, "device"),
    )
    connection.execute(
        "INSERT INTO steps(task_id,node_id,seq,payload_json) VALUES (?,?,?,?)",
        ("task-1", None, 1, "{}"),
    )
    connection.execute(
        "INSERT INTO traces(task_id,node_id,step_seq,kind,level,message,payload_ref,payload_json,ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("task-1", None, 1, "system", "info", "m", None, "{}", 3),
    )
    connection.commit()
    connection.close()


def _rewrite_manifest_file_hash(output: Path, relative: str) -> None:
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][relative] = hashlib.sha256(
        (output / relative).read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def test_export_follows_json_strings_and_artifact_json_recursively(tmp_path: Path):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    (artifacts / "llm").mkdir(parents=True)
    (artifacts / "trees").mkdir(parents=True)
    state_json = json.dumps(
        {"serialized": json.dumps({"request_ref": "llm/root.json"})}
    )
    _source_db(db, state_json=state_json)
    (artifacts / "llm" / "root.json").write_text(
        json.dumps({"serialized": json.dumps({"tree_ref": "trees/child.json"})}),
        encoding="utf-8",
    )
    (artifacts / "trees" / "child.json").write_text(
        json.dumps({"tree": True}), encoding="utf-8"
    )

    output = tmp_path / "frozen"
    manifest = export_task_trace(
        db_path=db,
        artifact_root=artifacts,
        task_id="task-1",
        output_dir=output,
        adjudication={"classification": "task_error"},
        require_complete=True,
    )

    assert manifest["referenced_refs"] == ["llm/root.json", "trees/child.json"]
    assert manifest["copied_refs"] == manifest["referenced_refs"]
    assert manifest["missing_refs"] == []
    assert manifest["ref_counts"] == {"referenced": 2, "copied": 2, "missing": 0}
    assert verify_frozen_task_trace(output, expected_task_id="task-1") == manifest


def test_export_records_missing_and_complete_mode_fails_before_publish(tmp_path: Path):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    nested = json.dumps({"serialized": json.dumps({"ref": "llm/missing.json"})})
    _source_db(db, state_json=nested)

    incomplete = tmp_path / "incomplete"
    manifest = export_task_trace(
        db_path=db,
        artifact_root=artifacts,
        task_id="task-1",
        output_dir=incomplete,
        adjudication={},
    )
    assert manifest["referenced_refs"] == ["llm/missing.json"]
    assert manifest["copied_refs"] == []
    assert manifest["missing_refs"] == ["llm/missing.json"]
    assert manifest["ref_counts"] == {"referenced": 1, "copied": 0, "missing": 1}

    complete = tmp_path / "complete"
    with pytest.raises(FileNotFoundError, match="llm/missing.json"):
        export_task_trace(
            db_path=db,
            artifact_root=artifacts,
            task_id="task-1",
            output_dir=complete,
            adjudication={},
            require_complete=True,
        )
    assert not complete.exists()


@pytest.mark.parametrize(
    "unsafe_ref",
    ["llm/../outside.json", "trees/a/../../outside.json", "som\\outside.png"],
)
def test_export_rejects_unsafe_refs_before_publish(tmp_path: Path, unsafe_ref: str):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _source_db(db, state_json=json.dumps({"ref": unsafe_ref}))
    output = tmp_path / "frozen"

    with pytest.raises(ValueError, match="artifact reference"):
        export_task_trace(
            db_path=db,
            artifact_root=artifacts,
            task_id="task-1",
            output_dir=output,
            adjudication={},
        )
    assert not output.exists()


def test_verifier_detects_file_task_instruction_and_count_tampering(tmp_path: Path):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _source_db(db, state_json="{}")
    output = tmp_path / "frozen"
    export_task_trace(
        db_path=db,
        artifact_root=artifacts,
        task_id="task-1",
        output_dir=output,
        adjudication={},
    )

    with pytest.raises(ValueError, match="task id mismatch"):
        verify_frozen_task_trace(output, expected_task_id="other-task")

    rows_path = output / "rows.json"
    original = rows_path.read_text(encoding="utf-8")
    rows = json.loads(original)
    rows["task"]["instruction"] = "tampered"
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(ValueError, match="file set or SHA-256"):
        verify_frozen_task_trace(output)

    rows_path.write_text(original, encoding="utf-8")
    rows = json.loads(original)
    rows["steps"].append(dict(rows["steps"][0], id=2, seq=2))
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    _rewrite_manifest_file_hash(output, "rows.json")
    with pytest.raises(ValueError, match="row counts"):
        verify_frozen_task_trace(output)


def test_verifier_recomputes_instruction_hash_after_file_hash_is_adjusted(tmp_path: Path):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _source_db(db, state_json="{}")
    output = tmp_path / "frozen"
    export_task_trace(
        db_path=db,
        artifact_root=artifacts,
        task_id="task-1",
        output_dir=output,
        adjudication={},
    )

    rows_path = output / "rows.json"
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    rows["task"]["instruction"] = "tampered"
    rows_path.write_text(json.dumps(rows), encoding="utf-8")
    _rewrite_manifest_file_hash(output, "rows.json")
    with pytest.raises(ValueError, match="instruction SHA-256"):
        verify_frozen_task_trace(output)


def test_verifier_recomputes_artifact_reference_closure(tmp_path: Path):
    db = tmp_path / "clickclick.db"
    artifacts = tmp_path / "artifacts"
    (artifacts / "llm").mkdir(parents=True)
    _source_db(db, state_json=json.dumps({"ref": "llm/root.json"}))
    (artifacts / "llm" / "root.json").write_text("{}", encoding="utf-8")
    output = tmp_path / "frozen"
    export_task_trace(
        db_path=db,
        artifact_root=artifacts,
        task_id="task-1",
        output_dir=output,
        adjudication={},
    )

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["referenced_refs"] = []
    manifest["copied_refs"] = []
    manifest["ref_counts"] = {"referenced": 0, "copied": 0, "missing": 0}
    artifact_relative = "artifacts/llm/root.json"
    (output / artifact_relative).unlink()
    manifest["files"].pop(artifact_relative)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="reference closure"):
        verify_frozen_task_trace(output)


@pytest.mark.parametrize(
    ("directory", "task_id", "row_counts", "ref_counts"),
    [
        (
            "045-safe-rerun-false-success-v2",
            "045ccbc1-485e-42c4-9b16-cd9774c5473a",
            {"tasks": 1, "steps": 6, "traces": 130},
            {"referenced": 94, "copied": 94, "missing": 0},
        ),
        (
            "c045-original-diagnostic-trace-v2",
            "c045e386-f359-4d70-9a5b-829c1812b704",
            {"tasks": 1, "steps": 27, "traces": 406},
            {"referenced": 256, "copied": 256, "missing": 0},
        ),
    ],
)
def test_checked_in_v2_incident_bundle_is_complete_and_verifiable(
    directory: str,
    task_id: str,
    row_counts: dict[str, int],
    ref_counts: dict[str, int],
):
    manifest = verify_frozen_task_trace(
        INCIDENTS / directory, expected_task_id=task_id
    )
    assert manifest["row_counts"] == row_counts
    assert manifest["ref_counts"] == ref_counts
    assert manifest["missing_refs"] == []
