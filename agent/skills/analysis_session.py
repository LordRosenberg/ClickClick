"""One source-local Learner conversation; no cross-task memory or publication."""
from __future__ import annotations

import copy
import json
from uuid import uuid4
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=700)
    status: Literal["observed", "hypothesis", "unknown", "contradicted"]
    evidence: list[str] = Field(default_factory=list, description="Copy returned versioned history or official_skill references; never invent IDs.")


class Omission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str] = Field(min_length=1, max_length=12, description="Versioned event references, never prose step descriptions.")
    condition: str = Field(min_length=1, max_length=500)
    purpose: str = Field(default="", max_length=400, description="User intent under which these steps are a detour; not an app-wide ban.")
    cost: str = Field(default="", max_length=600, description="Observed excess actions, added setup/recovery, and estimated net saving or unknown; cite basis in evidence.")
    priority: Literal["high", "low", "unknown"] = "unknown"
    dependency_check: str = Field(min_length=1, max_length=700)
    changed_decision: str = Field(min_length=1, max_length=500)
    status: Literal["supported", "unknown", "rejected"]
    evidence: list[str] = Field(min_length=1, description="Copy returned versioned history or official_skill references.")


class TrajectoryAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    working_path: list[Finding] = Field(default_factory=list, max_length=12)
    omission_candidates: list[Omission] = Field(default_factory=list, max_length=8)
    prerequisites: list[Finding] = Field(default_factory=list, max_length=12)
    unresolved: list[Finding] = Field(default_factory=list, max_length=12)
    next_reads: list[str] = Field(default_factory=list, max_length=8)
    candidate_value: str = Field(default="", max_length=1200)


def validate_analysis(value, resolve):
    analysis = TrajectoryAnalysis.model_validate(value)
    if len(json.dumps(analysis.model_dump(), ensure_ascii=False)) > 8000:
        raise ValueError("trajectory analysis exceeds checkpoint capacity")
    errors = []
    def check_reference(ref, *, event=False):
        try:
            resolved = resolve(ref)
            if event and (not isinstance(resolved, tuple) or resolved[0] != "event"):
                raise ValueError("omitted steps require event references")
        except ValueError as exc:
            errors.append(f"{ref}: {exc}")
    for item in [*analysis.working_path, *analysis.prerequisites, *analysis.unresolved]:
        if item.status in {"observed", "contradicted"} and not item.evidence:
            raise ValueError("observed analysis claims require native evidence")
        for ref in item.evidence:
            check_reference(ref)
    for item in analysis.omission_candidates:
        for ref in item.steps:
            check_reference(ref, event=True)
        for ref in item.evidence:
            check_reference(ref)
    if errors:
        raise ValueError("Invalid analysis evidence references: " + "; ".join(errors[:8]))
    # Next reads are questions/locators, not asserted native evidence.
    return analysis.model_dump()


TRAJECTORY_FORMAT = {
    "working_path": "list of findings",
    "prerequisites": "list of findings",
    "unresolved": "list of findings; preserve conflicting evidence and unknown dependencies",
    "finding": {"statement": "text", "status": "observed|hypothesis|unknown|contradicted", "evidence": ["returned history or official_skill reference"]},
    "omission_candidates": [{"steps": ["native event reference"], "purpose": "specific user intent", "condition": "detectable precondition",
        "cost": "observed excess actions; extra setup/recovery; net saving estimated or unknown",
        "priority": "high for substantial avoidable cost/harm; low for harmless minor work; unknown otherwise",
        "dependency_check": "state/information effects and later consumers; unknowns explicit",
        "changed_decision": "specific avoided work", "status": "supported|unknown|rejected", "evidence": ["native reference"]}],
    "next_reads": ["specific unresolved question or native locator"],
    "candidate_value": "changed decision, avoided cost and overlap with existing guidance",
    "format_policy": "Return the six analysis fields only; finding is an item definition, not an output key. Keep the checkpoint <=8000 characters. Examples are not evidence."}


