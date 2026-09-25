"""Test information survival and ownership, not semantic accuracy of an LLM."""

import json

import pytest
from pydantic import ValidationError

from agent.revisable.dialogue import restore_dialogue
from agent.revisable.store import TaskStore
from agent.revisable.summary import StructuredSummary, parse_summary, project_summary
from agent.revisable.tools import WriteNote, save_note
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import AgentState


def summary(text="Atlas: 27.4 units, candidate only", note_source=""):
    return StructuredSummary(results=[{"text": text, "note_source": note_source, "sources": ["R1"]}],
                             decisions_and_attempts=[], critical_context=[])


@pytest.fixture
def memory(tmp_path):
    db = Database(tmp_path / "memory.db")
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "task")
    state = AgentState(instruction="Transfer the requested result without modifying its unit.")
    yield store, state
    db.close()


def test_legacy_and_tagged_reload_are_lossless():
    for original in ("", "Old free-form summary", '{"results": ["legacy user data"]}'):
        assert project_summary(original) == original
    value = summary()
    assert parse_summary(value.encode()) == value
    assert project_summary(value.encode()) == value.render()


def test_schema_rejects_new_owner_fields_and_oversize_rendering():
    payload = summary().model_dump()
    with pytest.raises(ValidationError):
        StructuredSummary.model_validate({**payload, "unresolved": ["Check again"]})
    with pytest.raises(ValidationError):
        summary("x" * 4000)  # Headings are included in the same existing limit.
    with pytest.raises(ValidationError):
        StructuredSummary.model_validate({**payload, "results": [{"text": ""}]})


def test_only_exact_text_and_version_can_be_hidden():
    original = summary(note_source="note:value@1").encode()
    text = summary().results[0].text
    supplied = {"items": [{"source": "note:value@1", "text": text}]}
    assert project_summary(original, supplied) == ""
    assert project_summary(original) == summary().render()
    assert project_summary(original, {"items": [{"source": "note:value@2", "text": text}]})
    assert project_summary(original, {"items": [{"source": "note:value@1", "text": text + "; scope changed"}]})
    # Equal text with no shared identity may describe two separate occurrences.
    assert project_summary(summary().encode(), supplied)
    assert parse_summary(original).results[0].text == text


def test_note_eviction_restores_summary_for_every_role(memory):
    store, state = memory
    text = "Atlas: 27.4 units, candidate only. " + "Necessary source context. " * 19
    save_note(store, WriteNote(note_key="value", content=text, retained=text), state)
    state.revisable.summary = summary(text, "note:value@1").encode()
    assert project_summary(state.revisable.summary, store.retained_notes()) == ""
    for index in range(10):
        save_note(store, WriteNote(note_key=f"other-{index}", content="detail",
                                  retained=f"Other object {index}: " + "x" * 540), state)
    packet = store.retained_notes()
    assert all(item["note_key"] != "value" for item in packet["items"])
    assert text in project_summary(state.revisable.summary, packet)
    for role in ("planner", "reviewer"):
        context = store.context(state, "obs", role)
        assert text in context["operation_summary"]["earlier_executor_summary"]
    reloaded = AgentState.model_validate_json(state.model_dump_json())
    assert reloaded.revisable.summary == state.revisable.summary


