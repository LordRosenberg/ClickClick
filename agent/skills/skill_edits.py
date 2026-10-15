"""Exact-base draft edits; review and admission still consume complete files."""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class CandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=240)
    gist: str = Field(min_length=1, max_length=240)
    evidence: list[str] = Field(min_length=1, max_length=12, description="Native task records only.")
    scope: str = Field(min_length=1, max_length=800)
    benefit: str = Field(min_length=1, max_length=800)
    risks: str = Field(min_length=1, max_length=800)


class TextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old: str = Field(min_length=1, max_length=18000, description="Exact unique text in the original base; include heading/context for insertion or deletion.")
    new: str = Field(max_length=18000)


class LocalizedCandidate(CandidateSummary):
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA256 of the complete literal base file or supplied current draft.")
    edits: list[TextEdit] = Field(min_length=1, max_length=12)


def materialize_candidate(value, library, *, drafts=()):
    from agent.skills.learning import Candidate
    if not isinstance(value, dict) or "edits" not in value:
        return Candidate.model_validate(value)
    patch = LocalizedCandidate.model_validate(value)
    relative = Path(patch.target)
    if (relative.is_absolute() or ".." in relative.parts or relative.name != "SKILL.md"
            or any(p.startswith(("_", ".")) for p in relative.parts)):
        raise ValueError("invalid localized candidate target")
    target = (library.root / relative).resolve()
    target.relative_to(library.root.resolve())
    patch.target = target.relative_to(library.root.resolve()).as_posix()
    bases = [target.read_text(encoding="utf-8")] if target.exists() else []
    bases += [c["new_text"] for c in drafts if c.get("target") == patch.target and isinstance(c.get("new_text"), str)]
    base = next((t for t in bases if hashlib.sha256(t.encode()).hexdigest() == patch.base_hash), None)
    if base is None:
        raise ValueError("localized edit base is unavailable or stale; new files require complete new_text")
    # Locate against the original base, never against text inserted by another edit.
    spans = []
    for edit in patch.edits:
        start = base.find(edit.old)
        if start < 0 or base.find(edit.old, start + 1) >= 0:
            raise ValueError("localized edit anchor must occur exactly once")
        spans.append((start, start + len(edit.old), edit.new))
    spans.sort()
    if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        raise ValueError("localized edit anchors overlap")
    text = base
    for start, end, replacement in reversed(spans):
        text = text[:start] + replacement + text[end:]
    return Candidate(**patch.model_dump(exclude={"base_hash", "edits"}), new_text=text)
