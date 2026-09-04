"""Tests for the UI-independent Reviewer task-scope phase."""

from __future__ import annotations

import json

import pytest

from agent.reviewer import Reviewer
from agent.session import tools_for_role
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import AgentState


def _scope_arguments() -> dict:
    return {
        "must_happen": ["the requested search is performed in this task"],
        "final_ui_state": ["the first result is visible"],
        "answer": ["report the first result"],
        "disqualifying_clauses": ["the required search was skipped"],
    }


def _scope_response(arguments: dict, *, call_id: str = "scope") -> GatewayResponse:
    return GatewayResponse(
        content="",
        model="chatgpt/gpt-5.4",
        stop_reason="tool_calls",
        tool_calls=[ToolCall(
            id=call_id,
            name="submit_reviewer_scope",
            arguments=json.dumps(arguments),
        )],
    )


def test_task_scope_tool_is_the_complete_task_contract_schema():
    tools = tools_for_role("reviewer", reviewer_protocol="scope")
    assert len(tools) == 1
    tool = tools[0]
    function = tool["function"]
    schema = function["parameters"]

    assert function["name"] == "submit_reviewer_scope"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "must_happen",
        "final_ui_state",
        "answer",
        "disqualifying_clauses",
    }
    assert "subgoal_kind" not in json.dumps(schema)
    assert "answer" in json.dumps(schema)


@pytest.mark.asyncio
async def test_scope_request_contains_only_policy_task_and_scope_tool(monkeypatch):
    captured: dict = {}
    instruction = "search Q, then report the first result"

    async def fake_complete(model, messages, **kwargs):
        captured.update({
            "model": model,
            "messages": messages,
            "kwargs": kwargs,
        })
        return _scope_response(_scope_arguments())

    monkeypatch.setattr("agent.session.complete", fake_complete)
    reviewer = Reviewer(model="obsolete-model", settings=Settings())
    reviewer.set_model("chatgpt/gpt-5.4")
    contract, refs = await reviewer.author_task_scope(
        AgentState(instruction=instruction),
    )

    assert captured["model"] == "chatgpt/gpt-5.4"
    assert captured["kwargs"]["tools"] == tools_for_role(
        "reviewer", reviewer_protocol="scope",
    )
    wire = json.dumps(captured["messages"], ensure_ascii=False).lower()
    assert instruction.lower() in wire
    assert "image_url" not in wire
    assert "accessibility" not in wire
    assert "foreground_app" not in wire
    assert "task action audit" not in wire
    assert "skill:" not in wire
    tool_names = {
        item["function"]["name"] for item in captured["kwargs"]["tools"]
    }
    assert tool_names == {"submit_reviewer_scope"}
    assert len(refs["agent_rounds"]) == 1
    assert refs["agent_rounds"][0]["role"] == "reviewer"
    assert refs["agent_rounds"][0]["model"] == "chatgpt/gpt-5.4"
    assert refs["agent_rounds"][0]["image_count"] == 0
    assert contract.must_happen
    assert contract.answer

    policy = " ".join(captured["messages"][0]["content"].lower().split())
    assert "matching inherited ui cannot prove" in policy
    assert "must_happen" in policy
    assert "final_ui_state" in policy
    assert "answer" in policy
    assert "do not add `must_happen` merely because" in policy
    assert "empty `must_happen`" in policy
    assert "producing operation plus a question about its result" in policy
    assert "each explicit prohibition or forbidden side effect" in policy
    assert "preserve exact literals, order, and temporal strength" in policy


def test_scope_policy_distinguishes_reuse_allowed_state_from_required_occurrence():
    from agent.prompts import render_reviewer_scope_system

    policy = " ".join(render_reviewer_scope_system().lower().split())
    assert "`must_happen`: task-local operations" in policy
    assert "`final_ui_state`: facts that must be true on the final screen" in policy
    assert "`answer`: questions the user asked to be told" in policy
    assert "ordinary open, enter, go to, or stay wording describes this destination" in policy
    assert "matching inherited ui cannot prove" in policy
    assert "empty `must_happen`" in policy
    assert "visible text from the later step is not the earlier obtainment" in policy
    assert "preserve exact literals, order, and temporal strength" in policy


@pytest.mark.asyncio
async def test_scope_does_not_mutate_normal_reviewer_skill_lifecycle(monkeypatch):
    async def fake_complete(model, messages, **kwargs):
        del model, messages, kwargs
        return _scope_response(_scope_arguments())

    monkeypatch.setattr("agent.session.complete", fake_complete)
    reviewer = Reviewer(model="chatgpt/gpt-5.4", settings=Settings())
    normal_session = reviewer._runner.session  # noqa: SLF001
    normal_session.reset_lifecycle("task:existing")
    normal_session.freeze_allow_dirs(["generic"])
    normal_session.set_foreground_app("com.xingin.xhs")
    assert normal_session.loaded_skill_ids
    before = {
        "lifecycle": normal_session._lifecycle_key,  # noqa: SLF001
        "foreground": normal_session._foreground_app,  # noqa: SLF001
        "dirs": normal_session.frozen_allow_dirs,
        "skills": normal_session.loaded_skill_ids,
    }

    await reviewer.author_task_scope(
        AgentState(instruction="perform and report"),
        task_id="new-task",
    )

    assert reviewer._runner.session is normal_session  # noqa: SLF001
    assert {
        "lifecycle": normal_session._lifecycle_key,  # noqa: SLF001
        "foreground": normal_session._foreground_app,  # noqa: SLF001
        "dirs": normal_session.frozen_allow_dirs,
        "skills": normal_session.loaded_skill_ids,
    } == before


@pytest.mark.asyncio
async def test_scope_retries_one_invalid_contract_in_same_text_only_phase(monkeypatch):
    seen: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        del model, kwargs
        seen.append(list(messages))
        if len(seen) == 1:
            return _scope_response({}, call_id="invalid")
        return _scope_response(_scope_arguments(), call_id="corrected")

    monkeypatch.setattr("agent.session.complete", fake_complete)
    reviewer = Reviewer(model="chatgpt/gpt-5.4", settings=Settings())
    contract, refs = await reviewer.author_task_scope(
        AgentState(instruction="perform and report"),
    )

    assert len(seen) == 2
    assert [call["status"] for call in refs["tool_calls"]] == [
        "invalid_arguments",
        "succeeded",
    ]
    assert contract.must_happen
    correction = json.dumps(seen[1], ensure_ascii=False)
    assert "invalid_arguments" in correction
    assert all("image_url" not in json.dumps(round_messages) for round_messages in seen)
