"""LLM gateway unit tests: provider-agnostic, no real network.

Covers each exception category, structured-output success, and malformed
output surfacing. Uses a fake `litellm.acompletion` so no real provider is
contacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any

import pytest

from shared.config import Settings
from shared.llm_gateway import (
    GatewayAuthError,
    GatewayBudgetError,
    GatewayConfigError,
    GatewayError,
    GatewayInputSafetyError,
    GatewayResponse,
    GatewayTransientError,
    GatewayQuotaError,
    complete,
    ReasoningConfig,
)
from shared.revisable import PlannerDecision


# ---------------------------------------------------------------------------
# Fake litellm injection
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self.message = _FakeMessage(content)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, content: str, finish_reason: str = "stop", usage: dict | None = None) -> None:
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5}


class _FakeStream:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks

    def __aiter__(self):
        async def iterate():
            for chunk in self.chunks:
                yield chunk
        return iterate()


class _FakeExc(Exception):
    """Custom exception with a status_code attribute for classification tests."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _install_fake_litellm(monkeypatch, *, responses=None, exc=None):
    """Install a fake `litellm` module whose `acompletion` yields scripted responses."""
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        if exc is not None:
            raise exc
        if callable(responses):
            return responses()
        if responses is None:
            return _FakeResponse("{}")
        return responses

    fake.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return calls


