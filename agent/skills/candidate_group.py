"""Related skill patches, one frozen trial, and independent local admission."""
from __future__ import annotations

import asyncio
import difflib
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from agent.skills.learning import Candidate, digest
from shared.llm_gateway import GatewayError


class CandidateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patches: list[Candidate] = Field(min_length=1)
    dependencies: dict[str, list[str]] = Field(default_factory=dict)


def candidate_schema():
    return TypeAdapter(Candidate | CandidateGroup).json_schema()


def candidate_files(candidate):
    return {p.target: p.new_text for p in candidate.patches} if isinstance(candidate, CandidateGroup) else {candidate.target: candidate.new_text}


def candidate_hash(candidate):
    return digest(candidate_files(candidate)) if isinstance(candidate, CandidateGroup) else digest(candidate.new_text)


def validate_group(group, library, observed_apps, resolve):
    from agent.skills.learning import validate_candidate
    from agent.skills.library import parse_skill_markdown
    old, paths = {}, set()
    for patch in group.patches:
        patch.new_text = patch.new_text.rstrip() + "\n"
        previous = validate_candidate(patch, library, observed_apps)
        physical = library.root / patch.target
        if patch.target in old or physical in paths:
            raise ValueError("duplicate group target")
        paths.add(physical)
        old[patch.target] = previous
        for ref in patch.evidence:
            resolve(ref)
    identities = {p.id: p.path.resolve() for p in library.load_all() if p.path.resolve() not in paths}
    for patch in group.patches:
        pack = parse_skill_markdown(patch.new_text)
        if pack.active and pack.id in identities:
            raise ValueError("duplicate active skill id in group overlay")
        identities[pack.id] = (library.root / patch.target).resolve()
    check_dependencies(group, set(old))
    return old


def check_dependencies(group, targets):
    if len(targets) != len(group.patches):
        raise ValueError("duplicate group target")
    if any(k not in targets or len(v) != len(set(v)) or any(d not in targets for d in v)
           for k, v in group.dependencies.items()):
        raise ValueError("unknown or duplicate dependency")
    visiting, visited = set(), set()
    def visit(target):
        if target in visiting:
            raise ValueError("cyclic skill dependency")
        if target in visited:
            return
        visiting.add(target)
        for dependency in group.dependencies.get(target, []):
            visit(dependency)
        visiting.remove(target)
        visited.add(target)
    for target in targets:
        visit(target)


GROUP_POLICY = """Review a group, not one file. Return JSON:
patch_reviews: object keyed by EVERY exact target. Each value has verdict, the six
criteria, changes, tests, acceptance, plus effect:{status:supported|contradicted|
unassessed,reason:text,evidence:[native refs]}. verification:{checks:[],reason:text}.
Use the same criteria below independently for each indivisible patch. Reject mixed
supported/unsupported instructions or request a reduced patch; never admit its good
clauses while installing its bad clauses. Audit declared dependencies and missing
dependencies. Independent patches may pass despite another patch or task failing.
Do not attribute a global regression to all patches without native evidence, or
attribute joint success to each. A known harmful local effect blocks that patch.
For a claimed observed local effect, cite the action and observation proving it;
exact prompt exposure alone is not adoption. Source-evidence admission may remain
useful with unassessed ordinary adoption; record utility as unknown.
Knowledge belongs to the app/intent where it applies, not automatically the source
or last app. Check whether guidance can arrive before its changed decision. An
internal second-app rule needs no first-app dependency unless its effect really
requires one. Choose the smallest joint verification, never a matrix per patch.
During final review use declared available native histories for trial facts.
contracts.shared applies to every patch; contracts.patches holds each file's
related guidance. complete_contract_hashes binds the unabridged contracts.
Ignore the single-file output shape below; its substantive six criteria still apply.
"""


def group_review_contracts(contracts):
    """Send identical runtime contracts once; keep each file's guidance and hash."""
    first = next(iter(contracts.values()))
    shared = {k: v for k, v in first.items() if k != "related_skills" and all(c.get(k) == v for c in contracts.values())}
    return {"shared": shared,
        "patches": {t: {k: v for k, v in c.items() if k not in shared} for t, c in contracts.items()},
        "complete_contract_hashes": {t: digest(c) for t, c in contracts.items()}}


