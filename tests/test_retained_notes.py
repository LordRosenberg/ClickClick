"""Memory integrity regressions; model selection/interpretation is not simulated as truth."""

import json

import pytest

from agent.revisable.dialogue import restore_dialogue
from agent.revisable.recall import record_text
from agent.revisable.store import TaskStore
from agent.revisable.tools import WriteNote, save_note
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import AgentState


@pytest.fixture
def memory(tmp_path):
    db = Database(tmp_path / "memory.db")
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "task")
    state = AgentState(instruction="Carry the observed value to another app")
    for key in ("first", "second"):
        store.put("observation", key, {"text": "Candidate value", "image_ref": None})
    yield store, state
    db.close()


def test_unselected_notes_keep_existing_context_and_payload(memory):
    store, state = memory
    save_note(store, WriteNote(note_key="ordinary", content="Existing note"), state)
    assert "retained" not in store.get("note", "ordinary")["payload"]
    for role in ("executor", "planner", "reviewer"):
        assert "retained_notes" not in store.context(state, "first", role)
    assert "has_retained_content" not in store.note_index()[0]


def test_redundant_replace_does_not_make_versions_but_equal_appends_do(memory):
    store, state = memory
    args = WriteNote(note_key="values", content="7", observation_ids=["first"])
    save_note(store, args, state)
    state.step_number = 5
    unchanged = save_note(store, args, state)
    assert unchanged.data == {"note_key": "values", "version": 1, "unchanged": True}
    save_note(store, args.model_copy(update={"append": True}), state)
    assert store.get("note", "values")["payload"]["content"] == "7\n7"
    assert store.get("note", "values")["version"] == 2
    assert store.get("note", "values", 1)["payload"]["content"] == "7"


def test_preserve_correct_and_clear_retained_note_across_runtime_updates(memory):
    store, state = memory
    save_note(store, WriteNote(note_key="result", content="Source details",
                              retained="Object A: 7, unverified", observation_ids=["first"]), state)
    initial = store.execution_context(state, "first")["runtime_update"]
    assert initial["retained_notes"]["items"][0]["text"] == "Object A: 7, unverified"
    assert store.unresolved_notes() == []
    save_note(store, WriteNote(note_key="result", content="More details", observation_ids=["second"]), state)
    latest = store.get("note", "result")
    assert latest["payload"]["observation_ids"] == ["first", "second"]
    assert latest["payload"]["retained"] == "Object A: 7, unverified"
    assert "Object A: 7, unverified" in record_text("note", latest)
    save_note(store, WriteNote(note_key="result", content="Corrected",
                              retained="Object A: 8, observed", observation_ids=["second"]), state)
    update = store.execution_context(state, "second")["runtime_update"]
    assert update["retained_notes"]["items"][0]["text"] == "Object A: 8, observed"
    assert store.get("note", "result", 1)["payload"]["retained"] == "Object A: 7, unverified"
    save_note(store, WriteNote(note_key="result", content="Used in destination", retained=""), state)
    cleared = store.execution_context(state, "second")["runtime_update"]["retained_notes"]
    assert cleared["items"] == [] and cleared["omitted_count"] == 0
    assert "retained" not in store.get("note", "result")["payload"]


def test_preserving_retained_text_does_not_reopen_explicitly_resolved_question(memory):
    store, state = memory
    save_note(store, WriteNote(note_key="result", content="Conflicting values", retained="A: 7 or 8",
                              unresolved="Which value applies?", observation_ids=["first"]), state)
    save_note(store, WriteNote(note_key="result", content="Resolved scope",
                              resolution="Second view establishes requested scope", observation_ids=["second"]), state)
    assert store.unresolved_notes() == []
    assert store.get("note", "result")["payload"]["retained"] == "A: 7 or 8"
    # Preservation is intentionally literal; resolving a question is not automatic fact rewriting.


def test_retained_context_is_bounded_with_explicit_omissions_and_full_recall(memory):
    store, state = memory
    for index in range(12):
        save_note(store, WriteNote(note_key=f"value-{index}", content="Source detail",
                                  retained=f"Object {index}: " + "数" * 550,
                                  observation_ids=["first"]), state)
    packet = store.context(state, "first")["retained_notes"]
    assert len(json.dumps(packet, ensure_ascii=False)) <= 3000
    assert len(packet["items"]) + packet["omitted_count"] == 12
    assert packet["omitted_count"] > 0
    assert packet["items"][0]["note_key"] == "value-11"
    assert all(len(item["text"]) > 550 for item in packet["items"])
    assert all(row["has_retained_content"] for row in store.note_index())
    assert "Object 0:" in record_text("note", store.get("note", "value-0"))


async def test_repeated_compression_and_reload_cannot_rewrite_selected_candidate(memory, monkeypatch):
    store, state = memory
    candidate = "Object A / requested metric: 7; candidate from overview, unverified"
    save_note(store, WriteNote(note_key="answer", content="Overview finding", retained=candidate,
                              observation_ids=["first"]), state)
    original_refs = []

    async def summarize(*args, **kwargs):
        # Deliberately wrong summary: preservation must not rely on model compliance.
        return GatewayResponse(content="", model="test", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="summary", name="save_summary", arguments=json.dumps({
                "results": [{"text": "Candidate 7 was rejected.", "sources": ["R1"]}],
                "decisions_and_attempts": [], "critical_context": []}))
        ])

    monkeypatch.setattr("agent.session.complete", summarize)
    for cycle in range(2):
        for offset in range(6):
            state.step_number = cycle * 6 + offset
            store.save_dialogue([{"role": "user", "content": "Observed material " * 150}], state)
        original_refs.extend(ref for ref in state.revisable.dialogue_refs if ref not in original_refs)
        store.execution_context(state, "first")
        await restore_dialogue(store, state, model="test",
                              settings=Settings(_env_file=None, executor_context_tokens=1),
                              meter=None, event_sink=None)
        state = AgentState.model_validate_json(state.model_dump_json())
        for role in ("executor", "planner", "reviewer"):
            packet = store.context(state, "first", role)["retained_notes"]
            assert packet["items"][0]["text"] == candidate
            assert packet["items"][0]["source"] == "note:answer@1"
        assert store.execution_context(state, "first")["runtime_update"]["retained_notes"]["items"]
    assert len(store.records("compaction")) == 2
    assert len(store.read_dialogue(original_refs)) == 12
    assert store.unresolved_notes() == []
