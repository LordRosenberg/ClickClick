"""Human-input boundaries: no effects while waiting, exact resume and ownership."""

import asyncio
import json
import hashlib
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.hitl import register_human_tool
from agent.prompts import render_role_system
from agent.session import AgentSession
from agent.tool_registry import AgentRole, AgentToolRegistry, AgentToolResult, AgentToolSpec, ToolCategory
from shared.config import Settings
from shared.db import Database
from shared.hitl import AskUser, HitlConfig, HumanInputBroker, HumanInputSuspended, InputConflict, Reply
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import TaskStatus


def response(name, args, call_id="original-call"):
    return GatewayResponse(content="", model="test", stop_reason="tool_calls",
                           tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(args))])


def question(*, purpose="clarification"):
    return {"purpose": purpose, "questions": [{"id": "recipient", "question": "Which recipient?",
            "options": [{"id": "a", "label": "Alice"}, {"id": "b", "label": "Bob"}]}]}


@pytest.fixture
def broker(tmp_path):
    db = Database(tmp_path / "test.db")
    task = db.create_task("Send the requested email", device_serial="phone")
    value = HumanInputBroker(db)
    value.configure(task.id, HitlConfig(enabled=True))
    yield value, task.id
    db.close()


def begin(broker, task_id, request=None):
    return broker.begin(task_id, request or question(), {}, role="planner", invocation_id="inv", call_id="call")


def answer(item, *, outcome="answered", text="Alice", response_id="response"):
    return Reply(interaction_id=item["interaction_id"], owner_version=item["owner_version"],
                 response_id=response_id, outcome=outcome,
                 answers=[{"question_id": "recipient", "text": text}] if outcome == "answered" else [])


@pytest.mark.parametrize("args", [
    {"questions": []}, {"questions": [{"id": "x", "question": "  "}]},
    {"questions": [{"id": "x", "question": "Q", "options": [{"id": "a", "label": "A"}]}]},
    {"questions": [{"id": "x", "question": "Q"}], "extra": True},
    {"questions": [{"id": "x", "question": "Q", "reason": None}]},
    {"questions": [{"id": "x", "question": "Q"}, {"id": "x", "question": "Other"}]},
])
def test_strict_questions(args):
    with pytest.raises(ValidationError):
        AskUser.model_validate(args)


