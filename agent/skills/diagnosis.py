"""Independent deployment diagnoses, separate from admission and utility gates."""
from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class FailureDiagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["delivery_gap", "adoption_gap", "ineffective_rule", "remaining_blocker", "environment_fault", "unknown"]
    reason: str = Field(min_length=1, max_length=900)
    evidence: list[str] = Field(default_factory=list, max_length=8)


POLICY = """After ordinary trials, include diagnosis:{kind,reason,evidence:[native refs]}.
kind: delivery_gap (rule unavailable before its decision), adoption_gap (available
but not followed), ineffective_rule (followed within its scope but incorrect/ineffective),
remaining_blocker (rule works locally, another constraint remains), environment_fault,
or unknown. Cite actual action/observation evidence; exposure is not adoption and
missing activation alone does not prove a delivery failure. Use unknown for missing
or conflicting evidence; a successful trial with no failure to diagnose may use
unknown with evidence:[]. Evidence must copy returned [namespace/]kind:key@version
refs (optional #offset), never JSON paths, bare observation IDs or invented measurements.
Validation fields describe host measurements; they are not native evidence refs.
Diagnose each patch separately in groups. This does not
change admission/utility; retain correct local rules despite overall failure.
Fix supported matching gaps in semantic metadata, ambiguity in instructions;
explore a distinct blocker instead of rewriting a working rule.
"""


def check_diagnosis(value, resolve=None):
    if value is None:
        return None  # Legacy reviews remain valid; absence is not a diagnosis.
    item = FailureDiagnosis.model_validate(value)
    if not item.reason.strip():
        raise ValueError("failure diagnosis requires a meaningful reason")
    if item.kind != "unknown" and not item.evidence:
        raise ValueError("a concrete failure diagnosis requires native evidence")
    for ref in item.evidence:
        if not re.fullmatch(r"(?:[A-Za-z0-9_-]+/)?(?:event|observation|context|operation_summary):[^@\s]+@[0-9]+(?:#[0-9]+)?", ref):
            raise ValueError(f"Invalid diagnosis evidence {ref!r}: copy a returned [namespace/]kind:key@version reference; JSON paths and bare IDs are not evidence")
    if resolve:
        kinds = set()
        for ref in item.evidence:
            try:
                resolved = resolve(ref)
            except ValueError as exc:
                raise ValueError(f"Invalid diagnosis evidence {ref!r}: {exc}; copy its exact returned namespace, key and version") from exc
            if not isinstance(resolved, tuple) or len(resolved) != 3:
                raise ValueError("failure diagnosis requires native execution evidence")
            kind, _, _ = resolved
            if kind not in {"event", "observation", "context", "operation_summary"}:
                raise ValueError("failure diagnosis requires native execution evidence")
            kinds.add(kind)
        if item.kind in {"adoption_gap", "ineffective_rule", "remaining_blocker"} and not {"event", "observation"} <= kinds:
            raise ValueError("behavioral diagnosis requires native action and observation")
    return item.model_dump()


def diagnosis_feedback(review):
    """Keep diagnoses visible even when passed-review details are parked."""
    if not isinstance(review, dict):
        return []
    items = review.get("patch_reviews", {}) or {"": review}
    return [{"target": target, **(check_diagnosis(item.get("diagnosis")) or {
        "kind": "unknown", "reason": "No independent deployment diagnosis supplied; inspect trial evidence before attributing failure.",
        "evidence": []})} for target, item in items.items()]
