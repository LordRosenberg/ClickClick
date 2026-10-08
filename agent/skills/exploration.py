"""Learner device phase reuses Executor mechanics, not a nested task agent."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import time
from pathlib import Path

from driver.observation_deadline import ObservationStageError
from agent.action_observation import ActionObservationTransaction
from agent.revisable.recall import resolve_source, record_text
from agent.revisable.roles import PlanExecutor, prompt
from agent.revisable.session import ExecutionSession
from agent.revisable.store import TaskStore
from agent.revisable.tools import register_memory_tools
from agent.skills.snapshot import LibrarySnapshot
from agent.skills.environment import ResearchEnvironment
from agent.skills.research_tools import OFFICIAL_TOOL, register_research_tools
from agent.skills.learning import LearningStopped, REVIEW_SYSTEM, PROBE_REVIEW_SYSTEM, action_cost_directory, bounded_notes, digest, source, probe_projection
from agent.tool_registry import AgentRole, AgentToolRegistry, ToolExecutionContext, stable_hash
from perception.observation import ObservationBuilder
from shared.llm_gateway import complete
from shared.revisable import Plan, Stage, TaskLimits
from shared.schemas import AgentState, ExecutorDecisionKind, TaskStatus


class LearningSession(ExecutionSession):
    """Only learning sessions can read the original task; never write to it."""

    source_store = None
    source_state = None

    async def run(self, observation_messages, **kwargs):
        # Fixed experiment contracts are supplied once per request, outside the
        # append-only dialogue that compaction measures. Every historical/current
        # reference resolves here; changed experiments are never conflated.
        history = list(kwargs.get("history_messages") or [])
        contexts = {}
        for message in [*history, *observation_messages]:
            content = message.get("content")
            if (message.get("role") != "user" or not isinstance(content, str)
                    or not content.startswith('{"research_context_reference":')):
                continue
            envelope = json.loads(content)
            legacy_fields = {"research_context_reference", "environment", "budget"}
            if set(envelope) not in (legacy_fields, legacy_fields | {"budget_scope", "probe_cost"}):
                continue
            ref = envelope["research_context_reference"]
            if ref in contexts:
                continue
            kind, row, offset = resolve_source(self.store, ref)
            payload = row["payload"]
            if (kind != "measurement" or offset or payload.get("type") != "research_context"
                    or digest(payload.get("research_context")) != payload.get("content_hash")):
                raise ValueError("invalid native research context reference")
            contexts[ref] = payload["research_context"]
        if contexts:
            prefix = {"role":"user", "content":json.dumps({
                "research_context_sources":[{"source":ref, "research_context":value}
                    for ref,value in contexts.items()],
                "policy":"Exact native experiment contexts for the references in this request. Historical hypotheses are unverified data, not current grounding. Per-occurrence environment and budget remain in the dialogue."}, ensure_ascii=False)}
            names = list(kwargs.get("history_message_names") or
                [f"history_messages[{i}]" for i in range(len(history))])
            kwargs["history_messages"] = [prefix, *history]
            kwargs["history_message_names"] = ["research_context_sources", *names]
        return await super().run(observation_messages, **kwargs)

    def _build_registry(self, external_handlers):
        submitted = super()._build_registry(external_handlers)
        registry = AgentToolRegistry()
        # A terminal proposal can pass preflight, then fail its atomic note save.
        # Device dispatch occurs only after this registry returns an accepted
        # terminal value. Close that rejected intent now, before a retry can
        # replace it or a non-action decision can receive its effect receipt.
        for spec in submitted.specs_for_role(self.role):
            async def guarded(args, ctx, name=spec.name):
                executor = getattr(getattr(self, "research_backend", None), "executor", None)
                previous = getattr(executor, "intent_id", None)
                result = await submitted.execute(name, args, ctx)
                intent = getattr(executor, "intent_id", None)
                if (name == "submit_executor_step" and intent is not None and intent != previous
                        and (result.status.value != "succeeded" or result.terminal_value is None)):
                    executor.environment.effect(intent, {"success": False,
                        "receipt": {"dispatch_succeeded": False},
                        "reason": "terminal_submission_rejected_before_dispatch",
                        "submission_status": result.status.value, "error": result.error})
                    executor.intent_id = None
                return result
            registry.register(spec, guarded)
        native = AgentToolRegistry()
        register_memory_tools(native, "executor", self.source_store, self.source_state)
        spec = next(s for s in native.specs_for_role("executor") if s.name == "read_history")

        async def read_source(args, ctx):
            # Separate deduplication from the live experiment history.
            history_state = ctx.state.setdefault("source_history_state", {})
            history_state["artifacts"] = ctx.state.get("artifacts")
            source_ctx = ToolExecutionContext(role=AgentRole.EXECUTOR,
                invocation_id=ctx.invocation_id, state=history_state)
            return await native.execute("read_history", args, source_ctx)

        registry.register(spec.model_copy(update={
            "name": "read_source_history",
            "description": "Read missing original-task evidence by source or query. Read-only historical facts cannot ground current actions. Cite these records with source/ prefix."}),
            read_source)
        if getattr(self, "research_backend", None) is not None:
            register_research_tools(registry, self.research_backend, self.research_budget)
        return registry


class LearningExecutor(PlanExecutor):
    async def _terminal_preflight(self, step, context):
        if step.decision == ExecutorDecisionKind.ACT:
            if self.question_attempts >= self.question_limit:
                raise LearningStopped("question_action_budget")
            self.budget.action()
            self.question_attempts += 1
        rejection = await super()._terminal_preflight(step, context)
        if rejection is None and step.decision == ExecutorDecisionKind.ACT and getattr(self, "environment", None):
            package = context.state.get("active_package")
            try:
                self.intent_id = self.environment.intent(step.action.model_dump(mode="json"), package.observation_id)
            except ValueError as exc:
                from agent.tool_registry import AgentToolResult, ToolStatus
                return AgentToolResult(status=ToolStatus.PRECONDITION_NOT_MET,
                    summary=str(exc), data={"reason":"environment_precondition", "environment":self.environment.receipt()})
        return rejection

    async def _call_with_session(self, obs_messages, history_messages, **kwargs):
        context = kwargs["context_state"]
        original = context["render_observation_bucket"]
        research_context = getattr(self, "research_context", {})
        context_hash = digest(research_context)
        key = "research_context_" + context_hash
        try:
            record = self.store.get("measurement", key)
        except ValueError:
            record = self.store.put("measurement", key, {"type":"research_context",
                "content_hash":context_hash, "research_context":research_context})
        context_ref = source(record, "measurement")
        def render(package):
            messages, names = original(package)
            messages = [*messages, {"role":"user", "content":json.dumps({
                "research_context_reference":context_ref,
                "environment": self.environment.receipt() if getattr(self,"environment",None) else None,
                "budget": self.budget.snapshot(),
                "budget_scope": "Cumulative learning job; seconds is not elapsed probe time.",
                "probe_cost": self.budget.probe_snapshot()}, ensure_ascii=False)}]
            return messages, [*names, "research_runtime"]
        context["render_observation_bucket"] = render
        output = await super()._call_with_session(obs_messages, history_messages, **kwargs)
        for llm_round in output[1].llm_rounds:
            self.budget.record_usage(llm_round.usage.model_dump())
        return output


def diagnostic_text_size(messages):
    projected = [{**m, "content": [p for p in m["content"] if p.get("type") != "image_url"]}
                 if isinstance(m.get("content"), list) else m for m in messages]
    return len(json.dumps(projected, ensure_ascii=False))


def _columnar_directory(rows):
    """Losslessly encode uniform metadata rows; never summarize their values."""
    if not isinstance(rows, list) or len(rows) < 3 or not all(isinstance(r, dict) for r in rows):
        return rows
    columns = list(rows[0])
    if not columns or any(set(row) != set(columns) for row in rows):
        return rows
    encoded = {"encoding":"columnar_records", "columns":columns,
        "rows":[[row[key] for key in columns] for row in rows]}
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return encoded if size(encoded) < size(rows) else rows


def learner_action_directory(schema):
    def compact(value):
        if isinstance(value, dict):
            return {key:compact(item) for key,item in value.items()
                    if key not in {"description", "title", "default"}}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value
    return [{"type":variant["properties"]["type"]["enum"][0],
        "required":variant.get("required", []),
        "parameters":{key:compact(value) for key,value in variant["properties"].items() if key != "type"}}
        for variant in schema["anyOf"]]


def learning_budget_context(budget):
    """Live capacity without per-request audit records; original snapshot unchanged."""
    usage = budget.get("provider_usage")
    if not isinstance(usage, dict) or not isinstance(usage.get("records"), list):
        return budget
    return {**budget, "provider_usage": {
        **{key:value for key,value in usage.items() if key != "records"},
        "omitted_request_records":len(usage["records"]),
        "record_policy":"Per-request usage remains in job/research budget artifacts; availability and unknown counts are unchanged."}}


def checkpoint_phase_context(messages):
    """Give the same Learner current goals/disputes, not another full handoff."""
    payload = json.loads(messages[1]["content"])
    validation = payload.get("validation") or {}
    feedback = payload.get("review_feedback") or {}
    if isinstance(feedback, list):
        feedback = feedback[-1] if feedback else {}
    return {"verification_goal":payload.get("verification_goal"),
        "original_instruction":(payload.get("source") or {}).get("instruction"),
        "candidate":payload.get("candidate"),
        "validation_targets":[{key:trial[key] for key in ("kind", "goal",
            "baseline_execution_namespace", "candidate_execution_namespace", "pair_evidence_ref") if key in trial}
            for trial in validation.get("trials", [])],
        "review_gaps":{key:value for key,value in feedback.get("criteria", {}).items()
            if isinstance(value, dict) and value.get("status") != "pass"},
        "review_tests":feedback.get("tests", []),
        "policy":"Current task/check intent and independent review gaps are pinned context, not native app evidence. Preserve progress relative to these goals; do not infer an unobserved effect or add their fields as evidence references."}


def diagnostic_payload_projection(payload, *, character_budget=30000, compact=False):
    """Bound the combined handoff, preserving goal, candidate and review contract."""
    payload = dict(payload)
    # Per-request token records are audit bookkeeping, not remaining capacity.
    # The full budget snapshot remains in job/research artifacts. Keep every
    # live limit, counter, usage availability and unknown-cost policy inline.
    budget = payload.get("budget")
    if isinstance(budget, dict):
        payload["budget"] = learning_budget_context(budget)
    json_options = {"ensure_ascii":False, **({"separators":(",", ":")} if compact else {})}
    if compact:
        session = payload.get("research_session") or {}
        environment = session.get("environment") or {}
        capabilities = payload.get("environment_capabilities")
        if capabilities and environment.get("reset_capability") == capabilities:
            payload["research_session"] = {**session, "environment":{**environment,
                "reset_capability":{"same_request_reference":"environment_capabilities"}}}
    directory_requested = payload.pop("_source_history_directory", False)
    # A candidate schema is also supplied during initial trajectory assessment.
    # Keep its source facts inline when they fit; only the existing overflow
    # fallback should turn that initial history into a reference directory.
    writing = payload.get("candidate_json_schema") and not payload.get("initial_diagnosis")
    if (payload.get("review_feedback") or payload.get("probe_review") or writing or directory_requested) and isinstance(payload.get("source"), dict):
        packet = dict(payload["source"])
        removed = 0
        for field in ("events", "stages", "observations", "notes"):
            if not isinstance(packet.get(field), list):
                continue
            directory = []
            for original in packet[field]:
                item = {key:original[key] for key in ("source", "app", "title", "step", "decision") if key in original}
                for key in ("preview", "tail_preview", "text"):
                    removed += int(key in original)
                if field == "notes" and original.get("preview"):
                    item["preview"] = str(original["preview"])[:160]
                    item["truncated"] = True
                directory.append(item)
            packet[field] = directory
        if isinstance(packet.get("action_costs"), dict):
            packet["action_costs"] = {"history_namespace":"source",
                "policy":"Read the native execution outline for detailed action transitions and parameters; source counts retain totals. Omitted details are unknown, not waste labels."}
        ledger = payload.get("acquisition_ledger")
        if isinstance(ledger, dict):
            payload["acquisition_ledger"] = {**ledger,
                "recent_probes":[{key:item[key] for key in
                    ("learning_task_id", "outcome", "actions_attempted", "probe_cost") if key in item}
                    for item in ledger.get("recent_probes", [])],
                "probe_reviews":[{"question":str(item.get("question") or "")[:160],
                    "question_truncated":len(str(item.get("question") or ""))>160,
                    "verdict":item.get("verdict"), "omitted_reasons":len(item.get("reasons") or [])}
                    for item in ledger.get("probe_reviews", [])],
                "post_review_probe_policy":"Completed questions/tests and audit prose are archived; native probe histories retain results. Source problem, verdicts, outcomes and all caps stay explicit; omitted content is unknown."}
        validation = payload.get("validation")
        if isinstance(validation, dict):
            trials = []
            for original in validation.get("trials", []):
                trial = dict(original)
                for side in ("baseline_execution", "candidate_execution"):
                    if not isinstance(trial.get(side), dict):
                        continue
                    execution = dict(trial[side])
                    removed_report = execution.pop("last_report", None)
                    removed_inputs = execution.pop("text_inputs", None)
                    reason = str(execution.get("failure_reason") or "")
                    if len(reason) > 160:
                        execution["failure_reason"] = reason[:160]
                        execution["failure_reason_truncated"] = True
                    activation = execution.get("candidate_activation")
                    if isinstance(activation, dict) and "examples" in activation:
                        execution["candidate_activation"] = {
                            **{key:value for key,value in activation.items() if key != "examples"},
                            "omitted_examples":len(activation["examples"]),
                            "examples_policy":"Exact request references remain in the paired measurement; activation is not mechanism adoption."}
                    execution["history_omissions"] = {"last_report":removed_report is not None,
                        "text_inputs":len(removed_inputs or [])}
                    trial[side] = execution
                trials.append(trial)
            payload["validation"] = {**validation, "trials":trials,
                "history_policy":"Omitted literal reports/inputs remain in the registered native execution namespace. Outcomes, counts, limits, hashes and matched-oracle results are unchanged."} if trials and any(
                    "baseline_execution" in trial or "candidate_execution" in trial for trial in trials) else {**validation,"trials":trials}
        # The directory policy and native tool catalog replace these repeated
        # source preview/read reminders after review.
        packet.pop("notes_policy", None)
        packet.pop("history_tool", None)
        if removed or "action_costs" in packet:
            packet["post_review_history_policy"] = {
                "omitted_preview_fields":removed,
                "policy":"Source goal, execution limits, outcome and counts are unchanged. Earlier source previews become native-reference directories for research/writing/review; full versioned records remain readable. Omissions establish no absence or verified effects."}
        payload["source"] = packet
    evidence = payload.get("evidence")
    if isinstance(evidence, dict) and evidence.get("read_tool") != "read_diagnostic_context":
        from agent.skills.learning import probe_projection
        projected_evidence = probe_projection(evidence)
        projected_evidence["observations"] = [{key: item.get(key) for key in ("source", "app")}
            | {"preview": str(item.get("preview") or "")[:400], "truncated": True}
            for item in evidence.get("observations", [])[-2:]]
        projected_evidence["omitted_raw_events"] = len((evidence.get("events") or {}).get("events", []))
        projected_evidence["policy"] = "Bounded literal probe directory; raw events and observations remain in native exploration history. Omissions do not establish absence."
        if payload.get("review_feedback") and isinstance(projected_evidence.get("notes"), dict):
            notes = projected_evidence["notes"]
            projected_evidence["notes"] = {**notes, "items":[{key:item[key] for key in ("source", "title") if key in item}
                for item in notes.get("items", [])],
                "post_review_policy":"Review-linked raw records and versioned notes remain readable; previews are omitted, not disproven."}
        payload["evidence"] = projected_evidence
    optional = ("diagnostic_reads", "prior_diagnostic_reads", "prior_negative_experiments", "prior_experiments", "previous_experiments", "matched_experiences", "prior_probe_snapshots")
    for key in optional:
        if isinstance(payload.get(key), list):
            payload[key] = list(payload[key])
    omissions = {}
    if compact and len(json.dumps(payload, **json_options)) > character_budget:
        if "executor_action_directory" in payload:
            payload["executor_action_directory"] = _columnar_directory(payload["executor_action_directory"])
        packet = payload.get("source")
        directory = packet.get("official_skill_directory") if isinstance(packet, dict) else None
        if isinstance(directory, dict) and "items" in directory:
            payload["source"] = {**packet, "official_skill_directory":{**directory,
                "items":_columnar_directory(directory["items"])}}
    while len(json.dumps(payload, **json_options)) > character_budget:
        field = next((key for key in optional if isinstance(payload.get(key), list) and payload[key]), None)
        if field is None:
            raise LearningStopped("diagnostic_context_budget")
        removed = payload[field].pop(0)
        entry = omissions.setdefault(field, {"count": 0})
        entry["count"] += 1
        if isinstance(removed, dict) and len(entry.get("history_refs", [])) < 3:
            ref = removed.get("source") or removed.get("namespace") or removed.get("history_namespace")
            if ref and str(ref)[:180] not in entry.get("history_refs", []):
                entry.setdefault("history_refs", []).append(str(ref)[:180])
        payload["context_omissions"] = omissions
    return payload



def diagnostic_projection_with_reads(payload, *, character_budget, directories,
                                     pinned_messages=(), wire_budget=None, fallback_wire_budget=None):
    """Keep live facts inline; archive detail and account for serialized messages."""
    best_fit, best_wire = None, float("inf")
    def project(value):
        nonlocal best_fit, best_wire
        # Optional entries disappear in whole chunks. Subtracting the preferred
        # wire excess can jump below required input before measuring a smaller
        # fitting projection. Retry the existing fallback window independently;
        # retain its smallest fit while later exact archives seek more headroom.
        for target in (wire_budget, fallback_wire_budget):
            if target is None and wire_budget is not None:
                continue
            limit = character_budget
            while limit > 0:
                try:
                    projected = diagnostic_payload_projection(value, character_budget=limit, compact=True)
                except LearningStopped:
                    break
                if wire_budget is None:
                    return projected
                wire = diagnostic_text_size([*pinned_messages, {"role":"user",
                    "content":json.dumps(projected, ensure_ascii=False, separators=(",", ":"))}])
                if wire <= wire_budget:
                    return projected
                if fallback_wire_budget is not None and wire <= fallback_wire_budget and wire < best_wire:
                    best_fit, best_wire = copy.deepcopy(projected), wire
                if wire <= target:
                    break
                limit -= wire - target
        raise LearningStopped("diagnostic_context_budget")
    try:
        return project(payload)
    except LearningStopped:
        if not payload.get("diagnostic_read_rounds", 4):
            raise
    projected = dict(payload)
    projected["source"] = dict(payload.get("source") or {})
    feedback = projected.get("review_feedback")
    if isinstance(feedback, dict) and isinstance(feedback.get("criteria"), dict):
        # Passed explanations are available verbatim, but need not occupy every
        # repair request. Blocking reasons, changes, tests and verdict stay inline.
        text = json.dumps(feedback, ensure_ascii=False, separators=(",", ":"))
        ref = digest(text)
        criteria = {key:({**item, "reason":"Passed; exact assessment is in review_details. This is not independent app evidence."}
                        if isinstance(item, dict) and item.get("status") == "pass" else item)
                    for key,item in feedback["criteria"].items()}
        compact_feedback = {**feedback, "criteria":criteria, "review_details":{
            "read_tool":"read_diagnostic_context", "ref":ref, "content_hash":ref,
            "characters":len(text), "policy":"Complete independent review, including passed explanations and qualifications. Read for a named unresolved question; passing does not establish unclaimed utility."}}
        if len(json.dumps(compact_feedback,ensure_ascii=False,separators=(",", ":"))) < len(text):
            directories[ref] = text
            projected["review_feedback"] = compact_feedback
            try:
                return project(projected)
            except LearningStopped:
                pass
    validation = projected.get("validation")
    if isinstance(validation, dict) and validation.get("trials"):
        text = json.dumps(validation, ensure_ascii=False, separators=(",", ":"))
        ref = digest(text)
        trials = []
        for original in validation["trials"]:
            trial = dict(original)
            for side in ("baseline_execution", "candidate_execution"):
                raw = trial.get(side)
                if not isinstance(raw, dict):
                    continue
                trial[side] = {key:raw[key] for key in ("available", "task_id",
                    "runtime_status", "failure_reason", "execution_limits", "cost", "history_namespace") if key in raw}
                activation = raw.get("candidate_activation")
                if isinstance(activation, dict):
                    trial[side]["candidate_activation"] = {key:activation[key] for key in (
                        "skill_id", "kind", "executor_projection_hash", "planner_catalog_exposed",
                        "planner_read", "stage_selected", "executor_active_exact", "mechanism_adoption") if key in activation}
            trials.append(trial)
        projected["validation"] = {**validation, "trials":trials, "details":{
            "read_tool":"read_diagnostic_context", "ref":ref, "content_hash":ref,
            "characters":len(text), "policy":"Exact trial detail is archived in this diagnosis. Outcomes, bindings, caps, costs and activation facts remain inline; omitted detail is unknown, not mechanism proof."}}
        directories[ref] = text
        try:
            return project(projected)
        except LearningStopped:
            pass
    sections = [(projected["source"], "official_skill_directory", ()),
                (projected, "executor_action_directory", ()),
                (projected, "existing_active_guidance", ("omitted_ids", "policy")),
                (projected, "review_evidence", ()),
                (projected, "evidence", ("question", "outcome", "learning_task_id",
                    "actions_attempted", "frontier", "observation_failures", "environment_restoration", "probe_cost", "budget_scope")),
                (projected, "acquisition_ledger", ("source_problem", "problem_status",
                    "probe_count", "omitted_probes", "skip_reassessments", "released_reserve_calls"))]
    feedback = projected.get("review_feedback")
    if isinstance(feedback, dict) and feedback.get("evidence_reads"):
        projected["review_feedback"] = dict(feedback)
        sections.append((projected["review_feedback"], "evidence_reads", ()))
    session = projected.get("research_session") or {}
    environment = session.get("environment") or {}
    if environment.get("status") == "closed" and environment.get("verified_clean") is True:
        projected["research_session"] = {**session, "environment":dict(environment)}
        sections.append((projected["research_session"]["environment"], "plan", ()))
    for parent, key, fact_keys in sections:
        if key not in parent:
            continue
        value = parent[key]
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        ref = digest(text)
        pointer = {"read_tool":"read_diagnostic_context", "ref":ref,
            "characters":len(text), "content_hash":ref,
            "policy":"Exact archived context available in this diagnosis; omitted from inline input, not absent.",
            **({k:value[k] for k in fact_keys if k in value} if isinstance(value, dict) else {})}
        if key == "existing_active_guidance" and isinstance(value, dict):
            pointer["items"] = [{k:item[k] for k in ("id", "kind", "app", "content_hash") if k in item}
                for item in value.get("items", [])]
        if isinstance(value, dict) and "manifest_hash" in value:
            pointer["manifest_hash"] = value["manifest_hash"]
        if len(json.dumps(pointer, ensure_ascii=False)) >= len(text):
            continue
        directories[ref] = text
        parent[key] = pointer
        try:
            return project(projected)
        except LearningStopped:
            pass
    if best_fit is not None:
        return best_fit
    raise LearningStopped("diagnostic_context_budget")



def checkpoint_phase_input(messages):
    """After checkpointing, replace repeated initial previews with native IDs."""
    from agent.skills.analysis_session import diagnostic_payload_content
    base = copy.deepcopy(messages[:2])
    payload = json.loads(base[1]["content"])
    if not payload.get("initial_diagnosis") or not isinstance(payload.get("source"), dict):
        return base
    projected = diagnostic_payload_projection({**payload, "_source_history_directory": True},
        character_budget=max(30000, len(base[1]["content"])), compact=True)
    # The initial action/report index has already been delivered and checkpointed.
    # It is historical evidence, not a pinned task constraint. Its native outline
    # remains readable; keeping all rows here would resend them after every summary.
    overview = projected["source"].get("execution_overview")
    if isinstance(overview, dict) and isinstance(overview.get("rows"), list):
        projected["source"]["execution_overview"] = {
            "history_namespace": "source", "read_tool": "read_execution_outline",
            "start_step": 0,
            "policy": "Initial index archived before checkpointing; read native actions/reports for a named gap. Omitted rows do not establish absence or verified effects."}
    content = diagnostic_payload_content(projected)
    if len(content) < len(base[1]["content"]):
        base[1]["content"] = content
    return base


ANALYSIS_REPAIR_SYSTEM = """You are the same source-bound Learner repairing only
trajectory_analysis format or references. Return the complete decision JSON;
every other decision field must remain byte-for-byte equivalent after JSON parsing.
No tools, device actions, new app findings or changed decisions are permitted.
The pinned task, limits, candidate, review disputes and uncertainty still govern.
Keep prior validated analysis fallible; preserve unresolved dependencies and original
statuses unless already supplied evidence resolves them. Use the supplied format;
keep the checkpoint within 8000 characters. Observed/contradicted findings require
returned native references. Existing guidance may cite returned official_skill
references with complete-file hashes; versions and read_diagnostic_context locators
are not evidence references. Guidance, model reports and binary outcomes do not
prove app mechanisms. Repair invalid references from supplied records, never invent
IDs or promote unknown findings. Older exchanges remain archived; omissions do not
establish absence. The host validates references and rejects any other field change.
"""


def analysis_repair_context(messages, analysis, parsed, instruction):
    """Fit a format-only repair without discarding its decision or checkpoint."""
    checkpoint = ([{"role":"user", "learner_checkpoint":True,
        "content":json.dumps({"trajectory_analysis":analysis,
            "policy":"Prior validated source-local checkpoint; fallible, not new evidence."},
            ensure_ascii=False, separators=(",", ":"))}] if analysis is not None else [])
    base = copy.deepcopy(messages[:2])
    repair = [*base, *checkpoint,
        {"role":"user", "content":"Older raw exchanges and original output are archived. This is a format-only repair; omissions do not establish missing facts or authorize new decisions."},
        {"role":"assistant", "content":json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))},
        instruction]
    if diagnostic_text_size(repair) > 50000:
        # No tools or new action plan are permitted in this repair. Keep the
        # source, full decision/candidate, known guidance, limits and reference
        # directories; the execution action schema adds no repair information.
        phase = json.loads(base[1]["content"])
        if "executor_action_directory" in phase:
            phase.pop("executor_action_directory")
            phase["format_repair_policy"] = "Action directory is archived; repair trajectory_analysis references only. Keep every other decision field unchanged. No tools or device actions."
            base[1]["content"] = json.dumps(phase, ensure_ascii=False, separators=(",", ":"))
    if diagnostic_text_size(repair) > 50000:
        phase = json.loads(base[1]["content"])
        guidance = phase.get("existing_active_guidance")
        if isinstance(guidance, dict) and any("content" in item for item in guidance.get("items", [])):
            # Only reference format may change here. The complete original
            # phase is archived; retain every identity/hash and qualification.
            # Existing rule prose cannot authorize a different decision.
            phase["existing_active_guidance"] = {**guidance,
                "items":[{k:v for k,v in item.items() if k != "content"} for item in guidance["items"]],
                "format_repair_policy":"Exact rule bodies are in the archived phase. Reference format only; all rule identities, hashes and qualifications are unchanged. No new findings or decision changes."}
            base[1]["content"] = json.dumps(phase, ensure_ascii=False, separators=(",", ":"))
    if diagnostic_text_size(repair) > 50000:
        # This call cannot explore, reassess utility or change a candidate. The
        # full acquisition policy is archived; use its narrower repair contract
        # rather than duplicating it alongside both analyses and the full output.
        # All phase facts and the immutable non-analysis fields remain present.
        base[0]["content"] = ANALYSIS_REPAIR_SYSTEM
    if diagnostic_text_size(repair) > 50000:
        raise LearningStopped("analysis_output_repair_context_budget")
    return repair


def read_diagnostic_context(directories, ref, offset=0, length=4000):
    if ref not in directories:
        raise ValueError("unknown diagnostic context reference")
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 4000:
        raise ValueError("invalid diagnostic context page")
    text = directories[ref]
    end = min(len(text), offset + length)
    return {"ref":ref, "content_hash":digest(text), "offset":offset,
        "text":text[offset:end], "total_characters":len(text),
        "next_offset":end if end < len(text) else None,
        "policy":"Literal JSON text slice; continuation offsets are characters. Archived metadata or reports are not independent app evidence."}

def scoped_history_data(data, namespace):
    """Qualify reference metadata only; literal historical content stays intact."""
    items = []
    for original in data.get("items", []):
        item = dict(original)
        for key in ("source", "continue_source"):
            if isinstance(item.get(key), str):
                item[key] = namespace + "/" + item[key]
        if isinstance(item.get("event_sources"), list):
            item["event_sources"] = [namespace + "/" + ref for ref in item["event_sources"]]
        items.append(item)
    return {**data, "namespace":namespace, "items":items}


def execution_outline(rows, *, start_step=0, character_budget=8000):
    """Bounded chronological action/report directory, not a semantic summary."""
    if type(start_step) is not int or start_step < 0:
        raise ValueError("outline start_step must be a nonnegative integer")
    eligible = [row for row in sorted(rows, key=lambda row: row["payload"].get("step", -1))
        if type(row["payload"].get("step")) is int and row["payload"]["step"] >= start_step]
    result = {"items": [], "omitted_events": len(eligible), "next_step": None,
        "policy": "Chronological submitted actions and literal report excerpts, not verified effects, occurrence identity or waste labels. Historical parameters cannot ground actions. Full versioned events remain readable."}
    for row in eligible:
        event = row["payload"]
        action = event.get("submitted_action") or event.get("action") or {}
        report = str(event.get("executor_report") or "")
        item = {"source": source(row), "step": event["step"],
            "type": action.get("type") or event.get("decision"),
            "text": report[:96], "truncated": len(report) > 96}
        if action.get("type") in {"swipe", "scroll", "drag"}:
            item["requested_parameters"] = {key: action[key] for key in
                ("x", "y", "x2", "y2", "direction", "duration_ms", "hold_before_move", "image_size")
                if action.get(key) is not None}
        elif action.get("index") is not None:
            item["historical_index"] = action["index"]
        proposed = {**result, "items": [*result["items"], item],
            "omitted_events": result["omitted_events"] - 1}
        if len(json.dumps(proposed, ensure_ascii=False)) > character_budget - 50:
            result["next_step"] = event["step"]
            break
        result = proposed
    if len(json.dumps(result, ensure_ascii=False)) > character_budget:
        raise ValueError("outline budget too small")
    return result


def execution_overview(rows, *, character_budget=8000):
    """Compact literal chronology; no inferred success, detour or dependency."""
    ordered = sorted(rows, key=lambda row: row["payload"].get("step", -1))
    result = {"columns": ["step", "source", "action", "report", "report_truncated"],
        "rows": [], "omitted_events": len(ordered), "next_step": None,
        "policy": "Literal action/report index, not verified effects or waste labels. Read versioned events and linked observations for parameters and dependencies."}
    for row in ordered:
        event = row["payload"]
        if type(event.get("step")) is not int:
            continue
        action = event.get("submitted_action") or event.get("action") or {}
        report = str(event.get("executor_report") or "")
        entry = [event["step"], "source/" + source(row),
            action.get("type") or event.get("decision"), report[:96], len(report) > 96]
        proposed = {**result, "rows": [*result["rows"], entry],
            "omitted_events": result["omitted_events"] - 1}
        if len(json.dumps(proposed, ensure_ascii=False, separators=(",", ":"))) > character_budget:
            result["next_step"] = event["step"]
            break
        result = proposed
    return result


def live_experiment_context(context, *, character_budget=45000):
    """Keep the chosen test/frontier; research archives belong to diagnosis."""
    context = dict(context)
    archive = {}
    for key in ("prior_experiments", "prior_negative_experiments", "matched_experiences",
                "prior_probe_snapshots", "prior_diagnostic_reads"):
        entries = context.pop(key, None)
        if entries:
            refs = []
            for entry in entries:
                if isinstance(entry, dict):
                    ref = entry.get("source") or entry.get("namespace") or entry.get("history_namespace")
                    if ref and len(refs) < 12 and ref not in refs:
                        refs.append(str(ref)[:180])
            archive[key] = {"omitted_entries": len(entries), "history_refs": refs}
    prior = context.get("previous_experiments")
    if isinstance(prior, list) and prior:
        context["previous_experiments"] = [probe_projection(prior[-1])]
        archive["previous_experiments"] = {"omitted_entries": len(prior) - 1}
    reads = context.get("diagnostic_reads")
    if isinstance(reads, list):
        context["diagnostic_reads"] = [{**item, "text": str(item.get("text") or "")[:350],
            "truncated": bool(item.get("truncated")) or len(str(item.get("text") or "")) > 350}
            for item in reads[-2:]]
        archive["diagnostic_reads"] = {"omitted_entries": max(0, len(reads) - 2)}
    ledger = context.get("acquisition_ledger")
    if isinstance(ledger, dict):
        context["acquisition_ledger"] = {key: ledger[key] for key in
            ("source_problem", "problem_status", "probe_count", "omitted_probes") if key in ledger}
    context["_source_history_directory"] = True
    context["research_history_directory"] = archive
    context["history_access_policy"] = "Only the selected complete test, source problem and latest probe frontier are carried into live execution. Source/live records remain readable with native tools; other registered namespaces remain available in historical diagnosis. Omitted research is unknown, not an execution instruction."
    return diagnostic_payload_projection(context, character_budget=character_budget)


class ExplorationBackend:
    claim_admission = True

    def __init__(self, task, *, db, artifacts, driver, settings, library, cancelled=lambda: False, retain_probe_state=False):
        if type(retain_probe_state) is not bool:
            raise ValueError("retain_probe_state must be boolean")
        self.retain_probe_state = retain_probe_state
        self.task, self.db, self.artifacts = task, db, artifacts
        self.driver, self.settings, self.library = driver, settings, library
        self.cancelled = cancelled
        self.source_store = TaskStore(db, artifacts, task.id)
        self.record = self.store = self.state = self.executor = None
        self.diagnostic_reads = []
        self.learner_conversation = None
        self._conversation_budget = None
        self.additional_histories = {}
        self.experiences = {}
        self.probe_snapshots = []
        self.environment_transition = None
        self.history_states = {}
        self.owned_history_dbs = []
        self.snapshot = None
        self.environment = None
        self.environment_initializer = None
        self.environment_verifier = None
        self.fixture_reset_adapter = None
        from agent.skills.telemetry import ResearchTelemetry
        self.telemetry = ResearchTelemetry(artifacts, task.id)

    def _record_research(self, role, phase, payload, budget, *, status="recorded", model=None):
        self.telemetry.record(role, phase, payload, cost=budget.snapshot(),
            histories=self._stores(), status=status, model=model)

    def record_completed_verification_feedback(self, feedback, budget):
        ref = (self.learner_conversation.deliver_host_feedback(feedback)
            if self.learner_conversation else None)
        self._record_research("learner", "verification_feedback",
            {"origin": "host", "feedback": feedback, "conversation_ref": ref},
            budget, status="completed_no_followup")

    def research_session_context(self):
        if not self.retain_probe_state:
            return {}
        return {"research_session": {
            "mode": "continuous_owned_fixture",
            "environment": self.environment.receipt() if self.environment else None,
            "policy": "Completed probes retain live state and unresolved effects in this owned fixture. Re-ground observations; no reset is implied. Omit a replacement environment plan to continue under the current one; a changed plan requires verified cleanup and preparation. Cleanup remains reserved and mandatory before candidate adjudication, ordinary validation and exit. New completed evidence permits one planning repair, at most two total, without extra calls/actions/time. New hypotheses still need audit; findings are not accepted skills."}}

    def freeze_library(self):
        if self.snapshot is None:
            from uuid import uuid4
            self.snapshot = LibrarySnapshot.freeze(self.library.root,
                self.artifacts.root / "skill-learning" / self.task.id / uuid4().hex / "baseline")
            self.library = __import__("agent.skills.library", fromlist=["SkillLibrary"]).SkillLibrary(self.snapshot.root)
        return self.snapshot

    def official_skill_page(self, path, offset=0, length=4000):
        """One frozen page contract for diagnosis, deferred reads and review."""
        if self.snapshot is None:
            raise ValueError("no official snapshot")
        data = self.snapshot.read(path, offset, length)
        data.update(source="official_skill:" + path + "@" + data["hash"], offset=offset)
        if Path(path).name == "SKILL.md":
            from agent.skills.library import parse_skill_markdown
            raw = (self.snapshot.root/path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != data["hash"]:
                raise ValueError("frozen library file changed")
            # Match SkillLibrary's Path.read_text universal-newline projection;
            # the full-file hash above still binds the original bytes.
            text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            pack = parse_skill_markdown(text)
            role_hashes = {role:stable_hash(pack.section_for(role))
                for role in ("planner", "reviewer", "executor", "decision")}
            data.update(hash_scope="complete_SKILL.md_utf8",
                executor_projection_hash=role_hashes["executor"], role_projection_hashes=role_hashes,
                activation_comparison="Compare active_skills.content_hash with role_projection_hashes[its recorded role], not the full-file hash or version. An unknown role stays unknown; hashes do not establish delivery of unread text.")
        return data


    def _stores(self):
        return {"source": self.source_store, **({"exploration": self.store} if self.store else {}), **self.additional_histories}

    def add_history(self, namespace, store, state):
        if not namespace or "/" in namespace or namespace in {"source", "exploration"}:
            raise ValueError("invalid additional history namespace")
        self.additional_histories[namespace] = store
        self.history_states[namespace] = state

    def add_experience(self, namespace, *, origin, oracle_success, matched_environment, config_hash):
        """Explicit evaluator registration; never discover arbitrary history files."""
        if origin not in {"no_skill", "candidate_assisted"}:
            raise ValueError("reference or unknown experience origin is excluded")
        if oracle_success is not True or matched_environment is not True or not config_hash:
            raise ValueError("experience must be independently successful and matched")
        store = self.additional_histories.get(namespace)
        if store is None or store.db.get_task(store.task_id).instruction != self.task.instruction:
            raise ValueError("experience must have the same task instruction")
        if namespace not in self.experiences and len(self.experiences) >= 2:
            raise ValueError("at most two contrast experiences")
        self.experiences[namespace] = {"namespace": namespace, "origin": origin,
            "oracle_success": True, "matched_environment": True, "config_hash": str(config_hash)[:160]}

    def experience_directory(self):
        return [{**metadata,
            "action_costs": action_cost_directory(self.additional_histories[namespace].records("event"),
                character_budget=2200, namespace=namespace + "/"),
            "notes": bounded_notes(self.additional_histories[namespace], character_budget=1100, namespace=namespace + "/"),
            "policy": "Self-generated ordinary execution. Observational contrast, not proof of causality; no reference bodies or fixture answers supplied."}
            for namespace, metadata in self.experiences.items()]

    def resolve_evidence(self, reference):
        if reference.startswith("official_skill:"):
            raise ValueError("Candidate evidence requires native task records, not official_skill guidance: "
                + reference + ". Keep guidance references in trajectory_analysis; source/ prefixes on native references are valid.")
        namespace, ref = reference.split("/", 1) if "/" in reference else ("source", reference)
        if namespace not in self._stores():
            raise ValueError("unknown evidence namespace")
        return resolve_source(self._stores()[namespace], ref)

    def observed_apps(self):
        return {r["payload"]["app"] for store in self._stores().values()
                for r in store.records("observation") if r["payload"].get("app")}

    def add_probe_evidence(self, namespace, evidence):
        store = self._stores().get(namespace)
        if store is None or evidence.get("learning_task_id") != store.task_id:
            raise ValueError("probe evidence must match registered native history")
        question = evidence.get("question")
        if not isinstance(question, str) or not question or len(question) > 1600:
            raise ValueError("probe evidence requires bounded question")
        notes = []
        for item in (evidence.get("notes") or {}).get("items", [])[:2]:
            ref = item.get("source", "").split("/", 1)[-1]
            if not ref:
                continue
            # Full versioned records remain readable. No model-written summary
            # upgrades a probe result to verified truth.
            notes.append({"source": namespace + "/" + ref,
                "preview": str(item.get("preview") or "")[:450],
                "tail_preview": str(item.get("tail_preview") or "")[-300:],
                "truncated": True})
        frontier = evidence.get("frontier") or {}
        native_frontier = frontier.get("source", "").split("/", 1)[-1]
        frontier_directory = ({"source": namespace + "/" + native_frontier,
            "decision": frontier.get("decision"), "report": str(frontier.get("report") or "")[:1200],
            "truncated": len(str(frontier.get("report") or "")) > 1200,
            "policy": "Literal final Executor report, not independent verification."} if native_frontier else None)
        self.probe_snapshots.append({"namespace": namespace, "question": question[:600],
            "learning_task_id": store.task_id, "actions_attempted": evidence.get("actions_attempted"),
            "outcome": evidence.get("outcome"), "notes": notes, "frontier": frontier_directory,
            "observation_failures":evidence.get("observation_failures", [])[:1],
            "probe_cost": evidence.get("probe_cost") or {"status": "unknown", "policy": "No incremental receipt; cumulative job elapsed is not probe duration."},
            "policy": "Literal saved probe-time notes, not verified knowledge; read native records for missing facts."})

    def record_probe_cost(self, evidence):
        # Host boundary finishes after setup wrappers, continuation and cleanup.
        # Only the last matching dispatch receives its measured incremental cost.
        for entry in reversed(self.probe_snapshots):
            if (entry["learning_task_id"] == evidence.get("learning_task_id")
                    and entry["question"] == str(evidence.get("question") or "")[:600]):
                entry["probe_cost"] = dict(evidence["probe_cost"])
                return
        raise ValueError("probe cost must match registered probe evidence")

    def probe_directory(self, character_budget=10000):
        items = []
        for entry in reversed(self.probe_snapshots[-12:]):
            if len(json.dumps([entry, *items], ensure_ascii=False)) > character_budget - 200:
                break
            items.insert(0, entry)
        return {"items": items, "omitted_count": len(self.probe_snapshots) - len(items)}

    def existing_active_guidance(self, context):
        """The same bounded frozen rule evidence for diagnosis and probe review."""
        known_guidance, omitted_guidance, seen_guidance = [], [], set()
        from agent.tool_registry import stable_hash
        for metadata in (context.get("source") or {}).get("active_skills", []):
            if not isinstance(metadata, dict):
                continue
            skill_id = metadata.get("skill_id") or metadata.get("id")
            if not skill_id or skill_id in seen_guidance:
                continue
            seen_guidance.add(skill_id)
            pack = self.library.get(skill_id)
            if pack is None:
                omitted_guidance.append(skill_id)
                continue
            body = pack.section_for("executor")
            item = {"id":skill_id,"kind":pack.kind,"app":pack.app,
                "content_hash":stable_hash(body),"content":body}
            if len(json.dumps([*known_guidance,item],ensure_ascii=False)) <= 12000:
                known_guidance.append(item)
            else:
                omitted_guidance.append(skill_id)
        return {"items":known_guidance,"omitted_ids":omitted_guidance,
            "policy":"Role-projected rules from the frozen current library for recorded active IDs, not current app state or proof of correct prior use. Missing bodies are unknown."}

    async def review_probe(self, experiment, max_actions, budget, *, context):
        if (experiment.get("environment_plan") is None
                and not (self.retain_probe_state and self.environment
                         and self.environment.status == "open" and self.environment.plan)):
            result = {"verdict": "revise", "origin": "host_contract", "reasons": [
                "Missing environment_plan. Return it in the Learner explore decision JSON: mode observe|isolated, scope, preparation, expected_condition, cleanup, cleanup_actions. Even navigation requires a reviewed plan; the Executor cannot install one. No probe or independent model audit has run."]}
            self._record_research("learner", "exploration_contract", {"decision": result}, budget,
                status="blocked", model=None)
            return result
        budget.check(exploration=True)
        model = self.settings.skill_reviewer_model.strip()
        if not model:
            from agent.skills.learner import resolve_learner_model
            model = resolve_learner_model(self.settings)
        from agent.session import executor_action_variants_schema
        payload = {**context, **self.research_session_context(), "experiment": experiment, "requested_actions": max_actions,
            "existing_active_guidance":self.existing_active_guidance(context),
            "environment_transition": self.environment_transition,
            "environment_capabilities": self.fixture_reset_adapter.metadata() if self.fixture_reset_adapter else {"available":False},
            "executor_action_contract": executor_action_variants_schema(),
            "budget": budget.snapshot(), "diagnostic_reads": self.diagnostic_reads,
            "dispatch_capacity": {
                "exploration_calls_after_audit": max(0, budget.snapshot()["remaining_exploration_calls"] - 1),
                "policy": "This audit consumes one request before device dispatch; memory, observation and reporting requests also count. Request latency still consumes remaining time."},
            "prior_probe_snapshots": self.probe_directory()["items"]}
        payload = diagnostic_payload_projection(payload, character_budget=45000)
        messages = [{"role": "system", "content": PROBE_REVIEW_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        self._record_research("reviewer", "probe_audit", {"messages": messages}, budget, status="started", model=model)
        result = await budget.ask(model, PROBE_REVIEW_SYSTEM, payload, self.settings)
        messages.append({"role": "assistant", "content": json.dumps(result, ensure_ascii=False)})
        self._record_research("reviewer", "probe_audit", {"messages": messages, "decision": result}, budget, status="completed", model=model)
        return result

    def resolve_analysis_evidence(self, reference):
        if reference.startswith("official_skill:"):
            path, separator, expected_hash = reference[len("official_skill:"):].rpartition("@")
            if not separator or self.snapshot is None:
                raise ValueError("copy a returned frozen official_skill reference")
            page = self.snapshot.read(path, 0, 1)
            if page["hash"] != expected_hash:
                raise ValueError("official skill reference hash mismatch")
            return page  # Existing guidance, never proof of actual app behavior.
        return self.resolve_evidence(reference)

    async def _queued_diagnostic_read(self, call, registries, directories, capacity):
        """Deliver a previously requested read; no new model decision or device work."""
        args = json.loads(call.arguments)
        if call.name == "read_diagnostic_context":
            data = read_diagnostic_context(directories, **{**args,
                "length": min(args.get("length", 4000), capacity)})
            return data, []
        if call.name == "read_official_skill":
            if self.snapshot is None or set(args) - {"path", "offset", "length"}:
                raise ValueError("invalid official read")
            data = self.official_skill_page(args["path"], args.get("offset", 0),
                min(args.get("length", 4000), capacity))
            return data, []
        if call.name not in {"read_history", "read_execution_outline"}:
            raise ValueError("diagnosis permits only historical read tools")
        requested_namespace = args.pop("namespace", None)
        namespace = requested_namespace or "source"
        reference = args.get("source")
        if isinstance(reference, str) and "/" in reference:
            prefix, native = reference.split("/", 1)
            if ":" not in prefix:
                if requested_namespace is not None and requested_namespace != prefix:
                    raise ValueError("conflicting history namespaces")
                namespace, args["source"] = prefix, native
        if namespace not in registries:
            raise ValueError("unknown history namespace")
        if args.get("view") == "image" and not args.get("source"):
            raise ValueError("Historical images require an exact source; locate observations with a text query first")
        if call.name == "read_execution_outline":
            if set(args) - {"start_step"}:
                raise ValueError("invalid outline arguments")
            try:
                data = scoped_history_data(execution_outline(self._stores()[namespace].records("event"),
                    start_step=args.get("start_step", 0), character_budget=min(8000, capacity)), namespace)
            except ValueError as exc:
                if str(exc) != "outline budget too small":
                    raise
                return {"status":"deferred", "reason":"outline page does not fit the remaining text space"}, []
            return {"status": "deferred" if not data["items"] and data["omitted_events"] else "success",
                "namespace": namespace, "data": data}, []
        ctx = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="learning-diagnosis-queued",
            state={"artifacts": self.artifacts, "history_text_budget": capacity})
        result = await registries[namespace].execute(call.name, args, ctx)
        data = scoped_history_data(result.data, namespace)
        return result.model_copy(update={"data": data}).model_metadata(call.name), result.attachments

    async def _compact_diagnosis(self, model, messages, session, budget, reserve_after):
        from agent.skills.analysis_session import COMPACTION_SYSTEM, TrajectoryAnalysis, validate_analysis
        from agent.skills.learner import _extract_json
        # The same native read response can checkpoint the preceding evidence.
        # Keep its entire tool batch and every subsequent literal result: the
        # checkpoint cannot cover those newly requested/unseen records.
        for index in range(len(messages)-1, 2, -1):
            message = messages[index]
            if message.get('role') != 'assistant' or not message.get('tool_calls') or not message.get('content'):
                continue
            try:
                raw = _extract_json(message['content']).get('trajectory_analysis')
                if raw is None:
                    continue
                analysis = validate_analysis(raw, self.resolve_analysis_evidence)
                if not any(analysis[key] for key in ('working_path', 'omission_candidates', 'prerequisites', 'unresolved')):
                    continue
            except (ValueError, TypeError, AttributeError):
                continue  # Invalid optional checkpoints cannot authorize eviction.
            prefix = messages[2:index]
            requested = {call['id'] for item in prefix for call in item.get('tool_calls', [])}
            answered = {item.get('tool_call_id') for item in prefix if item.get('role') == 'tool'}
            if not requested <= answered:
                continue
            compacted = [*checkpoint_phase_input(messages), *copy.deepcopy(messages[index:])]
            if diagnostic_text_size(compacted) >= diagnostic_text_size(messages) or diagnostic_text_size(compacted) > 50000:
                continue
            session.save(messages)  # Archive the complete old exchanges first.
            session.analysis = analysis
            session.analysis_current = False  # The preserved tail still has new results.
            session.reused_checkpoints += 1
            session.save(compacted, compacted=True)
            return compacted
        retained = [m for m in messages[2:] if not any(m.get(key) for key in
            ("research_capacity", "diagnostic_final_instruction", "learner_checkpoint"))]
        if (session.analysis_current and session.analysis is not None
                and retained[:len(session.history)] == session.history
                and all(m.get("learner_phase_marker") for m in retained[len(session.history):])):
            # The last decision already checkpointed these exact reads. Reuse it
            # at handoff instead of paying the model to summarize it again.
            analysis = session.analysis
            session.save(messages)
            session.reused_checkpoints += 1
            base = checkpoint_phase_input(messages)
            session.save(base, analysis=analysis, compacted=True)
            compacted = [*base, *session.checkpoint_message()]
            if diagnostic_text_size(compacted) > 50000:
                raise LearningStopped("diagnostic_context_budget")
            return compacted
        if budget.max_calls - budget.calls <= reserve_after + 1:
            raise LearningStopped("analysis_checkpoint_budget")
        history = [m for m in messages[2:] if not m.get("research_capacity")
                   and not m.get("diagnostic_final_instruction")]
        if not history:
            raise LearningStopped("diagnostic_context_budget")
        session.save(messages)  # Archive before any model compression; no source writes.
        checkpoint_input = [{"role": "system", "content": COMPACTION_SYSTEM},
            {"role": "user", "content": json.dumps({
                "source_binding": session.binding,
                "current_phase": checkpoint_phase_context(messages),
                "trajectory_analysis_schema": TrajectoryAnalysis.model_json_schema()}, ensure_ascii=False)},
            *[{k:v for k,v in m.items() if k not in {"learner_checkpoint", "diagnostic_final_instruction", "learner_phase_marker"}}
              for m in history]]
        if diagnostic_text_size(checkpoint_input) > 50000:
            raise LearningStopped("analysis_checkpoint_input_budget")
        for attempt in range(2):
            if budget.max_calls - budget.calls <= reserve_after + 1:
                raise LearningStopped("analysis_checkpoint_budget")
            budget.check()
            response = await asyncio.wait_for(complete(model, checkpoint_input, settings=self.settings,
                attempt_meter=budget.meter, max_retries=0), max(.1, budget.remaining_seconds()))
            budget.record_usage(getattr(response, "usage", None))
            try:
                analysis = validate_analysis(_extract_json(response.content), self.resolve_analysis_evidence)
                if not any(analysis[key] for key in ("working_path", "omission_candidates", "prerequisites", "unresolved")):
                    raise ValueError("empty trajectory checkpoint cannot replace history")
                break
            except ValueError as exc:
                self.artifacts.save_json("skill-learning/conversations/" + session.id, {
                    "checkpoint_error": str(exc), "response": response.content, "previous": session.ref})
                if attempt:
                    raise
                repair = [*checkpoint_input, {"role": "assistant", "content": response.content},
                    {"role": "user", "content": "Repair this checkpoint once: " + str(exc)[:1000]
                     + ". Pinned task constraints need not be repeated. Keep observed claims tied to native sources; preserve uncertainty without inventing citations."}]
                if diagnostic_text_size(repair) > 50000:
                    raise
                checkpoint_input = repair
        # Fail closed on invalid references/schema. Never silently discard history.
        base = checkpoint_phase_input(messages)
        session.save(base, analysis=analysis, compacted=True)
        compacted = [*base, *session.checkpoint_message()]
        if diagnostic_text_size(compacted) > 50000:
            raise LearningStopped("diagnostic_context_budget")
        return compacted

    async def diagnose(self, model, system, payload, budget):
        from agent.skills.learner import _extract_json
        from agent.skills.analysis_session import LearnerConversation, TRAJECTORY_FORMAT, validate_analysis
        if self._conversation_budget is not budget:
            self._conversation_budget = budget
            self.learner_conversation = LearnerConversation(self.artifacts, {
                "task_id": self.task.id, "instruction": self.task.instruction,
                "source_hash": digest({kind:self.source_store.records(kind)
                    for kind in ("event", "observation", "note")}),
                "library_hash": self.snapshot.manifest["hash"] if self.snapshot else None})
            self.diagnostic_reads = []
        session = self.learner_conversation
        session.stages += 1
        session.stalled_rounds = 0  # A new phase may introduce probe evidence or feedback.
        payload = {**payload, "trajectory_analysis_format": TRAJECTORY_FORMAT,
            "conversation_stage": session.stages}
        schema = payload.get("candidate_json_schema")
        if isinstance(schema, dict):
            # Pydantic's generated display titles repeat field names. Keep every
            # actual constraint and description; these labels need no wire space.
            schema = copy.deepcopy(schema)
            schema.pop("title", None)
            for field in schema.get("properties", {}).values():
                if isinstance(field, dict):
                    field.pop("title", None)
            payload["candidate_json_schema"] = schema
        if payload.get("environment_transition") is not None:
            self.environment_transition = payload["environment_transition"]
        from agent.session import executor_action_variants_schema
        profile_provider = getattr(self.driver, "skill_profile_ids", None)
        profiles = await profile_provider() if callable(profile_provider) else []
        action_schema = executor_action_variants_schema()
        live_exploration_disabled = payload.get("live_exploration_disabled") or budget.actions >= budget.max_actions
        if live_exploration_disabled:
            from agent.skills.learning import TARGET_SYSTEM, LEARN_SYSTEM, READONLY_SYSTEM
            if system in {TARGET_SYSTEM, LEARN_SYSTEM}:
                system = READONLY_SYSTEM
        # Learner plans experiments in prose; only Executor submits atomic actions.
        # Keep available kinds/parameter constraints, leaving execution descriptions
        # and the full schema at the Executor and independent review boundaries.
        action_context = {"executor_action_directory": learner_action_directory(action_schema)}
        if live_exploration_disabled:
            action_context = {"executor_action_directory": [{"type": v["properties"]["type"]["enum"][0]}
                for v in action_schema["anyOf"]]}
        payload = {**payload, **self.research_session_context(), **action_context,
            "prior_diagnostic_reads": [] if session.history or session.analysis else self.diagnostic_reads, "device_profiles": profiles,
            "existing_active_guidance":self.existing_active_guidance(payload),
            "environment_capabilities": self.fixture_reset_adapter.metadata() if self.fixture_reset_adapter else {"available":False},
            "matched_experiences": self.experience_directory(),
            "prior_probe_snapshots": self.probe_directory()["items"],
            "omitted_probe_snapshots": self.probe_directory()["omitted_count"],
            "live_exploration_disabled": bool(live_exploration_disabled),
            "capability_policy": "Selection with available live exploration receives the actual action schema; read-only assessment and writing receive a mechanical type/parameter directory, not a full validation schema. Actual Executor, experiment audit and Skill Reviewer enforce/use the full schema. Neither establishes app affordances or identity; compounds require authorization and historical coordinates cannot ground actions.",
            "skill_document_contract": {"frontmatter": "name,description,version,kind,app; system interfaces require interface_scope: system plus observed device_profiles"}}
        if payload.get("initial_diagnosis") and self.source_store.records("event"):
            resumed_source = (session.analysis is not None and session.binding.get("source_hash") ==
                digest({kind:self.source_store.records(kind) for kind in ("event", "observation", "note")}))
            payload["source"] = {**payload.get("source", {}),
                "execution_overview": ({"history_namespace":"source", "read_tool":"read_execution_outline",
                    "start_step":0, "policy":"Source-bound analysis already exists; read exact native actions/reports for a named gap. Omitted rows do not establish absence or verified effects."}
                    if resumed_source else execution_overview(self.source_store.records("event")))}
            # Replace repeated large previews with their existing native references.
            # The overview supplies work order; necessary detail is read on demand.
            payload["_source_history_directory"] = True
        # Reserve space for the latest native exchange as well as the system
        # objective. A 30k payload plus writing instructions can otherwise leave
        # no room even for the first bounded history result.
        # Post-review directories have no repeated source UI previews. Reserve
        # a bounded exchange allowance; native pages also adapt to the actual
        # serialized space. The 50k cap and checkpoint-before-eviction remain.
        exchange_reserve = 16000 if payload.get("review_feedback") else 20000
        input_budget = min(30000, 50000 - len(system) - exchange_reserve)
        if input_budget <= 0:
            raise LearningStopped("diagnostic_context_budget")
        diagnostic_directories = session.directories
        pinned = [{"role":"system", "content":system}]
        if session.analysis is not None:
            pinned.append({"role":"user", "content":json.dumps({"trajectory_analysis":session.analysis,
                "policy":"Source-bound working progress, not independent proof. Continue its original statuses and citations; re-read for named gaps, contradictions or review disputes. Preserve unknown dependencies."},ensure_ascii=False),
                "learner_checkpoint":True})
        capacity = learning_budget_context(budget.snapshot())
        pinned.append({"role":"user", "research_capacity":True, "content":json.dumps({
            "latest_budget":capacity, "omitted_history_rounds":session.compactions,
            "exploration_calls_after_response_and_audit":max(0,capacity["remaining_exploration_calls"]-2),
            "policy":"Latest budget supersedes earlier balances; omitted native records remain readable."},ensure_ascii=False)})
        # Prefer room for one ordinary 4000-character page plus its escaped
        # wire/metadata. Required input may still use the original46k ceiling.
        preferred_wire = 50000 - 2 * 4000 - 2000
        def project_phase(value, character_budget):
            return diagnostic_projection_with_reads(value, character_budget=character_budget,
                directories=diagnostic_directories, pinned_messages=pinned, wire_budget=preferred_wire,
                fallback_wire_budget=46000 if preferred_wire < 46000 else None)
        try:
            payload = project_phase(payload, input_budget)
        except LearningStopped:
            if (not any(payload.get(key) for key in ("review_feedback", "probe_review"))
                    and (payload.get("initial_diagnosis") or "candidate_json_schema" not in payload)):
                # Full action contracts can overflow even with an empty skill library.
                # Preserve exact contracts/task/ledger/budget; move source previews
                # to native-readable reference directories only when necessary.
                payload = project_phase({**payload, "_source_history_directory":True}, input_budget)
            else:
                if input_budget >= 30000:
                    raise
                # A conservative exchange estimate must not reject required writing
                # conditions that fit the measured wire window. Keep the original
                # 30k payload/46k wire ceilings; native pages adapt to remaining room.
                payload = project_phase(payload, 30000)
        registries = {}
        for namespace, store in self._stores().items():
            registry = AgentToolRegistry()
            state = self.task.state if namespace == "source" else self.history_states.get(namespace, self.state)
            register_memory_tools(registry, "executor", store, state)
            registries[namespace] = registry
        native = next(iter(registries.values())).catalog_for_role("executor")
        tool = next((spec for spec in native if spec["function"]["name"] == "read_history"), None)
        if tool:
            tool["function"]["description"] += " Text pages are at most 4000 characters; follow continue_source for needed remainder. Images require exact source, at most two fresh screenshots per request. Diagnosis shares a 16000-character result budget; surplus reads are queued, not absent."
            tool["function"]["parameters"]["properties"]["namespace"] = {
                "type": "string", "enum": list(registries), "default": "source"}
        outline_tool = {"type": "function", "function": {"name": "read_execution_outline",
            "description": "Read a bounded chronological action/report directory for comparing work order, costs and traversal parameters. No full UI trajectory or semantic waste labels; reports remain claims. Shares the 16000-character result budget; use read_history for necessary full events.",
            "parameters": {"type": "object", "properties": {
                "namespace": {"type": "string", "enum": list(registries), "default": "source"},
                "start_step": {"type": "integer", "minimum": 0, "default": 0}},
                "required": [], "additionalProperties": False}}}
        official_tool = OFFICIAL_TOOL if self.snapshot else None
        directory_tool = {"type":"function", "function":{
            "name":"read_diagnostic_context", "description":"Read exact frozen JSON pages of directories or archived research context deferred from this diagnostic input. No filesystem, device or private evaluator access. Shares the existing read rounds and result budget.",
            "parameters":{"type":"object", "properties":{
                "ref":{"type":"string", "enum":list(diagnostic_directories)},
                "offset":{"type":"integer", "minimum":0, "default":0},
                "length":{"type":"integer", "minimum":1, "maximum":4000, "default":4000}},
                "required":["ref"], "additionalProperties":False}}} if diagnostic_directories else None
        messages = session.messages(system, payload)
        omitted_rounds = session.compactions
        requested_rounds = payload.get("diagnostic_read_rounds")
        if requested_rounds is not None and (type(requested_rounds) is not int or requested_rounds < 0):
            raise ValueError("invalid diagnostic read round limit")
        # Explicit smaller limits remain supported for repairs/final-only calls.
        # Normal analysis follows the shared budget, not a fixed four-read cutoff.
        reserve_after = min(budget.reserve_calls, max(0, budget.max_calls - budget.calls - 1)) if requested_rounds is None else 0
        read_rounds = min(requested_rounds if requested_rounds is not None else budget.max_calls,
            max(0, budget.max_calls - budget.calls - reserve_after - 1))
        if requested_rounds is not None:
            reserve_after = max(0, budget.max_calls - budget.calls - read_rounds - 1)
        if diagnostic_text_size(messages) > 50000 and len(messages) > 2:
            messages = await self._compact_diagnosis(model, messages, session, budget, reserve_after)
        session.save(messages)
        self._record_research("learner", "diagnosis", {"conversation_ref": session.ref,
            "messages": messages, "analysis": session.analysis, "stage": session.stages,
            "compactions": session.compactions}, budget, status="started", model=model)
        pending_reads = []  # Phase-local only; never leak across jobs or feedback.
        unseen_images = 0  # Fresh attachments must reach a model before eviction.
        def queue_read(call):
            key = (call.name, json.dumps(json.loads(call.arguments), sort_keys=True))
            if not any((item.name, json.dumps(json.loads(item.arguments), sort_keys=True)) == key
                       for item in pending_reads):
                pending_reads.append(call)
        for read_round in range(read_rounds + 1):
            allow_reads = (read_round < read_rounds and (session.stalled_rounds < 2 or pending_reads)
                and budget.max_calls - budget.calls > reserve_after + 1)
            # History reads and the decision itself consume the same allowance.
            # Advertise dispatch capacity after this response and its next audit,
            # rather than leaving the initial diagnostic snapshot stale.
            snapshot = learning_budget_context(budget.snapshot())
            # Earlier balances cannot authorize work and need not accumulate
            # between native reads. Exact prior exchanges remain in research traces.
            messages = [m for m in messages if not m.get("research_capacity")]
            messages.append({"role":"user", "research_capacity":True, "content":json.dumps({
                "latest_budget":snapshot, "omitted_history_rounds":omitted_rounds,
                "exploration_calls_after_response_and_audit":max(0,snapshot["remaining_exploration_calls"]-2),
                "policy":"Latest budget supersedes earlier balances; omitted native records remain readable."}, ensure_ascii=False)})
            final_instruction = ({"role":"user", "diagnostic_final_instruction":True,
                "content":"History reads have ended because of the shared budget, explicit phase limit or repeated reads without new results. Return a decision and trajectory_analysis from supplied evidence, preserving unresolved dependencies; do not invent facts."}
                )
            if not allow_reads:
                messages.append(final_instruction)
            # Measure the complete request, including the final instruction,
            # before deciding whether a checkpoint is needed.
            # Checkpoint complete old exchanges, never partial call/result pairs.
            # Versioned sources remain readable; keep the original goal/packet.
            def size(candidate_messages=None):
                return diagnostic_text_size(candidate_messages if candidate_messages is not None else messages)
            # A repair's compacted base can still exceed the preferred reserve.
            # Permit a small exact follow-up page while the existing hard window
            # and per-result metadata reserve fit; don't checkpoint it prematurely.
            read_threshold = 50000 - 2000 if payload.get("review_feedback") else 46000
            queued_needs_space = allow_reads and pending_reads and size() > 50000 - 3000 - 2*4000
            if size() > (read_threshold if allow_reads else 50000) or queued_needs_space:
                messages = await self._compact_diagnosis(model, messages, session, budget, reserve_after)
                omitted_rounds = session.compactions
                allow_reads = allow_reads and budget.max_calls - budget.calls > reserve_after + 1
            if allow_reads and pending_reads:
                # Prior tool calls already have paired deferred results. A host
                # delivery references that request without fabricating model calls.
                delivered_chars, fresh_delivery = 0, False
                for call in list(pending_reads):
                    if json.loads(call.arguments).get("view") == "image" and unseen_images >= 2:
                        continue
                    capacity = min(4000, max(0, (50000 - size() - 3000) // 2),
                        max(0, (16000 - delivered_chars - 1500) // 2))
                    if capacity < 256:
                        break
                    budget.check()
                    try:
                        data, attachments = await self._queued_diagnostic_read(call, registries,
                            diagnostic_directories, capacity)
                    except (ValueError, KeyError, TypeError) as exc:
                        data, attachments = {"status": "failed", "reason": str(exc)[:700]}, []
                    if data.get("status") == "deferred":
                        break  # Keep the requested page queued; no evidence arrived.
                    delivery = {"original_tool_call_id": call.id, "requested_tool": call.name,
                        "arguments": json.loads(call.arguments), "result": data,
                        "policy": "Host completion of this phase's previously requested deferred read; literal evidence only. No new model request, device action or verified conclusion."}
                    message = {"role": "user", "content": json.dumps({"deferred_read_delivery": delivery},
                        ensure_ascii=False, separators=(",", ":"))}
                    if size([*messages, message]) > 50000 - 1500:
                        break
                    messages.append(message)
                    for attachment in attachments:
                        if isinstance(attachment.content, bytes):
                            encoded = base64.b64encode(attachment.content).decode()
                            messages.append({"role": "user", "content": [
                                {"type": "text", "text": "Historical evidence from the deferred read: " + attachment.label + "; cannot ground current actions."},
                                {"type": "image_url", "image_url": {"url": f"data:{attachment.mime_type};base64,{encoded}"}}]})
                            unseen_images += 1
                    delivered_chars += len(message["content"])
                    fresh_delivery = session.observe_read_result(data) or fresh_delivery
                    pending_reads.remove(call)
                if fresh_delivery:
                    session.stalled_rounds = 0
                session.save(messages)
            if pending_reads and allow_reads:
                notice = {"role": "user", "content": json.dumps({"queued_reads": [
                    {"tool": call.name, "original_tool_call_id": call.id} for call in pending_reads],
                    "policy": "These requested reads remain queued for available context space in this phase. Do not request them again; undelivered results remain unknown."})}
                if size([*messages, notice]) <= 50000 - 1500:
                    messages.append(notice)
            image_messages = [i for i, m in enumerate(messages) if isinstance(m.get("content"), list)
                and any(part.get("type") == "image_url" for part in m["content"])]
            for i in reversed(image_messages[:-2]):
                del messages[i]
            if not allow_reads:
                if not any(m.get("diagnostic_final_instruction") for m in messages):
                    messages.append(final_instruction)
                if size() > 50000:
                    raise LearningStopped("diagnostic_context_budget")
            if size() > 50000:
                raise LearningStopped("diagnostic_context_budget")
            budget.check()
            response = await asyncio.wait_for(complete(model, [{k:v for k,v in m.items() if k not in {"research_capacity", "learner_checkpoint", "diagnostic_final_instruction", "learner_phase_marker"}} for m in messages],
                tools=([tool, outline_tool] + ([official_tool] if official_tool else []) + ([directory_tool] if directory_tool else [])) if tool and allow_reads else None,
                settings=self.settings, attempt_meter=budget.meter, max_retries=0),
                max(.1, budget.remaining_seconds()))
            budget.record_usage(getattr(response, "usage", None))
            unseen_images = 0  # All attachments in this request have now been delivered.
            if not response.tool_calls:
                parsed = _extract_json(response.content)
                if parsed is None:
                    raise ValueError("invalid learner JSON")
                messages.append({"role": "assistant", "content": response.content})
                session.save(messages)  # Preserve invalid output too, before validation.
                self._record_research("learner", "diagnosis", {"conversation_ref": session.ref,
                    "messages": messages, "decision": parsed, "stage": session.stages,
                    "compactions": session.compactions}, budget, status="output_received", model=model)
                try:
                    analysis = validate_analysis(parsed["trajectory_analysis"], self.resolve_analysis_evidence) if "trajectory_analysis" in parsed else None
                except ValueError as exc:
                    repair_messages = [*messages, {"role": "user", "content":
                        "Repair this decision's trajectory_analysis once: " + str(exc)[:1800]
                        + ". Use returned native or official_skill references only. Pinned task, budget and external evaluation fields are context, not discovered app findings. Preserve unresolved hypotheses and dependencies; do not invent evidence or upgrade uncertainty. Change trajectory_analysis only; keep every other decision field unchanged. Return complete decision JSON; no tools."}]
                    if diagnostic_text_size(repair_messages) > 50000:
                        # Full exchanges and the invalid output were archived
                        # above. A format-only repair needs the pinned phase,
                        # prior validated checkpoint and this output, rather
                        # than every older raw exchange. Never truncate these
                        # required parts or add an unvalidated new summary.
                        repair_messages = analysis_repair_context(messages, session.analysis,
                            parsed, repair_messages[-1])
                    budget.check()
                    repaired = await asyncio.wait_for(complete(model,
                        [{k: v for k, v in m.items() if k not in {"research_capacity", "learner_checkpoint", "diagnostic_final_instruction", "learner_phase_marker"}} for m in repair_messages],
                        tools=None, settings=self.settings, attempt_meter=budget.meter, max_retries=0),
                        max(.1, budget.remaining_seconds()))
                    budget.record_usage(getattr(repaired, "usage", None))
                    messages = [*repair_messages, {"role": "assistant", "content": repaired.content}]
                    session.save(messages)
                    self._record_research("learner", "analysis_output_repair", {"conversation_ref": session.ref,
                        "invalid_reason": str(exc)[:1800], "messages": messages}, budget, model=model)
                    corrected = _extract_json(repaired.content)
                    if repaired.tool_calls or not isinstance(corrected, dict) or "trajectory_analysis" not in corrected:
                        raise ValueError("invalid repaired learner analysis output")
                    if ({k:v for k,v in corrected.items() if k != "trajectory_analysis"}
                            != {k:v for k,v in parsed.items() if k != "trajectory_analysis"}):
                        raise ValueError("analysis format repair changed the decision")
                    parsed = corrected
                    analysis = validate_analysis(parsed["trajectory_analysis"], self.resolve_analysis_evidence)
                if analysis is not None:
                    session.save(messages, analysis=analysis)
                self._record_research("learner", "diagnosis", {"conversation_ref": session.ref,
                    "messages": messages, "decision": parsed, "analysis": session.analysis,
                    "stage": session.stages, "compactions": session.compactions}, budget, status="completed", model=model)
                return parsed
            if not allow_reads:
                raise LearningStopped("diagnostic_read_budget")
            messages.append({"role": "assistant", "content": response.content or None,
                "tool_calls": [{"id": c.id, "type": "function", "function": {
                    "name": c.name, "arguments": c.arguments}} for c in response.tool_calls]})
            # Read-only batches share the text/result and total call budgets below.
            # A ninth read must not abort analysis when its bounded results fit.
            round_result_chars = 0
            for call_index, call in enumerate(response.tool_calls):
                try:
                    # Space for all remaining tool results (including deferrals) and
                    # the final decision instruction/budget; never break a call pair.
                    pending_result_reserve = 1500 + 600 * (len(response.tool_calls) - call_index - 1)
                    if call.name == "read_diagnostic_context":
                        args = json.loads(call.arguments)
                        capacity = min(4000, max(0, (50000 - size() - pending_result_reserve) // 2),
                            max(0, (16000 - round_result_chars - 800) // 2))
                        # Validate even deferred requests; only this call's frozen refs exist.
                        data = read_diagnostic_context(diagnostic_directories, **args)
                        if capacity < 256:
                            data = {"status":"deferred", "ref":args["ref"], "reason":"shared diagnostic text budget"}
                            queue_read(call)
                        else:
                            data = read_diagnostic_context(diagnostic_directories, **{**args,
                                "length":min(args.get("length",4000),capacity)})
                        wire = json.dumps(data, ensure_ascii=False)
                        round_result_chars += len(wire)
                        messages.append({"role":"tool", "tool_call_id":call.id, "content":wire})
                        continue
                    if call.name == "read_official_skill":
                        if self.snapshot is None:
                            raise ValueError("no official snapshot")
                        args = json.loads(call.arguments)
                        if set(args) - {"path","offset","length"}:
                            raise ValueError("invalid official read arguments")
                        data = self.official_skill_page(args["path"], args.get("offset",0), args.get("length",4000))
                        wire = json.dumps(data, ensure_ascii=False)
                        if round_result_chars + len(wire) > 16000 or size() + len(wire) + pending_result_reserve > 50000:
                            wire = json.dumps({"status":"deferred","reason":"official page exceeds shared result budget"})
                            queue_read(call)
                        round_result_chars += len(wire)
                        messages.append({"role":"tool", "tool_call_id":call.id, "content":wire})
                        continue
                    if call.name not in {"read_history", "read_execution_outline"}:
                        raise ValueError("diagnosis permits only historical read tools")
                    args = json.loads(call.arguments)
                    requested_namespace = args.pop("namespace", None)
                    namespace = requested_namespace or "source"
                    reference = args.get("source")
                    if isinstance(reference, str) and "/" in reference:
                        prefix, native = reference.split("/", 1)
                        if ":" not in prefix:
                            if requested_namespace is not None and requested_namespace != prefix:
                                raise ValueError("conflicting history namespaces")
                            namespace, args["source"] = prefix, native
                    if namespace not in registries:
                        raise ValueError("unknown history namespace")
                    if args.get("view") == "image":
                        if not args.get("source"):
                            raise ValueError("Historical images require an exact source; locate observations with a text query first")
                        if unseen_images >= 2:
                            queue_read(call)
                            messages.append({"role": "tool", "tool_call_id": call.id,
                                "content": json.dumps({"status": "deferred", "namespace": namespace,
                                    "requested_source": args["source"], "reason": "Fresh screenshot capacity; queued until earlier images reach the model. Not yet supplied."})})
                            continue
                    # Never compact or discard a partially answered tool batch.
                    # Defer pages that do not fit; checkpoint at the next round boundary.
                    messages.append({"role":"user", "content":json.dumps({"omitted_history_rounds":omitted_rounds,
                        "policy":"Omitted records remain readable; missing means unknown."})})
                    # Reserve real serialized exchange space, including escaping and
                    # deferred-result metadata. Native reads page at the supplied
                    # limit, preserving exact continuation offsets.
                    text_capacity = max(0, min(4000,
                        (50000 - size() - max(2000, pending_result_reserve)) // 2,
                        (16000 - round_result_chars - 2000) // 2))
                    # Page to the actual remaining batch space; a maximum-size estimate
                    # must not defer a short second record that would fit.
                    # Small exact pages remain useful at the boundary. The
                    # serialized result below is checked before delivery, so a
                    # fixed minimum must not reject a page that actually fits.
                    if text_capacity <= 0:
                        queue_read(call)
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({
                            "status": "deferred", "reason": "diagnostic response text budget",
                            "namespace": namespace, "requested_source": args.get("source"),
                            "requested_query": args.get("query"),
                            "policy": "Not read in this exchange; no absence or hypothesis conclusion follows. Request this native source in a later permitted round."}, ensure_ascii=False)})
                        continue
                    if call.name == "read_execution_outline":
                        if set(args) - {"start_step"}:
                            raise ValueError("invalid outline arguments")
                        try:
                            data = execution_outline(self._stores()[namespace].records("event"),
                                start_step=args.get("start_step", 0), character_budget=min(8000, text_capacity))
                        except ValueError as exc:
                            if str(exc) != "outline budget too small":
                                raise
                            data = {"items":[], "omitted_events":1}
                        data = scoped_history_data(data, namespace)
                        attachments = []
                        if not data["items"] and data["omitted_events"]:
                            queue_read(call)
                            messages.append({"role":"tool", "tool_call_id":call.id, "content":json.dumps({
                                "status":"deferred", "namespace":namespace,
                                "reason":"No outline record fits the remaining text space; requested page is queued. Not an empty history."})})
                            continue
                        wire = json.dumps({"status":"success", "namespace":namespace,
                            "summary":"Bounded literal execution outline", "data":data}, ensure_ascii=False)
                    else:
                        ctx = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="learning-diagnosis",
                            state={"artifacts": self.artifacts, "history_text_budget":text_capacity})
                        result = await registries[namespace].execute(call.name, args, ctx)
                        data, attachments = scoped_history_data(result.data, namespace), result.attachments
                        wire = json.dumps(result.model_copy(update={"data":data}).model_metadata(call.name), ensure_ascii=False)
                    projected = [*messages, {"role":"tool", "tool_call_id":call.id,"content":wire}]
                    if round_result_chars + len(wire) > 16000 or size(projected) + pending_result_reserve > 50000:
                        queue_read(call)
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({
                            "status": "deferred", "reason": "native metadata exceeds remaining diagnostic response budget",
                            "namespace": namespace, "requested_source": args.get("source"),
                            "requested_query": args.get("query"),
                            "policy": "Result was not supplied; raw versioned sources remain available. No absence or conclusion follows."}, ensure_ascii=False)})
                        continue
                    round_result_chars += len(wire)
                    for item in data.get("items", []):
                        if item.get("text"):
                            excerpt = {**item, "source": item["source"],
                                "text": item["text"][:1200]}
                            excerpt["truncated"] = item.get("truncated", False) or len(item["text"]) > 1200
                            self.diagnostic_reads.append(excerpt)
                    self.diagnostic_reads = self.diagnostic_reads[-5:]
                    messages.append({"role": "tool", "tool_call_id": call.id,
                        "content": wire})
                    for attachment in attachments:
                        if isinstance(attachment.content, bytes):
                            image = base64.b64encode(attachment.content).decode()
                            messages.append({"role": "user", "content": [
                                {"type": "text", "text": "Historical read-only evidence: " + attachment.label + "; cannot ground current actions."},
                                {"type": "image_url", "image_url": {
                                    "url": f"data:{attachment.mime_type};base64,{image}"}}]})
                            unseen_images += 1
                except (ValueError, KeyError, TypeError) as exc:
                    if str(exc) in {"conflicting history namespaces", "unknown history namespace",
                                    "diagnosis permits only historical read tools"}:
                        raise
                    # A malformed read is repairable model output, not a reason
                    # to abandon the source-local conversation. Account for the
                    # request, pair every call, and stop repeated failed reads
                    # through the existing no-progress/shared-budget boundary.
                    messages.append({"role": "tool", "tool_call_id": call.id,
                        "content": json.dumps({"status": "failed",
                            "reason": "Invalid historical read: " + str(exc)[:700],
                            "policy": "No evidence supplied by this call; repair arguments or preserve the gap as unknown."}, ensure_ascii=False)})
            # Progress is fresh supplied evidence, not another identical read.
            fresh = False
            for message in messages:
                if message.get("role") != "tool":
                    continue
                wire = message.get("content", "")
                try:
                    data = json.loads(wire)
                except ValueError:
                    continue
                if data.get("status") in {"deferred", "failed"}:
                    continue
                supplied_new_content = session.observe_read_result(data)
                fresh = fresh or supplied_new_content
            session.stalled_rounds = 0 if fresh else session.stalled_rounds + 1
            session.save(messages)
            self._record_research("learner", "history_reads", {"conversation_ref": session.ref,
                "messages": messages, "analysis": session.analysis, "stage": session.stages,
                "compactions": session.compactions}, budget, model=model)
        raise LearningStopped("diagnostic_read_budget")

    async def explore(self, question, max_actions, budget, *, context=None):
        if self.record is None:
            from agent.skills.learner import resolve_learner_model
            self.state = AgentState(instruction="Investigate reusable knowledge for: " + self.task.instruction,
                temporal_conventions=list(self.task.state.temporal_conventions) if self.task.state else [])
            self.state.revisable.limits = TaskLimits(device_actions=budget.max_actions,
                deadline_at=time.time() + budget.max_seconds)
            self.record = self.db.create_task(self.state.instruction, self.state, device_serial=self.task.device_serial)
            self.db.update_task(self.record.id, status=TaskStatus.RUNNING)
            self.store = TaskStore(self.db, self.artifacts, self.record.id)
            self.executor = LearningExecutor(self.driver, self.artifacts,
                model=resolve_learner_model(self.settings), settings=self.settings)
            self.executor._session = LearningSession("executor", self.executor.model, settings=self.settings)
            self.executor._session.library = self.library
            self.executor._session.source_store = self.source_store
            self.executor._session.source_state = self.task.state
            self.executor.store = self.store
            from agent.traces import TraceWriter
            self.executor.traces = TraceWriter(self.db, self.artifacts)
            self.executor.cancel_requested = self.cancelled
            self.executor.budget = budget
            self.environment = ResearchEnvironment(self.store, cancelled=self.cancelled,
                verifier=self.environment_verifier, reset_adapter=self.fixture_reset_adapter)
            self.executor.environment = self.environment
            self.executor.intent_id = None
            self.executor._session.research_backend = self
            self.executor._session.research_budget = budget
        plan = ((context or {}).get("experiment") or {}).get("environment_plan")
        if plan and (self.environment.plan is None or self.environment.plan.model_dump() != plan):
            if self.retain_probe_state and self.environment.receipt()["unresolved_effects"]:
                restored = await self.complete_probe(budget)
                if not restored or not restored["verified_clean"]:
                    raise LearningStopped("research_plan_change_cleanup_unverified")
            self.environment.configure(plan)
            if plan["cleanup_actions"] >= budget.max_actions:
                raise ValueError("cleanup reserve leaves no probing capacity")
            budget.cleanup_actions = plan["cleanup_actions"]
            budget.set_phase("prepare")
            await self.environment.capture_checkpoint()
            if self.fixture_reset_adapter:
                await self.prepare_disposable_fixture(budget)
            else:
                await self.environment.prepare(self.environment_initializer)
        if self.environment.stage != "cleanup":
            self.environment.set_stage("probe")
            budget.set_phase("probe")
        experiment_context = {**(context or {}), **self.research_session_context(), "environment_transition": self.environment_transition or (context or {}).get("environment_transition"), "diagnostic_reads": self.diagnostic_reads,
            "budget_scope": "Cumulative learning job; use live research_runtime.probe_cost for elapsed probe time.",
            "policy": "Historical excerpts and hypotheses are unverified data. Only the current observation grounds actions."}
        # Bound the aggregate as well as each component; prune only optional
        # literal histories, preserving source, full experiment and reset state.
        try:
            experiment_context = live_experiment_context(experiment_context, character_budget=45000)
        except LearningStopped as exc:
            raise LearningStopped("experiment_context_budget; required context cannot fit") from exc
        date_reader = getattr(self.driver, "current_device_date", None)
        if callable(date_reader):
            self.state.current_device_date = ""
            try:
                date_value = str(await asyncio.wait_for(date_reader(), max(.1, budget.remaining_seconds(exploration=budget.phase != "cleanup"))))
                self.state.current_device_date = date_value
                date_evidence = {"status":"available", "current_device_date":date_value}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                date_evidence = {"status":"unavailable", "error_type":type(exc).__name__}
            self.store.put("measurement", "device_date_context", date_evidence)
        self.executor.system_suffix = """