@pytest.mark.parametrize("count", [2, 3])
async def test_batch_answers_resume_once_and_match_by_question_id(broker, tmp_path, monkeypatch, count):
    value, task_id = broker
    request = {"questions": [{"id": f"q{index}", "question": f"Missing user fact {index}?"} for index in range(count)]}
    expected = {f"q{index}": f"User answer {index}" for index in range(count)}
    calls = 0

    async def complete(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response("ask_user", request, "batch-call")
        results = [m for m in messages if m["role"] == "tool" and m.get("tool_call_id") == "batch-call"]
        assert len(results) == 1
        assert {a["question_id"]: a["text"] for a in json.loads(results[0]["content"])["data"]["answers"]} == expected
        return response("submit_result", {}, "final-call")

    monkeypatch.setattr("agent.session.complete", complete)
    agent, registry = session(value, task_id, tmp_path)
    with pytest.raises(HumanInputSuspended):
        await agent.run([], tool_registry=registry, reserve_terminal_round=True)
    pending = value.current(task_id)
    answers = [{"question_id": key, "text": text} for key, text in reversed(list(expected.items()))]

    def reply(answers, response_id):
        return Reply(interaction_id=pending["interaction_id"], owner_version=pending["owner_version"],
                     response_id=response_id, outcome="answered", answers=answers)

    for invalid in (answers[:-1], [{**answers[0], "question_id": "unknown"}, *answers[1:]], [answers[0]] * count):
        with pytest.raises(InputConflict, match="mapping"):
            value.reply(task_id, reply(invalid, "invalid"))
        assert value.current(task_id)["status"] == "pending"
        assert value.db.get_task(task_id).status == TaskStatus.WAITING_FOR_INPUT
        assert calls == 1
    accepted = reply(answers, "accepted")
    assert value.reply(task_id, accepted)
    assert not value.reply(task_id, accepted)
    resumed, registry = session(value, task_id, tmp_path)
    result = await resumed.run([], tool_registry=registry, reserve_terminal_round=True)
    assert result.decision == "finished"
    assert calls == 2
    assert len(value.db.list_agent_records(task_id, "human_input")) == 1
    assert {r.invocation_id for r in result.llm_rounds} == {pending["invocation_id"]}
    # The orchestrator closes the interaction after accepting the role result;
    # this focused session test checks the resumed provider dialogue itself.


def test_owner_takeover_retry_and_binding_loss(broker):
    value, task_id = broker
    binding = value.bind("assistant")
    value.db.set_local_preference("hitl_binding:" + binding, {"owner": "assistant", "expires_at": time.time() + 60, "subscribed": True})
    row = value.config(task_id)
    value.db.append_agent_record(task_id, "hitl_config", "snapshot", {**row, "origin": "assistant", "binding": binding})
    item = begin(value, task_id)
    assert item["owner"] == "assistant"
    assert not value.public(task_id)["can_answer"]
    assert value.public(task_id)["questions"] == []
    with pytest.raises(InputConflict):
        value.reply(task_id, answer(item))
    takeover = value.takeover(task_id)
    assert takeover["owner_version"] == 2
    with pytest.raises(InputConflict):
        value.reply(task_id, answer(item), owner="assistant", binding=binding)
    item = value.current(task_id)
    assert value.reply(task_id, answer(item))
    assert not value.reply(task_id, answer(item))
    with pytest.raises(InputConflict, match="response_id_conflict"):
        value.reply(task_id, answer(item, text="Bob"))
    assert value.db.get_task(task_id).status == TaskStatus.QUEUED
    assert value.db.busy_serials() == {"phone": task_id}


def test_console_origin_never_uses_unrelated_assistant(broker):
    value, task_id = broker
    value.bind("assistant")
    assert begin(value, task_id)["owner"] == "console"


def test_confirmations_are_explicit_and_decline_is_not_a_grant(broker):
    value, task_id = broker
    item = begin(value, task_id, question(purpose="confirmation"))
    with pytest.raises(InputConflict, match="mapping"):
        value.reply(task_id, answer(item))
    assert value.reply(task_id, answer(item, outcome="declined"))
    assert value.continuation(task_id, "planner")["outcome"] == "declined"
    assert "grant" not in value.current(task_id)


def test_restart_pending_answer_and_timeout(broker):
    value, task_id = broker
    item = begin(value, task_id)
    db_path = value.db.path
    reopened = Database(db_path)
    try:
        restored = HumanInputBroker(reopened)
        assert restored.current(task_id)["interaction_id"] == item["interaction_id"]
        assert reopened.get_task(task_id).status == TaskStatus.WAITING_FOR_INPUT
        assert reopened.busy_serials() == {"phone": task_id}
        item["deadline_at"] = time.time() - 1
        restored._save(item)
        assert restored.reconcile(task_id)
        assert restored.continuation(task_id, "planner")["outcome"] == "timeout"
        with pytest.raises(InputConflict):
            restored.reply(task_id, answer(item))
    finally:
        reopened.close()


def test_cancel_never_resumes(broker):
    value, task_id = broker
    item = begin(value, task_id)
    value.db.update_task(task_id, status=TaskStatus.CANCELLED)
    assert not value.reconcile(task_id)
    assert value.current(task_id)["status"] == "invalidated"
    with pytest.raises(InputConflict):
        value.reply(task_id, answer(item))


def test_disabled_prompts_have_no_human_policy_and_omitted_equals_false():
    for role in ("planner", "executor", "reviewer"):
        disabled = render_role_system(role)
        assert disabled == render_role_system(role, hitl_enabled=False)
        assert "ask_user" not in disabled
        assert "purpose=confirmation" not in disabled
        assert "{{" not in disabled
        enabled = render_role_system(role, hitl_enabled=True)
        assert "interactive user replies are unavailable" not in enabled
        assert ("purpose=confirmation" in enabled) == (role != "reviewer")


def session(broker, task_id, tmp_path, *, rounds=5):
    value = AgentSession("planner", "test", settings=Settings(_env_file=None), max_total_rounds=rounds)
    value.freeze_allow_dirs(["generic"])
    value.set_stable_system(render_role_system("planner", hitl_enabled=True,
                                             hitl_mode=broker.config(task_id)["mode"]))
    value.human_input = (broker, task_id)
    registry = AgentToolRegistry()
    register_human_tool(registry, value)
    registry.register(AgentToolSpec(name="submit_result", description="Finish", category=ToolCategory.TERMINAL,
                                   roles=(AgentRole.PLANNER,), parameters={"type": "object", "properties": {}}),
                      lambda args, ctx: AgentToolResult(terminal_value="finished"))
    return value, registry


async def test_delegated_recovery_submission_and_resume_use_flat_profile(broker, tmp_path, monkeypatch):
    value, task_id = broker
    value.db.append_agent_record(task_id, "hitl_config", "snapshot", {
        **value.config(task_id), "mode": "delegated",
    })
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages)
        wire = json.dumps(messages, ensure_ascii=False)
        assert "purpose=confirmation" not in wire
        if len(calls) == 1:
            return GatewayResponse(content="I need the recipient", model="test", stop_reason="stop")
        if len(calls) == 2:
            assert "one clarification question" in wire
            return response("ask_user", {"id": "recipient", "question": "Which recipient?"})
        results = [m for m in messages if m["role"] == "tool" and m.get("tool_call_id") == "original-call"]
        assert len(results) == 1 and "Alice" in results[0]["content"]
        return response("submit_result", {}, "final-call")
    monkeypatch.setattr("agent.session.complete", complete)
    first, registry = session(value, task_id, tmp_path, rounds=4)
    with pytest.raises(HumanInputSuspended):
        await first.run([], tool_registry=registry, reserve_terminal_round=True)
    pending = value.current(task_id)
    assert "human_recovery_nudge" in pending["checkpoint"]
    assert "purpose=confirmation" not in pending["checkpoint"]["human_submission_hint"]
    assert pending["owner"] == "delegated"
    value.reply(task_id, answer(pending), owner="delegated")
    resumed, registry = session(value, task_id, tmp_path, rounds=4)
    result = await resumed.run([], tool_registry=registry, reserve_terminal_round=True)
    assert result.decision == "finished" and len(calls) == 3