ANALYSIS_POLICY = """Continue this job's analysis across phases; the latest input and budget govern.
Include JSON trajectory_analysis on decisions. When older reads need checkpointing,
include a concise trajectory_analysis alongside the next read tool calls, so the
host can reuse it without a separate summary request. Use supplied format and
already delivered evidence; newly requested reads stay unknown. Preserve named
gaps and dependencies; aim below 4000 characters without dropping necessary facts.
Read to resolve a named gap. For needed visual state absent from text, read the
known observation with read_history view=image; text omission is not screen absence.
Resume checkpointed progress with its original statuses and native citations.
Priority reflects net cost/harm, not detour presence. Preserve unresolved high-value
opportunities after recovery; leave harmless minor work alone.
Re-read only for a named gap, contradiction or review dispute, not merely because
older exchanges left the window. Checkpoints are fallible, not independent proof.
Namespace names are locators, not evidence references; cite pair_evidence_ref
measurements for host-recorded trial outcomes, and native records for app effects.
read_diagnostic_context locators are not evidence citations; cite the returned
native or official_skill references for claims from those directories.
"""

FEW_SHOTS = """FICTIONAL examples of reasoning only; no real app route or evidence:
A. An imaginary sorter: A/B discover an observed control used by C without changing
its required state. A rule can retain that fact and use C directly, keeping C's
prerequisites and verification. If D independently shows a wrong setting decision,
one same-goal candidate may combine the supported entry shortcut and setting fix.
Keep identity/context checks; if setup is uncertain, require its effect before C.
Prefer C conditionally without proving globally shortest routes or absent alternatives.
Another app owns its internal rule; only the handoff belongs to the starting app.
An unknown reason for D's failure supports no setting fix and neither refutes the
local omission nor proves task recovery.
B. An imaginary test rig: P reports failure but enables calibration needed by Q.
Keep P's required effect; a cheaper setup is only a hypothesis. Conflicting reports
and observations stay unresolved. Never copy these examples into actual app claims.
"""


COMPACTION_SYSTEM = """You are the SAME Learner checkpointing this source-local
conversation before older exchanges leave the working window. Current task and
constraints stay pinned: do not repeat them as findings. Keep only analysis progress,
aim below 4000 characters, and cite native evidence for every observed/contradicted
finding. Omission steps must be event references, not descriptions. Existing-skill
claims may cite returned official_skill references; these do not prove app behavior.
read_diagnostic_context locators are not evidence citations.
Return JSON matching
the supplied trajectory_analysis_schema, without prose. Preserve observed path,
omission candidates with purpose, conditions, observed/estimated cost and state/information dependencies, contradictory
or missing evidence, failed hypotheses and the next necessary reads. Preserve
previous findings with their statuses and native citations unless cited new evidence
changes them. Merge overlapping findings and resolved read questions instead of
repeating them; retain their conclusions, statuses, citations and needed dependencies.
Prioritize facts that change the current decision, not repeated passed-review prose.
Keep unresolved questions until evidence resolves them. Never upgrade a hypothesis or report to an observation. Failed actions may
establish prerequisites. Use only registered native references found in the supplied
conversation. Few-shot examples are fictional, never evidence. This checkpoint
cannot approve a skill, claim utility or add any app solution not in the evidence.
"""


def diagnostic_payload_content(payload):
    """Keep unchanged contracts before changing phase facts; omit nothing."""
    prefix = ("trajectory_analysis_format", "candidate_json_schema",
        "skill_document_contract", "capability_policy", "executor_action_directory",
        "device_profiles", "environment_capabilities", "existing_active_guidance")
    ordered = {key: payload[key] for key in prefix if key in payload}
    ordered.update((key, value) for key, value in payload.items() if key not in ordered)
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def retained_admission_context(payload, directories):
    """Park completed admission details; keep rule, outcomes and unresolved facts."""
    from agent.skills.learning import digest, review_gate
    import copy
    review = payload.get("review_feedback")
    if not isinstance(review, dict) or not review_gate(review) or review.get("changes") or review.get("tests"):
        return payload
    result = copy.deepcopy(payload)
    def archive(value):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        ref = digest(text)
        directories[ref] = text
        return {"read_tool": "read_diagnostic_context", "ref": ref, "content_hash": ref,
            "characters": len(text), "policy": "Exact completed record; read only for a new conflict or named gap."}
    result["review_feedback"] = {"verdict": review["verdict"], "acceptance": review.get("acceptance"),
        "criteria": {k: {"status": v["status"], "reason": "Passed for the exact retained rule; details remain readable."}
            for k, v in review["criteria"].items()}, "changes": [], "tests": [], "details": archive(review)}
    candidate = result.get("candidate")
    if isinstance(candidate, dict) and isinstance(candidate.get("new_text"), str):
        result["candidate"] = {k: candidate[k] for k in ("target", "new_text", "scope") if k in candidate}
        result["candidate"]["details"] = archive(candidate)
    validation = result.get("validation")
    if isinstance(validation, dict):
        keep = ("kind", "case_id", "goal", "matched_environment", "independent_oracle", "config_hash",
            "baseline_success", "candidate_success", "efficiency_gain", "request_gain", "pair_evidence_ref",
            "baseline_execution_namespace", "candidate_execution_namespace")
        trials = []
        for trial in validation.get("trials", []):
            compact = {k: trial[k] for k in keep if k in trial}
            for side in ("baseline_execution", "candidate_execution"):
                execution = trial.get(side)
                if isinstance(execution, dict):
                    compact[side] = {k: execution[k] for k in ("cost", "execution_limits", "candidate_activation", "patch_activation") if k in execution}
            trials.append(compact)
        result["validation"] = {k: validation[k] for k in ("candidate_hash", "base_hash", "baseline_library_hash", "candidate_library_hash", "validation_stage") if k in validation}
        result["validation"].update(trials=trials, details=archive(validation))
    result["retained_admission_policy"] = "Retain completed local admission. Original failed-source facts stay unchanged. Assess only a distinct significant opportunity or new contradiction; do not re-prove passed criteria. Whole-task outcomes and local effects remain separate."
    return result


