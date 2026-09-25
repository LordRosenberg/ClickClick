"""Bounded, read-only history access and explicit versioned working notes."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.revisable.store import TaskStore
from agent.tool_registry import (
    AgentRole,
    AgentToolRegistry,
    AgentToolResult,
    AgentToolSpec,
    ToolCategory,
    redact_value,
)
from shared.schemas import AgentState


def tool_schema(model: type[BaseModel]) -> dict:
    """Emit explicit optional-field lists for Chat Completions validators."""

    def normalize(node):
        if isinstance(node, dict):
            result = {key: normalize(value) for key, value in node.items()}
            if result.get("type") == "object":
                result.setdefault("required", [])
            return result
        if isinstance(node, list):
            return [normalize(value) for value in node]
        return node

    return normalize(model.model_json_schema())


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WriteNote(Arguments):
    note_key: str = Field(
        min_length=1,
        max_length=80,
        description="Choose a descriptive key for a new note; copy note_key from supplied notes or read_history to update one.",
    )
    title: str = Field(default="", max_length=160)
    content: str = Field(min_length=1, max_length=6000)
    retained: str | None = Field(default=None, max_length=600,
        description="Optional short content needed after context loss; retain scope, sources and uncertainty. Omit/null preserves text and references; empty string clears it. No verification verdict or completion gate.")
    observation_ids: list[str] = Field(default_factory=list, max_length=20)
    append: bool = False
    source_refs: list[str] = Field(default_factory=list, max_length=8,
        description="Optional copied tool/history source IDs supporting a consequential conclusion.")
    unresolved: str = Field(default="", max_length=400,
        description="Only a material contradiction/question that still affects task decisions. Omission preserves an existing question.")
    resolution: str = Field(default="", max_length=400,
        description="To clear an existing question, explain how referenced evidence resolves it or makes it irrelevant; supply observation_ids or source_refs.")


class ReadHistory(Arguments):
    source: str = Field(default="", max_length=512,
                        description="Copy a supplied source ID, note_key or observation_id; use plan for missing current plan context.")
    query: str = Field(default="", max_length=200,
                       description="A few literal keywords when the source is unknown.")
    view: Literal["text", "image"] = "text"
    full: bool = Field(default=False, description="Read fuller text only when an excerpt is insufficient.")


class ReadResult(AgentToolResult):
    def model_metadata(self, tool_name: str = "") -> dict:
        # Read sizes are bounded at the tool boundary; do not silently cut notes.
        return redact_value(
            {"status": self.status.value, "summary": self.summary, "data": self.data},
            max_string=None,
            max_items=None,
        )


def save_note(store: TaskStore, args: WriteNote, state: AgentState) -> ReadResult:
    for observation_id in args.observation_ids:
        store.get("observation", observation_id)
    from agent.revisable.recall import resolve_source
    for reference in args.source_refs:
        resolve_source(store, reference)
    previous = store.db.get_agent_record(store.task_id, "note", args.note_key)
    old_question = previous["payload"].get("unresolved", "") if previous else ""
    if args.resolution and not (args.observation_ids or args.source_refs):
        raise ValueError("Support a resolution with referenced observation_ids or source_refs")
    if args.resolution and args.unresolved:
        raise ValueError("Either retain/update the question or resolve it, not both")
    if args.unresolved and not old_question and len(store.unresolved_notes()) >= 8:
        raise ValueError("Eight material questions are open; update or resolve an existing note")
    if previous is None and len(store.records("note")) >= 64:
        raise ValueError("Note directory full; update an existing note")
    payload = args.model_dump(exclude={"note_key", "append"})
    preserved_retained = args.retained is None and previous and previous["payload"].get("retained")
    if preserved_retained:
        payload["retained"] = previous["payload"]["retained"]
    if (old_question and not args.resolution) or preserved_retained:
        if old_question and not args.resolution and not args.unresolved:
            payload["unresolved"] = old_question
        # Replacing prose must not detach preserved memory from its sources.
        for key in ("observation_ids", "source_refs"):
            payload[key] = list(dict.fromkeys([*previous["payload"].get(key, []), *payload.get(key, [])]))
        WriteNote.model_validate({"note_key": args.note_key, **payload})
    payload["title"] = args.title or (previous["payload"]["title"] if previous else args.note_key)
    if args.append and previous:
        payload["content"] = previous["payload"]["content"] + "\n" + args.content
        payload["observation_ids"] = list(
            dict.fromkeys(
                [
                    *previous["payload"]["observation_ids"],
                    *args.observation_ids,
                ]
            )
        )
        payload["source_refs"] = list(dict.fromkeys([
            *previous["payload"].get("source_refs", []), *args.source_refs,
        ]))
        WriteNote.model_validate({"note_key": args.note_key, **payload})
    for key in ("unresolved", "resolution", "source_refs", "retained"):
        if not payload.get(key):
            payload.pop(key, None)
    if previous and not args.append:
        old_payload = {key: value for key, value in previous["payload"].items()
                       if key not in {"written_step", "stage_id"}}
        if payload == old_payload:
            return ReadResult(data={"note_key": args.note_key, "version": previous["version"], "unchanged": True})
    payload.update(written_step=state.step_number, stage_id=state.revisable.stage_id)
    record = store.put("note", args.note_key, payload)
    return ReadResult(data={"note_key": args.note_key, "version": record["version"]})


def register_memory_tools(
    registry: AgentToolRegistry, role: str, store: TaskStore, state: AgentState
) -> None:
    from agent.revisable.recall import read_history, record_text, resolve_source, source_id

    # Only whole records actually supplied to a fresh decision-role session qualify.
    initial_records = set()
    if role != "executor":
        for note in store.recent_notes():
            row = store.get("note", note["note_key"], note["version"])
            initial_records.add((source_id("note", row), hashlib.sha256(record_text("note", row).encode()).hexdigest()))
        for event in store.operation_summary(state)["events"]:
            # This projection retains the whole event, omitting only null fields.
            kind, row, _ = resolve_source(store, event["source"])
            initial_records.add((event["source"], hashlib.sha256(record_text(kind, row).encode()).hexdigest()))

    async def read(args, ctx):
        parsed = ReadHistory.model_validate(args)
        parsed.source, parsed.query = parsed.source.strip(), parsed.query.strip()
        if bool(parsed.source.strip()) == bool(parsed.query.strip()):
            raise ValueError("Provide either source or query, not both. Use supplied evidence before searching.")
        if parsed.view == "image" and not parsed.source:
            raise ValueError("Use a known observation source for an image; keyword searches return text.")
        if parsed.source == "plan":
            return ReadResult(data={"plan_revision": state.revisable.revision,
                                    "plan": state.revisable.plan.model_dump() if state.revisable.plan else None})
        ctx.state["history_reads"] = ctx.state.get("history_reads", 0) + 1
        data, attachments = read_history(store, parsed, ctx, initial_records)
        duplicate = bool(data["items"]) and all(item.get("already_supplied") for item in data["items"])
        return ReadResult(data=data, attachments=attachments, summary=(
            "Already supplied; use the existing evidence. Request full text only for a material missing detail."
            if duplicate else "No matching history; this does not prove no event occurred."
            if not data["items"] else "Historical evidence; use the answer to continue."
        ))

    if role == "executor" or len(store.records("observation")) > 1 or store.records("note") or store.records("event") or store.records("stage"):
        registry.register(AgentToolSpec(
            name="read_history",
            description="Read history only for a fact missing or contradicted in supplied evidence. Pass a known source, or query keywords to get relevant excerpts directly. Stop when answered.",
            category=ToolCategory.KNOWLEDGE, roles=(AgentRole(role),),
            parameters=tool_schema(ReadHistory),
        ), read)

    if role == "executor":
        async def write(args, ctx):
            return save_note(store, WriteNote.model_validate(args), state)
        registry.register(AgentToolSpec(
            name="write_note", description="Save facts needed later. append=true preserves ordered entries; otherwise replace the current note.",
            category=ToolCategory.KNOWLEDGE, roles=(AgentRole.EXECUTOR,),
            parameters=tool_schema(WriteNote),
        ), write)
