"""Versioned, source-evidence admission; does not assert measured task utility."""
from __future__ import annotations

POLICY = """Choose acceptance: source_evidence or measured_utility.
source_evidence: native observations support the scoped rule, detectable trigger,
specific useful decision change and nonredundancy. For omitted/replaced steps,
verify later state/information dependencies and preserved goal constraints. All six
criteria must pass. Do not require fresh task pairs or measured speedup for a
supported local fact/pitfall; do not approve mere observations or broad bans.
measured_utility: the candidate actually claims improved overall success/cost and
needs ordinary comparative evidence. Prefer narrowing unsupported claims when the
remaining rule still changes a useful decision. Never relabel a measured claim to
avoid its evidence requirements. Unknown frequency or average savings can remain
in criteria reasons; unknown correctness or preservation blocks acceptance.
Use tests only for unresolved acceptance blockers, each {claim,gap,check,decision}:
the claim at risk, missing fact, smallest discriminating check, and how its outcomes
change admission. Prefer native reads, then a local probe; complete task trials only
when necessary. List all currently visible blockers together. Do not request tests
only to quantify unclaimed benefits. No tests when existing evidence suffices.
A review of an unchanged body/evidence need not be repeated. Revisions must resolve
an identified blocker or remove unsupported scope, not cosmetically rephrase it.
"""


def check_review_contract(review):
    if review.get("acceptance") not in {"source_evidence", "measured_utility"}:
        raise ValueError("review requires an acceptance evidence mode")
    if not isinstance(review.get("tests"), list) or not isinstance(review.get("changes"), list):
        raise ValueError("review requires changes and blocking tests lists")
    for test in review["tests"]:
        if not isinstance(test, dict) or set(test) != {"claim", "gap", "check", "decision"}:
            raise ValueError("blocking test requires claim/gap/check/decision")
        if any(not isinstance(v, str) or not v.strip() for v in test.values()):
            raise ValueError("blocking test fields must be nonempty text")
    from agent.skills.verification import verification_plan
    verification_plan(review.get("verification"))
    if review.get("verdict") == "pass" and (review["tests"] or review["changes"]):
        raise ValueError("passing review cannot retain acceptance blockers")


def source_validation(candidate, old, *, review, evidence, source_id, contracts, manifest, ordinary_validation=None):
    from agent.skills.learning import digest
    from agent.skills.snapshot import _hash
    files = dict(manifest["files"])
    import hashlib
    files[candidate.target] = hashlib.sha256(candidate.new_text.encode()).hexdigest()
    return {"mode": "source_evidence", "version": 1,
        "candidate_hash": digest(candidate.new_text), "base_hash": digest(old),
        "candidate_contract_hash": digest(candidate.model_dump()),
        "source_task_id": source_id, "evidence_refs": list(candidate.evidence),
        "evidence": evidence, "evidence_hash": digest(evidence),
        "review_hash": digest(review), "contracts_hash": digest(contracts),
        "baseline_library_hash": manifest["hash"], "candidate_library_hash": _hash(files),
        "utility": "unmeasured",
        **({"ordinary_validation": ordinary_validation, "ordinary_validation_hash": digest(ordinary_validation)}
           if ordinary_validation is not None else {})}


def source_gate(candidate, validation, review, contracts_hash):
    from agent.skills.learning import digest, review_gate
    if validation.get("version") != 1:
        return False, "unknown_source_evidence_contract"
    try:
        check_review_contract(review or {})
    except ValueError:
        return False, "invalid_source_review"
    if (not review_gate(review) or review["acceptance"] != "source_evidence"
            or validation.get("review_hash") != digest(review)):
        return False, "source_review_mismatch"
    if not contracts_hash or validation.get("contracts_hash") != contracts_hash:
        return False, "source_contracts_mismatch"
    if (validation.get("candidate_contract_hash") != digest(candidate.model_dump())
            or validation.get("evidence_refs") != candidate.evidence):
        return False, "source_candidate_mismatch"
    evidence = validation.get("evidence")
    if (not validation.get("source_task_id") or not isinstance(evidence, list) or not evidence
            or validation.get("evidence_hash") != digest(evidence)):
        return False, "missing_source_evidence"
    # A report/note or model approval alone is never an observed app fact.
    if not any(isinstance(row, dict) and "/observation:" in row.get("source", "")
               and row.get("text") for row in evidence):
        return False, "missing_native_observation"
    ordinary = validation.get("ordinary_validation")
    if ordinary is not None:
        from agent.skills.learning import validation_gate
        if (not isinstance(ordinary, dict) or ordinary.get("mode") == "source_evidence"
                or validation.get("ordinary_validation_hash") != digest(ordinary)
                or any(ordinary.get(key) != validation.get(key) for key in
                       ("candidate_hash", "base_hash", "baseline_library_hash", "candidate_library_hash"))):
            return False, "source_ordinary_evidence_mismatch"
        passed, reason = validation_gate(candidate, validation["base_hash"], ordinary)
        if not passed and reason not in {"incomplete_coverage", "no_marginal_benefit"}:
            return False, reason
        if not ordinary.get("trials") and ordinary.get("validation_stage") != "budget_deferred":
            return False, "missing_ordinary_evidence"
    if validation.get("utility") != "unmeasured":
        return False, "source_evidence_is_not_measured_utility"
    return True, "source_evidence_reviewed"