class LearnerConversation:
    def __init__(self, artifacts, binding):
        self.artifacts = artifacts
        self.binding = copy.deepcopy(binding)
        self.id = uuid4().hex
        self.history = []
        self.analysis = None
        self.directories = {}
        self.version = 0
        self.ref = None
        self.compactions = 0
        self.reused_checkpoints = 0
        self.analysis_current = False
        self.stages = 0
        self.seen_results = set()
        self.read_fragments = {}
        self.read_spans = {}
        self.stalled_rounds = 0

    def restore_read_progress(self, archive):
        """Restore literal-read bookkeeping only for the same source/library binding.

        Callers that rename history namespaces must replay normalized reads instead
        of restoring opaque identity hashes; these counts are not factual analysis.
        """
        if archive.get("source_binding") != self.binding:
            raise ValueError("read progress belongs to a different source binding")
        progress = archive.get("read_progress") or {}
        self.seen_results = set(progress.get("seen_results", []))
        self.read_fragments = copy.deepcopy(progress.get("fragments", {}))
        self.read_spans = copy.deepcopy(progress.get("spans", {}))

    def observe_read_result(self, packet):
        """Count supplied content, not changed pagination or result-envelope prose."""
        if not isinstance(packet, dict) or packet.get("status") in {"failed", "deferred", "error"}:
            return False
        from agent.skills.learning import digest
        data = packet.get("data", packet)
        if not isinstance(data, dict):
            return False
        fresh = False
        def fragment(identity, text):
            nonlocal fresh
            if not isinstance(text, str) or not text:
                return
            offset = identity.get("offset")
            if type(offset) is int and offset >= 0:
                base = {key:value for key,value in identity.items() if key != "offset"}
                if "source" in base and "step" not in base:
                    base = {"source": base["source"]}
                spans = self.read_spans.setdefault(digest(base), [])
                uncovered = [(offset, offset + len(text))]
                for known in spans:
                    left, right = known["offset"], known["offset"] + len(known["text"])
                    start, end = max(offset, left), min(offset + len(text), right)
                    if start >= end or text[start-offset:end-offset] != known["text"][start-left:end-left]:
                        continue
                    uncovered = [(a, b) for lo, hi in uncovered for a, b in
                        ((lo, min(hi, start)), (max(lo, end), hi)) if a < b]
                if not uncovered:
                    return
                spans.append({"offset": offset, "text": text})
            key = digest(identity)
            previous = self.read_fragments.setdefault(key, [])
            if any(text in known for known in previous):
                return  # A shorter overlapping excerpt adds no literal information.
            previous[:] = [known for known in previous if known not in text]
            previous.append(text)
            fresh = True
        def fact(value):
            nonlocal fresh
            key = digest(value)
            fresh = fresh or key not in self.seen_results
            self.seen_results.add(key)
        if isinstance(data.get("items"), list):
            for item in data["items"]:
                if not isinstance(item, dict) or item.get("already_supplied") or not item.get("source"):
                    continue
                identity = {key:item[key] for key in ("source", "step", "type", "requested_parameters",
                    "historical_index", "observation_id", "observation_ids", "note_key", "event_sources") if key in item}
                if item.get("text"):
                    # Explicit continuation offsets distinguish repeated literal text
                    # at different positions of one versioned record.
                    continuation = item.get("continue_source", "")
                    identity["offset"] = item.get("offset", (int(continuation.rsplit("#", 1)[1]) - len(item["text"])
                        if "#" in continuation else None if item.get("truncated") and "step" not in item else 0))
                    fragment(identity, item["text"])
                elif item.get("observation_id"):
                    fact({"historical_image":identity})
        elif data.get("source") and data.get("hash") and "text" in data:
            # Frozen skill pages: path/hash in source binds the immutable document.
            fragment({"official_skill":data["source"], "offset":data.get("offset", 0)}, data["text"])
        elif data.get("ref") and data.get("content_hash") and "text" in data:
            fragment({"diagnostic_context":data["ref"], "hash":data["content_hash"],
                "offset":data.get("offset", 0)}, data["text"])
        elif "plan" in data:
            fact({"namespace":data.get("namespace"), "plan_revision":data.get("plan_revision"), "plan":data["plan"]})
        return fresh

    def checkpoint_message(self):
        if self.analysis is None:
            return []
        for message in reversed(self.history):
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                try:
                    value = json.loads(message["content"]).get("trajectory_analysis")
                    if value is not None and TrajectoryAnalysis.model_validate(value).model_dump() == self.analysis:
                        return []
                except (ValueError, AttributeError):
                    pass
        return [{"role": "user", "content": json.dumps({
            "trajectory_analysis": self.analysis,
            "policy": "Source-bound working progress, not independent proof. Continue its original statuses and citations; re-read for named gaps, contradictions or review disputes. Preserve unknown dependencies."}, ensure_ascii=False),
            "learner_checkpoint": True}]

    def messages(self, system, payload):
        return [{"role": "system", "content": system},
                {"role": "user", "content": diagnostic_payload_content(payload)},
                *self.checkpoint_message(), *copy.deepcopy(self.history),
                *([{"role": "user", "learner_phase_marker": True, "content": json.dumps({"learner_phase": self.stages,
                    "policy": "Continue after the preceding decisions. The current phase input in the first user message supersedes old budgets, candidate and feedback."})}]
                  if self.history else [])]

    def deliver_host_feedback(self, feedback):
        """Archive completed review feedback without another model request."""
        if self.ref is None:
            raise ValueError("feedback requires an archived bound conversation")
        previous = json.loads(self.artifacts.read_text(self.ref))
        if previous.get("source_binding") != self.binding:
            raise ValueError("feedback conversation binding mismatch")
        message = {"role": "user", "content": json.dumps({
            "completed_verification_feedback": copy.deepcopy(feedback),
            "policy": "Host-delivered independent review and measurement receipts, not a Learner decision or new app evidence. Retain reviewed scoped candidates and all declared unknowns. Selected or deferred checks are not completed measurements; causal or transfer benefit is not inferred."},
            ensure_ascii=False, separators=(",", ":"))}
        return self.save([*previous["phase_input"], *self.checkpoint_message(),
            *copy.deepcopy(self.history), message])

    def save(self, messages, *, analysis=None, compacted=False):
        if analysis is not None:
            self.analysis = copy.deepcopy(analysis)
        history = copy.deepcopy([m for m in messages[2:]
            if not m.get("learner_checkpoint") and not m.get("research_capacity")
            and not m.get("diagnostic_final_instruction")])
        unchanged_evidence = (history[:len(self.history)] == self.history
            and all(m.get("learner_phase_marker") for m in history[len(self.history):]))
        if not unchanged_evidence:
            self.analysis_current = False
        if analysis is not None:
            self.analysis_current = True
        self.history = history
        if compacted:
            self.compactions += 1
        self.version += 1
        self.ref = self.artifacts.save_json("skill-learning/conversations/" + self.id, {
            "schema_version": 1, "job_id": self.id, "source_binding": self.binding,
            "version": self.version, "previous": self.ref, "analysis": self.analysis,
            "history": self.history, "phase_input": messages[:2],
            "compactions": self.compactions, "reused_checkpoints": self.reused_checkpoints,
            "analysis_current": self.analysis_current, "stage": self.stages,
            "read_progress": {"seen_results": sorted(self.seen_results), "fragments": self.read_fragments, "spans": self.read_spans},
            "policy": "Analysis is unverified; originals remain in native history. No cross-task retrieval or automatic publication."})
        return self.ref
