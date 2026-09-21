"""Per-model tool selection preserves validated Agent submission semantics."""
import json

import pytest

from agent.session import AgentSession
from agent.skills.library import SkillLibrary
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall


def test_unspecified_model_tool_choice_remains_required():
    settings = Settings(_env_file=None, models_json="{}")
    assert settings.tool_choice_for("chatgpt/gpt-5.6-sol") == "required"


@pytest.mark.parametrize("invalid", [None, False, "none", "AUTO", "", [], {"type": "function"}])
@pytest.mark.asyncio
async def test_invalid_tool_choice_is_rejected_before_model_call(invalid, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, models_json=json.dumps({"m": {"tool_choice": invalid}}))
    session = AgentSession("executor", "m", library=SkillLibrary(tmp_path), settings=settings)
    session.set_stable_system("Submit a valid decision using the tool.")

    async def unexpected_call(*args, **kwargs):
        pytest.fail("Invalid configuration must not issue a model request")

    monkeypatch.setattr("agent.session.complete", unexpected_call)
    with pytest.raises(ValueError, match="Model tool_choice"):
        await session.run([{"role": "user", "content": "Wait briefly."}])


@pytest.mark.asyncio
async def test_auto_is_model_scoped_and_text_does_not_complete_task(tmp_path, monkeypatch):
    choices = {
        "openai/deepseek-v4.1-flash-expires-on-0910": {"tool_choice": "auto"},
        "chatgpt/gpt-5.6-sol": {},
        "openai/gpt-configured": {"tool_choice": "required"},
    }
    settings = Settings(_env_file=None, models_json=json.dumps(choices))
    seen = {model: [] for model in choices}

    async def fake_complete(model, messages, **kwargs):
        seen[model].append(kwargs["tool_choice"])
        if len(seen[model]) == 1:
            return GatewayResponse(content="Done.", model=model, stop_reason="stop")
        return GatewayResponse(content="", model=model, stop_reason="tool_calls", tool_calls=[
            ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps({
                "decision": "act", "summary": "Wait briefly.",
                "action": {"type": "sleep", "duration_ms": 1},
            })),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    for model in choices:
        session = AgentSession("executor", model, library=SkillLibrary(tmp_path), settings=settings)
        session.set_stable_system("Submit a valid decision using the tool.")
        result = await session.run([{"role": "user", "content": "Wait briefly."}])
        expected = "auto" if "deepseek" in model else "required"
        assert seen[model] == [expected, expected]
        assert result.decision.action.type == "sleep"
        assert len(result.llm_rounds) == 2
        assert [r["tool_choice"] for r in result.request_snapshot["rounds"]] == [expected] * 2