async def test_exact_session_resume_and_full_long_answer(broker, tmp_path, monkeypatch):
    value, task_id = broker
    seen = []
    async def complete(model, messages, **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return response("ask_user", question())
        assert "新鲜画面" in json.dumps(messages, ensure_ascii=False)
        answers = [m for m in messages if m["role"] == "tool" and m.get("tool_call_id") == "original-call"]
        assert len(answers) == 1
        assert json.loads(answers[0]["content"])["data"]["answers"][0]["text"] == "A" * 4096
        return response("submit_result", {}, "final-call")
    monkeypatch.setattr("agent.session.complete", complete)
    first, registry = session(value, task_id, tmp_path)
    with pytest.raises(HumanInputSuspended):
        await first.run([{"role": "user", "content": "old screen"}], tool_registry=registry, reserve_terminal_round=True)
    pending = value.current(task_id)
    old_invocation = pending["invocation_id"]
    assert pending["checkpoint"]["round_index"] == 1
    assert len(seen) == 1
    value.reply(task_id, answer(pending, text="A" * 4096))
    # A new Python session simulates worker release/restart. No repeated ask.
    resumed, registry = session(value, task_id, tmp_path)
    result = await resumed.run([{"role": "user", "content": "新鲜画面"}], tool_registry=registry, reserve_terminal_round=True)
    assert result.decision == "finished"
    assert len(result.llm_rounds) == 2
    assert {r.invocation_id for r in result.llm_rounds} == {old_invocation}
    assert result.tool_calls[0].call_id == "original-call"
    assert len(value.db.list_agent_records(task_id, "human_input")) == 1


async def test_human_cobatch_rejects_every_effect(broker, tmp_path, monkeypatch):
    value, task_id = broker
    agent, registry = session(value, task_id, tmp_path)
    effects = []
    registry.register(AgentToolSpec(name="write", description="Write", category=ToolCategory.KNOWLEDGE,
                                   roles=(AgentRole.PLANNER,), parameters={"type": "object"}),
                      lambda args, ctx: effects.append(True) or AgentToolResult())
    calls = 0
    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            message = response("ask_user", question())
            message.tool_calls.append(ToolCall(id="effect", name="write", arguments="{}"))
            return message
        return response("submit_result", {})
    monkeypatch.setattr("agent.session.complete", complete)
    result = await agent.run([], tool_registry=registry)
    assert not effects
    assert value.current(task_id) is None
    assert all(call.error == "human_call_must_be_alone" for call in result.tool_calls[:2])


async def test_final_round_rejects_ask_before_wait(broker, tmp_path, monkeypatch):
    value, task_id = broker
    agent, registry = session(value, task_id, tmp_path, rounds=1)
    async def complete(*args, **kwargs):
        return response("ask_user", question())
    monkeypatch.setattr("agent.session.complete", complete)
    from shared.llm_gateway import GatewayError
    with pytest.raises(GatewayError):
        await agent.run([], tool_registry=registry)
    assert value.current(task_id) is None


@pytest.mark.parametrize("asking_role", ["planner", "executor"])
async def test_real_runtime_wait_resume_refresh_and_shared_answers(tmp_path, monkeypatch, asking_role):
    import io
    from PIL import Image
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.artifacts import ArtifactStore
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(buffer, "PNG")
    monkeypatch.setattr("driver.fixture._blank_png", lambda: buffer.getvalue())
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="test")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver)
    task = db.create_task("Go home", device_serial="fixture")
    value = HumanInputBroker(db)
    value.configure(task.id, HitlConfig(enabled=True))
    received = False
    asked = False
    planned = False
    requests = []
    before_observation = None

    async def complete(model, messages, *, tools, attempt_meter, **kwargs):
        nonlocal asked, planned, before_observation
        attempt_meter("model_call_started", {"round": len(requests) + 1})
        requests.append(messages)
        names = {tool["function"]["name"] for tool in tools}
        role = "planner" if "submit_planner_decision" in names else "executor"
        assert "ask_user" in names
        if role == asking_role and not asked:
            asked = True
            if role == "executor":
                update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                              if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
                before_observation = update["current_observation_id"]
            return response("ask_user", question(), "pending-call")
        if received:
            encoded = json.dumps(messages, ensure_ascii=False)
            assert "Alice" in encoded
        if role == "planner":
            if planned and driver.actions:
                return response("submit_planner_decision", {"decision": "complete", "reason": "Home visible"})
            planned = True
            return response("submit_planner_decision", {"decision": "execute", "reason": "Go home", "plan": {"current_stage": {"goal": "Go home"}}})
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        observation = update["current_observation_id"]
        if received and asking_role == "executor":
            assert observation != before_observation
        if driver.actions:
            return response("submit_executor_step", {"decision": "finish", "summary": "Home visible", "observation_id": observation})
        return response("submit_executor_step", {"decision": "act", "summary": "Go home", "observation_id": observation, "action": {"type": "home"}})

    monkeypatch.setattr("agent.session.complete", complete)
    try:
        assert await runtime.run_task(task.id) == TaskStatus.WAITING_FOR_INPUT
        count = len(requests)
        assert not driver.actions
        assert db.get_task(task.id).step_number == 0
        assert await runtime.run_task(task.id) == TaskStatus.WAITING_FOR_INPUT
        assert len(requests) == count
        item = value.current(task.id)
        assert item["role"] == asking_role
        received = True
        value.reply(task.id, answer(item))
        # New orchestrator and role objects; the exact unfinished session is durable.
        resumed = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver)
        assert await resumed.run_task(task.id) == TaskStatus.SUCCEEDED
        assert len(driver.actions) == 1
        assert db.get_task(task.id).state.role_invocation_count == len(requests)
        assert value.current(task.id)["status"] == "consumed"
    finally:
        db.close()


