"""Version-trigger boundaries and native Learner history routing."""

import json
from io import BytesIO
from types import coroutine

import pytest
from PIL import Image

from agent.revisable.recall import read_history
from agent.revisable.store import TaskStore
from agent.revisable.tools import ReadHistory, register_memory_tools
from agent.skills.library import parse_skill_markdown
from agent.skills.version_audit import (
    VERSION_AUDIT_KIND,
    VERSION_AUDIT_RUN_KIND,
    audit_task_versions,
)
from agent.tool_registry import AgentRole, AgentToolRegistry, ToolExecutionContext, stable_hash
from agent.traces import TraceWriter
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayError, GatewayResponse, ToolCall
from shared.schemas import TaskStatus


PACKAGE = "example.notes"
RULE = "In the file list, open Rename for the selected note."
SKILL = f"""---
name: notes-rename
description: Rename a saved note from the file list.
version: '1.0'
app: {PACKAGE}
kind: workflow
capability: rename_note
---
## Procedure
{RULE}

## Verification
The Rename dialog shows the selected note's name.
"""
SETTINGS_RULE = "Activate the confirmation icon to save the notification settings."
SETTINGS_SKILL = f"""---
name: notification-settings
description: Update notification settings.
version: '1.0'
app: {PACKAGE}
kind: workflow
capability: update_notifications
---
## Procedure
{SETTINGS_RULE}

## Verification
The screen confirms the notification settings were saved.
"""


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    db = Database(settings.db_path)
    yield db, artifacts, TraceWriter(db, artifacts), settings
    db.close()


def episode(runtime, code, *, name=None, profiles=(), paired=True, delivered=True, first_step=1,
            instruction="Rename the current note.", actions=None, skill=SKILL, images=None):
    db, artifacts, traces, _ = runtime
    task = db.create_task(instruction)
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    store = TaskStore(db, artifacts, task.id)
    task = db.get_task(task.id)
    pack = parse_skill_markdown(skill)
    projection = pack.section_for("executor")
    metadata = {"skill_id": pack.id, "version": pack.version, "scope": f"apps/{PACKAGE}",
                "content_hash": stable_hash(projection), "activation_source": "selected_workflow",
                "selected_device_profiles": list(profiles)}
    store.put("skill_snapshot", pack.id, {"frontmatter": pack.frontmatter, "body": pack.body,
              "relative_path": f"apps/{PACKAGE}/workflows/{pack.id}/SKILL.md", "version": pack.version})
    request_ref = artifacts.save_json("llm", {"kind": "agent_round_request", "role": "executor", "round_id": "round",
        "active_skills": [metadata], "messages": [{"role": "user", "content":
            f"[selected_workflow skill:{pack.id}@{pack.version}]\n{projection}"}] if delivered else []})
    response_ref = artifacts.save_json("llm", {"role": "executor", "round_id": "round"})
    actions = actions or [("tap_index", 7, "File list\n[7] Rename", "Rename dialog")]
    with db.transaction():
        for index, (action_type, target, before, after) in enumerate(actions):
            before_id, after_id = f"before-{index}", f"after-{index}"
            before_payload = {"app": PACKAGE, "text": before}
            after_payload = {"app": PACKAGE, "text": after}
            if images:
                for observation_id, payload in ((before_id, before_payload), (after_id, after_payload)):
                    image = images.get(observation_id)
                    if image is not None:
                        payload["image_ref"] = artifacts.save_bytes("history_images", image, suffix=".png")
            store.put("observation", before_id, before_payload)
            if paired:
                store.put("observation", after_id, after_payload)
            receipt = {"transaction_id": f"action-{index}", "dispatch_succeeded": True,
                       "observation_accepted": True, "observation_id": after_id}
            store.put("event", str(index), {"step": index, "observation_id": before_id, "submitted_action": {
                "type": action_type, "index": target, "text": "omit user text"},
                "action_result": {"success": True, "receipt": receipt}})
            traces.write(task.id, kind="executor_tick", step_seq=index + 1, payload={
                "app_id": PACKAGE, "basis_observation_id": before_id, "observation_id": before_id,
                "observation_accepted": True, "decision": "act", "active_skills": [metadata],
                "action": {"type": "tap" if action_type == "tap_index" else action_type},
                "action_result": {"receipt": receipt}, "agent_rounds": [{"role": "executor", "round_id": "round",
                    "request_ref": request_ref, "response_ref": response_ref}]})
    build = {PACKAGE: {"package": PACKAGE, "version_code": code, "version_name": name,
                       "first_step": first_step, "observation_id": "before-0"}}
    return task, store, build


def run(runtime, code, **episode_options):
    task, store, builds = episode(runtime, code, **episode_options)
    # The stub returns inline, so these tests need no provider or event-loop sockets.
    with pytest.raises(StopIteration):
        audit_task_versions(task, store, runtime[3], builds).send(None)
    return store