def check_group_review(review, group, resolve):
    from agent.skills.admission import check_review_contract
    from agent.skills.learning import CRITERIA
    from agent.skills.verification import verification_plan
    targets = set(candidate_files(group))
    items = review.get("patch_reviews")
    if not isinstance(items, dict) or set(items) != targets:
        raise ValueError("group review must cover every target exactly")
    verification_plan(review.get("verification"))
    for target, item in items.items():
        check_review_contract(item)
        criteria = item.get("criteria", {})
        if (item.get("verdict") not in {"pass", "revise", "reject", "insufficient"}
                or set(criteria) != set(CRITERIA)
                or any(not isinstance(v, dict) or v.get("status") not in {"pass", "fail", "insufficient"}
                       or not isinstance(v.get("reason"), str) or not v["reason"].strip() for v in criteria.values())):
            raise ValueError("invalid per-patch criteria")
        effect = item.get("effect")
        if (not isinstance(effect, dict) or set(effect) != {"status", "reason", "evidence"}
                or effect["status"] not in {"supported", "contradicted", "unassessed"}
                or not isinstance(effect["reason"], str) or not effect["reason"].strip()
                or not isinstance(effect["evidence"], list)
                or any(not isinstance(ref, str) or not ref for ref in effect["evidence"])):
            raise ValueError("invalid per-patch effect")
        kinds = []
        for ref in effect["evidence"]:
            record = resolve(ref)
            kinds.append(record[0] if isinstance(record, tuple) else None)
        if effect["status"] != "unassessed" and not {"event", "observation"} <= set(kinds):
            raise ValueError("local effect requires native action and observation")


def dependency_admission(group, reviews, evidence, *, validation=None, trial_group=None, old=None, manifest=None):
    from agent.skills.learning import review_gate
    measured = bool(validation and trial_group and candidate_files(trial_group) == candidate_files(group)
        and group_utility_gate(group, old, validation, manifest))
    accepted, reasons = set(), {}
    for patch in group.patches:
        item = reviews[patch.target]
        native = any("/observation:" in row.get("source", "") and row.get("text") for row in evidence[patch.target])
        mode_supported = item["acceptance"] == "source_evidence" or (
            item["acceptance"] == "measured_utility" and measured and item["effect"]["status"] == "supported")
        if (review_gate(item) and mode_supported and native
                and item["effect"]["status"] != "contradicted"):
            accepted.add(patch.target)
        else:
            reasons[patch.target] = "review_or_local_evidence_blocked"
    while True:
        blocked = {t for t in accepted if not set(group.dependencies.get(t, [])) <= accepted}
        if not blocked:
            break
        accepted -= blocked
        reasons.update({t: "dependency_not_admitted" for t in blocked})
    return accepted, reasons


