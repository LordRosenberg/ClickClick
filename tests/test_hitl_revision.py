"""Submission reservation and durable handoff between successive human calls."""

import json

import pytest

from agent.hitl import register_human_tool
from agent.prompts import render_role_system
from agent.tool_registry import AgentRole, AgentToolResult, AgentToolSpec, ToolCategory, ToolStatus
from shared.db import Database
from shared.hitl import HumanInputBroker, HumanInputSuspended, InputConflict
from tests.test_hitl import answer, broker, question, response, session


@pytest.mark.parametrize("enabled", [False, True])
async def test_reserved_rounds_deliver_stage_guidance_then_submit(broker, tmp_path, monkeypatch, enabled):
    value, task_id = broker
    agent, registry = session(value, task_id, tmp_path, rounds=8)
    registry = type(registry)()
    if not enabled:
        del agent.human_input
        agent.set_stable_system(render_role_system("planner"))
    else:
        register_human_tool(registry, agent)
    gathered, submissions, catalogs = [], [], []
    registry.register(AgentToolSpec(
        name="load_skill", roles=(AgentRole.PLANNER,), category=ToolCategory.KNOWLEDGE,
        description="Load guidance", parameters={"type": "object", "properties": {}}),
        lambda args, ctx: gathered.append(ctx.invocation_id) or AgentToolResult(
            status=ToolStatus.INVALID_ARGUMENTS, summary="Missing skill ID"))

    def submit(args, ctx):
        submissions.append(ctx.invocation_id)
        # A first valid submission may supply app-core/stage guidance and
        # require another submission. That round must survive failed gathering.
        if len(submissions) == 1:
            return AgentToolResult(summary="Read supplied stage guidance, then resubmit")
        return AgentToolResult(terminal_value="finished")

    registry.register(AgentToolSpec(
        name="submit_result", roles=(AgentRole.PLANNER,), category=ToolCategory.TERMINAL,
        description="Submit", parameters={"type": "object", "properties": {}}), submit)

    async def complete(*args, tools, **kwargs):
        names = {t["function"]["name"] for t in tools}
        catalogs.append(names)
        return response("load_skill" if "load_skill" in names else "submit_result", {})

    monkeypatch.setattr("agent.session.complete", complete)
    result = await agent.run([], tool_registry=registry, reserve_terminal_round=True)
    assert result.decision == "finished"
    assert len(gathered) == 6 and len(submissions) == 2
    assert catalogs[6] == ({"submit_result", "ask_user"} if enabled else {"submit_result"})
    assert catalogs[7] == {"submit_result"}
    assert len(result.llm_rounds) == 8
    assert len(set(gathered + submissions)) == 1


async def test_successive_questions_transfer_exact_answer_and_survive_restart(broker, tmp_path, monkeypatch):
    value, task_id = broker
    calls = 0

    async def complete(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response("ask_user", question(), "first-call")
        answers = {m["tool_call_id"]: json.loads(m["content"])["data"]["answers"]
                   for m in messages if m["role"] == "tool" and m.get("tool_call_id") in {"first-call", "second-call"}}
        assert answers["first-call"][0]["text"] == "Alice"
        if calls == 2:
            request = question()
            request["questions"][0]["question"] = "Who is the second recipient?"
            return response("ask_user", request, "second-call")
        assert answers["second-call"][0]["text"] == "Bob"
        assert len(answers) == 2
        return response("submit_result", {}, "final-call")

    monkeypatch.setattr("agent.session.complete", complete)
    agent, registry = session(value, task_id, tmp_path)
    with pytest.raises(HumanInputSuspended):
        await agent.run([], tool_registry=registry)
    first = value.current(task_id)
    reply = answer(first)
    value.reply(task_id, reply)
    reopened = Database(value.db.path)
    try:
        restored = HumanInputBroker(reopened)
        agent, registry = session(restored, task_id, tmp_path)
        with pytest.raises(HumanInputSuspended):
            await agent.run([], tool_registry=registry)
        second = restored.current(task_id)
        old = reopened.get_agent_record(task_id, "human_input", first["interaction_id"])["payload"]
        assert old["status"] == "consumed" and old["checkpoint"] is None
        assert old["answers"][0]["text"] == "Alice"
        assert second["status"] == "pending"
        assert second["invocation_id"] == first["invocation_id"]
        assert not restored.reply(task_id, reply)
        assert restored.current(task_id)["interaction_id"] == second["interaction_id"]
        restored.reply(task_id, answer(second, text="Bob", response_id="second-response"))
    finally:
        reopened.close()
    agent, registry = session(value, task_id, tmp_path)
    result = await agent.run([], tool_registry=registry)
    value.complete(task_id)
    rows = value.db.list_agent_records(task_id, "human_input")
    assert len(rows) == 2 and all(r["payload"]["status"] == "consumed" for r in rows)
    assert result.decision == "finished" and calls == 3
    assert {r.invocation_id for r in result.llm_rounds} == {first["invocation_id"]}


async def test_successor_persistence_failure_retains_prior_continuation(broker, tmp_path, monkeypatch):
    value, task_id = broker
    replies = iter([response("ask_user", question(), "first-call"), response("ask_user", {
        "questions": [{"id": "other", "question": "Which other recipient?"}]}, "second-call")])

    async def complete(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr("agent.session.complete", complete)
    agent, registry = session(value, task_id, tmp_path)
    with pytest.raises(HumanInputSuspended):
        await agent.run([], tool_registry=registry)
    first = value.current(task_id)
    value.reply(task_id, answer(first))
    original = value.db.append_agent_record

    def fail_new_pending(task, kind, key, payload):
        if kind == "human_input" and payload["status"] == "pending":
            raise RuntimeError("simulated database failure")
        return original(task, kind, key, payload)

    monkeypatch.setattr(value.db, "append_agent_record", fail_new_pending)
    agent, registry = session(value, task_id, tmp_path, rounds=2)
    # Registry turns ordinary handler exceptions into a failed tool result;
    # inspect durable state whether the enclosing session later exhausts limits.
    try:
        await agent.run([], tool_registry=registry)
    except (RuntimeError, StopIteration):
        pass
    prior = value.continuation(task_id, "planner")
    assert prior["interaction_id"] == first["interaction_id"]
    assert prior["checkpoint"] is not None and prior["status"] == "resolved"
    assert len(value.db.list_agent_records(task_id, "human_input")) == 1


@pytest.mark.parametrize("role,invocation,dialogue", [
    ("executor", "inv", [{"role": "tool", "tool_call_id": "call"}]),
    ("planner", "wrong", [{"role": "tool", "tool_call_id": "call"}]),
    ("planner", "inv", [{"role": "tool", "tool_call_id": "wrong"}]),
])
def test_successor_cannot_consume_unmatched_interaction(broker, role, invocation, dialogue):
    value, task_id = broker
    first = value.begin(task_id, question(), {"recoverable": True}, role="planner", invocation_id="inv", call_id="call")
    value.reply(task_id, answer(first))
    with pytest.raises(InputConflict, match="invalid_resumed_interaction"):
        value.begin(task_id, {"questions": [{"id": "other", "question": "Other?"}]},
                    {"dialogue": dialogue}, role=role, invocation_id=invocation,
                    call_id="next", resumed_interaction_id=first["interaction_id"])
    assert value.continuation(task_id, "planner")["checkpoint"] == {"recoverable": True}