def stub_learner(monkeypatch, *, lost=False):
    requests = []

    async def learn(task, *, settings, library, store, backend, manual, version_changes, budget):
        assert not manual
        assert backend.driver is None and backend.source_store is store
        assert budget.max_actions == 0
        assert budget.max_calls == settings.learning_default_calls
        assert budget.max_seconds == settings.learning_default_seconds
        requests.append((version_changes, store))
        budget.meter("model_call_started", {})
        if lost:
            raise GatewayError("test transport loss", category="transient")
        return {"ok": True, "pending_id": "pending-proposal", "eligible": False, "reason": "candidate_staged"}

    monkeypatch.setattr("agent.skills.learning.run_task_learning", learn)
    return requests


def test_baseline_then_one_changed_build_enters_shared_learner(runtime, monkeypatch):
    requests = stub_learner(monkeypatch)
    baseline = run(runtime, "1").records(VERSION_AUDIT_KIND)[0]["payload"]
    assert baseline["status"] == "baseline" and baseline["window_sources"]
    assert run(runtime, "1", name="display-only rename").records(VERSION_AUDIT_KIND) == []
    audited = run(runtime, "2")
    record = audited.records(VERSION_AUDIT_KIND)[0]["payload"]
    assert record["status"] == "completed" and record["assessment"] == "unassessed"
    assert record["build"]["version_code"] == "2" and record["previous_build"]["version_code"] == "1"
    assert record["pending_id"] == "pending-proposal"
    changes, _ = requests[0]
    assert changes[0]["projection"] == parse_skill_markdown(SKILL).section_for("executor")
    assert changes[0]["evidence"] == ["event:0@1", "observation:after-0@1", "observation:before-0@1"]
    assert changes[0]["target"] == f"apps/{PACKAGE}/workflows/notes-rename/SKILL.md"
    learner_run = audited.records(VERSION_AUDIT_RUN_KIND)[0]["payload"]
    assert learner_run["pending_id"] == "pending-proposal" and not learner_run["eligible"]
    assert learner_run["allocated_calls"] == runtime[3].learning_default_calls
    assert learner_run["budget"]["calls"] == 1 and learner_run["budget"]["actions"] == 0
    assert run(runtime, "2").records(VERSION_AUDIT_KIND) == []
    assert len(requests) == 1


def test_missing_delivery_scope_or_paired_observation_does_not_dispatch(runtime, monkeypatch):
    requests = stub_learner(monkeypatch)
    assert run(runtime, "1", delivered=False).records(VERSION_AUDIT_KIND) == []
    assert run(runtime, "1", first_step=2).records(VERSION_AUDIT_KIND) == []
    assert run(runtime, "1").records(VERSION_AUDIT_KIND)[0]["payload"]["status"] == "baseline"
    scoped = run(runtime, "2", profiles=("tablet",)).records(VERSION_AUDIT_KIND)[0]["payload"]
    assert scoped["status"] == "baseline"
    no_pair = run(runtime, "2", paired=False).records(VERSION_AUDIT_KIND)[0]["payload"]
    assert no_pair["assessment"] == "unassessed" and no_pair["reason"] == "missing_paired_evidence"
    assert not requests
    run(runtime, "2")
    assert len(requests) == 1


def test_lost_result_keeps_single_persisted_claim_and_is_not_replayed(runtime, monkeypatch):
    requests = stub_learner(monkeypatch, lost=True)
    run(runtime, "1")
    pending = run(runtime, "2")
    record = pending.records(VERSION_AUDIT_KIND)[0]["payload"]
    assert record["status"] == "pending" and record["assessment"] == "unassessed"
    assert record["reason"] == "provider_result_unknown"
    assert pending.records(VERSION_AUDIT_RUN_KIND)[0]["payload"]["budget"]["calls"] == 1
    assert run(runtime, "2").records(VERSION_AUDIT_KIND) == []
    assert len(requests) == 1


def test_a_second_task_cannot_claim_a_new_build_while_learner_is_in_flight(runtime, monkeypatch):
    requests = []

    @coroutine
    def pause():
        yield

    async def learn(task, **kwargs):
        requests.append(task.id)
        await pause()

    monkeypatch.setattr("agent.skills.learning.run_task_learning", learn)
    run(runtime, "1")
    task, store, builds = episode(runtime, "2")
    audit = audit_task_versions(task, store, runtime[3], builds)
    try:
        audit.send(None)
        assert store.records(VERSION_AUDIT_KIND)[0]["payload"]["status"] == "pending"
        assert run(runtime, "2").records(VERSION_AUDIT_KIND) == []
        assert requests == [task.id]
    finally:
        audit.close()


def test_read_projection_binds_unlabeled_target_across_languages_and_frames(runtime, monkeypatch):
    requests = stub_learner(monkeypatch)
    run(runtime, "1", skill=SETTINGS_SKILL)
    before_target = '[4] ImageButton resource_id="apply_changes"'
    after_state = '[4] ImageButton resource_id="menu"\n[5] TextView raw_text="Cambios guardados"'
    run(runtime, "2", skill=SETTINGS_SKILL, instruction="通知の設定を保存してください", actions=[
        ("tap", 4, 'TextView raw_text="Avisos"\n' + before_target, after_state),
    ])
    _, scoped = requests[0]
    event, = scoped.records("event")
    assert event["payload"]["target_in_before"] == before_target
    assert "menu" not in event["payload"]["target_in_before"]
    assert "omit user text" not in str(event)
    assert scoped.get("observation", "after-0", 1)["payload"]["text"] == after_state


