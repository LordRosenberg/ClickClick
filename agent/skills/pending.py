"""Hermes-style pending skill patches under skills/_pending/."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.skills.library import (
    SkillLibrary,
    SkillNotFoundError,
    SkillPathJailError,
    default_skills_root,
    serialize_skill_markdown,
)


@dataclass
class PendingPatch:
    id: str
    path: Path
    meta: dict[str, Any]

    @property
    def gist(self) -> str:
        return str(self.meta.get("gist") or self.meta.get("summary") or self.id)

    @property
    def target_rel(self) -> str:
        return str(self.meta.get("target") or "")

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "pending")


def _pending_root(root: Path | None = None) -> Path:
    base = (root or default_skills_root()).resolve()
    d = base / "_pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stage_pending_patch(
    *,
    target_rel: str,
    new_text: str,
    gist: str,
    source_task_id: str = "",
    outcome: str = "",
    app: str | None = None,
    root: Path | None = None,
    old_text: str = "",
    review_metadata: dict[str, Any] | None = None,
) -> PendingPatch:
    """Write a pending JSON+body pair. Does not touch canonical skills."""
    pending_dir = _pending_root(root)
    pid = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
    meta = {
        "id": pid,
        "status": "pending",
        "gist": (gist or "")[:240],
        "target": target_rel.strip().strip("/"),
        "source_task_id": source_task_id,
        "outcome": outcome,
        "app": app or "",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "old_text": old_text,
    }
    if review_metadata:
        meta["review"] = dict(review_metadata)
    meta_path = pending_dir / f"{pid}.json"
    body_path = pending_dir / f"{pid}.md"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    body_path.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")
    return PendingPatch(id=pid, path=meta_path, meta=meta)


def list_pending(*, root: Path | None = None, status: str = "pending") -> list[PendingPatch]:
    pending_dir = _pending_root(root)
    out: list[PendingPatch] = []
    for path in sorted(pending_dir.glob("*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        if status and str(meta.get("status") or "") != status:
            continue
        out.append(PendingPatch(id=str(meta.get("id") or path.stem), path=path, meta=meta))
    return out


def get_pending(pending_id: str, *, root: Path | None = None) -> PendingPatch | None:
    pending_dir = _pending_root(root)
    path = pending_dir / f"{pending_id}.json"
    if not path.exists():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    return PendingPatch(id=pending_id, path=path, meta=meta)


def pending_diff(pending_id: str, *, root: Path | None = None) -> dict[str, Any]:
    import difflib

    item = get_pending(pending_id, root=root)
    if item is None:
        raise SkillNotFoundError(pending_id)
    pending_dir = item.path.parent
    new_text = (pending_dir / f"{pending_id}.md").read_text(encoding="utf-8")
    old_text = str(item.meta.get("old_text") or "")
    if not old_text and item.target_rel:
        lib_root = (root or default_skills_root()).resolve()
        cand = lib_root / item.target_rel
        if cand.is_file():
            old_text = cand.read_text(encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=item.target_rel or "old",
            tofile=item.target_rel or "new",
        )
    )
    return {
        "id": pending_id,
        "gist": item.gist,
        "target": item.target_rel,
        "status": item.status,
        "diff": diff,
        "old_text": old_text,
        "new_text": new_text,
        "meta": item.meta,
    }


def approve_pending(pending_id: str, *, root: Path | None = None) -> Path:
    """Apply pending body to canonical target; mark pending approved."""
    lib_root = (root or default_skills_root()).resolve()
    item = get_pending(pending_id, root=lib_root)
    if item is None:
        raise SkillNotFoundError(pending_id)
    if item.status != "pending":
        raise ValueError(f"pending {pending_id} is not awaiting approval")
    target_rel = item.target_rel
    if not target_rel or ".." in target_rel:
        raise SkillPathJailError(f"invalid target: {target_rel!r}")
    dest = (lib_root / target_rel).resolve()
    try:
        dest.relative_to(lib_root)
    except ValueError as exc:
        raise SkillPathJailError(f"target escapes skills root: {dest}") from exc
    if dest.parts[-2] in ("_pending", "_drafts") or dest.parts[-1].startswith("_"):
        raise SkillPathJailError(f"refusing to write into pending/drafts: {dest}")

    new_text = (item.path.parent / f"{pending_id}.md").read_text(encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")

    meta = dict(item.meta)
    meta["status"] = "approved"
    meta["approved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item.path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def reject_pending(pending_id: str, *, reason: str = "", root: Path | None = None) -> None:
    item = get_pending(pending_id, root=root)
    if item is None:
        raise SkillNotFoundError(pending_id)
    meta = dict(item.meta)
    meta["status"] = "rejected"
    meta["rejected_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if reason:
        meta["reject_reason"] = reason[:500]
    item.path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def bump_patch_version(version: str) -> str:
    parts = (version or "0.1.0").strip().split(".")
    try:
        nums = [int(x) for x in parts]
        while len(nums) < 3:
            nums.append(0)
        nums[-1] += 1
        return ".".join(str(x) for x in nums[:3])
    except ValueError:
        return "0.1.1"
