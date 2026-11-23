"""Hermes-style pending skill patches under skills/_pending/."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
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
from shared.file_lock import FileLock


_PENDING_MUTATIONS = threading.RLock()


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

    @property
    def operation(self) -> str:
        return self.meta.get("operation") or ("update" if self.meta.get("old_text") else "add")


def _pending_root(root: Path | None = None) -> Path:
    base = (root or default_skills_root()).resolve()
    d = base / "_pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
    """Replace a pending record only after its new contents are complete."""
    _replace_bytes(path, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))


def _replace_bytes(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    operation: str | None = None,
) -> PendingPatch:
    """Write a pending JSON+body pair. Does not touch canonical skills."""
    pending_dir = _pending_root(root)
    if operation is not None and (operation != "delete" or new_text or not old_text
                                  or (review_metadata or {}).get("experiment") != "task_explore_delete"):
        raise ValueError("pending deletion requires an old Skill and a deletion review")
    if operation == "delete" and pending_dir.resolve() != pending_dir:
        raise SkillPathJailError("pending deletion requires a directory inside the skill library")
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
    if operation is not None:
        meta["operation"] = operation
    meta_path = pending_dir / f"{pid}.json"
    body_path = pending_dir / f"{pid}.md"
    body = new_text if operation == "delete" or new_text.endswith("\n") else new_text + "\n"
    body_path.write_text(body, encoding="utf-8")
    _write_metadata(meta_path, meta)
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
        if status == "pending" and _publication_decision(meta.get("review") or {}) == "low":
            continue
        out.append(PendingPatch(id=str(meta.get("id") or path.stem), path=path, meta=meta))
    return out


def get_pending(pending_id: str, *, root: Path | None = None) -> PendingPatch | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", pending_id):
        return None
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
            tofile="/dev/null" if item.operation == "delete" else item.target_rel or "new",
        )
    )
    return {
        "id": pending_id,
        "gist": item.gist,
        "target": item.target_rel,
        "status": item.status,
        "operation": item.operation,
        "diff": diff,
        "old_text": old_text,
        "new_text": new_text,
        "meta": item.meta,
    }


def approve_pending(pending_id: str, *, root: Path | None = None, automatic: bool = False, settings=None) -> Path:
    """Apply an approved pending operation to the canonical library."""
    lib_root = (root or default_skills_root()).resolve()
    with _PENDING_MUTATIONS, FileLock(_pending_root(lib_root) / ".publication.lock", timeout=5):
        return _approve_pending(pending_id, root=lib_root, automatic=automatic, settings=settings)


def _publication_decision(review_meta):
    proof = review_meta.get("prepared_gui_regression") if isinstance(review_meta, dict) else None
    impact = proof.get("publication") if isinstance(proof, dict) else None
    return impact.get("decision") if isinstance(impact, dict) else None


def _require_gui_publication(review_meta, candidate, current, *, automatic):
    from agent.skills.learning import digest
    from agent.skills.library import parse_skill_markdown

    proof = review_meta.get("prepared_gui_regression")
    if proof is None and not automatic:
        return
    if not isinstance(proof, dict):
        raise ValueError("verified GUI regression is required for automatic publication")
    if not automatic and _publication_decision(review_meta) == "low":
        raise ValueError("low-impact Skill uses automatic publication")
    validation = review_meta.get("validation") or {}
    try:
        regression = json.loads(Path(proof["ref"]).read_text(encoding="utf-8"))
        trials = regression["trials"]
        cases, delivery = proof["cases"], proof["skill_delivery"]
        source_delivery = delivery["cases"]["source"]
        if (not isinstance(cases, dict) or "source" not in cases or not isinstance(trials, list)
                or len(trials) != len(cases) or {trial["kind"] for trial in trials} != cases.keys()
                or regression.get("prepared_gate", {}).get("status") != "passed"
                or digest(regression) != proof["report_hash"] or proof["candidate_hash"] != digest(candidate.new_text)
                or regression["base_hash"] != digest(current) or delivery.get("verified") is not True
                or not source_delivery["candidate_roles"] or current and not source_delivery["baseline_roles"]
                or any(regression[key] != validation[key] for key in
                       ("candidate_hash", "base_hash", "baseline_library_hash", "candidate_library_hash"))
                or regression["baseline_library_hash"] != proof["baseline_library_hash"]
                or any(not all(trial.get(check) is True for check in
                               ("matched_environment", "independent_oracle", "candidate_success", "guard_passed"))
                       or trial.get("candidate_branch_matches") is not True
                       and (not trial["kind"].startswith("registered-") or trial.get("candidate_branch_required") is not False)
                       for trial in trials)):
            raise ValueError("prepared GUI regression differs from its reviewed publication proof")
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("prepared GUI regression proof is missing or unreadable") from exc
    if automatic:
        pack = parse_skill_markdown(candidate.new_text)
        impact = proof.get("publication")
        evidence = impact.get("evidence_cases") if isinstance(impact, dict) else None
        evidence_cases = set(evidence) if isinstance(evidence, list) and all(isinstance(key, str) for key in evidence) else set()
        if (review_meta.get("eligible") is not True or _publication_decision(review_meta) != "low"
                or "source" not in evidence_cases or evidence_cases - cases.keys() or pack.kind != "workflow"
                or pack.interface_scope != "app" or not candidate.target.startswith(f"apps/{pack.app}/workflows/")):
            raise ValueError("automatic publication requires low-impact App workflow evidence")


def _approved_metadata(metadata, *, automatic):
    meta = {**metadata, "status": "approved",
            "approved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if automatic:
        meta["approved_via"] = "automatic"
    return meta


def _approve_pending(pending_id: str, *, root: Path, automatic: bool = False, settings=None) -> Path:
    lib_root = (root or default_skills_root()).resolve()
    item = get_pending(pending_id, root=lib_root)
    if item is None:
        raise SkillNotFoundError(pending_id)
    if item.meta.get("operation") == "delete":
        if automatic:
            raise ValueError("deletion requires manual publication")
        return _approve_deletion(item, lib_root)
    if item.status != "pending" and not (automatic and item.status == "approved"):
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
    if item.meta.get("operation") is not None or review_meta.get("experiment") == "task_explore_delete":
        raise ValueError("invalid pending operation")
    if automatic and review_meta.get("experiment") != "task_explore":
        raise ValueError("automatic publication requires one independently reviewed candidate")
    if review_meta.get("experiment") == "task_explore_group":
        return _approve_pending_group([pending_id], root=lib_root)[0]
    intent = None
    if review_meta.get("experiment") == "task_explore":
        from agent.skills.learning import Candidate, digest, review_gate, validation_gate
        if digest(new_text) != review_meta.get("candidate_hash"):
            raise ValueError("experimental candidate changed since review")
        if automatic:
            intent = digest({"id": item.id, "target": target_rel, "source": item.meta.get("source_task_id"),
                             "old_text": item.meta.get("old_text"), "review": review_meta})
            recorded_intent = item.meta.get("automatic_publication_intent")
            if recorded_intent is not None:
                if recorded_intent != intent or _publication_decision(review_meta) != "low":
                    raise ValueError("automatic publication intent changed")
                if dest.is_file() and dest.read_bytes() == new_text.encode("utf-8"):
                    if item.status == "pending":
                        _write_metadata(item.path, _approved_metadata(item.meta, automatic=True))
                    return dest
            if item.status == "approved":
                raise ValueError("previously published Skill revision changed")
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
        contracts = build_review_contracts(new_text, SkillLibrary(lib_root), settings or get_settings())
        if review_meta.get("contracts_hash") != digest(contracts):
            raise ValueError("related skills/prompts/schemas changed since review")
        _require_gui_publication(review_meta, candidate, current, automatic=automatic)
        if automatic and (manifest is None or library_manifest(lib_root) != manifest):
            raise ValueError("official library/resources changed before automatic publication")
    dest.parent.mkdir(parents=True, exist_ok=True)
    original = dest.read_bytes() if dest.exists() else None
    if original is not None and review_meta.get("prepared_gui_regression"):
        _replace_bytes(item.path.with_suffix(".previous"), original)
    metadata = dict(item.meta)
    if intent is not None:
        metadata["automatic_publication_intent"] = intent
        _write_metadata(item.path, metadata)
    published = new_text if new_text.endswith("\n") else new_text + "\n"
    _replace_bytes(dest, published.encode("utf-8"))
    try:
        _write_metadata(item.path, _approved_metadata(metadata, automatic=automatic))
    except OSError:
        if original is None:
            dest.unlink(missing_ok=True)
        else:
            _replace_bytes(dest, original)
        raise
    return dest


def _deletion_archive(item: PendingPatch) -> Path:
    return item.path.with_suffix(".deleted")


def _approve_deletion(item: PendingPatch, lib_root: Path) -> Path:
    from agent.skills.admission import check_review_contract
    from agent.skills.exploration import build_review_contracts
    from agent.skills.learning import digest, review_gate
    from agent.skills.skill_edits import DeleteCandidate, deletion_base
    from agent.skills.snapshot import _hash, library_manifest
    from shared.config import get_settings

    target = Path(item.target_rel)
    if (target.is_absolute() or target.as_posix() != item.target_rel or target.name != "SKILL.md"
            or target.parts[0] not in {"apps", "generic"}
            or any(part.startswith(("_", ".")) for part in target.parts)):
        raise SkillPathJailError("invalid canonical deletion target")
    dest = lib_root / target
    if item.path.parent.resolve() != lib_root / "_pending":
        raise SkillPathJailError("deletion must use the exact canonical path")

    proof = item.meta.get("review") or {}
    if not isinstance(proof, dict) or not isinstance(proof.get("review"), dict):
        raise ValueError("deletion is missing a review")
    candidate = DeleteCandidate.model_validate(proof.get("candidate_contract") or {})
    review = proof["review"]
    check_review_contract(review)
    if (proof.get("experiment") != "task_explore_delete" or proof.get("eligible") is not True
            or candidate.target != item.target_rel or candidate.base_hash != proof.get("base_hash")
            or digest(candidate.model_dump()) != proof.get("candidate_hash")
            or review.get("candidate_hash") != proof.get("candidate_hash")
            or candidate.evidence != proof.get("evidence") or candidate.scope != proof.get("scope")
            or digest(review) != proof.get("review_hash") or not review_gate(review)
            or review.get("acceptance") != "source_evidence" or review.get("removal_coverage") != "entire_skill"
            or review.get("changes") or review.get("tests") or (review.get("verification") or {}).get("checks")
            or not item.meta.get("source_task_id")
            or item.path.with_suffix(".md").read_bytes()):
        raise ValueError("deletion is not bound to an eligible whole-Skill review")
    environment = proof.get("environment_receipt")
    if environment and (environment.get("unresolved_effects") or environment.get("status") == "interrupted"
                        or environment.get("verified_clean") is False):
        raise ValueError("research environment has unresolved effects")

    manifest = proof.get("baseline_manifest") or {}
    files = manifest.get("files")
    if not isinstance(files, dict) or manifest.get("hash") != _hash(files) or files.get(item.target_rel) != candidate.base_hash:
        raise ValueError("deletion is missing its exact frozen library binding")
    archive = _deletion_archive(item)
    if archive.is_symlink():
        raise SkillPathJailError("deletion archive cannot be a symlink")
    approval = digest({"pending_id": item.id, "source_task_id": item.meta["source_task_id"],
                       "old_text": item.meta.get("old_text"), "review": proof})
    if item.status not in {"pending", "approved"}:
        raise ValueError(f"pending {item.id} is not awaiting approval")
    archived = archive.is_file()
    if archived:
        if (item.meta.get("deletion_approval") != approval
                or hashlib.sha256(archive.read_bytes()).hexdigest() != candidate.base_hash
                or archive.read_text(encoding="utf-8") != item.meta.get("old_text")):
            raise ValueError("existing deletion archive fails its approval binding")
        if item.status == "approved":
            return dest
        # The durable approval and archived bytes bind the move that already
        # happened. Only finalize its record, even if the library later changed.
    else:
        if item.status == "approved":
            raise ValueError("approved deletion archive is missing")
        if dest.resolve() != dest:
            raise SkillPathJailError("deletion must use the exact canonical path")
        library = SkillLibrary(lib_root)
        _, old_text = deletion_base(candidate, library)
        if old_text != item.meta.get("old_text"):
            raise ValueError("deletion diff changed since review")
        if library_manifest(lib_root) != manifest:
            raise ValueError("official library changed since deletion review")
        contracts = build_review_contracts(old_text, library, get_settings())
        if digest(contracts) != proof.get("contracts_hash"):
            raise ValueError("related skills/prompts/schemas changed since deletion review")

    meta = {**item.meta, "deletion_approval": approval}
    if not archived:
        _write_metadata(item.path, meta)
        dest.rename(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != candidate.base_hash:
            if not dest.exists():
                archive.rename(dest)
            raise ValueError("canonical base changed during deletion approval")
    meta.update(status="approved", approved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        _write_metadata(item.path, meta)
    except OSError:
        if not archived and not dest.exists():
            archive.rename(dest)
        raise
    return dest


def approve_pending_group(pending_ids: list[str], *, root: Path | None = None) -> list[Path]:
    """Approve a dependency-closed subset only after checking every file and receipt."""
    lib_root = (root or default_skills_root()).resolve()
    with _PENDING_MUTATIONS, FileLock(_pending_root(lib_root) / ".publication.lock", timeout=5):
        return _approve_pending_group(pending_ids, root=lib_root)


def _approve_pending_group(pending_ids: list[str], *, root: Path) -> list[Path]:
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
    with _PENDING_MUTATIONS, FileLock(_pending_root(root) / ".publication.lock", timeout=5):
        item = get_pending(pending_id, root=root)
        if item is None:
            raise SkillNotFoundError(pending_id)
        if _publication_decision(item.meta.get("review") or {}) == "low":
            raise ValueError("low-impact Skill uses automatic publication")
        if item.meta.get("operation") == "delete":
            if item.status == "rejected":
                return
            if item.status != "pending" or _deletion_archive(item).exists():
                raise ValueError("pending deletion has already been decided or started")
        meta = dict(item.meta)
        meta["status"] = "rejected"
        meta["rejected_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if reason:
            meta["reject_reason"] = reason[:500]
        _write_metadata(item.path, meta)


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
