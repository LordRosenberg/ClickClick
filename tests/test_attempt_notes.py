"""Automatic notes preserve scoped outcomes without changing explicit notes."""
import json
import pytest

from agent.revisable.attempt_notes import apply_updates, candidates, contains_quote
from agent.revisable.store import TaskStore
from agent.revisable.tools import WriteNote, save_note
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from agent.revisable.dialogue import restore_dialogue
from agent.revisable.summary import parse_summary


@pytest.fixture
def memory(tmp_path):
    db = Database(tmp_path / "memory.db")
    yield TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "task"), AgentState(instruction="Finish transfer")
    db.close()


def update(**kwargs):
    return dict(text="Transfer rejected while offline; not retried online.",
                remaining_goal="Finish transfer", decision_effect="Check connectivity before retry",
                source="block", quote="Transfer rejected while offline", **kwargs)


def source(text):
    return json.dumps([{"role": "assistant", "tool_calls": [{"function": {
        "name": "submit_executor_step", "arguments": json.dumps({"summary": text})}}]}])


SOURCES = {"block": source("Transfer rejected while offline; later connection restored and transfer succeeded.")}


def test_quotes_in_nested_json_receipts():
    receipt = json.dumps([{"content": json.dumps({"result": "focus unverified"})}])
    assert contains_quote(receipt, '"result": "focus unverified"')
    assert not contains_quote(receipt, '"result": "success"')


def test_raw_error_cannot_seed_a_durable_note(memory):
    store, state = memory
    result = apply_updates(store, state, [update()], {"block": "Transfer rejected while offline"})
    assert result[0]["reason"] == "requires_executor_conclusion"
    assert not candidates(store)


def test_lifecycle_and_invalid_retirement(memory):
    store, state = memory
    result = apply_updates(store, state, [update()], SOURCES)
    key = result[0]["note_key"]
    assert candidates(store)[0]["retained"]
    assert apply_updates(store, state, [], SOURCES) == []
    assert len(candidates(store)) == 1
    bad = update(note_key=key, retire=True)
    bad["quote"] = "invented success evidence"
    assert apply_updates(store, state, [bad], SOURCES)[0]["status"] == "skipped"
    assert len(candidates(store)) == 1
    good = update(note_key=key, retire=True)
    good.update(text="Transfer now complete", quote="connection restored and transfer succeeded")
    assert apply_updates(store, state, [good], SOURCES)[0]["status"] == "retired"
    assert not candidates(store)
    assert store.get("note", key, 1)["payload"]["retained"]
    assert not store.unresolved_notes()


def test_manual_notes_are_protected_and_prioritized(memory):
    store, state = memory
    save_note(store, WriteNote(note_key="answer", content="Verified deliverable", retained="Verified deliverable"), state)
    assert apply_updates(store, state, [update(note_key="answer")], SOURCES)[0]["status"] == "skipped"
    apply_updates(store, state, [update()], SOURCES)
    assert store.retained_notes()["items"][0]["note_key"] == "answer"
    assert store.get("note", "answer")["version"] == 1


def test_capacity_and_quotes_fail_closed(memory):
    store, state = memory
    for i in range(4):
        value = update()
        value["text"] += str(i)
        result = apply_updates(store, state, [value], SOURCES)
    assert result[0]["reason"] == "note_capacity"
    assert len(candidates(store)) == 3
    assert store.unresolved_notes() == []


def test_empty_relevance_is_not_saved(memory):
    store, state = memory
    value = update()
    value["decision_effect"] = ""
    assert apply_updates(store, state, [value], SOURCES)[0]["status"] == "skipped"
    assert not store.records("note")


async def test_compaction_pipeline_restores_note_without_rewriting_it(memory, monkeypatch):
    store, state = memory
    calls = 0

    async def summarize(model, messages, **kwargs):
        nonlocal calls
        payload = next(json.loads(m["content"]) for m in messages
                       if isinstance(m.get("content"), str) and '"dialogue_sources"' in m["content"])
        assert "historical_dialogue_to_summarize" not in payload
        additions = []
        if calls == 0:
            additions = [update()]
            additions[0]["source"] = next(iter(payload["dialogue_sources"]))
        calls += 1
        return GatewayResponse(content="", model="test", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="summary", name="save_summary", arguments=json.dumps({
                "results": [{"text": "Later events only", "sources": [payload["records"][0]["source"]]}],
                "decisions_and_attempts": [], "critical_context": [], "attempt_updates": additions}))])

    monkeypatch.setattr("agent.session.complete", summarize)
    for cycle in range(2):
        for offset in range(6):
            state.step_number = cycle * 6 + offset
            store.save_dialogue(json.loads(source("Transfer rejected while offline " * 150)), state)
        await restore_dialogue(store, state, model="test", settings=Settings(_env_file=None, executor_context_tokens=1, compaction_attempt_notes=True),
                              meter=None, event_sink=None)
        state = AgentState.model_validate_json(state.model_dump_json())
        packet = store.execution_context(state, "current")["runtime_update"]["retained_notes"]
        assert packet["items"][0]["text"] == update()["text"]
        assert not store.unresolved_notes()
    assert len(candidates(store)) == 1
    assert store.records("note")[0]["version"] == 1


@pytest.mark.parametrize("removed_flag", [None, "false", "true"])
async def test_default_uses_structured_compaction_without_automatic_notes(memory, monkeypatch, removed_flag):
    if removed_flag is None:
        monkeypatch.delenv("CLICKCLICK_STRUCTURED_COMPACTION", raising=False)
    else:
        monkeypatch.setenv("CLICKCLICK_STRUCTURED_COMPACTION", removed_flag)
    store, state = memory
    async def summarize(model, messages, **kwargs):
        payload = next(json.loads(m["content"]) for m in messages
                       if isinstance(m.get("content"), str) and '"previous_items"' in m["content"])
        assert payload["records"] and "previous_items" in payload
        assert "dialogue_sources" not in payload and "automatic_notes" not in payload
        assert "attempt_updates" not in json.dumps(messages)
        assert "attempt_updates" not in json.dumps(kwargs.get("tools", []))
        return GatewayResponse(content="", model="test", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="summary", name="save_summary", arguments=json.dumps({"results": [{"text": "Observed outcome",
                "sources": [payload["records"][0]["source"]]}], "decisions_and_attempts": [], "critical_context": []}))])
    monkeypatch.setattr("agent.session.complete", summarize)
    for step in range(6):
        state.step_number = step
        store.save_dialogue([{"role": "user", "content": "Ordinary history " * 200}], state)
    await restore_dialogue(store, state, model="test", settings=Settings(_env_file=None, executor_context_tokens=1),
                          meter=None, event_sink=None)
    assert parse_summary(state.revisable.summary).results[0].text == "Observed outcome"
    assert not store.records("note")