async def run_candidate_group(group, *, task, settings, library, publication_root, backend,
                              budget, frozen, packet, validator, phase_context,
                              revision=0, seen=None, validation_cache=None):
    """Preflight all patches once, trial the eligible group once, then admit locally."""
    from agent.skills.learning import REVIEW_SYSTEM, READONLY_SYSTEM, LearningStopped, validation_for_learning
    from agent.skills.learner import resolve_learner_model
    from agent.skills.library import parse_skill_markdown
    from agent.skills.pending import stage_pending_patch
    from agent.skills.verification import available_checks, check_conditions, verification_plan
    if frozen is None:
        raise ValueError("group learning requires a frozen library")
    old = validate_group(group, library, backend.observed_apps(), backend.resolve_evidence)
    contracts = {p.target: backend.review_contracts(p) for p in group.patches}
    evidence = {p.target: backend.review_evidence(p) for p in group.patches}
    key = candidate_hash(group)
    seen = set() if seen is None else seen
    validation_cache = {} if validation_cache is None else validation_cache
    input_key = digest({"candidate": group.model_dump(), "old": old, "contracts": contracts, "evidence": evidence,
        "proposed_verification": phase_context.get("proposed_verification")})
    if input_key in seen:
        raise LearningStopped("unchanged_group_and_evidence")
    seen.add(input_key)
    shared_evidence = {r.get("source", digest(r)): r for rows in evidence.values() for r in rows}
    host_checks = available_checks(validator)
    payload = {"source": packet, "candidate": group.model_dump(), **phase_context,
        "verification_checks": host_checks, "verification_conditions": check_conditions(validator),
        "contracts": group_review_contracts(contracts), "evidence": list(shared_evidence.values()),
        "diffs": {p.target: "\n".join(difflib.unified_diff(old[p.target].splitlines(), p.new_text.splitlines())) for p in group.patches}}
    async def review(phase, validation=None):
        budget.check()
        result = await backend.review(settings.skill_reviewer_model or resolve_learner_model(settings),
            GROUP_POLICY + REVIEW_SYSTEM, {**payload, "phase": phase,
            "validation": validation_for_learning(validation)}, budget)
        check_group_review(result, group, backend.resolve_evidence)
        return result
    preflight = await review("group_preflight")
    # Remove concrete preflight failures and their dependents before device spending.
    runnable = {t for t, item in preflight["patch_reviews"].items()
                if item["verdict"] != "reject" and all(v["status"] != "fail" for v in item["criteria"].values())}
    while True:
        blocked = {t for t in runnable if not set(group.dependencies.get(t, [])) <= runnable}
        if not blocked:
            break
        runnable -= blocked
    validation = None
    selected = verification_plan(preflight.get("verification"), available=host_checks)
    # A subset is a different candidate: never use a full-group receipt for it.
    trial_group = CandidateGroup(patches=[p for p in group.patches if p.target in runnable],
        dependencies={t: d for t, d in group.dependencies.items() if t in runnable}) if runnable else None
    final, completed = preflight, []
    if selected and selected["checks"] and trial_group:
        if not getattr(getattr(validator, "__self__", validator), "supports_candidate_groups", False):
            raise ValueError("validator does not support candidate groups")
        binding = (candidate_hash(trial_group), digest({t: old[t] for t in runnable}), digest(contracts))
        attempted = set()
        # Reuse only exact-body, exact-base checks from this local repair chain.
        for cache_key, cached in validation_cache.items():
            if cache_key[:3] == binding:
                attempted.update(cache_key[3])
                validation = merge_group_validation(validation, cached)
        payload["trial_targets"] = sorted(runnable)
        while selected and selected["checks"]:
            requested = [c for c in selected["checks"] if c not in attempted]
            if not requested:
                if final is preflight and validation is not None:
                    final = await review("group_final", validation)
                    selected = verification_plan(final.get("verification"), available=host_checks)
                    continue
                break
            if budget.max_calls - budget.calls < 1 or budget.remaining_seconds() <= 0:
                completed.append({"selected": selected, "unrun": requested, "reason": "verification_capacity_exhausted"})
                break
            import time
            started = time.monotonic()
            try:
                fresh = await asyncio.wait_for(validator(trial_group,
                    {t: old[t] for t in runnable}, budget=budget, checks=requested), max(.1, budget.remaining_seconds()))
                check_trial_binding(trial_group, {t: old[t] for t in runnable}, fresh, frozen.manifest)
                validation_cache[(*binding, tuple(requested))] = fresh
                attempted.update(requested)
                validation = merge_group_validation(validation, fresh)
            finally:
                budget.validation_seconds += time.monotonic() - started
            completed.append({"selected": selected, "executed_request": requested,
                "completed": [t["kind"] for t in fresh.get("trials", [])],
                "deferred": fresh.get("deferred_kinds", [])})
            payload["completed_verification"] = completed
            final = await review("group_final", validation)
            selected = verification_plan(final.get("verification"), available=host_checks)
    # New Reviewer reads may establish a local effect absent from the initial packet.
    # Bind their literal records, not merely the model's effect verdict.
    for patch in group.patches:
        effect_refs = final["patch_reviews"][patch.target]["effect"]["evidence"]
        expanded = patch.model_copy(update={"evidence": list(dict.fromkeys([*patch.evidence, *effect_refs]))})
        evidence[patch.target] = backend.review_evidence(expanded)
    environment = getattr(backend, "environment", None)
    receipt = environment.receipt() if environment else None
    if receipt and (receipt.get("unresolved_effects") or receipt.get("status") == "interrupted"):
        raise LearningStopped("unresolved_environment")
    accepted, reasons = dependency_admission(group, final["patch_reviews"], evidence,
        validation=validation, trial_group=trial_group, old=old, manifest=frozen.manifest)
    group_receipt = {"version": 1, "candidate": group.model_dump(), "base": old,
        "group_policy_hash": digest(GROUP_POLICY),
        "manifest": frozen.manifest, "contracts": contracts, "evidence": evidence,
        "review": final, "validation": validation,
        "trial_candidate": trial_group.model_dump() if validation else None,
        "verification_execution": completed,
        "accepted": sorted(accepted), "environment": receipt,
        "utility": "per_patch_reviewed; aggregate utility not established"}
    pending = []
    for patch in group.patches:
        item = stage_pending_patch(target_rel=patch.target, new_text=patch.new_text,
            gist=patch.gist, source_task_id=task.id, outcome=task.status.value,
            app=parse_skill_markdown(patch.new_text).app, root=publication_root, old_text=old[patch.target],
            review_metadata={"experiment": "task_explore_group", "candidate_hash": digest(patch.new_text),
                "base_hash": digest(old[patch.target]), "eligible": patch.target in accepted,
                "gate_reason": ("local_source_evidence_reviewed" if final["patch_reviews"][patch.target]["acceptance"] == "source_evidence"
                    else "measured_group_utility_with_local_effect") if patch.target in accepted else reasons[patch.target],
                "group_hash": key, "group_receipt": group_receipt, "receipt_hash": digest(group_receipt)})
        pending.append({"id": item.id, "target": patch.target, "eligible": patch.target in accepted})
    feedback = {"origin": "host", "candidate_group_hash": key, "review": final,
                "validation": validation_for_learning(validation), "accepted": sorted(accepted),
                "verification_execution": completed,
                "policy": "Local admission is separate from whole-task outcome. Unknown utility stays unknown."}
    record = getattr(backend, "record_completed_verification_feedback", None)
    if callable(record):
        record(feedback, budget)
    result = {"ok": True, "eligible": bool(accepted), "pending_ids": pending,
        "accepted_targets": sorted(accepted), "reason": "awaiting_human_approval" if accepted else "blocked_by_review_or_validation",
        "reviews": [final], "preflight_reviews": [preflight], "verification_feedback": [feedback],
        "cost": budget.snapshot()}
    repair = any(item["verdict"] in {"revise", "insufficient"} and (item["changes"] or item["tests"])
        for item in final["patch_reviews"].values())
    analysis = getattr(getattr(backend, "learner_conversation", None), "analysis", None) or {}
    worthwhile = [i for i in analysis.get("omission_candidates", []) if i.get("priority") == "high" and i.get("status") in {"supported", "unknown"}]
    if revision < 2 and (repair or worthwhile) and budget.max_calls-budget.calls >= 3 and budget.remaining_seconds() > 0:
        # Read-only repair keeps the isolated trial/cleanup boundary closed. New
        # affordances still need research evidence; an unproved rule stays pending.
        try:
            decision = await backend.diagnose(resolve_learner_model(settings), READONLY_SYSTEM,
                {"source": packet, **phase_context, "candidate_json_schema": candidate_schema(),
                 "review_feedback": final, "candidate": group.model_dump(),
                 "validation": validation_for_learning(validation), "remaining_opportunities": worthwhile,
                 "retained_targets": sorted(accepted), "budget": budget.snapshot(),
                 "live_exploration_disabled": True}, budget)
            if decision.get("decision") == "propose":
                value = decision.get("candidate") or {}
                revised = CandidateGroup.model_validate(value) if "patches" in value else CandidateGroup(patches=[Candidate.model_validate(value)])
                revised_old = validate_group(revised, library, backend.observed_apps(), backend.resolve_evidence)
                next_context = {**phase_context, "proposed_verification": verification_plan(decision.get("verification"))}
                next_key = digest({"candidate": revised.model_dump(), "old": revised_old,
                    "contracts": {p.target: backend.review_contracts(p) for p in revised.patches},
                    "evidence": {p.target: backend.review_evidence(p) for p in revised.patches},
                    "proposed_verification": next_context["proposed_verification"]})
                if next_key not in seen:
                    # Only the next independent Reviewer selects proposed checks.
                    followup = await run_candidate_group(revised, task=task, settings=settings, library=library,
                        publication_root=publication_root, backend=backend, budget=budget, frozen=frozen,
                        packet=packet, validator=validator, phase_context=next_context,
                        revision=revision+1, seen=seen, validation_cache=validation_cache)
                    for field in ("pending_ids", "reviews", "preflight_reviews", "verification_feedback"):
                        result[field].extend(followup[field])
                    result["accepted_targets"] = sorted(set(result["accepted_targets"]) | set(followup["accepted_targets"]))
                    result["eligible"] = bool(result["accepted_targets"])
                    result["cost"] = budget.snapshot()
                    result["reason"] = "awaiting_human_approval" if result["eligible"] else followup["reason"]
            elif decision.get("decision") != "skip":
                raise ValueError("group read-only repair must propose or skip")
        except (LearningStopped, GatewayError, ValueError, TimeoutError) as exc:
            result["revision_stop"] = {"reason": str(exc), "policy": "Retain previously reviewed pending patches; unresolved repair is not a local disproof."}
        result["cost"] = budget.snapshot()
    return result


