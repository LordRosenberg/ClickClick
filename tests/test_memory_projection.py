"""Slim context must preserve every durable read/update path."""
import json

import pytest

from agent.revisable.dialogue import structured_input
from agent.revisable.recall import read_history, resolve_source
from agent.revisable.store import TaskStore
from agent.revisable.summary import StructuredSummary
from agent.revisable.tools import ReadHistory, WriteNote, save_note
from agent.tool_registry import AgentRole, ToolExecutionContext
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState


@pytest.fixture
def memory(tmp_path):
    db = Database(tmp_path / "memory.db")
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "task")
    state = AgentState(instruction="Carry the exact reference to another app")
    yield store, state
    db.close()


def read(store, source, **kwargs):
    return read_history(store, ReadHistory(source=source, **kwargs),
                        ToolExecutionContext(AgentRole.EXECUTOR, "read", {}), set())


def test_note_projection_can_read_old_full_sources_image_and_update(memory):
    store, state = memory
    image = b"original screenshot bytes"
    store.put("observation", "original", {"text": "Reference A-019", "step": 0,
        "image_ref": store.artifacts.save_bytes("history_images", image)})
    save_note(store, WriteNote(note_key="transfer", content="A-019 details " * 400,
        retained="Reference A-019", unresolved="Destination not yet reached",
        observation_ids=["original"]), state)
    packet = store.context(state, "current")
    note = packet["retained_notes"]["items"][0]
    assert "observation_ids" not in note
    assert "source_refs" not in note
    assert packet["notes"][0]["body_in_retained_notes"]
    assert "observation_ids" not in packet["unresolved_questions"][0]
    # Internal version remains available to reviewer cache/digest logic.
    assert store.note_index()[0]["version"] == 1
    save_note(store, WriteNote(note_key=note["note_key"], content="Corrected details"), state)
    old, _ = read(store, note["source"], full=True)
    assert old["items"][0]["observation_ids"] == ["original"]
    assert old["items"][0]["text"].startswith("transfer\nA-019")
    partial, _ = read(store, note["source"])
    assert partial["items"][0]["truncated"]
    rest, _ = read(store, partial["items"][0]["continue_source"])
    assert rest["items"][0]["text"]
    _, attachments = read(store, old["items"][0]["observation_ids"][0], view="image")
    assert attachments[0].content == image
    assert store.get("note", note["note_key"])["version"] == 2


def test_provenance_survives_two_compactions_and_reload_without_id_prose(memory):
    store, state = memory
    store.save_dialogue([{"role": "user", "content": "View A had zero exact matches; no global absence established."}], state)
    first = state.revisable.dialogue_refs[0]
    payload, labels = structured_input(store, state, [first])
    assert payload["records"][0]["source"] == "R1"
    value = StructuredSummary(results=[], critical_context=[], decisions_and_attempts=[{
        "text": "View A exact lookup returned zero matches.", "sources": ["R1"]}])
    value.bind_sources(labels)
    state.revisable.summary = value.encode()
    store.put("compaction", "0", {"summary": value.encode()})
    state = AgentState.model_validate_json(state.model_dump_json())
    state.step_number = 1
    store.save_dialogue([{"role": "user", "content": "Destination opened"}], state)
    payload, labels = structured_input(store, state, state.revisable.dialogue_refs[-1:])
    assert payload["previous_items"][0]["source"] == "P1"
    value.decisions_and_attempts[0].sources = ["P1"]
    value.bind_sources(labels)
    assert value.decisions_and_attempts[0].sources == [f"dialogue:{first}@1"]
    assert first not in value.render()
    record, _ = read(store, value.decisions_and_attempts[0].sources[0], full=True)
    assert "no global absence" in record["items"][0]["text"]
    source = store.context(state, "current")["summary_source"]
    stored, _ = read(store, source, full=True)
    assert first in stored["items"][0]["text"]


def test_invalid_source_rejected_without_mutating_other_items():
    value = StructuredSummary(results=[{"text": "A", "sources": ["R1"]},
                                       {"text": "B", "sources": ["made-up"]}],
                              decisions_and_attempts=[], critical_context=[])
    before = value.model_dump()
    with pytest.raises(ValueError, match="source labels"):
        value.bind_sources({"R1": ["dialogue:original@1"]})
    assert value.model_dump() == before


def test_summary_retry_requests_margin_instead_of_tiny_trimming():
    with pytest.raises(ValueError, match="Rewrite toward 3200"):
        StructuredSummary(results=[{"text": "x" * 3995}], decisions_and_attempts=[], critical_context=[])
