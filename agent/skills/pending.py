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
    review_meta = item.meta.get("review") or {}
    if review_meta.get("experiment") == "task_explore_group":
        return approve_pending_group([pending_id], root=lib_root)[0]
    if review_meta.get("experiment") == "task_explore":
        from agent.skills.learning import Candidate, digest, review_gate, validation_gate
        if digest(new_text) != review_meta.get("candidate_hash"):
            raise ValueError("experimental candidate changed since review")
        current = dest.read_text(encoding="utf-8") if dest.exists() else ""
        if digest(current) != review_meta.get("base_hash"):
            raise ValueError("canonical base changed since validation")
        if not review_gate(review_meta.get("review") or {}):
            raise ValueError("Skill Reviewer has not passed all six criteria")
        candidate = Candidate(target=target_rel, new_text=new_text, gist=item.gist,
            evidence=review_meta.get("evidence") or ["pending"],
            scope=review_meta.get("scope") or "pending", benefit="reviewed", risks="reviewed")
        if (review_meta.get("validation") or {}).get("mode") == "source_evidence":
            candidate = Candidate.model_validate(review_meta.get("candidate_contract") or {})
            if review_meta["validation"].get("source_task_id") != item.meta.get("source_task_id"):
                raise ValueError("source-evidence task binding changed")
            if candidate.target != target_rel or candidate.new_text != new_text:
                raise ValueError("source-evidence candidate contract changed")
        manifest = review_meta.get("baseline_manifest")
        if manifest is not None:
            from agent.skills.snapshot import library_manifest
            if library_manifest(lib_root) != manifest:
                raise ValueError("official library/resources changed since validation")
        environment = review_meta.get("environment_receipt")
        if environment and (environment.get("unresolved_effects") or environment.get("status") == "interrupted"):
            raise ValueError("research environment has unresolved effects")
        valid, reason = validation_gate(candidate, digest(current), review_meta.get("validation"), baseline_manifest=manifest,
            review=review_meta.get("review"), contracts_hash=review_meta.get("contracts_hash"))
        if not valid:
            raise ValueError(f"experimental validation blocked: {reason}")
        from agent.skills.exploration import build_review_contracts
        from shared.config import get_settings
        contracts = build_review_contracts(new_text, SkillLibrary(lib_root), get_settings())
        if review_meta.get("contracts_hash") != digest(contracts):
            raise ValueError("related skills/prompts/schemas changed since review")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")

    meta = dict(item.meta)
    meta["status"] = "approved"
    meta["approved_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    item.path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def approve_pending_group(pending_ids: list[str], *, root: Path | None = None) -> list[Path]:
    """Approve a dependency-closed subset only after checking every file and receipt."""
    from agent.skills.candidate_group import candidate_files, check_group_receipt
    from agent.skills.library import parse_skill_markdown
    from agent.skills.learning import digest
    from agent.skills.exploration import build_review_contracts
    from agent.skills.snapshot import library_manifest
    from shared.config import get_settings
    lib_root = (root or default_skills_root()).resolve()
    items = [get_pending(pid, root=lib_root) for pid in pending_ids]
    if not items or len(set(pending_ids)) != len(items) or any(i is None or i.status != "pending" for i in items):
        raise ValueError("group approval requires distinct pending items")
    metas = [i.meta.get("review") or {} for i in items]
    receipt = metas[0].get("group_receipt")
    receipt_hash = digest(receipt)
    if any(m.get("experiment") != "task_explore_group" or m.get("receipt_hash") != receipt_hash or m.get("group_receipt") != receipt for m in metas):
        raise ValueError("group receipt mismatch")
    group, accepted = check_group_receipt(receipt)
    patches = candidate_files(group)
    requested = {i.target_rel for i in items}
    if len(requested) != len(items) or not requested <= accepted:
        raise ValueError("group patch not locally admitted")
    approved = {i.target_rel for i in list_pending(root=lib_root, status="approved")
        if (i.meta.get("review") or {}).get("receipt_hash") == receipt_hash}
    expected = dict(receipt["manifest"]["files"])
    import hashlib
    for target in approved:
        if target not in patches:
            raise ValueError("foreign approved group member")
        expected[target] = hashlib.sha256(patches[target].encode()).hexdigest()
    if library_manifest(lib_root)["files"] != expected:
        raise ValueError("official library changed since group review")
    for target in requested:
        if not set(group.dependencies.get(target, [])) <= requested | approved:
            raise ValueError("approve dependent patches together")
    destinations = []
    approved_skill_ids = {parse_skill_markdown(patches[t]).id for t in approved}
    for item, meta in zip(items, metas):
        text = (item.path.parent / f"{item.id}.md").read_text(encoding="utf-8")
        if text != patches[item.target_rel] or digest(text) != meta.get("candidate_hash") or meta.get("group_hash") != digest(patches):
            raise ValueError("group pending body changed")
        dest = (lib_root / item.target_rel).resolve()
        dest.relative_to(lib_root)
        current = dest.read_text(encoding="utf-8") if dest.exists() else ""
        if current != receipt["base"][item.target_rel] or digest(current) != meta.get("base_hash"):
            raise ValueError("group pending base changed")
        contracts = build_review_contracts(text, SkillLibrary(lib_root), get_settings())
        reviewed_contracts = receipt["contracts"][item.target_rel]
        # Previously approved exact siblings are the only allowed library change.
        # Their joint texts were reviewed in this receipt; unrelated guidance and
        # runtime contracts must still be identical to the original review.
        if approved_skill_ids:
            contracts = {**contracts, "related_skills": [p for p in contracts["related_skills"] if p["id"] not in approved_skill_ids]}
            reviewed_contracts = {**reviewed_contracts, "related_skills": [p for p in reviewed_contracts["related_skills"] if p["id"] not in approved_skill_ids]}
        if digest(contracts) != digest(reviewed_contracts):
            raise ValueError("group related contracts changed")
        destinations.append(dest)
    # All structural/dependency/contract checks precede the first canonical write.
    originals = {dest: dest.read_bytes() if dest.exists() else None for dest in destinations}
    try:
        for item, dest in zip(items, destinations):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(patches[item.target_rel].encode())
    except OSError:
        for dest, old in originals.items():
            if old is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_bytes(old)
        raise
    for item in items:
        meta = {**item.meta, "status": "approved", "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        item.path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return destinations


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