def _settings(**kw) -> Settings:
    return Settings(**kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_output_success_validates(monkeypatch):
    payload = {
        "decision": "review",
        "reason": "Inspect the result",
    }
    _install_fake_litellm(monkeypatch, responses=_FakeResponse(json.dumps(payload)))
    resp = await complete(
        "kimi-k3", [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision, settings=_settings(),
    )
    assert isinstance(resp, GatewayResponse)
    assert resp.parse_risk is False
    assert resp.stop_reason == "stop"
    assert resp.content == json.dumps(payload)


@pytest.mark.asyncio
async def test_malformed_output_surfaces_parse_risk(monkeypatch):
    # Syntactically valid JSON but wrong types → parse_risk=True.
    _install_fake_litellm(monkeypatch, responses=_FakeResponse('{"thought": "x", "plan": "not-a-list", "done": "yes"}'))
    resp = await complete(
        "kimi-k3", [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision, settings=_settings(),
    )
    assert resp.parse_risk is True
    # The raw content is preserved so the caller can apply tolerant fallback.
    assert "thought" in resp.content


@pytest.mark.asyncio
async def test_non_json_content_surfaces_parse_risk(monkeypatch):
    _install_fake_litellm(monkeypatch, responses=_FakeResponse("not json at all"))
    resp = await complete(
        "kimi-k3", [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision, settings=_settings(),
    )
    assert resp.parse_risk is True
    assert resp.content == "not json at all"


@pytest.mark.asyncio
async def test_transient_error_retries_with_backoff(monkeypatch):
    monkeypatch.setattr("shared.llm_gateway.GATEWAY_RETRY_BASE_DELAY_S", 0.001)
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("boom 503", status_code=503),
    )
    metered: list[int] = []
    with pytest.raises(GatewayTransientError) as ei:
        await complete(
            "kimi-k3",
            [{"role": "user", "content": "hi"}],
            settings=_settings(),
            attempt_meter=lambda _kind, payload: metered.append(payload["attempt"]),
        )
    assert ei.value.category == "transient"
    # Initial attempt + the fixed three retries.
    assert len(calls) == 4
    assert metered == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_complete_respects_per_call_retry_cap_and_output_cap(monkeypatch):
    monkeypatch.setattr("shared.llm_gateway.GATEWAY_RETRY_BASE_DELAY_S", 0.001)
    settings = _settings(
        models_json=json.dumps({
            "kimi-k3": {
                "provider": "kimi",
                "base_url": "https://kimi.example/v1",
                "api_key": "sk-abc",
                "max_tokens": 1024,
            }
        })
    )
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("boom 503", status_code=503),
    )

    with pytest.raises(GatewayTransientError):
        await complete(
            "kimi-k3",
            [{"role": "user", "content": "hi"}],
            settings=settings,
            max_retries=0,
            max_output_tokens=256,
        )

    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 256


@pytest.mark.asyncio
async def test_attempt_meter_can_stop_before_a_transport_retry(monkeypatch):
    monkeypatch.setattr("shared.llm_gateway.GATEWAY_RETRY_BASE_DELAY_S", 0.001)
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("boom 503", status_code=503),
    )

    def meter(_kind, payload):
        if payload["attempt"] == 2:
            raise RuntimeError("model fuse exhausted")

    with pytest.raises(RuntimeError, match="model fuse exhausted"):
        await complete(
            "kimi-k3",
            [{"role": "user", "content": "hi"}],
            settings=_settings(),
            attempt_meter=meter,
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_input_image_safety_error_is_typed_and_not_retried(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc(
            "input new_sensitive, messages[2]'s content[3] image is sensitive (1026)",
            status_code=500,
        ),
    )
    with pytest.raises(GatewayInputSafetyError) as ei:
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert ei.value.category == "content_safety"
    assert ei.value.offending_locations == [(2, 3)]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_429_is_transient(monkeypatch):
    monkeypatch.setattr("shared.llm_gateway.GATEWAY_RETRY_BASE_DELAY_S", 0.001)
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("rate limited", status_code=429),
    )
    with pytest.raises(GatewayTransientError):
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('status,message,body', [
    (429, 'The usage limit has been reached', None),
    (429, 'request rejected', {'error': {'type': 'usage_limit_reached', 'resets_at': 1234}}),
    (402, 'insufficient credits', None),
    (429, 'insufficient_quota', None),
    (None, "You've hit your usage limit", None),
])
async def test_quota_exhaustion_is_typed_and_never_retried(monkeypatch, status, message, body):
    exc = _FakeExc(message, status_code=status)
    exc.body = body
    calls = _install_fake_litellm(monkeypatch, exc=exc)
    with pytest.raises(GatewayQuotaError) as caught:
        await complete('kimi-k3', [{'role': 'user', 'content': 'hi'}], settings=_settings())
    assert caught.value.category == 'quota'
    assert caught.value.transport_attempts == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_auth_error_not_retried(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("unauthorized", status_code=401),
    )
    with pytest.raises(GatewayAuthError) as ei:
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert ei.value.category == "auth"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_forbidden_treated_as_auth(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc("forbidden", status_code=403),
    )
    with pytest.raises(GatewayAuthError):
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_budget_length_surfaces_distinctly(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        responses=_FakeResponse("partial", finish_reason="length"),
    )
    resp = await complete(
        "kimi-k3", [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision, settings=_settings(),
    )
    assert resp.stop_reason == "length"
    # Only one call — length exhaustion is NOT retried.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_400_is_config_and_not_retried(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        exc=_FakeExc(
            "Thinking mode does not support this tool_choice",
            status_code=400,
        ),
    )
    with pytest.raises(GatewayConfigError) as ei:
        await complete(
            "openai/deepseek-v4-flash-vision-exp",
            [{"role": "user", "content": "hi"}],
            settings=_settings(),
        )
    assert ei.value.category == "config"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_model_extra_body_is_forwarded_without_model_name_heuristics(monkeypatch):
    settings = _settings(
        models_json=json.dumps({
            "openai/provider-model": {
                "provider": "openai",
                "base_url": "https://example.invalid/v1",
                "api_key": "sk-test",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        }),
    )
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "openai/provider-model",
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "submit_contract"}}],
        tool_choice="required",
        settings=settings,
    )
    assert calls[0]["tool_choice"] == "required"
    assert calls[0]["extra_body"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_model_without_extra_body_gets_no_provider_specific_request_fields(monkeypatch):
    settings = _settings(
        models_json=json.dumps({
            "openai/provider-model": {
                "provider": "openai",
                "base_url": "https://example.invalid/v1",
                "api_key": "sk-test",
            },
        }),
    )
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "openai/provider-model",
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "submit_contract"}}],
        tool_choice="required",
        settings=settings,
    )
    assert "extra_body" not in calls[0]
    assert "allowed_openai_params" not in calls[0]


@pytest.mark.asyncio
async def test_relay_reasoning_override_is_explicit_and_model_scoped(monkeypatch):
    calls = _install_fake_litellm(monkeypatch)
    model = "openai/deepseek-v4.1-flash-expires-on-0910"
    settings = _settings(models_json=json.dumps({
        model: {
            "provider": "openai",
            "reasoning": {"effort": "high"},
            "extra_body": {"thinking": {"type": "enabled"}},
            "allowed_openai_params": ["reasoning_effort"],
        },
    }))
    await complete(model, [{"role": "user", "content": "test"}], settings=settings)
    assert calls[0]["allowed_openai_params"] == ["reasoning_effort"]
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "drop_params" not in calls[0]
    await complete("openai/other", [{"role": "user", "content": "test"}], settings=settings)
    assert "allowed_openai_params" not in calls[1]