You are the Skill Learner performing a bounded experiment.
The stage tests the stated tentative mechanism with current tools; it is not a
request to repeat the whole user task. Read missing original evidence with
read_source_history; read_history accesses this exploration only.
Historical checks do not establish complete coverage. Test the discriminating
observation. Native observations, actions and environment receipts are already
journaled; notes should capture new mechanism findings, contradictions and unresolved
gaps, not repeat every action or stage receipt. Cite actual source IDs and separate
observations from hypotheses. When a note and a grounded decision are ready together,
you may batch write_note before one submit_executor_step in the same response;
never batch multiple device actions or use old grounding. Avoid redundant observe
calls when the current observation already answers the question; refresh when it is
missing, stale or insufficient. Finish with the result and unresolved gaps.
Do not invent knowledge, alter unrelated data, or publish canonical skills.
Preparation, probing and cleanup share this task memory. Use research_environment
for stage/claims/verification. UI navigation requires the reviewed environment plan. Content writes require
its isolated mode; observe mode prohibits committing changes. When the harness
explicitly supplies a trusted disposable fixture reset, finish with complete
experiment notes and report reset cleanup pending. Normally the harness rebuilds
the fixture after this probe. In continuous_owned_fixture research_session mode,
retain live state for audited follow-up; cleanup occurs before candidate adjudication,
independent validation or exit. To request its reset now, set research_environment
stage to cleanup and finish with notes and reset pending; the host runs the adapter
after this probe returns. No Executor tool executes that reset. Pending cleanup
never proves restoration. Do not spend probe actions rediscovering UI deletion
solely for cleanup in that declared lab. Without that
capability, existing ownership and cleanup checks remain binding: do not finish
with unverified effects on a user device. In the declared disposable lab, pending
reset is an unresolved cleanup report until the harness independently verifies it.
Cleanup consumes the reserved part of the same action budget.
"""
        self.executor.research_context = experiment_context
        self.state.revisable.limits.deadline_at = time.time() + max(0, budget.remaining_seconds(exploration=budget.phase != "cleanup"))
        self.executor.question_limit, self.executor.question_attempts = max_actions, 0
        self.state.revisable.revision += 1
        self.state.revisable.plan = Plan(current_stage=Stage(goal=question))
        self.state.revisable.stage_start_step = self.state.step_number
        transaction = ActionObservationTransaction(self.driver, ObservationBuilder())
        outcome = "question_budget"
        starting_actions = budget.actions
        starter = getattr(self.driver, "begin_task_session", None)
        if callable(starter):
            await starter(self.record.id)
        observation_failures = []
        def retain_observation_failure(exc, phase):
            row = self.store.put("measurement", "probe_observation_failure_" + str(self.state.step_number),
                {"phase":phase,"step":self.state.step_number,"diagnostics":exc.diagnostics(),
                 "policy":"No accepted current grounding; not evidence of an app affordance or failed task mechanism."})
            observation_failures.append({"source":"exploration/" + source(row,"measurement"),
                "phase":phase,"stage":exc.stage,"reason":exc.reason})

        while self.executor.question_attempts < max_actions:
            try:
                budget.check(exploration=budget.phase != "cleanup")
                if budget.actions >= budget.max_actions:
                    raise LearningStopped("action_budget")
            except LearningStopped as exc:
                outcome = str(exc)
                break
            try:
                package = await transaction.observe_current(attach_image=True)
                self.store.observe(package, self.state)
                # Capture can return a rejected package instead of raising.
                # Preserve that evidence without rendering/acting on absent grounding.
                if not package.accepted:
                    reason = package.acceptance_reason
                    raise ObservationStageError("acceptance",
                        reason if reason and reason != "accepted" else "observation_rejected")
                if not package.ui.app_id.strip():
                    raise ObservationStageError("acceptance", "foreground_application_unavailable")
            except ObservationStageError as exc:
                retain_observation_failure(exc, "current_observation")
                outcome = "observation_unavailable"
                break
            remaining = max(.1, budget.remaining_seconds(exploration=budget.phase != "cleanup"))
            try:
                step, result, _, _, refs = await asyncio.wait_for(self.executor.act_once(question, package,
                    task_id=self.record.id, state=self.state, model_call_meter=budget.exploration_meter), remaining)
            except LearningStopped as exc:
                # Reserved writing/review capacity must receive the evidence
                # already acquired, even when the stop occurs inside a tool loop.
                outcome = str(exc)
                break
            except TimeoutError:
                outcome = "time_budget_reserved"
                break
            except ObservationStageError as exc:
                retain_observation_failure(exc, "executor_observation")
                # A pending action may have dispatched; leave it unresolved, not absent.
                if self.executor.intent_id is not None:
                    self.environment.effect(self.executor.intent_id, {"success":False,
                        "observation_failure":exc.diagnostics(),"dispatch_outcome":"unknown"})
                    self.executor.intent_id = None
                outcome = "observation_unavailable"
                break
            for key in ("active_package", "post_action_package", "compound_intermediate_package"):
                observed = refs.get(key)
                if observed is not None:
                    self.store.observe(observed, self.state)
            if self.executor.intent_id is not None:
                self.environment.effect(self.executor.intent_id, result.model_dump(mode="json"))
                self.executor.intent_id = None
            self.executor.record_result(step, result, self.state,
                compound_evidence=refs.get("compound_model_evidence_package"))
            if step.decision == ExecutorDecisionKind.ACT:
                self.state.revisable.execution_count += result.detail.get("device_action_units", 1)
            self.state.step_number += 1
            self.db.update_task(self.record.id, state=self.state)
            if self.executor.directive != "act":
                outcome = self.executor.directive
                break
        # Native histories persist; only bounded new evidence is returned for writing.
        summary = self.store.operation_summary(self.state, character_budget=7000)
        for event in summary["events"]:
            event["source"] = "exploration/" + event["source"]
        latest = self.store.records("event")[-1:]  # Native claim, not an oracle result.
        frontier = ({"source":"exploration/" + source(latest[0]),
            "decision":latest[0]["payload"].get("decision"),
            "report":str(latest[0]["payload"].get("executor_report") or "")[:600],
            "policy":"Literal latest Executor report; inspect native observations to verify claims."} if latest else None)
        evidence = {"question": question, "outcome": outcome, "learning_task_id": self.record.id,
                "frontier":frontier,
                "actions_attempted": budget.actions - starting_actions,
                "events": summary, "notes": bounded_notes(self.store, namespace="exploration/"),
                "observation_failures":observation_failures,
                "observations": [{"source": "exploration/" + source(row, "observation"),
                    "app": row["payload"].get("app"),
                    "preview": str(row["payload"].get("text") or "")[:1200],
                    "truncated": len(str(row["payload"].get("text") or "")) > 1200}
                    for row in self.store.records("observation")[-2:]],
                "observed_apps": sorted(self.observed_apps()), "budget": budget.snapshot(),
                "budget_scope": "Cumulative learning job, not this probe's cost.",
                "environment": self.environment.receipt(),
                "evidence_policy": "Use exploration/<source> to cite exploratory records."}
        self.add_probe_evidence("exploration", evidence)
        transition = experiment_context.get("environment_transition") or self.environment_transition
        if transition:
            self.environment_transition = {**transition, "fixture_reset": False,
                "last_probe_task": self.record.id, "last_probe_actions": evidence["actions_attempted"],
                "policy": "The prior initialization precedes this completed probe. No new reset is confirmed; viewport, display settings and records may have changed. Re-ground current observations and measure restoration before relying on the initial state."}
        return evidence

    async def prepare_disposable_fixture(self, budget):
        """Establish actual scoped isolation before the first experimental write."""
        budget.set_phase("prepare")
        budget.action()
        await self.release_device()
        self.environment.set_stage("prepare")
        receipt = await asyncio.wait_for(self.environment.reset_fixture(),
            max(.1, budget.remaining_seconds(exploration=True)))
        if not receipt["verified_clean"]:
            raise LearningStopped("disposable_fixture_preparation_unverified")
        self.environment_transition = {"fixture_reset":True,
            "scope":self.fixture_reset_adapter.scope, "restoration_receipt":receipt,
            "policy":"Independent teardown and initializer hash established the official initial fixture. Read fixture_reset records for actual proof. All old targets and created objects are historical; re-ground live observations."}
        return receipt

    async def complete_probe(self, budget):
        """Dispose only after the controller completes any in-flight continuation."""
        if not self.fixture_reset_adapter or not self.environment or not self.environment.receipt()["unresolved_effects"]:
            return None
        old_phase = budget.phase
        budget.set_phase("cleanup")
        try:
            budget.action()  # Count the scoped adapter attempt, not its hidden device units.
            await self.release_device()
            self.environment.set_stage("cleanup")
            receipt = await asyncio.wait_for(self.environment.reset_fixture(), max(.1,budget.remaining_seconds()))
            if receipt["verified_clean"]:
                self.environment_transition = {"fixture_reset":True,"scope":self.fixture_reset_adapter.scope,
                    "policy":"Official fixture independently rebuilt after the complete probe. All old coordinates and research objects are historical; research notes remain."}
                self.environment.set_stage("probe")
            return receipt
        finally:
            budget.set_phase(old_phase)

    async def release_device(self):
        if self.record:
            ender = getattr(self.driver, "end_task_session", None)
            if callable(ender):
                await ender(self.record.id)

    async def review_preflight(self, model, payload, budget):
        system = REVIEW_SYSTEM + """