async def test_scoped_reads_require_exact_sources_and_redact_cross_app_and_raw_actions(runtime, monkeypatch):
    requests = stub_learner(monkeypatch)
    run(runtime, "1")
    task, source, builds = episode(runtime, "2", actions=[
        ("tap_xy", None, 'ImageButton id=save', 'TextView raw_text="Saved"'),
        ("tap", 2, '[2] TextView raw_text="Share"', 'PRIVATE_EXTERNAL_SCREEN'),
    ])
    first = source.get("event", "0")["payload"]
    source.put("event", "0", {**first, "submitted_action": {**first["submitted_action"], "x": 80, "y": 170}})
    source.put("observation", "after-1", {"app": "another.app", "text": "PRIVATE_EXTERNAL_SCREEN"})
    await audit_task_versions(task, source, runtime[3], builds)
    changes, scoped = requests[0]
    first, last = scoped.records("event")
    assert "x" not in first["payload"]["submitted_action"]
    assert last["payload"]["receipt_state"] == "left_app"
    assert "after-1" not in str(last)
    assert changes[0]["evidence"] == ["event:0@2", "observation:after-0@1", "observation:before-0@1"]

    registry = AgentToolRegistry()
    register_memory_tools(registry, "executor", scoped, task.state)
    context = ToolExecutionContext(AgentRole.EXECUTOR, "scope-test")
    result = await registry.execute("read_history", {"source": "event:0@2", "full": True}, context)
    details = json.loads(result.data["items"][0]["text"])
    assert details["submitted_action"] == {"type": "tap_xy", "x": 80, "y": 170}
    for args in ({"source": "observation:after-1@2"}, {"source": "after-1"}, {"source": "event:0@1"},
                 {"source": "event:0"}, {"source": "plan"}, {"query": "PRIVATE_EXTERNAL_SCREEN"}):
        denied = await registry.execute("read_history", args, context)
        assert denied.status.value == "failed" and "verified history" in denied.error


def test_selected_historical_images_stay_bound_to_scoped_observations(runtime, monkeypatch):
    requests = stub_learner(monkeypatch)
    run(runtime, "1")
    data = BytesIO()
    Image.new("RGB", (2, 2), "red").save(data, format="PNG")
    image = data.getvalue()
    run(runtime, "2", images={"before-0": image, "after-0": image})
    _, scoped = requests[0]
    context = ToolExecutionContext(AgentRole.EXECUTOR, "scope-test")
    page, attachments = read_history(scoped, ReadHistory(source="observation:before-0@1", view="image"), context, set())
    assert page["items"][0]["source"] == "observation:before-0@1"
    attachment, = attachments
    assert attachment.content == image and attachment.observation_id == "before-0"
    assert not attachment.actionable_coordinate_reference
    with pytest.raises(ValueError, match="verified history"):
        read_history(scoped, ReadHistory(source="observation:after-0@2", view="image"), context, set())


async def test_changed_build_reuses_adaptive_learner_reads(runtime, monkeypatch, tmp_path):
    skill_file = tmp_path / "skills" / "apps" / PACKAGE / "workflows" / "notification-settings" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(SETTINGS_SKILL, encoding="utf-8")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    run(runtime, "1", skill=SETTINGS_SKILL)
    calls = []
    desired = ["event:25@1", "observation:before-25@1", "observation:after-25@1"]

    async def complete(model, messages, *, tools, attempt_meter, **kwargs):
        attempt_meter("model_call_started", {})
        index = len(calls)
        calls.append(messages)
        if index > 0:
            delivered = [m.get("content", "") for m in messages if m["role"] == "tool"]
            assert any(desired[index - 1] in body for body in delivered)
            assert "omit user text" not in str(delivered)
        if index < len(desired):
            names = {tool["function"]["name"] for tool in tools}
            assert "read_history" in names and "tap" not in names and "write_note" not in names
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(
                id=f"read-{index}", name="read_history", arguments=json.dumps({"source": desired[index], "full": True}))])
        return GatewayResponse(model=model, content=json.dumps({"decision": "skip", "reason": "No grounded revision"}))

    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    actions = [("tap", 4, '[4] ImageButton id=apply', 'TextView raw_text="Changes saved"')] * 30
    task, store, builds = episode(runtime, "2", skill=SETTINGS_SKILL, actions=actions)
    await audit_task_versions(task, store, runtime[3], builds)
    run_record = store.records(VERSION_AUDIT_RUN_KIND)[0]["payload"]
    assert len(calls) == 4
    assert run_record["status"] == "completed" and run_record["skipped"]
    assert run_record["budget"]["calls"] == 4 and run_record["budget"]["actions"] == 0