@pytest.mark.asyncio
async def test_config_error_not_retried(monkeypatch):
    calls = _install_fake_litellm(
        monkeypatch,
        exc=ValueError("invalid argument value"),
    )
    with pytest.raises(GatewayConfigError):
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_resolution_from_settings(monkeypatch):
    settings = _settings(
        models_json=json.dumps({
            "kimi-k3": {"provider": "kimi", "base_url": "https://kimi.example/v1", "api_key": "sk-abc", "max_tokens": 1024}
        })
    )
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=settings)
    kw = calls[0]
    assert kw["model"] == "kimi-k3"
    assert kw["api_key"] == "sk-abc"
    assert kw["api_base"] == "https://kimi.example/v1"
    assert kw["max_tokens"] == 1024
    assert kw["timeout"] == 60.0


@pytest.mark.asyncio
async def test_no_provider_config_uses_litellm_defaults(monkeypatch):
    settings = _settings()  # no models_json
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=settings)
    kw = calls[0]
    assert kw["model"] == "kimi-k3"
    assert "api_key" not in kw
    assert "api_base" not in kw
    assert "max_tokens" not in kw


@pytest.mark.asyncio
async def test_response_format_translated_to_json_schema(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "kimi-k3", [{"role": "user", "content": "hi"}],
        response_format=PlannerDecision, settings=_settings(),
    )
    rf = calls[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "PlannerDecision"
    assert "properties" in rf["json_schema"]["schema"]


@pytest.mark.asyncio
async def test_common_error_type_does_not_leak_provider_shapes(monkeypatch):
    """All gateway exceptions are GatewayError subclasses; no provider type leaks."""
    _install_fake_litellm(monkeypatch, exc=_FakeExc("boom", status_code=500))
    with pytest.raises(GatewayError) as ei:
        await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert isinstance(ei.value, GatewayError)
    assert not isinstance(ei.value, _FakeExc)


@pytest.mark.asyncio
async def test_transient_retry_succeeds_after_failure(monkeypatch):
    """A transient error on attempt 1 followed by success on attempt 2 returns a response."""
    state = {"n": 0}

    async def acompletion(**kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise _FakeExc("transient", status_code=503)
        return _FakeResponse("{}")

    fake = types.ModuleType("litellm")
    fake.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)

    resp = await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    assert state["n"] == 2
    assert resp.content == "{}"


# ---------------------------------------------------------------------------
# D12: prefix-cache control (cache_system_prefix)
# ---------------------------------------------------------------------------


def _anthropic_settings() -> Settings:
    return _settings(
        models_json=json.dumps({
            "claude-test": {"provider": "anthropic", "api_key": "sk-ant", "max_tokens": 1024}
        })
    )


def _system_msg(calls):
    for m in calls[0]["messages"]:
        if m["role"] == "system":
            return m
    raise AssertionError("no system message in call")


@pytest.mark.asyncio
async def test_cache_system_prefix_attaches_cache_control_on_anthropic(monkeypatch):
    """Anthropic-routed model + cache_system_prefix=True → system message carries
    an ephemeral cache_control marker on a content-block list."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "claude-test",
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
        cache_system_prefix=True,
        settings=_anthropic_settings(),
    )
    sm = _system_msg(calls)
    assert isinstance(sm["content"], list)
    block = sm["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "SYS"
    assert block["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_system_prefix_noop_on_non_anthropic(monkeypatch):
    """Non-Anthropic model + cache_system_prefix=True → system message stays a
    plain string (no provider-specific cache marker; relies on automatic
    prefix caching)."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "kimi-k3",
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
        cache_system_prefix=True,
        settings=_settings(),
    )
    sm = _system_msg(calls)
    assert isinstance(sm["content"], str)
    assert sm["content"] == "SYS"