This is the independent admission review before any new ordinary trials. Complete
source_evidence review here when facts/dependencies suffice; it needs no duplicate
final review. For an unresolved correctness gap, return a specific blocking check
or scope repair. Keep unclaimed measured benefit unknown, not an automatic blocker.
"""
        return await self.review(model,system,payload,budget)

    def _read_review_record(self, args, supplied_images, image_count):
        """One exact historical page, used for immediate and queued delivery."""
        ref = args["source"]
        kind, row, offset = self.resolve_evidence(ref)
        namespace = ref.split("/", 1)[0] if "/" in ref else "source"
        store = self._stores()[namespace]
        canonical = namespace + "/" + source(row, kind)
        text = record_text(kind, row, store)
        item = {"source": canonical, "status": "success", "text": text[offset:offset+4000],
            "truncated": offset > 0 or offset+4000 < len(text),
            "policy": "Historical literal evidence. Images establish visible content only; never infer unseen bytes or ground current device actions."}
        if offset+4000 < len(text):
            item["continue_source"] = canonical + "#" + str(offset+4000)
        raw = row["payload"]
        receipt = (raw.get("action_result") or {}).get("receipt") or {}
        ids = [raw.get("observation_id"), receipt.get("observation_id"), receipt.get("effect_observation_id")]
        item["linked_observations"] = []
        for key in dict.fromkeys(filter(None, ids)):
            try:
                item["linked_observations"].append(namespace + "/" + source(store.get("observation", key), "observation"))
            except ValueError:
                item.setdefault("unresolved_observation_ids", []).append(key)
        image_data = None
        if args.get("include_image", False):
            if kind != "observation" or not raw.get("image_ref"):
                item["image_status"] = "unavailable; inspect linked observation refs"
            else:
                data = store.artifacts.read_bytes(raw["image_ref"])
                image_hash = hashlib.sha256(data).hexdigest()
                if len(data) > 2*1024*1024:
                    item["image_status"] = "deferred; image byte limit"
                elif not (data.startswith(b"\xff\xd8") or data.startswith(b"\x89PNG\r\n\x1a\n")):
                    item["image_status"] = "unavailable; unsupported image encoding"
                elif image_hash in supplied_images:
                    item.update(image_status="already_supplied", image_sha256=image_hash,
                        same_image_source=supplied_images[image_hash])
                elif image_count >= 4:
                    item["image_status"] = "deferred; four-image review limit"
                else:
                    image_data = data
                    item.update(image_status="supplied", image_sha256=image_hash)
        return item, image_data

    async def review(self, model, system, payload, budget):
        """Independent review reads native evidence and frozen guidance on demand."""
        from agent.skills.learner import _extract_json
        if self.learner_conversation and self.learner_conversation.analysis is not None:
            payload = {**payload, "learner_analysis": {
                "analysis": self.learner_conversation.analysis,
                "source_binding": self.learner_conversation.binding,
                "artifact": self.learner_conversation.ref,
                "policy": "Fallible source-local analysis, not independent proof. Audit cited native evidence and omission dependencies; no Learner conversation is inherited."}}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > 200000:
            raise LearningStopped("review_input_budget")
        messages = [{"role":"system", "content":system}, {"role":"user", "content":encoded}]
        tool = {"type":"function", "function":{
            "name":"read_review_evidence",
            "description":"Read a registered task's versioned native record or historical observation screenshot. Follow linked_observations for before/after evidence. Capacity-deferred requests are delivered in later rounds; do not request them again. No device, publication, private oracle labels or arbitrary file access.",
            "parameters":{"type":"object", "properties":{
                "source":{"type":"string", "description":"Qualified namespace/kind:key@version, optionally with #offset."},
                "include_image":{"type":"boolean"}}, "required":["source"], "additionalProperties":False}}}
        tools = [tool] + ([OFFICIAL_TOOL] if self.snapshot else [])
        image_count = 0
        supplied_images = {}
        read_log = []
        pending_reads = []  # Review-local; no extra model rounds or device work.
        def text_size():
            projected = [{**m, "content":([p for p in m["content"] if p.get("type") != "image_url"]
                if isinstance(m.get("content"),list) else m.get("content"))} for m in messages]
            return len(json.dumps(projected,ensure_ascii=False))
        def append_image(item, image_data):
            nonlocal image_count
            if image_data is None:
                return
            mime = "image/jpeg" if image_data[:2] == b"\xff\xd8" else "image/png"
            messages.append({"role":"user", "content":[
                {"type":"text", "text":"Historical read-only observation: " + item["source"] + "; visible content only, not current grounding or private fixture truth."},
                {"type":"image_url", "image_url":{"url":"data:"+mime+";base64,"+base64.b64encode(image_data).decode("ascii")}}]})
            image_count += 1
            supplied_images[item["image_sha256"]] = item["source"]
        def log_read(item, call_id):
            read_log.append({"original_tool_call_id":call_id, **{k:v for k,v in item.items()
                if k in {"source", "requested_source", "status", "image_status", "image_sha256",
                    "same_image_source", "truncated", "continue_source", "reason"}}})
        self._record_research("reviewer", "candidate_review", {"messages": messages}, budget, status="started", model=model)
        for read_round in range(4):
            budget.check()
            delivered_chars = 0
            for call_id, args in list(pending_reads):
                if delivered_chars + 6000 > 16000:
                    break
                try:
                    item, image_data = self._read_review_record(args, supplied_images, image_count)
                except (ValueError, KeyError) as exc:
                    if str(exc) == "unknown evidence namespace":
                        raise
                    item, image_data = {"requested_source":args["source"], "status":"failed", "reason":str(exc)[:700]}, None
                delivery = {"deferred_review_delivery": {"original_tool_call_id":call_id,
                    "tool":"read_review_evidence", "arguments":args, "result":item},
                    "policy":"Literal result of the earlier requested read; no new tool call or evidence judgment."}
                wire = json.dumps(delivery, ensure_ascii=False)
                if delivered_chars + len(wire) > 16000 or text_size() + len(wire) + 1500 > 250000:
                    break
                messages.append({"role":"user", "content":wire})
                append_image(item, image_data)
                delivered_chars += len(wire)
                log_read(item, call_id)
                pending_reads.remove((call_id, args))
            allow_reads = read_round < 3 and budget.max_calls - budget.calls > 1
            if not allow_reads:
                messages.append({"role":"user", "content":"Historical reads have ended within the shared call budget. Return the six-criterion verdict from supplied evidence; missing facts remain insufficient. Do not request tools or infer missing evidence as fact."})
                if text_size() > 250000:
                    raise LearningStopped("review_context_budget")
            response = await asyncio.wait_for(complete(model, messages,
                tools=tools if allow_reads else None, settings=self.settings,
                attempt_meter=budget.meter, max_retries=0),
                max(.1, budget.remaining_seconds()))
            budget.check(new_request=False)
            budget.record_usage(getattr(response,"usage",None))
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                self._record_research("reviewer", "candidate_review", {"messages": messages,
                    "evidence_reads": read_log}, budget, status="output_received", model=model)
                result = _extract_json(response.content)
                if result is None:
                    raise ValueError("invalid skill review JSON")
                # Literal read provenance, separate from six-criterion judgments.
                result["evidence_reads"] = read_log
                self._record_research("reviewer", "candidate_review", {"messages": messages,
                    "decision": result, "evidence_reads": read_log}, budget, status="completed", model=model)
                return result
            if not allow_reads:
                raise LearningStopped("review_read_budget")
            messages.append({"role":"assistant", "content":response.content or None,
                "tool_calls":[{"id":c.id,"type":"function","function":{"name":c.name,"arguments":c.arguments}} for c in response.tool_calls]})
            result_chars = 0
            for call in response.tool_calls:
                if call.name == "read_official_skill" and self.snapshot:
                    capacity = max(1, min(4000, (16000-result_chars-1500)//2,
                        (250000-text_size()-1500)//2))
                    try:
                        item, _ = await self._queued_diagnostic_read(call, {}, {}, capacity)
                        item.update(status="success", source_kind="official_skill")
                    except (ValueError, KeyError, TypeError) as exc:
                        item = {"status":"failed", "reason":str(exc)[:700]}
                    wire = json.dumps(item, ensure_ascii=False)
                    if result_chars+len(wire) > 16000 or text_size()+len(wire)+1500 > 250000:
                        item = {"status":"deferred", "reason":"review result budget; no guidance supplied",
                            "policy":"Request this exact frozen page in a later available round; missing guidance stays unknown."}
                        wire = json.dumps(item, ensure_ascii=False)
                    if result_chars+len(wire) > 16000 or text_size()+len(wire)+500 > 250000:
                        raise LearningStopped("review_context_budget")
                    messages.append({"role":"tool", "tool_call_id":call.id, "content":wire})
                    result_chars += len(wire)
                    log_read(item, call.id)
                    continue
                if call.name != "read_review_evidence":
                    raise ValueError("skill review permits only registered evidence reads")
                def failed_read(reason):
                    nonlocal result_chars
                    item = {"status":"failed", "reason":reason[:700],
                        "policy":"No evidence supplied. Copy a returned native reference to repair this read within the existing round budget; missing facts stay unknown."}
                    wire = json.dumps(item, ensure_ascii=False)
                    if result_chars + len(wire) > 16000 or text_size() + len(wire) + 500 > 250000:
                        raise LearningStopped("review_context_budget")
                    messages.append({"role":"tool", "tool_call_id":call.id, "content":wire})
                    result_chars += len(wire)
                    read_log.append(item)
                try:
                    args = json.loads(call.arguments)
                    if (not isinstance(args, dict) or set(args) - {"source", "include_image"}
                            or not isinstance(args.get("source"), str) or not args["source"].strip()
                            or len(args["source"]) > 1000 or type(args.get("include_image", False)) is not bool):
                        raise ValueError("invalid review evidence arguments")
                except (ValueError, TypeError) as exc:
                    failed_read(str(exc)); continue
                ref = args["source"]
                if ref.startswith("official_skill:"):
                    # A frozen-guidance identifier is not a foreign task namespace.
                    # This tool retrieves native evidence only. Repair the local
                    # tool mismatch, never resolve
                    # a fabricated hash/version or grant arbitrary file access.
                    failed_read("Supplied guidance is in contracts.related_skills. If read_official_skill is available, use its frozen relative path for missing guidance. An official_skill identifier is not a native task record, and a version label is not its content hash. Guidance never proves app state.")
                    continue
                image_data = None
                item = {"requested_source":ref, "status":"deferred", "reason":"review result budget; not supplied"}
                if result_chars + 6000 <= 16000:
                    try:
                        item, image_data = self._read_review_record(args, supplied_images, image_count)
                    except (ValueError, KeyError) as exc:
                        if str(exc) == "unknown evidence namespace":
                            raise
                        failed_read(str(exc)); continue
                wire = json.dumps(item,ensure_ascii=False)
                if result_chars + len(wire) > 16000:
                    item = {"requested_source":ref,"status":"deferred","reason":"review result budget; not supplied"}
                    wire = json.dumps(item,ensure_ascii=False);image_data = None
                # Images are bounded separately; text never drops the full quality input.
                if text_size() + len(wire) + 500 > 250000:
                    raise LearningStopped("review_context_budget")
                messages.append({"role":"tool","tool_call_id":call.id,"content":wire})
                result_chars += len(wire)
                log_read(item, call.id)
                append_image(item, image_data)
                if item["status"] == "deferred" and not any(queued_args == args for _, queued_args in pending_reads):
                    pending_reads.append((call.id, args))
            self._record_research("reviewer", "evidence_reads", {"messages": messages,
                "evidence_reads": read_log}, budget, model=model)
        raise LearningStopped("review_read_budget")

    def review_evidence(self, candidate, *, character_budget=24000):
        result, seen, linked = [], set(), []
        for ref in candidate.evidence:
            kind, row, _ = self.resolve_evidence(ref)
            namespace = ref.split("/", 1)[0] if "/" in ref else "source"
            payload = row["payload"]
            ids = list(payload.get("observation_ids") or [])
            if payload.get("observation_id"):
                ids.append(payload["observation_id"])
            receipt = (payload.get("action_result") or {}).get("receipt") or {}
            ids += [receipt.get(key) for key in ("observation_id", "effect_observation_id")]
            for observation_id in filter(None, ids):
                record = self._stores()[namespace].get("observation", observation_id)
                linked.append(namespace + "/" + source(record, "observation"))
        omitted = 0
        for ref in [*candidate.evidence, *linked]:
            kind, row, offset = self.resolve_evidence(ref)
            namespace = ref.split("/", 1)[0] if "/" in ref else "source"
            canonical = namespace + "/" + source(row, kind)
            base_source = canonical
            if offset:
                canonical += "#" + str(offset)
            if canonical in seen:
                continue
            seen.add(canonical)
            text = record_text(kind, row, self._stores()[namespace])
            entry = {"source": canonical, "text": text[offset:offset + 3000],
                "truncated": offset > 0 or offset + 3000 < len(text),
                **({"continue_source": base_source + "#" + str(offset + 3000)}
                    if offset + 3000 < len(text) else {}),
                "policy": "Literal observation or working record; model reports are not independent truth."}
            if len(json.dumps([*result, entry], ensure_ascii=False)) <= character_budget - 100:
                result.append(entry)
            else:
                omitted += 1
        if omitted:
            result.append({"omitted_records": omitted, "policy": "Add the needed explicit observation refs for missing evidence."})
        return result

    def review_contracts(self, candidate):
        return build_review_contracts(candidate.new_text, self.library, self.settings)

    def close(self, reason="learning_finished"):
        if self.environment:
            self.environment.close()
        for db in self.owned_history_dbs:
            db.close()
        self.owned_history_dbs.clear()
        if self.record:
            self.db.update_task(self.record.id, status=TaskStatus.CANCELLED,
                failure_reason=reason, state=self.state)


def build_review_contracts(new_text, library, settings):
    app = __import__("agent.skills.library", fromlist=["parse_skill_markdown"]).parse_skill_markdown(new_text).app
    related = [{"id": p.id, "hash": digest(p.path.read_text(encoding="utf-8")),
                "hash_scope":"complete_SKILL.md_utf8",
                "executor_projection_hash":stable_hash(p.section_for("executor")),
                "activation_comparison":"Compare source active_skills.content_hash with executor_projection_hash, never with the full-file hash.",
                "text": p.path.read_text(encoding="utf-8")}
               for p in library.load_all() if p.app in (None, app)]
    from agent.session import AgentSession
    schemas = AgentSession("executor", "unused", settings=settings)._build_registry({}).catalog_for_role("executor")
    data = {"skill_reviewer_system_hash": digest(REVIEW_SYSTEM), "related_skills": related, "prompts": {r: prompt(r) for r in ("planner", "reviewer", "executor")},
            "action_tool_schemas": schemas, "executor_submission_schema": None}
    from agent.revisable.session import executor_submission_schema
    data["executor_submission_schema"] = executor_submission_schema()
    if len(json.dumps(data, ensure_ascii=False, separators=(",", ":"))) > 90000:
        raise LearningStopped("review_contract_budget; select narrower skill scope")
    return data