def merge_group_validation(previous, fresh):
    """Retain earlier exact-group pairs, including failures, across selected checks."""
    if previous is None:
        return fresh
    bindings = ("candidate_hash", "base_hash", "baseline_library_hash", "candidate_library_hash")
    if any(previous.get(k) != fresh.get(k) for k in bindings):
        raise ValueError("cannot merge differently bound group checks")
    trials = [*previous.get("trials", []), *fresh.get("trials", [])]
    observed = {t.get("kind") for t in trials}
    deferred = sorted((set(previous.get("deferred_kinds", [])) | set(fresh.get("deferred_kinds", []))) - observed)
    return {**previous, **fresh, "trials": trials, "deferred_kinds": deferred}


def check_group_receipt(receipt):
    """Recompute local eligibility and dependency closure at publication time."""
    group = CandidateGroup.model_validate(receipt["candidate"])
    targets = set(candidate_files(group))
    check_dependencies(group, targets)
    if (receipt.get("version") != 1 or receipt.get("group_policy_hash") != digest(GROUP_POLICY)
            or set(receipt["base"]) != targets or set(receipt["contracts"]) != targets or set(receipt["evidence"]) != targets):
        raise ValueError("invalid group receipt")
    literal = {r.get("source"): r for rows in receipt["evidence"].values() for r in rows}
    def resolve(ref):
        qualified = ref if "/" in ref else "source/" + ref
        row = literal.get(qualified)
        if row is None or not row.get("text"):
            raise ValueError("missing bound local-effect evidence")
        return qualified.split("/", 1)[1].split(":", 1)[0], row, 0
    check_group_review(receipt["review"], group, resolve)
    trial = CandidateGroup.model_validate(receipt["trial_candidate"]) if receipt.get("trial_candidate") else None
    accepted, _ = dependency_admission(group, receipt["review"]["patch_reviews"], receipt["evidence"],
        validation=receipt.get("validation"), trial_group=trial, old=receipt["base"], manifest=receipt["manifest"])
    if set(receipt["accepted"]) != accepted:
        raise ValueError("group admission changed")
    environment = receipt.get("environment")
    if environment and (environment.get("unresolved_effects") or environment.get("status") == "interrupted" or environment.get("verified_clean") is False):
        raise ValueError("group environment unverified")
    if receipt.get("validation") is not None:
        trial = CandidateGroup.model_validate(receipt["trial_candidate"])
        if any(candidate_files(group).get(t) != text for t, text in candidate_files(trial).items()):
            raise ValueError("group trial changed")
        check_trial_binding(trial, {t: receipt["base"][t] for t in candidate_files(trial)}, receipt["validation"], receipt["manifest"])
    return group, accepted