def test_cancel_race_cannot_resurrect_task(broker):
    value, task_id = broker
    item = begin(value, task_id)
    value.db.update_task(task_id, status=TaskStatus.CANCELLED)
    with pytest.raises(InputConflict, match="not_waiting"):
        value.reply(task_id, answer(item))
    assert value.db.get_task(task_id).status == TaskStatus.CANCELLED


def test_query_budget_duplicate_options_and_secrets(tmp_path):
    settings = Settings(_env_file=None, models_json=json.dumps({"test/model": {"api_key": "CANARY_SECRET_123"}}))
    db = Database(tmp_path / "test.db")
    task = db.create_task("goal")
    value = HumanInputBroker(db, settings=settings)
    settings.models_json = json.dumps({"test/model": {"api_key": "UPDATED_CANARY_SECRET_456"}})
    assert value.sanitize("UPDATED_CANARY_SECRET_456") == "[REDACTED]"
    settings.models_json = json.dumps({"test/model": {"api_key": "CANARY_SECRET_123"}})
    value.configure(task.id, HitlConfig(enabled=True, max_queries=1))
    args = question()
    args["questions"][0]["reason"] = "Evidence CANARY_SECRET_123"
    item = value.begin(task.id, args, {"dialogue": "CANARY_SECRET_123"}, role="planner", invocation_id="i", call_id="c")
    assert "CANARY_SECRET_123" not in json.dumps(item)
    bad = answer(item)
    bad.answers[0].option_id = "b"
    with pytest.raises(InputConflict, match="option_label"):
        value.reply(task.id, bad)
    value.reply(task.id, answer(item, text="CANARY_SECRET_123"))
    assert value.current(task.id)["answers"][0]["text"] == "[REDACTED]"
    with pytest.raises(InputConflict, match="budget_exhausted"):
        begin(value, task.id)
    db.close()


