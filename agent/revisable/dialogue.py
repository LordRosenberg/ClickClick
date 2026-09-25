"""Compact closed interaction blocks; keep the originals in task storage."""

import json

from pydantic import Field

from agent.revisable.session import terminal_registry
from agent.context_projection import estimate_context, reuse_observation_text
from agent.session import AgentSession
from agent.tool_registry import AgentToolResult, redact_value
from agent.revisable.attempt_notes import (
    AttemptUpdate, STRUCTURED_ATTEMPT_INSTRUCTIONS, apply_updates, candidates,
)
from agent.revisable.summary import (
    StructuredSummary, STRUCTURED_PROMPT, SUBMISSION_INSTRUCTION, MAX_CHARACTERS, project_summary,
    parse_summary, SECTIONS, FORMAT,
)


class StructuredSummaryWithAttempts(StructuredSummary):
    attempt_updates: list[AttemptUpdate] = Field(default_factory=list, max_length=3)


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
    retained = store.retained_notes()
    for index, note in enumerate(retained["items"], 1):
        label = f"N{index}"
        labels[label] = [note["source"]]
        records.append({"source": label, "note_source": note["source"], "retained_text": note["text"]})
    return {"original_instruction": state.instruction,
            "previous_items": previous, "records": records,
            "separately_supplied": {"current_stage": state.revisable.stage.model_dump() if state.revisable.stage else None}}, labels


async def restore_dialogue(store, state, *, model, settings, meter, event_sink):
    runtime = state.revisable
    if not runtime.dialogue_refs:
        runtime.delivered_context = {}
    messages = store.read_dialogue(runtime.dialogue_refs)
    compact_refs, recent_refs = store.split_dialogue(runtime.dialogue_refs)
    policy = settings.context_policy(model, "executor")
    before_tokens = estimate_context(reuse_observation_text(messages))
    compact_messages = store.read_dialogue(compact_refs)
    # A maximum-size replacement must release space before we pay for it.
    summary_bound = estimate_context([{"role": "user", "content": "x" * (MAX_CHARACTERS + 100)}])
    useful = estimate_context(compact_messages) > summary_bound
    if before_tokens > policy["history_tokens"] and compact_refs and useful:
        enabled = settings.compaction_attempt_notes
        payload, source_labels = structured_input(store, state, compact_refs)
        summary_type = StructuredSummaryWithAttempts if enabled else StructuredSummary
        sources = {ref: json.dumps(redact_value(
            list(compaction_history(store.read_dialogue([ref]))),
            max_string=None, max_items=None), ensure_ascii=False) for ref in compact_refs} if enabled else {}
        session = AgentSession("executor", model, settings=settings, max_total_rounds=3)
        session.set_stable_system(
            STRUCTURED_PROMPT + (STRUCTURED_ATTEMPT_INSTRUCTIONS if enabled else "")
        )

        async def submit(args, ctx):
            value = summary_type.model_validate(args)
            value.bind_sources(source_labels)
            for key, _ in SECTIONS:
                for item in getattr(value, key):
                    if item.note_source:
                        from agent.revisable.recall import resolve_source
                        kind, note, _ = resolve_source(store, item.note_source)
                        if kind != "note" or note["payload"].get("retained") != item.text:
                            raise ValueError("note_source requires exact retained text from that note version")
            return AgentToolResult(terminal_value=value)

        registry = terminal_registry(
            session, summary_type, "save_summary", "Return a concise continuation summary.", submit
        )
        result = await session.run(
            [
                {
                    "role": "user",
                    "content": json.dumps(
                        {**payload, **({"dialogue_sources": sources,
                            "automatic_notes": candidates(store),
                            "existing_notes": [{"note_key": row["key"],
                                "retained": row["payload"].get("retained", "")}
                                for row in store.records("note")]} if enabled else {})},
                        ensure_ascii=False,
                    ),
                }
            ],
            tool_registry=registry,
            include_skill_context=False,
            reserve_terminal_round=True,
            terminal_submission_instruction=SUBMISSION_INSTRUCTION,
            model_call_meter=meter,
            event_sink=event_sink,
            artifacts=store.artifacts,
        )
        summary = result.decision.encode()
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
                **({"attempt_note_updates": apply_updates(store, state, result.decision.attempt_updates, sources)}
                   if enabled else {}),
                "request_ref": store.artifacts.save_json("llm", result.request_snapshot),
                "history_tokens_before": before_tokens,
                "history_soft_limit": policy["history_tokens"],
                "history_tokens_after": estimate_context(
                    [{"role": "user", "content": project_summary(summary)}]
                    + store.read_dialogue(recent_refs)
                ),
            },
        )
        runtime.summary = summary
        runtime.dialogue_refs = recent_refs
        runtime.delivered_context = {}
        messages = store.read_dialogue(recent_refs)
    displayed_summary = project_summary(runtime.summary, store.retained_notes())
    summary_messages = (
        [
            {
                "role": "user",
                "content": "Earlier execution summary (may be stale):\n" + displayed_summary,
            }
        ]
        if displayed_summary
        else []
    )
    return summary_messages + reuse_observation_text(messages)
