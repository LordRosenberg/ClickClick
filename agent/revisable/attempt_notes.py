"""Source-checked compaction notes; never gates completion or device actions."""

import json
import uuid

from pydantic import Field

from agent.revisable.tools import Arguments


class AttemptUpdate(Arguments):
    note_key: str = Field(default="", description="Empty string for a NEW note; copy an existing automatic note_key only for update/retirement.")
    retire: bool = False
    text: str = Field(min_length=1, max_length=360)
    remaining_goal: str = Field(default="", max_length=160)
    decision_effect: str = Field(default="", max_length=160)
    source: str = Field(min_length=1)
    quote: str = Field(min_length=12, max_length=500)


def candidates(store):
    return [{"note_key": row["key"], **row["payload"]}
            for row in store.records("note")
            if row["payload"].get("origin") == "compaction_attempt"
            and row["payload"].get("retained")]


def contains_quote(value, quote):
    """Match literal text, including JSON tool receipts encoded inside messages."""
    if isinstance(value, str):
        if quote in value:
            return True
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return False
        return contains_quote(decoded, quote) if decoded != value else False
    if isinstance(value, dict):
        return any(contains_quote(item, quote) for item in value.values())
    if isinstance(value, list):
        return any(contains_quote(item, quote) for item in value)
    return False


def decision_quotes(source):
    """Only explicit executor conclusions can seed automatic working notes."""
    try:
        messages = json.loads(source)
    except (ValueError, TypeError):
        return []
    if not isinstance(messages, list):
        return []
    result = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function", {})
            if function.get("name") != "submit_executor_step":
                continue
            try:
                args = json.loads(function.get("arguments", "{}"))
                if isinstance(args, dict) and isinstance(args.get("summary"), str):
                    result.append(args["summary"])
            except (ValueError, TypeError):
                continue
    return result


def apply_updates(store, state, updates, sources):
    """Invalid optional updates are diagnostics, not failures of summarization."""
    outcomes = []
    for update in updates:
        try:
            update = AttemptUpdate.model_validate(update)
            if update.source not in sources or not contains_quote(sources[update.source], update.quote):
                raise ValueError("source_quote_mismatch")
            if not update.retire and not any(update.quote in text for text in decision_quotes(sources[update.source])):
                raise ValueError("requires_executor_conclusion")
            previous = store.db.get_agent_record(store.task_id, "note", update.note_key) if update.note_key else None
            if update.note_key and (not previous or previous["payload"].get("origin") != "compaction_attempt"):
                raise ValueError("not_an_automatic_note")
            if update.retire and not previous:
                raise ValueError("retirement_requires_existing_note")
            if not update.retire and (not update.remaining_goal.strip() or not update.decision_effect.strip()):
                raise ValueError("missing_future_relevance")
            if not previous:
                if len(candidates(store)) >= 3 or len(store.records("note")) >= 64:
                    raise ValueError("note_capacity")
                if any(row["text"] == update.text for row in candidates(store)):
                    raise ValueError("duplicate_text")
            key = update.note_key or "attempt_" + uuid.uuid4().hex[:16]
            payload = dict(previous["payload"]) if previous else {}
            payload.setdefault("observation_ids", [])
            payload.update(origin="compaction_attempt", title="Attempt: " + update.remaining_goal[:120],
                           text=update.text, content=update.text + "\nEvidence: " + update.quote,
                           remaining_goal=update.remaining_goal, decision_effect=update.decision_effect,
                           evidence={"source": update.source, "quote": update.quote},
                           written_step=state.step_number, stage_id=state.revisable.stage_id)
            if update.retire:
                payload.pop("retained", None)
                payload["retirement_reason"] = update.text
            else:
                payload["retained"] = update.text
                payload.pop("retirement_reason", None)
            store.put("note", key, payload)
            outcomes.append({"note_key": key, "status": "retired" if update.retire else "retained"})
        except ValueError as exc:
            outcomes.append({"status": "skipped", "reason": str(exc)[:160]})
    return outcomes


STRUCTURED_ATTEMPT_INSTRUCTIONS = (
    ' Missing historical records do not establish that a fact was never observed; consult supplied notes.'
    ' Before writing the summary, use attempt_updates for explicitly recorded obstacles that caused a '
    'method to be abandoned or changed AND still affect an unfinished goal. Preserve their scope, '
    'attempted method and outcome in notes independently of the historical summary. Return [] when none '
    'qualify. Exclude routine receipts, recovered errors, completed detours, and information already in '
    'supplied notes. Use an empty note_key for a new note, or copy an existing automatic note_key to '
    'update it; retire it only when its dependent goal is complete or cited later evidence invalidates '
    "its scoped conclusion. A different method working does not resolve the original method's obstacle "
    'while that goal remains unfinished. Keep one conclusion per note; omit temporary position or '
    'pending-action status. Never turn a scoped failure into a prohibition or global absence. For '
    'retention, quote an explicit method-changing conclusion from submit_executor_step.summary, not a raw'
    ' tool error or routine correction. For retirement, later outcomes may also be quoted. For each '
    'update copy a source key and an exact quote from dialogue_sources; distinguish executor judgments '
    'from observed results. Do not infer unseen images. State remaining_goal and decision_effect for '
    'retention. Do not narrow the requested goal to a chosen method or add obligations. Omission '
    'preserves existing notes.'
)
