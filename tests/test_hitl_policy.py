"""Real profile, evidence, persistence and selective rollback boundaries."""

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from agent.hitl import register_human_tool
from agent.interaction_policy import interaction_profile
from agent.prompts import render_role_system
from agent.revisable.recall import read_history, record_text, resolve_source, source_id
from agent.revisable.store import TaskStore
from agent.revisable.tools import ReadHistory, WriteNote, save_note
from agent.tool_registry import AgentRole, AgentToolRegistry, ToolExecutionContext
from evaluation.hitl_policy_revision import inventory, restore, seal
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.hitl import HitlConfig, HumanInputBroker, Reply
from shared.schemas import AgentState


@pytest.fixture
def task(tmp_path):
    db = Database(tmp_path / "task.db")
    value = db.create_task("Set the requested alarm", device_serial="phone")
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), value.id)
    broker = HumanInputBroker(db)
    broker.configure(value.id, HitlConfig(enabled=True))
    yield store, broker, value.id
    db.close()


@pytest.mark.parametrize("role", ["planner", "executor", "reviewer"])
@pytest.mark.parametrize("mode", ["disabled", "managed", "delegated"])
def test_real_registry_and_prompt_agree_on_role_and_shape(task, role, mode):
    store, broker, task_id = task
    broker.db.append_agent_record(task_id, "hitl_config", "snapshot", {
        **broker.config(task_id), "enabled": mode != "disabled",
        "mode": "managed" if mode == "disabled" else mode,
    })
    session = SimpleNamespace(role=role, human_input=(broker, task_id))
    registry = AgentToolRegistry()
    register_human_tool(registry, session)
    catalog = registry.catalog_for_role(AgentRole(role))
    text = render_role_system(role, hitl_enabled=mode != "disabled",
                              hitl_mode="managed" if mode == "disabled" else mode)
    profile = interaction_profile(enabled=mode != "disabled",
                                  mode="managed" if mode == "disabled" else mode)
    if mode == "disabled" or role == "reviewer":
        assert not catalog
        assert "ask_user" not in text
        return
    tool = catalog[0]["function"]
    fields = tool["parameters"]["properties"]
    if mode == "delegated":
        assert set(fields) == {"id", "question", "reason", "options"}
        assert "purpose=confirmation" not in text + tool["description"] + profile.recovery_nudge + profile.submission_hint
    else:
        assert set(fields) == {"purpose", "questions"}
        assert "purpose=confirmation" in text + tool["description"]
    assert profile.tool_form in text and profile.tool_form in tool["description"]


def test_role_name_without_broker_never_grants_human_tool():
    registry = AgentToolRegistry()
    register_human_tool(registry, SimpleNamespace(role="executor"))
    assert not registry.catalog_for_role(AgentRole.EXECUTOR)


def resolved_answer(task):
    store, broker, task_id = task
    pending = broker.begin(task_id, {"questions": [{"id": "time", "question": "Which time?"}]},
                           {"private_checkpoint": "must-not-leak"},
                           role="planner", invocation_id="inv", call_id="call")
    broker.reply(task_id, Reply(interaction_id=pending["interaction_id"],
                 owner_version=pending["owner_version"], response_id="answer",
                 outcome="answered", answers=[{"question_id": "time", "text": "9:45 AM"}]))
    return store.records("human_input")[-1]


def test_real_answer_source_clears_note_without_leaking_checkpoint(task):
    store, broker, task_id = task
    state = AgentState(instruction="Set the requested alarm")
    save_note(store, WriteNote(note_key="time", content="Time conflicts", unresolved="AM or PM?"), state)
    row = resolved_answer(task)
    source = source_id("human_input", row)
    kind, public, _ = resolve_source(store, source)
    text = record_text(kind, public, store)
    assert "9:45 AM" in text
    assert "checkpoint" not in text and "must-not-leak" not in text and "owner_version" not in text
    save_note(store, WriteNote(note_key="time", content="User clarified 9:45 AM",
                              resolution="User resolved AM versus PM", source_refs=[source]), state)
    assert not store.unresolved_notes()
    assert store.context(state, "obs", "planner")["user_input"][0]["source"] == source
    context = ToolExecutionContext(AgentRole.PLANNER, "inv", {})
    result, _ = read_history(store, ReadHistory(query="9:45"), context, set())
    assert any(item["source"] == source for item in result["items"])
    assert "must-not-leak" not in json.dumps(result)