@pytest.mark.asyncio
async def test_cache_system_prefix_false_attaches_no_marker_even_on_anthropic(monkeypatch):
    """cache_system_prefix=False (default) → no cache_control even on Anthropic."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "claude-test",
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
        settings=_anthropic_settings(),  # cache_system_prefix defaults to False
    )
    sm = _system_msg(calls)
    assert isinstance(sm["content"], str)
    assert sm["content"] == "SYS"


@pytest.mark.asyncio
async def test_cache_system_prefix_anthropic_detected_by_model_id_heuristic(monkeypatch):
    """When no provider config is present, a `claude*` model id is treated as
    Anthropic and gets the cache_control marker."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "claude-3-5-sonnet",
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
        cache_system_prefix=True,
        settings=_settings(),  # no models_json → heuristic fallback
    )
    sm = _system_msg(calls)
    assert isinstance(sm["content"], list)
    assert sm["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_cache_skill_index_marks_last_k_v2_user_message(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "claude-test",
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "[generic skill: generic.p]\nP"},
            {"role": "user", "content": "[foreground_app_workflow skill: app.w]\nW"},
            {"role": "user", "content": "OBS"},
        ],
        cache_system_prefix=True,
        cache_skill_index=True,
        settings=_anthropic_settings(),
    )
    msgs = calls[0]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[1]["content"] == "[generic skill: generic.p]\nP"
    assert msgs[2]["role"] == "user"
    assert isinstance(msgs[2]["content"], list)
    assert msgs[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[3]["content"] == "OBS"


@pytest.mark.asyncio
async def test_cache_skill_index_noop_on_non_anthropic(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete(
        "kimi-k3",
        [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "SKILL INDEX\nx\n"},
        ],
        cache_system_prefix=True,
        cache_skill_index=True,
        settings=_settings(),
    )
    msgs = calls[0]["messages"]
    assert isinstance(msgs[0]["content"], str)
    assert isinstance(msgs[1]["content"], str)


# ---------------------------------------------------------------------------
# User-Agent header injection
# ---------------------------------------------------------------------------


from shared.llm_gateway import DEFAULT_GATEWAY_USER_AGENT  # noqa: E402


@pytest.mark.asyncio
async def test_default_user_agent_applied_when_setting_empty(monkeypatch):
    """Empty gateway_user_agent → built-in default UA on every request."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    extra = calls[0]["extra_headers"]
    assert extra["User-Agent"] == DEFAULT_GATEWAY_USER_AGENT


@pytest.mark.asyncio
async def test_custom_user_agent_applied_when_configured(monkeypatch):
    """Non-empty gateway_user_agent → that exact string overrides the default."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    settings = _settings(gateway_user_agent="MyAgent/1.0 (custom)")
    await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=settings)
    assert calls[0]["extra_headers"]["User-Agent"] == "MyAgent/1.0 (custom)"


@pytest.mark.asyncio
async def test_extra_headers_does_not_touch_auth_or_content_type(monkeypatch):
    """We only inject User-Agent; provider-managed headers stay untouched."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    await complete("kimi-k3", [{"role": "user", "content": "hi"}], settings=_settings())
    extra = calls[0]["extra_headers"]
    assert set(extra.keys()) == {"User-Agent"}
    assert "Authorization" not in extra
    assert "Content-Type" not in extra


@pytest.mark.asyncio
async def test_user_agent_injected_provider_agnostically(monkeypatch):
    """Same injection path for an OpenAI-compatible relay and an Anthropic model."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    relay_settings = _settings(
        models_json=json.dumps({
            "openai/gpt-4o": {"provider": "openai", "base_url": "https://relay/v1", "api_key": "sk-x", "max_tokens": 1024}
        })
    )
    await complete("openai/gpt-4o", [{"role": "user", "content": "hi"}], settings=relay_settings)
    assert calls[0]["extra_headers"]["User-Agent"] == DEFAULT_GATEWAY_USER_AGENT

    await complete(
        "claude-3-5-sonnet",
        [{"role": "user", "content": "hi"}],
        settings=_settings(),
    )
    assert calls[1]["extra_headers"]["User-Agent"] == DEFAULT_GATEWAY_USER_AGENT


