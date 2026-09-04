"""Gateway tool_calls normalization + tools forwarding."""

from __future__ import annotations

import json
import sys
import types

import pytest

from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall, complete
from shared.schemas import PlannerDecision


def _install_fake_litellm(monkeypatch, *, response):
    fake = types.ModuleType("litellm")
    calls: list[dict] = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        return response

    fake.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return calls


class _Fn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id: str, name: str, arguments: str) -> None:
        self.id = id
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, *, content: str = "", tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _Resp:
    def __init__(self, message, finish_reason: str = "tool_calls") -> None:
        self.choices = [_Choice(message, finish_reason)]
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1}


@pytest.mark.asyncio
async def test_complete_normalizes_tool_calls(monkeypatch):
    args = json.dumps({"skill_id": "generic.permission_dialogs"})
    raw = _Resp(_Msg(tool_calls=[_TC("call_1", "load_skill", args)]))
    calls = _install_fake_litellm(monkeypatch, response=raw)
    settings = Settings(gateway_max_retries=0, gateway_request_timeout=1.0)
    out = await complete(
        "gpt-test",
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "load_skill"}}],
        tool_choice="required",
        settings=settings,
    )
    assert out.stop_reason == "tool_calls"
    assert len(out.tool_calls) == 1
    assert out.tool_calls[0].name == "load_skill"
    assert "permission" in out.tool_calls[0].arguments
    assert calls and "tools" in calls[0]
    assert calls[0]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_response_format_still_works_without_tools(monkeypatch):
    payload = {
        "mode": "review",
        "review_requirement_ref": "final_ui_state:1",
    }
    raw = _Resp(_Msg(content=json.dumps(payload)), finish_reason="stop")
    calls = _install_fake_litellm(monkeypatch, response=raw)
    settings = Settings(gateway_max_retries=0, gateway_request_timeout=1.0)
    out = await complete(
        "gpt-test",
        [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision,
        settings=settings,
    )
    assert out.stop_reason == "stop"
    assert out.tool_calls == []
    assert out.parse_risk is False
    assert "tool_choice" not in calls[0]
    PlannerDecision.model_validate_json(out.content)
