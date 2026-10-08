"""Compact closed interaction blocks; keep the originals in task storage."""

import asyncio
import copy
import json
import re

from agent.revisable.session import terminal_registry
from agent.context_projection import estimate_context, reuse_observation_text
from agent.session import AgentSession, SessionResult
from agent.tool_registry import AgentToolResult, ToolStatus, redact_value
from shared.llm_gateway import GatewayError
from agent.revisable.summary import (
    StructuredSummary, STRUCTURED_PROMPT, SUBMISSION_INSTRUCTION, MAX_CHARACTERS, project_summary,
    parse_summary, SECTIONS, FORMAT, MAX_CHECK_CONTEXT_CHARACTERS,
)


def compaction_history(messages):
    """Drop ephemeral state only at the existing compaction boundary."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.startswith('{"runtime_update":'):
            update = json.loads(content)["runtime_update"]
            transition = {
                key: update[key]
                for key in ("stage_id", "current_stage", "feedback", "plan_reason")
                if key in update
            }
            if "current_stage" in transition or transition.get("feedback") or transition.get("plan_reason"):
                yield {
                    "role": "user",
                    "content": json.dumps({"historical_stage_update": transition}),
                }
        else:
            yield message


def structured_input(store, state, compact_refs):
    """Short labels are local to this request; durable provenance stays in storage."""
    from agent.revisable.recall import source_id, resolve_source

    labels, records, previous = {}, [], []
    for index, ref in enumerate(compact_refs, 1):
        row = store.get("dialogue", ref)
        label = f"R{index}"
        labels[label] = [source_id("dialogue", row)]
        records.append({"source": label, "step": row["payload"]["step"],
                        "messages": redact_value(list(compaction_history(store.read_dialogue([ref]))),
                                                 max_string=None, max_items=None)})
    summary = parse_summary(state.revisable.summary)
    from agent.revisable.memory_checks import summary_checks, item_key, visible_check
    checks = summary_checks(store, state.revisable.summary)
    if summary:
        for key, _ in SECTIONS:
            for item in getattr(summary, key):
                label = f"P{len(previous) + 1}"
                # No invented handles: every stored source must remain readable.
                for ref in item.sources:
                    resolve_source(store, ref)
                labels[label] = item.sources
                previous.append({"source": label, "section": key, "text": item.text,
                                 **({"note_source": item.note_source} if item.note_source else {})})
                check = visible_check(checks.get(item_key(item.text, item.sources, item.note_source)))
                if check:
                    previous[-1]["text_check"] = check
    retained = store.retained_notes()
    for index, note in enumerate(retained["items"], 1):
        label = f"N{index}"
        labels[label] = [note["source"]]
        records.append({"source": label, "note_source": note["source"], "retained_text": note["text"]})
    return {"original_instruction": state.instruction,
            **({"cited_summary_text_checks": retained["cited_summary_text_checks"]}
               if retained.get("cited_summary_text_checks") else {}),
            "previous_items": previous, "records": records,
            "separately_supplied": {"current_stage": state.revisable.stage.model_dump() if state.revisable.stage else None}}, labels


async def restore_dialogue(store, state, *, model, settings, meter, event_sink):
    store.jev_status_context = settings.jev_status_context and settings.jev_mode == "enforce"
    runtime = state.revisable
    if not runtime.dialogue_refs:
        runtime.delivered_context = {}
    messages = store.read_dialogue(runtime.dialogue_refs)
    compact_refs, recent_refs = store.split_dialogue(runtime.dialogue_refs)
    policy = settings.context_policy(model, "executor")
    before_tokens = estimate_context(reuse_observation_text(messages))
    compact_messages = store.read_dialogue(compact_refs)
    # A maximum-size replacement must release space before we pay for it.
    annotation_bound = MAX_CHECK_CONTEXT_CHARACTERS if store.jev_status_context else 0
    summary_bound = estimate_context([{"role": "user", "content": "x" * (MAX_CHARACTERS + 100 + annotation_bound)}])
    useful = estimate_context(compact_messages) > summary_bound
    mode = settings.jev_mode
    # A rejected raw prefix must not start another slow correction loop every step.
    failed = getattr(store, "_jev_failed_prefix", None) or tuple(runtime.compaction_failed_prefix)
    suppressed = (mode == "enforce" and settings.jev_failure_policy == 'rollback'
                  and failed and compact_refs[:len(failed)] == list(failed))
    fallback_reason = "previous_compaction_rejected" if suppressed else ""
    if before_tokens > policy["history_tokens"] and compact_refs and useful and not suppressed:
        if (mode == "enforce" and settings.jev_failure_policy == 'rollback'
                and (not settings.jev_allow_external or not settings.jev_api_key.get_secret_value())):
            raise GatewayError("Jev enforce requires jev_allow_external=true and a configured TypeSafe API key.",
                               category="config")
        payload, source_labels = structured_input(store, state, compact_refs)
        session = AgentSession("executor", model, settings=settings,
                               max_total_rounds=2 if mode == "enforce" else 3)
        session.set_stable_system(
            STRUCTURED_PROMPT + (
                " Keep each item focused on one fact or recorded decision. A text-fidelity harness "
                "may reject items; there is only one correction round, shared with format errors. "
                "Preserve accepted items exactly and correct only the identified failures."
                if mode == "enforce" else "")
        )
        checks, frozen = [], {}
        submissions, abort_reason = 0, ""
        last_candidate, last_report = None, None

        async def submit(args, ctx):
            nonlocal submissions, abort_reason, frozen, last_candidate, last_report
            submissions += 1
            secret = settings.jev_api_key.get_secret_value() if mode != "off" else ""
            if secret and secret in json.dumps(args, ensure_ascii=False):
                raise ValueError("Summary contains a protected integration credential")
            value = StructuredSummary.model_validate(args)
            short_candidate = value.model_dump()
            value.bind_sources(source_labels)
            for key, _ in SECTIONS:
                for item in getattr(value, key):
                    if item.note_source:
                        if not re.fullmatch(r"note:[^@#]+@[1-9][0-9]*", item.note_source):
                            raise ValueError(
                                "note_source must copy a supplied note_source value (note:...@version), "
                                "not an R/P/N source label or observation ID. "
                                "If this item does not copy supplied retained note text, omit note_source "
                                "and keep the historical labels in sources."
                            )
                        from agent.revisable.recall import resolve_source
                        kind, note, _ = resolve_source(store, item.note_source)
                        if kind != "note" or note["payload"].get("retained") != item.text:
                            raise ValueError(
                                "note_source requires exact retained text from that note version. "
                                "For a paraphrase, omit note_source and keep its supplied source labels; "
                                "otherwise copy the retained text exactly."
                            )
            if mode != "off":
                from agent.revisable.summary_check import SummaryVerifier, feedback, preserve_frozen
                if mode == "enforce" and frozen:
                    preserve_frozen(value, frozen)
                last_candidate, last_report = value, None
                checker = None
                try:
                    checker = SummaryVerifier.for_store(store, settings)
                    from agent.revisable.summary_check import text_only
                    source_catalog=[{'source':source_labels[r['source']][0],
                        'type':'role_labeled_dialogue','step':r['step'],
                        'content':text_only(r['messages'])} for r in payload['records']
                        if r['source'].startswith('R')]
                    report = await checker.verify(store.task_id, value,
                        context={"original_instruction":payload["original_instruction"],
                                 "separately_supplied":payload["separately_supplied"]},source_catalog=source_catalog)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Checker faults are operational, never semantic allegations.
                    from agent.revisable.summary_check import unavailable_report
                    if checker is not None:
                        checker.availability.finish(checker.availability.generation, failed=True,
                            cooldown_s=settings.jev_failure_cooldown_s)
                    report = unavailable_report(store.task_id, value, settings, store.artifacts)
                last_report = report
                checks.append(report)
                if mode == "enforce" and not report["accepted"]:
                    if submissions == 1 and report["repairable"]:
                        frozen = {section: [item.model_dump() for index, item in enumerate(getattr(value, section))
                            if any(r["item_id"] == f"/{section}/{index}" and r["accepted"]
                                   for r in report["results"])] for section, _ in SECTIONS}
                        data = feedback(report)
                        # Model-facing references must remain the supplied short labels.
                        data["frozen_items"] = [{"item_id": r["item_id"],
                            **copy.deepcopy(short_candidate[r["item_id"].split("/")[1]][int(r["item_id"].split("/")[2])])}
                            for r in report["results"] if r["accepted"]]
                        data["instruction"] += " For submission use the supplied short source labels (R/P/N), not durable source IDs from the audit."
                        return AgentToolResult(status=ToolStatus.INVALID_ARGUMENTS,
                            summary="Correct the rejected summary items once; preserve frozen items.",
                            error="summary_text_fidelity", data=data)
                    # Operational/uncertain failures, or a failed correction, do not buy another LLM call.
                    abort_reason = "summary_check_rejected" if report.get("repairable") else "summary_check_unavailable_or_uncertain"
            return AgentToolResult(terminal_value=value)

        registry = terminal_registry(
            session, StructuredSummary, "save_summary", "Return a concise continuation summary.", submit,
            timeout_ms=min(120000, int((settings.jev_timeout_s + 2) * 1000)) if mode != "off" else 10000,
        )
        async def run_compaction():
            from agent.revisable.citation_check import sanitize
            input_payload = dict(payload)
            if mode != "off":
                input_payload = sanitize(input_payload, secrets=(settings.jev_api_key.get_secret_value(),))
            return await session.run(
                [{"role": "user", "content": json.dumps(input_payload, ensure_ascii=False)}],
                tool_registry=registry,
                include_skill_context=False,
                reserve_terminal_round=True,
                terminal_submission_instruction=SUBMISSION_INSTRUCTION,
                model_call_meter=meter,
                event_sink=event_sink,
                artifacts=store.artifacts,
            )
        try:
            if mode == "enforce":
                async with asyncio.timeout(settings.jev_compaction_timeout_s):
                    result = await run_compaction()
            else:
                result = await run_compaction()
        except (GatewayError, TimeoutError) as exc:
            if mode != "enforce":
                raise
            # Authentication, model quota and other LLM failures retain their established behavior.
            if isinstance(exc, GatewayError) and exc.category not in {"budget", "malformed", "context_capacity"}:
                raise
            result = None
            abort_reason = "compaction_deadline" if isinstance(exc, TimeoutError) else "summary_correction_failed"
        if abort_reason and settings.jev_failure_policy == 'bypass' and last_candidate is not None:
            # One correction is the ceiling. Keep accepted or unassessed items;
            # never re-admit a known rejected assertion to obtain availability.
            rejected = {row['item_id'] for row in (last_report or {}).get('results', [])
                        if row.get('repairable')}
            fallback = last_candidate.model_copy(deep=True)
            omitted = []
            for section, _ in SECTIONS:
                kept = []
                for index, item in enumerate(getattr(fallback, section)):
                    path = f'/{section}/{index}'
                    if path in rejected:
                        omitted.append({'item_id': path, **item.model_dump()})
                    else:
                        kept.append(item)
                setattr(fallback, section, kept)
            degradation = {'status': 'degraded_compaction', 'reason': abort_reason,
                'validated': False, 'omitted_items': omitted, 'source_refs': compact_refs,
                'llm_submission_count': submissions,
                'check_refs': [check.get('audit_ref') for check in checks]}
            # Store this beside compaction, with the original histories still durable.
            store.put('compaction_check', str(len(store.records('compaction_check'))), degradation)
            result = SessionResult(decision=fallback, request_snapshot={
                'type': 'degraded_compaction_candidate', 'reason': abort_reason,
                'original_request_snapshot': result.request_snapshot if result is not None else None,
                'candidate': fallback.model_dump(), 'check_refs': degradation['check_refs']})
            checks.append({'audit_ref': None, 'degraded': True, 'reason': abort_reason,
                           'omitted_item_count': len(omitted)})
            abort_reason = ''
        if abort_reason:
            fallback_reason = abort_reason
            store._jev_failed_prefix = tuple(compact_refs)
            runtime.compaction_failed_prefix = list(compact_refs)
            audit_ref = store.artifacts.save_json("jev-checks", {
                "status": "rolled_back", "reason": abort_reason, "source_refs": compact_refs,
                "summary_preserved": True, "llm_submission_count": submissions,
                "check_refs": [check.get("audit_ref") for check in checks]})
            store.put("compaction_check", str(len(store.records("compaction_check"))),
                      {"status": "rolled_back", "reason": abort_reason, "audit_ref": audit_ref})
        else:
            _commit_compaction(store, state, result, compact_refs, recent_refs, before_tokens,
                               policy, checks, last_report=last_report)
            messages = store.read_dialogue(recent_refs)
    displayed_summary = store.project_summary(runtime.summary, store.retained_notes())
    summary_messages = (
        [{"role": "user", "content": "Earlier execution summary (may be stale):\n" + displayed_summary}]
        if displayed_summary else []
    )
    restored = summary_messages + reuse_observation_text(messages)
    if fallback_reason:
        limit = policy.get("max_input_tokens")
        if limit is None or estimate_context(restored) > limit:
            raise GatewayError(
                "Jev compaction could not be admitted; original history was preserved. "
                "Context capacity is insufficient or no max_input_tokens is configured.",
                category="context_capacity")
        # AgentSession still checks the complete request (system, tools, observation, history)
        # before any executor action. A history-only estimate never overrides that check.
    return restored


def _commit_compaction(store, state, result, compact_refs, recent_refs, before_tokens,
                       policy, checks, *, last_report=None):
    from agent.revisable.memory_checks import collect_checks
    runtime = state.revisable
    summary = result.decision.encode()
    item_checks = collect_checks(result.decision, last_report or {"results": []}) if checks else {}
    store.put(
        "compaction",
        str(len(store.records("compaction"))),
        {
            "source_refs": compact_refs,
            "retained_refs": recent_refs,
            "source_steps": sorted(
                {store.get("dialogue", ref)["payload"]["step"] for ref in compact_refs}
            ),
            "retained_steps": sorted(
                {store.get("dialogue", ref)["payload"]["step"] for ref in recent_refs}
            ),
            "summary": summary,
            "summary_format": FORMAT,
            **({"item_text_checks": item_checks} if checks else {}),
            **({"jev_check_refs": [check.get("audit_ref") for check in checks]} if checks else {}),
            **({'jev_degraded': any(check.get('degraded') for check in checks),
                'jev_degraded_reasons': sorted({check.get('reason') or check.get('error_category')
                    or row.get('degraded_reason') for check in checks
                    for row in (check.get('results') or [{}])
                    if check.get('reason') or check.get('error_category') or row.get('degraded_reason')})}
               if checks else {}),
            "request_ref": store.artifacts.save_json("llm", result.request_snapshot),
            "history_tokens_before": before_tokens,
            "history_soft_limit": policy["history_tokens"],
            "history_tokens_after": estimate_context(
                [{"role": "user", "content": project_summary(summary,
                    text_checks=item_checks if store.jev_status_context else None)}]
                + store.read_dialogue(recent_refs)
            ),
        },
    )
    runtime.summary = summary
    runtime.dialogue_refs = recent_refs
    runtime.delivered_context = {}