@pytest.mark.parametrize("attempts", [False, True])
async def test_real_compaction_contract_preserves_tail_notes_and_full_previous_summary(memory, monkeypatch, attempts):
    store, state = memory
    text = "Atlas: 27.4 units, candidate only"
    save_note(store, WriteNote(note_key="value", content=text, retained=text,
                              unresolved="Attribution remains uncertain"), state)
    calls = []
    value = summary(text, "note:value@1")
    value.decisions_and_attempts.append(type(value.results[0])(
        text="View A exact lookup returned no matches; this did not refute the candidate.", sources=["R1"]))

    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        calls.append(payload)
        schema = kwargs["tools"][0]["function"]["parameters"]
        assert "unresolved" not in schema["properties"]
        assert set(schema["properties"]) == {"results", "decisions_and_attempts", "critical_context"} | (
            {"attempt_updates"} if attempts else set())
        assert "Do not duplicate retained notes in the narrative summary" not in str(messages)
        assert "instead of relying on the narrative summary to carry them" not in str(messages)
        if len(calls) == 2:
            assert payload["previous_items"][0]["text"] == text
        return GatewayResponse(content="", model="test", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="summary", name="save_summary", arguments=value.model_dump_json())])

    monkeypatch.setattr("agent.session.complete", complete)
    for cycle in range(2):
        refs = []
        for offset in range(5):
            state.step_number = cycle * 5 + offset
            # Multiple blocks per step, including an action receipt, are atomic.
            for content in ("Original observation " * 100, "Receipt " * 100):
                store.save_dialogue([{"role": "user", "content": content}], state)
                refs.append(state.revisable.dialogue_refs[-1])
        expected_tail = store.read_dialogue(refs[-4:])
        restored = await restore_dialogue(store, state, model="test",
            settings=Settings(_env_file=None, executor_context_tokens=1,
                              compaction_attempt_notes=attempts),
            meter=None, event_sink=None)
        assert restored[-4:] == expected_tail
        assert state.revisable.dialogue_refs == refs[-4:]
        assert text not in restored[0]["content"]  # Exact note is supplied separately.
        assert "did not refute" in restored[0]["content"]
        assert len(store.read_dialogue(refs)) == 10
        assert store.context(state, "obs")["unresolved_questions"][0]["question"] == "Attribution remains uncertain"
        assert store.get("note", "value")["version"] == 1
        assert parse_summary(state.revisable.summary).results[0].text == text
        state = AgentState.model_validate_json(state.model_dump_json())
    assert len(store.records("compaction")) == 2
    assert all(row["payload"]["summary_format"] == "clickclick.summary.v2"
               for row in store.records("compaction"))


async def test_failed_summary_does_not_commit_or_drop_history(memory, monkeypatch):
    store, state = memory
    for step in range(5):
        state.step_number = step
        store.save_dialogue([{"role": "user", "content": "Required original " * 120}], state)
    original = state.revisable.model_dump()

    async def fail(*args, **kwargs):
        raise RuntimeError("Provider unavailable")

    monkeypatch.setattr("agent.session.complete", fail)
    with pytest.raises(RuntimeError):
        await restore_dialogue(store, state, model="test",
            settings=Settings(_env_file=None, executor_context_tokens=1),
            meter=None, event_sink=None)
    assert state.revisable.model_dump() == original
    assert not store.records("compaction")


async def test_oversize_summary_retries_before_replacing_history(memory, monkeypatch):
    store, state = memory
    for step in range(5):
        state.step_number = step
        store.save_dialogue([{"role": "user", "content": "Required original " * 120}], state)
    original_refs = list(state.revisable.dialogue_refs)
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append(messages)
        assert state.revisable.dialogue_refs == original_refs
        assert not store.records("compaction")
        if len(calls) == 1:
            payload = {"results": [{"text": "x" * 3990}],
                       "decisions_and_attempts": [], "critical_context": []}
        else:
            assert any("4000 characters" in str(m.get("content", ""))
                       for m in messages if m.get("role") == "tool")
            assert "supported next step" not in str(messages)
            assert "Do not add next steps or verification requirements" in str(messages)
            payload = summary().model_dump()
        return GatewayResponse(content="", model="test", stop_reason="tool_calls", tool_calls=[
            ToolCall(id=f"summary-{len(calls)}", name="save_summary", arguments=json.dumps(payload))])

    monkeypatch.setattr("agent.session.complete", complete)
    await restore_dialogue(store, state, model="test",
        settings=Settings(_env_file=None, executor_context_tokens=1),
        meter=None, event_sink=None)
    assert len(calls) == 2
    assert len(store.records("compaction")) == 1
    assert len(store.read_dialogue(original_refs)) == 5
    saved = parse_summary(state.revisable.summary)
    assert saved.results[0].text == summary().results[0].text
    assert saved.results[0].sources == [f"dialogue:{original_refs[0]}@1"]