async def test_no_remaining_task_provider_budget_does_not_ask(broker, tmp_path, monkeypatch):
    value, task_id = broker
    agent, registry = session(value, task_id, tmp_path)
    agent.human_provider_budget = lambda: 0
    calls = 0
    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        return response("ask_user", question()) if calls == 1 else response("submit_result", {})
    monkeypatch.setattr("agent.session.complete", complete)
    result = await agent.run([], tool_registry=registry)
    assert "task_provider_budget_exhausted" in result.tool_calls[0].result_summary
    assert value.current(task_id) is None


async def test_delegated_single_profile_yields_without_console_fallback(broker, tmp_path, monkeypatch):
    value, task_id = broker
    row = value.config(task_id)
    value.db.append_agent_record(task_id, "hitl_config", "snapshot", {**row, "mode": "delegated"})
    agent, registry = session(value, task_id, tmp_path)
    schema = registry.spec("ask_user", "planner").parameters
    assert "question" in schema["properties"] and "questions" not in schema["properties"]
    async def complete(*args, **kwargs):
        return response("ask_user", {"id": "recipient", "question": "Which recipient?"})
    monkeypatch.setattr("agent.session.complete", complete)
    with pytest.raises(HumanInputSuspended):
        await agent.run([], tool_registry=registry)
    item = value.current(task_id)
    assert item["owner"] == "delegated"
    assert not value.public(task_id)["can_answer"]
    with pytest.raises(InputConflict):
        value.takeover(task_id)
    assert value.reply(task_id, answer(item), owner="delegated")


async def test_enabled_submission_boundary_can_ask_and_then_submit(broker, tmp_path, monkeypatch):
    value, task_id = broker
    agent, registry = session(value, task_id, tmp_path, rounds=3)
    calls = 0
    observed_tools = []
    async def complete(*args, tools, **kwargs):
        nonlocal calls
        calls += 1
        observed_tools.append({tool["function"]["name"] for tool in tools})
        if calls == 1:
            return GatewayResponse(content="thinking", model="test", tool_calls=[])
        return response("ask_user", question()) if calls == 2 else response("submit_result", {})
    monkeypatch.setattr("agent.session.complete", complete)
    with pytest.raises(HumanInputSuspended):
        await agent.run([], tool_registry=registry, reserve_terminal_round=True)
    item = value.current(task_id)
    assert item["checkpoint"]["round_index"] == 2
    value.reply(task_id, answer(item))
    resumed, registry = session(value, task_id, tmp_path, rounds=3)
    result = await resumed.run([], tool_registry=registry, reserve_terminal_round=True)
    assert result.decision == "finished"
    assert "ask_user" in observed_tools[1]
    assert observed_tools[2] == {"submit_result"}