def check_trial_binding(group, old, validation, manifest, *, require_scored=False):
    from agent.skills.snapshot import _hash
    import hashlib
    if not isinstance(validation, dict) or validation.get("candidate_hash") != candidate_hash(group) or validation.get("base_hash") != digest(old):
        raise ValueError("stale group validation")
    files = dict(manifest["files"])
    files.update({p: hashlib.sha256(t.encode()).hexdigest() for p, t in candidate_files(group).items()})
    if validation.get("baseline_library_hash") != manifest["hash"] or validation.get("candidate_library_hash") != _hash(files):
        raise ValueError("stale group library validation")
    for trial in validation.get("trials", []):
        if (not trial.get("case_id") or not trial.get("config_hash")
                or trial.get("baseline_library_hash") != manifest["hash"]
                or trial.get("candidate_library_hash") != _hash(files)):
            raise ValueError("unverified group trial")
        if require_scored and (not trial.get("matched_environment") or not trial.get("independent_oracle")
                or type(trial.get("baseline_success")) is not bool or type(trial.get("candidate_success")) is not bool):
            raise ValueError("unmatched or unscored group trial")


def group_utility_gate(group, old, validation, manifest):
    """Keep the existing complete-coverage utility bar for aggregate claims."""
    from agent.skills.learning import COVERAGE, trial_has_regression
    try:
        check_trial_binding(group, old, validation, manifest, require_scored=True)
    except (ValueError, TypeError, KeyError):
        return False
    trials = validation.get("trials", [])
    if not COVERAGE <= {t.get("kind") for t in trials} or any(trial_has_regression(t) for t in trials):
        return False
    return any(t["candidate_success"] and (not t["baseline_success"] or any(
        type(t.get(k)) in (int, float) and t[k] > 0 for k in ("efficiency_gain", "request_gain"))) for t in trials)
