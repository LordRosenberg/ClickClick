"""Seal and selectively restore a prompt revision's working-tree snapshot."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def inventory(baseline, workspace):
    manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf8"))
    result = []
    for item in manifest["files"]:
        rel = Path(item["path"])
        target = (workspace / rel).resolve()
        source = (baseline / "files" / rel).resolve()
        if rel.is_absolute() or not target.is_relative_to(workspace.resolve()) or not source.is_relative_to((baseline / "files").resolve()):
            raise ValueError("Snapshot path escapes its workspace")
        if item["existed"] and digest(source) != item["sha256"]:
            raise ValueError("Baseline snapshot hash mismatch: " + item["path"])
        result.append((item, target, source))
    return manifest, result


def seal(baseline, workspace):
    manifest, rows = inventory(baseline, workspace)
    output = baseline.parent / "candidate.json"
    if output.exists():
        raise ValueError("Candidate already sealed; preserve it before resealing")
    value = {"files": {item["path"]: digest(target) for item, target, _ in rows}}
    output.write_text(json.dumps(value, indent=2), encoding="utf8")
    return {"sealed": True, "files": len(rows)}


def restore(baseline, workspace, *, candidate=None):
    _, rows = inventory(baseline, workspace)
    # Validate all files before any write; never overwrite a later edit.
    if candidate is not None:
        conflicts = [item["path"] for item, target, _ in rows
                     if digest(target) != candidate["files"][item["path"]]]
        if conflicts:
            raise ValueError("Later edits prevent restoration: " + ", ".join(conflicts))
    for item, target, source in rows:
        if item["existed"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        elif target.exists():
            if not target.is_file():
                raise ValueError("Refusing to remove a non-file: " + item["path"])
            target.unlink()
    mismatches = [item["path"] for item, target, _ in rows
                  if digest(target) != item["sha256"] or target.exists() != item["existed"]]
    if mismatches:
        raise ValueError("Restored hash/existence mismatch: " + ", ".join(mismatches))
    return {"restored": True, "verified_files": len(rows)}


def rehearse(baseline, workspace):
    _, rows = inventory(baseline, workspace)
    with tempfile.TemporaryDirectory(prefix="hitl-policy-restore-", dir=baseline.parent) as directory:
        temporary = Path(directory).resolve()
        if not temporary.is_relative_to(baseline.parent.resolve()):
            raise ValueError("Rehearsal directory escapes the snapshot directory")
        for item, target, _ in rows:
            copy = temporary / item["path"]
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(target.read_bytes() if target.exists() else b"revision-created-file")
        untouched = temporary / "unrelated.txt"
        untouched.write_text("retain unrelated changes", encoding="utf8")
        report = restore(baseline, temporary)
        assert untouched.read_text(encoding="utf8") == "retain unrelated changes"
        manifest = json.loads((baseline / "manifest.json").read_text(encoding="utf8"))
        if manifest.get("rendered"):
            spec = importlib.util.spec_from_file_location("restored_role_prompts", temporary / "agent/prompts/__init__.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for label, expected in manifest["rendered"].items():
                role, mode = label.split("-", 1)
                actual = module.render_role_system(role, hitl_enabled=mode != "disabled")
                if hashlib.sha256(actual.encode()).hexdigest() != expected:
                    raise ValueError("Restored rendered prompt mismatch: " + label)
            report["verified_rendered_prompts"] = len(manifest["rendered"])
        report["disposable_copy_only"] = True
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seal", "rehearse", "restore"])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--runtime-drained", action="store_true")
    args = parser.parse_args()
    baseline, workspace = args.baseline.resolve(), args.workspace.resolve()
    if args.operation == "restore":
        if not args.runtime_drained:
            parser.error("Drain or preserve pending runtime work before restoration; pass --runtime-drained")
        candidate = json.loads((baseline.parent / "candidate.json").read_text(encoding="utf8"))
        report = restore(baseline, workspace, candidate=candidate)
    elif args.operation == "seal":
        report = seal(baseline, workspace)
    else:
        report = rehearse(baseline, workspace)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