@pytest.mark.asyncio
async def test_reasoning_controls_and_summary_are_normalized(monkeypatch):
    response = {
        "choices": [{
            "message": {"content": "", "reasoning_summary": "checked evidence"},
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    calls = _install_fake_litellm(monkeypatch, responses=response)
    settings = _settings(models_json=json.dumps({
        "reasoner": {"reasoning_supported": True},
    }))
    result = await complete(
        "reasoner", [{"role": "user", "content": "hi"}],
        reasoning=ReasoningConfig(effort="high", summary="concise"),
        settings=settings,
    )
    assert calls[0]["reasoning_effort"] == "high"
    assert calls[0]["reasoning"] == {"summary": "concise"}
    assert result.reasoning.status == "supported"
    assert result.reasoning.summary == "checked evidence"


@pytest.mark.asyncio
async def test_unsupported_reasoning_is_recorded_without_provider_kwargs(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    result = await complete(
        "plain", [{"role": "user", "content": "hi"}],
        reasoning=ReasoningConfig(effort="high", summary="concise"),
        settings=_settings(),
    )
    assert "reasoning_effort" not in calls[0]
    assert "reasoning" not in calls[0]
    assert result.reasoning.status == "unsupported"
    assert result.reasoning.summary is None


@pytest.mark.asyncio
async def test_reasoning_content_promoted_when_summary_requested(monkeypatch):
    """LiteLLM normalizes Responses-API reasoning summaries into
    `message.reasoning_content`; when the caller explicitly requested a
    summary, that field carries the summary text and must be extracted —
    otherwise the Console reasoning diagnostic can never show anything."""
    response = {
        "choices": [{
            "message": {"content": "", "reasoning_content": "checked the evidence"},
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    _install_fake_litellm(monkeypatch, responses=response)
    settings = _settings(models_json=json.dumps({
        "reasoner": {"reasoning_supported": True},
    }))
    result = await complete(
        "reasoner", [{"role": "user", "content": "hi"}],
        reasoning=ReasoningConfig(summary="concise"), settings=settings,
    )
    assert result.reasoning.summary == "checked the evidence"


@pytest.mark.asyncio
async def test_reasoning_content_ignored_without_summary_request(monkeypatch):
    """Without an explicit summary preference, `reasoning_content` may hold a
    relay's private chain-of-thought — it must not leak into traces."""
    response = {
        "choices": [{
            "message": {"content": "", "reasoning_content": "private chain"},
            "finish_reason": "stop",
        }],
        "usage": {},
    }
    _install_fake_litellm(monkeypatch, responses=response)
    settings = _settings(models_json=json.dumps({
        "reasoner": {"reasoning_supported": True},
    }))
    result = await complete(
        "reasoner", [{"role": "user", "content": "hi"}],
        reasoning=ReasoningConfig(effort="high"), settings=settings,
    )
    assert result.reasoning.summary is None


# ---------------------------------------------------------------------------
# ChatGPT subscription special case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chatgpt_route_omits_ua_max_tokens_and_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    token_dir = tmp_path / "cc-chatgpt"
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    settings = _settings(
        chatgpt_token_dir=str(token_dir),
        models_json=json.dumps({
            "chatgpt/gpt-5.4": {
                "provider": "chatgpt",
                "api_key": "should-not-pass",
                "base_url": "https://should-not-pass.example/v1",
                "max_tokens": 2048,
            },
        }),
    )
    await complete("chatgpt/gpt-5.4", [{"role": "user", "content": "hi"}], settings=settings)
    kw = calls[0]
    assert kw["model"] == "chatgpt/gpt-5.4"
    assert "extra_headers" not in kw
    assert "max_tokens" not in kw
    assert "api_key" not in kw
    assert "api_base" not in kw
    import os
    assert os.environ["CHATGPT_TOKEN_DIR"] == str(token_dir)


@pytest.mark.asyncio
async def test_chatgpt_prefix_detected_without_provider_field(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    settings = _settings(
        models_json=json.dumps({
            "chatgpt/gpt-5.4": {"max_tokens": 1024, "api_key": "x"},
        }),
    )
    await complete("chatgpt/gpt-5.4", [{"role": "user", "content": "hi"}], settings=settings)
    kw = calls[0]
    assert "extra_headers" not in kw
    assert "max_tokens" not in kw
    assert "api_key" not in kw


@pytest.mark.asyncio
async def test_relay_openai_keeps_ua_and_credentials_alongside_chatgpt(monkeypatch):
    """Coexistence: openai relay still gets UA + credentials; chatgpt does not."""
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("{}"))
    settings = _settings(
        models_json=json.dumps({
            "openai/gpt-4o": {
                "provider": "openai",
                "base_url": "https://relay/v1",
                "api_key": "sk-relay",
                "max_tokens": 1024,
            },
            "chatgpt/gpt-5.4": {"provider": "chatgpt", "max_tokens": 2048},
        }),
    )
    await complete("openai/gpt-4o", [{"role": "user", "content": "hi"}], settings=settings)
    await complete("chatgpt/gpt-5.4", [{"role": "user", "content": "hi"}], settings=settings)

    relay = calls[0]
    assert relay["api_key"] == "sk-relay"
    assert relay["api_base"] == "https://relay/v1"
    assert relay["max_tokens"] == 1024
    assert relay["extra_headers"]["User-Agent"] == DEFAULT_GATEWAY_USER_AGENT

    chatgpt = calls[1]
    assert "extra_headers" not in chatgpt
    assert "max_tokens" not in chatgpt
    assert "api_key" not in chatgpt
    assert "api_base" not in chatgpt


@pytest.mark.asyncio
async def test_streaming_aggregates_text_reasoning_usage_and_progress(monkeypatch):
    stream = _FakeStream([
        {"choices": [{"delta": {"reasoning_content": "Checking "}}]},
        {"choices": [{"delta": {"content": "hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ])
    calls = _install_fake_litellm(monkeypatch, responses=stream)
    updates: list[dict[str, Any]] = []
    settings = _settings(models_json=json.dumps({
        "openai/test": {
            "provider": "openai", "stream": True, "reasoning_supported": True,
            "reasoning": {"effort": "high", "summary": "concise"},
        },
    }))
    result = await complete(
        "openai/test", [{"role": "user", "content": "hi"}],
        settings=settings, stream_sink=updates.append,
    )
    assert calls[0]["stream"] is True
    assert result.content == "hello"
    assert result.reasoning.summary == "Checking"
    assert result.usage["input_tokens"] == 7
    assert updates[0]["status"] == "started"
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["text"] == "hello"


@pytest.mark.asyncio
async def test_streaming_reassembles_parallel_fragmented_tool_calls(monkeypatch):
    stream = _FakeStream([
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "first", "arguments": "{\"x\":"}},
            {"index": 1, "id": "call_b", "function": {"name": "second", "arguments": "{\"y\":"}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}},
            {"index": 1, "function": {"arguments": "2}"}},
        ]}, "finish_reason": "tool_calls"}]},
    ])
    _install_fake_litellm(monkeypatch, responses=stream)
    updates: list[dict[str, Any]] = []
    settings = _settings(models_json=json.dumps({"openai/test": {"stream": True}}))
    result = await complete(
        "openai/test", [{"role": "user", "content": "hi"}],
        settings=settings, stream_sink=updates.append,
    )
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_a", "first", '{"x":1}'),
        ("call_b", "second", '{"y":2}'),
    ]
    assert result.stop_reason == "tool_calls"
    assert updates[-1]["tool_call_count"] == 2
    assert "arguments" not in updates[-1]


@pytest.mark.asyncio
async def test_stream_callback_failure_is_isolated(monkeypatch):
    _install_fake_litellm(monkeypatch, responses=_FakeStream([
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ]))
    settings = _settings(models_json=json.dumps({"openai/test": {"stream": True}}))

    def broken_sink(_payload):
        raise RuntimeError("console disconnected")

    result = await complete(
        "openai/test", [{"role": "user", "content": "hi"}],
        settings=settings, stream_sink=broken_sink,
    )
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_stream_retry_starts_a_new_attempt(monkeypatch):
    fake = types.ModuleType("litellm")
    attempts = 0

    async def acompletion(**_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _FakeExc("temporary", status_code=500)
        return _FakeStream([
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        ])

    async def no_sleep(_delay):
        return None

    fake.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setattr("shared.llm_gateway.asyncio.sleep", no_sleep)
    updates: list[dict[str, Any]] = []
    result = await complete(
        "openai/test", [{"role": "user", "content": "hi"}], max_retries=1,
        settings=_settings(models_json=json.dumps({"openai/test": {"stream": True}})),
        stream_sink=updates.append,
    )
    assert result.content == "ok"
    assert result.transport_attempts == 2
    lifecycle = [
        (update["attempt"], update["status"])
        for update in updates
        if update["status"] in {"started", "failed"}
    ]
    assert lifecycle == [
        (1, "started"), (1, "failed"), (2, "started"),
    ]


@pytest.mark.asyncio
async def test_non_streaming_model_ignores_stream_callback(monkeypatch):
    calls = _install_fake_litellm(monkeypatch, responses=_FakeResponse("ok"))
    updates: list[dict[str, Any]] = []
    result = await complete(
        "openai/test", [{"role": "user", "content": "hi"}],
        settings=_settings(models_json=json.dumps({"openai/test": {"stream": False}})),
        stream_sink=updates.append,
    )
    assert "stream" not in calls[0]
    assert result.content == "ok"
    assert updates == []