def test_pending_cross_task_and_wrong_version_are_not_answer_evidence(task):
    store, broker, task_id = task
    item = broker.begin(task_id, {"questions": [{"id": "q", "question": "Which time?"}]}, {},
                        role="planner", invocation_id="inv", call_id="call")
    pending = store.records("human_input")[-1]
    with pytest.raises(ValueError, match="resolved"):
        resolve_source(store, source_id("human_input", pending))
    with pytest.raises(ValueError, match="Unknown"):
        resolve_source(store, f"human_input:{item['interaction_id']}@999")
    other = TaskStore(store.db, store.artifacts, "other-task")
    with pytest.raises(ValueError, match="Unknown"):
        resolve_source(other, source_id("human_input", pending))


def test_all_role_texts_freeze_together_across_source_changes(task, monkeypatch, tmp_path):
    import agent.prompts as prompts
    store, broker, task_id = task
    config = broker.config(task_id)
    expected = {role: render_role_system(role, hitl_enabled=True) for role in ("planner", "executor", "reviewer")}
    assert store.role_prompt("planner", config) == expected["planner"]
    private = tmp_path / "changed-prompts"
    shutil.copytree(prompts._PROMPTS_DIR, private)
    path = private / "revisable_executor.md"
    path.write_text(path.read_text(encoding="utf8") + "\nNew source version\n", encoding="utf8")
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", private)
    assert store.role_prompt("executor", config) == expected["executor"]
    assert store.role_prompt("reviewer", config) == expected["reviewer"]
    with pytest.raises(ValueError, match="profile changed"):
        store.role_prompt("planner", {**config, "mode": "delegated"})


def test_human_wire_stays_frozen_with_task_prompt(task, monkeypatch):
    import agent.interaction_policy as policies
    store, broker, task_id = task
    store.role_prompt("planner", broker.config(task_id))
    original = store.get("prompt_policy", "roles")["payload"]["wire"]
    changed = {**original, "description": "new source description", "tool_form": "new source form"}
    monkeypatch.setattr(policies, "interaction_profile", lambda **kwargs: policies.InteractionProfile("managed", changed))
    session = SimpleNamespace(role="planner", human_input=(broker, task_id), store=store)
    registry = AgentToolRegistry()
    register_human_tool(registry, session)
    tool = registry.catalog_for_role(AgentRole.PLANNER)[0]["function"]
    assert tool["description"] == original["description"]
    assert tool["parameters"] == original["schema"]
    profile = policies.session_interaction_profile(session, broker.config(task_id))
    assert profile.recovery_nudge == original["recovery_nudge"]
    assert profile.submission_hint == original["submission_hint"]


def test_rollback_preserves_later_edits_and_restores_dirty_baseline(tmp_path):
    import hashlib
    root, baseline = tmp_path / "workspace", tmp_path / "snapshot/baseline"
    root.mkdir()
    (baseline / "files").mkdir(parents=True)
    (root / "old.txt").write_bytes(b"original uncommitted work")
    (baseline / "files/old.txt").write_bytes(b"original uncommitted work")
    manifest = {"files": [
        {"path": "old.txt", "existed": True, "sha256": hashlib.sha256(b"original uncommitted work").hexdigest()},
        {"path": "new.txt", "existed": False, "sha256": None},
    ]}
    (baseline / "manifest.json").write_text(json.dumps(manifest), encoding="utf8")
    (root / "old.txt").write_bytes(b"candidate")
    (root / "new.txt").write_bytes(b"new candidate file")
    (root / "unrelated.txt").write_bytes(b"keep me")
    seal(baseline, root)
    candidate = json.loads((baseline.parent / "candidate.json").read_text())
    (root / "old.txt").write_bytes(b"later user edit")
    with pytest.raises(ValueError, match="Later edits"):
        restore(baseline, root, candidate=candidate)
    assert (root / "new.txt").exists()
    assert (root / "old.txt").read_bytes() == b"later user edit"
    (root / "old.txt").write_bytes(b"candidate")
    restore(baseline, root, candidate=candidate)
    assert (root / "old.txt").read_bytes() == b"original uncommitted work"
    assert not (root / "new.txt").exists()
    assert (root / "unrelated.txt").read_bytes() == b"keep me"


def test_rollback_rejects_path_escape_before_writes(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "manifest.json").write_text(json.dumps({"files": [
        {"path": "../outside.txt", "existed": False, "sha256": None}]}))
    with pytest.raises(ValueError, match="escapes"):
        inventory(baseline, tmp_path / "workspace")
