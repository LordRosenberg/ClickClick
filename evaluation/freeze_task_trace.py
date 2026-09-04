"""Freeze and verify task-local database rows plus referenced artifacts.

The utility is evaluation-only: it never starts a task or touches a device. It
parses JSON stored inside database text columns, follows artifact references in
JSON artifacts, writes only to a fresh directory, and records enough metadata
to verify the frozen bundle without trusting the source database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from typing import Any


_ARTIFACT_PREFIXES = frozenset(
    {"llm", "trees", "images", "model-images", "screens", "som", "artifacts"}
)
_ARTIFACT_REF = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?P<ref>(?:llm|trees|images|model-images|screens|som|artifacts)[/\\]"
    r"[A-Za-z0-9._/\\-]+)"
)
_MAX_EMBEDDED_JSON_DEPTH = 32


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite frozen artifact: {path}") from exc


def _row_dicts(
    connection: sqlite3.Connection, query: str, task_id: str
) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query, (task_id,)).fetchall()]


def _normalize_ref(candidate: str) -> str:
    if "\\" in candidate or candidate.startswith("/"):
        raise ValueError(f"unsafe artifact reference: {candidate!r}")
    path = PurePosixPath(candidate)
    if (
        not path.parts
        or path.parts[0] not in _ARTIFACT_PREFIXES
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe artifact reference: {candidate!r}")
    normalized = path.as_posix()
    if normalized != candidate:
        raise ValueError(f"non-canonical artifact reference: {candidate!r}")
    return normalized


def _candidate_refs(value: Any) -> set[str]:
    """Find refs recursively, including JSON serialized inside string fields."""

    found: set[str] = set()
    decoded_strings: set[str] = set()

    def visit(current: Any, depth: int) -> None:
        if depth > _MAX_EMBEDDED_JSON_DEPTH:
            raise ValueError("embedded JSON nesting exceeds safe recursion limit")
        if isinstance(current, dict):
            for key, nested in current.items():
                visit(key, depth)
                visit(nested, depth)
            return
        if isinstance(current, (list, tuple)):
            for nested in current:
                visit(nested, depth)
            return
        if not isinstance(current, str):
            return

        stripped = current.strip()
        if not stripped:
            return
        if stripped not in decoded_strings and (
            stripped[0] in '[{"' or stripped in {"null", "true", "false"}
        ):
            try:
                decoded = json.loads(stripped)
            except (json.JSONDecodeError, RecursionError):
                pass
            else:
                decoded_strings.add(stripped)
                if decoded != current:
                    visit(decoded, depth + 1)
                    return

        for match in _ARTIFACT_REF.finditer(current):
            found.add(_normalize_ref(match.group("ref")))

    visit(value, 0)
    return found


def collect_artifact_refs(value: Any) -> set[str]:
    """Return validated repository artifact references found in nested values."""
    return _candidate_refs(value)


def _artifact_graph(
    *, artifact_root: Path, initial_values: tuple[Any, ...]
) -> tuple[list[str], list[str], list[str]]:
    root = artifact_root.resolve()
    pending: set[str] = set()
    for value in initial_values:
        pending.update(_candidate_refs(value))

    referenced: set[str] = set()
    copied: set[str] = set()
    missing: set[str] = set()
    while pending:
        relative = _normalize_ref(pending.pop())
        if relative in referenced:
            continue
        referenced.add(relative)
        source = (root / relative).resolve()
        if source == root or root not in source.parents:
            raise ValueError(f"artifact reference escapes root: {relative!r}")
        if not source.is_file():
            missing.add(relative)
            continue
        copied.add(relative)
        if source.suffix.lower() == ".json":
            try:
                artifact_value = json.loads(source.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid referenced JSON artifact: {relative!r}") from exc
            pending.update(_candidate_refs(artifact_value))

    return sorted(referenced), sorted(copied), sorted(missing)


def _frozen_artifact_refs(output_dir: Path, initial_values: tuple[Any, ...]) -> set[str]:
    """Recompute the reference closure using only bytes in a frozen bundle."""

    pending: set[str] = set()
    for value in initial_values:
        pending.update(_candidate_refs(value))
    referenced: set[str] = set()
    while pending:
        relative = _normalize_ref(pending.pop())
        if relative in referenced:
            continue
        referenced.add(relative)
        frozen = output_dir / "artifacts" / relative
        if not frozen.is_file() or frozen.suffix.lower() != ".json":
            continue
        try:
            artifact_value = json.loads(frozen.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid frozen JSON artifact: {relative!r}") from exc
        pending.update(_candidate_refs(artifact_value))
    return referenced


def export_task_trace(
    *,
    db_path: Path,
    artifact_root: Path,
    task_id: str,
    output_dir: Path,
    adjudication: dict[str, Any],
    require_complete: bool = False,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to reuse frozen output directory: {output_dir}")
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        tasks = _row_dicts(connection, "SELECT * FROM tasks WHERE id = ?", task_id)
        if len(tasks) != 1:
            raise ValueError(f"expected exactly one task row for {task_id!r}")
        steps = _row_dicts(
            connection,
            "SELECT * FROM steps WHERE task_id = ? ORDER BY seq, id",
            task_id,
        )
        traces = _row_dicts(
            connection,
            "SELECT * FROM traces WHERE task_id = ? ORDER BY id",
            task_id,
        )
    finally:
        connection.close()

    rows = {"task": tasks[0], "steps": steps, "traces": traces}
    referenced, copied, missing = _artifact_graph(
        artifact_root=artifact_root,
        initial_values=(rows, adjudication),
    )
    if require_complete and missing:
        raise FileNotFoundError(
            "frozen trace requires complete artifacts; missing: " + ", ".join(missing)
        )

    output_dir.mkdir(parents=True)
    _publish(output_dir / "rows.json", _canonical_bytes(rows))
    _publish(output_dir / "adjudication.json", _canonical_bytes(adjudication))
    root = artifact_root.resolve()
    for relative in copied:
        source = (root / relative).resolve()
        _publish(output_dir / "artifacts" / relative, source.read_bytes())

    files = {
        str(path.relative_to(output_dir)): _sha256(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "schema_version": 2,
        "task_id": task_id,
        "instruction_utf8_sha256": hashlib.sha256(
            str(tasks[0]["instruction"]).encode("utf-8")
        ).hexdigest(),
        "row_counts": {"tasks": 1, "steps": len(steps), "traces": len(traces)},
        "source_database_sha256": _sha256(db_path),
        "referenced_refs": referenced,
        "copied_refs": copied,
        "missing_refs": missing,
        "ref_counts": {
            "referenced": len(referenced),
            "copied": len(copied),
            "missing": len(missing),
        },
        "files": files,
    }
    _publish(output_dir / "manifest.json", _canonical_bytes(manifest))
    verify_frozen_task_trace(output_dir, expected_task_id=task_id)
    return manifest


def verify_frozen_task_trace(
    output_dir: Path, *, expected_task_id: str | None = None
) -> dict[str, Any]:
    """Recompute all self-contained integrity claims in a frozen trace."""

    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported frozen trace manifest schema")
    task_id = manifest.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("manifest task_id must be a non-empty string")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ValueError(f"task id mismatch: expected {expected_task_id!r}, got {task_id!r}")

    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict):
        raise ValueError("manifest files must be an object")
    actual_files = {
        str(path.relative_to(output_dir)): _sha256(path)
        for path in sorted(output_dir.rglob("*"))
        if path.is_file() and path != manifest_path
    }
    if actual_files != declared_files:
        raise ValueError("frozen file set or SHA-256 does not match manifest")

    rows = json.loads((output_dir / "rows.json").read_text(encoding="utf-8"))
    task = rows.get("task")
    steps = rows.get("steps")
    traces = rows.get("traces")
    if not isinstance(task, dict) or not isinstance(steps, list) or not isinstance(traces, list):
        raise ValueError("rows.json has invalid task/steps/traces structure")
    if not all(isinstance(row, dict) for row in [*steps, *traces]):
        raise ValueError("rows.json step and trace entries must be objects")
    if task.get("id") != task_id:
        raise ValueError("rows task id does not match manifest")
    if any(row.get("task_id") != task_id for row in [*steps, *traces]):
        raise ValueError("step or trace task id does not match manifest")
    row_counts = {"tasks": 1, "steps": len(steps), "traces": len(traces)}
    if row_counts != manifest.get("row_counts"):
        raise ValueError("row counts do not match manifest")
    instruction = task.get("instruction")
    if not isinstance(instruction, str):
        raise ValueError("rows task instruction must be a string")
    instruction_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    if instruction_hash != manifest.get("instruction_utf8_sha256"):
        raise ValueError("instruction SHA-256 does not match manifest")

    referenced = manifest.get("referenced_refs")
    copied = manifest.get("copied_refs")
    missing = manifest.get("missing_refs")
    if not all(isinstance(refs, list) for refs in (referenced, copied, missing)):
        raise ValueError("manifest artifact refs must be lists")
    if any(refs != sorted(set(refs)) for refs in (referenced, copied, missing)):
        raise ValueError("manifest artifact refs must be sorted and unique")
    if not all(isinstance(relative, str) for relative in referenced):
        raise ValueError("manifest artifact refs must be strings")
    for relative in referenced:
        _normalize_ref(relative)
    if set(copied) & set(missing) or set(referenced) != set(copied) | set(missing):
        raise ValueError("copied and missing refs must partition referenced refs")
    copied_files = {f"artifacts/{relative}" for relative in copied}
    actual_artifact_files = {
        relative for relative in actual_files if relative.startswith("artifacts/")
    }
    if copied_files != actual_artifact_files:
        raise ValueError("copied refs do not match frozen artifact files")
    expected_counts = {
        "referenced": len(referenced),
        "copied": len(copied),
        "missing": len(missing),
    }
    if manifest.get("ref_counts") != expected_counts:
        raise ValueError("artifact ref counts do not match manifest")
    adjudication = json.loads(
        (output_dir / "adjudication.json").read_text(encoding="utf-8")
    )
    recomputed_refs = _frozen_artifact_refs(output_dir, (rows, adjudication))
    if recomputed_refs != set(referenced):
        raise ValueError("artifact reference closure does not match manifest")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--adjudication", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify is not None:
        result = verify_frozen_task_trace(args.verify, expected_task_id=args.task_id)
    else:
        required = {
            "--db": args.db,
            "--artifacts": args.artifacts,
            "--task-id": args.task_id,
            "--output": args.output,
            "--adjudication": args.adjudication,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error("export requires " + ", ".join(missing))
        adjudication = json.loads(args.adjudication.read_text(encoding="utf-8"))
        result = export_task_trace(
            db_path=args.db,
            artifact_root=args.artifacts,
            task_id=args.task_id,
            output_dir=args.output,
            adjudication=adjudication,
            require_complete=args.require_complete,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
