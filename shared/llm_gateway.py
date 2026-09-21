"""Provider-agnostic LLM gateway backed by LiteLLM Chat Completions.

Planner, Reviewer, and Executor (via Agent Session) call `complete()` with a model
id, a list of `messages`, and optionally `tools` / a pydantic
`response_format`. The gateway resolves the provider/endpoint/key from
`Settings`, applies a four-category exception policy, and returns a
`GatewayResponse`. The gateway is **sessionless**; multi-turn tool loops are
owned by the Agent Session.

Design (see openspec/changes/agent-native-llm + clickclaw-skill-harness):
- D2: LiteLLM-backed Chat Completions (`acompletion`), one interface for
  OpenAI/Anthropic/GLM/Kimi/...
- D3: pydantic schema preferred for non-tool callers; tool_calls for AgentSession.
- D10: typed exception handling (transient / auth / malformed / budget-length /
  content-safety). LLM-call retries are safe only for transient failures;
  driver retries are NOT.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Type, TypeVar

from pydantic import BaseModel, ValidationError

from shared.config import Settings, get_settings


T = TypeVar("T", bound=BaseModel)

GATEWAY_MAX_RETRIES = 3
GATEWAY_RETRY_BASE_DELAY_S = 0.5
GATEWAY_REQUEST_TIMEOUT_S = 60.0
DEFAULT_GATEWAY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0"
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Common error type raised by the gateway.

    `category` is one of: `transient`, `auth`, `malformed`, `budget`, `config`,
    `content_safety`, `quota`.
    Provider-specific error shapes MUST NOT leak past this boundary; callers
    branch on `category`, not on the underlying exception type.
    """

    def __init__(self, message: str, *, category: str, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.cause = cause
        self.transport_attempts = 1


class GatewayAuthError(GatewayError):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message, category="auth", cause=cause)


class GatewayConfigError(GatewayError):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message, category="config", cause=cause)


class GatewayQuotaError(GatewayError):
    """Provider allowance exhausted; immediate retries cannot restore it."""

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message, category="quota", cause=cause)


class GatewayTransientError(GatewayError):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message, category="transient", cause=cause)


class GatewayBudgetError(GatewayError):
    def __init__(self, message: str, cause: Exception | None = None) -> None:
        super().__init__(message, category="budget", cause=cause)


class GatewayInputSafetyError(GatewayError):
    """The provider rejected one or more input images as unsafe.

    Some OpenAI-compatible relays incorrectly surface deterministic input
    moderation failures as HTTP 500.  Keep the provider-specific coordinates
    as optional diagnostics so the session owner can choose a safe, generic
    projection without parsing exception strings itself.
    """

    def __init__(
        self,
        message: str,
        cause: Exception | None = None,
        *,
        offending_locations: list[tuple[int, int]] | None = None,
    ) -> None:
        super().__init__(message, category="content_safety", cause=cause)
        self.offending_locations = list(offending_locations or [])


@dataclass
class ToolCall:
    """One normalized tool call from a Chat Completions response."""

    id: str
    name: str
    arguments: str  # JSON string (provider-native); AgentSession parses


@dataclass(frozen=True)
class ReasoningConfig:
    """Provider-neutral request controls for reasoning-capable models."""

    effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


@dataclass(frozen=True)
class ReasoningResult:
    """Out-of-band diagnostic result; never semantic task memory."""

    status: Literal["not_requested", "supported", "unsupported"] = "not_requested"
    effort: str | None = None
    summary_preference: str | None = None
    summary: str | None = None


@dataclass
class GatewayResponse:
    """Result of a single Chat Completions `complete()` round-trip.

    `complete()` is the project's LiteLLM **Chat Completions** transport
    (`messages` + optional `tools`). The gateway is sessionless — the Skill
    AgentSession owns multi-turn `messages[]` and may call `complete()` repeatedly.

    `content` is the raw text the model produced (may be empty on tool_calls).
    When `parse_risk` is True the content did NOT validate against the
    requested schema — the caller should retry once with a correction nudge,
    then apply a tolerant fallback parser.

    `stop_reason` is normalized to one of: `stop`, `length`, `tool_calls`,
    `content_filter`, `error`, `unknown`. `tool_calls` means `tool_calls` is
    populated for the AgentSession to execute.
    """

    content: str
    model: str
    stop_reason: Literal["stop", "length", "tool_calls", "content_filter", "error", "unknown"] = "stop"
    parse_risk: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    raw: Any = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: ReasoningResult = field(default_factory=ReasoningResult)
    transport_attempts: int = 1


StreamSink = Callable[[dict[str, Any]], Any]


def normalize_usage(raw: Any) -> dict[str, int | bool | None]:
    """Normalize OpenAI/Anthropic/LiteLLM token and cache usage shapes."""
    if raw is None:
        data: dict[str, Any] = {}
    elif isinstance(raw, dict):
        data = raw
    else:
        dumped = getattr(raw, "model_dump", lambda: {})()
        data = dumped if isinstance(dumped, dict) else {}

    input_details = data.get("prompt_tokens_details") or data.get("input_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = getattr(input_details, "model_dump", lambda: {})()
    output_details = data.get("completion_tokens_details") or data.get("output_tokens_details") or {}
    if not isinstance(output_details, dict):
        output_details = getattr(output_details, "model_dump", lambda: {})()
    cache_creation = data.get("cache_creation") or {}
    if not isinstance(cache_creation, dict):
        cache_creation = getattr(cache_creation, "model_dump", lambda: {})()

    def _first(*values: Any) -> int | None:
        for value in values:
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    input_tokens = _first(
        data.get("input_tokens"), data.get("prompt_tokens"), data.get("prompt_token_count"),
    )
    output_tokens = _first(
        data.get("output_tokens"), data.get("completion_tokens"), data.get("candidates_token_count"),
    )
    cached_read = _first(
        data.get("cache_read_input_tokens"),
        data.get("cached_input_tokens"),
        input_details.get("cached_tokens"),
        input_details.get("cache_read_tokens"),
    )
    cache_write = _first(
        data.get("cache_creation_input_tokens"),
        data.get("cache_write_input_tokens"),
        input_details.get("cache_write_tokens"),
        cache_creation.get("ephemeral_5m_input_tokens"),
    )
    reasoning_tokens = _first(
        data.get("reasoning_tokens"),
        output_details.get("reasoning_tokens"),
    )
    total_tokens = _first(data.get("total_tokens"), data.get("total_token_count"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "cached_read_tokens": cached_read,
        "cache_write_tokens": cache_write,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total_tokens,
        "cache_hit": None if cached_read is None else cached_read > 0,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _schema_to_json_schema(model: Type[BaseModel]) -> dict[str, Any]:
    """Translate a pydantic model to a JSON schema dict for providers."""
    return model.model_json_schema()


def _effective_reasoning_config(
    settings: Settings,
    model_id: str,
    requested: ReasoningConfig | None,
) -> tuple[ReasoningConfig | None, bool]:
    provider = settings.provider_for(model_id)
    raw = provider.get("reasoning")
    config = requested
    if config is None and isinstance(raw, dict):
        effort = raw.get("effort")
        summary = raw.get("summary")
        config = ReasoningConfig(
            effort=effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else None,
            summary=summary if summary in {"auto", "concise", "detailed"} else None,
        )
    supported = bool(provider.get("reasoning_supported", raw))
    return config, supported


def _extract_reasoning_summary(resp: Any, *, allow_reasoning_content: bool = False) -> str | None:
    """Read only explicit provider summaries, never private reasoning content.

    LiteLLM normalizes Responses-API reasoning *summaries* into
    `message.reasoning_content` (on that route the full chain-of-thought is
    encrypted and never exposed). We read that field only when the caller
    explicitly requested a summary — otherwise a relay's private
    chain-of-thought (e.g. deepseek-style `reasoning_content`) would leak
    into persisted traces.
    """
    candidates: list[Any] = []
    if isinstance(resp, dict):
        candidates.extend([resp.get("reasoning_summary"), resp.get("summary")])
        choices = resp.get("choices") or []
    else:
        candidates.extend([
            getattr(resp, "reasoning_summary", None),
            getattr(resp, "summary", None),
        ])
        choices = getattr(resp, "choices", None) or []
    if choices:
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else getattr(first, "message", None)
        if isinstance(message, dict):
            candidates.extend([message.get("reasoning_summary"), message.get("summary")])
            if allow_reasoning_content:
                candidates.append(message.get("reasoning_content"))
        elif message is not None:
            candidates.extend([
                getattr(message, "reasoning_summary", None),
                getattr(message, "summary", None),
            ])
            if allow_reasoning_content:
                candidates.append(getattr(message, "reasoning_content", None))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()[:4000]
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            text = "\n".join(part.strip() for part in parts if part.strip())
            if text:
                return text[:4000]
    return None


def _normalize_stop_reason(raw: Any) -> Literal["stop", "length", "tool_calls", "content_filter", "error", "unknown"]:
    """Normalize provider stop-reason strings to a single vocabulary."""
    if raw is None:
        return "stop"
    s = str(raw).lower().strip()
    if s in ("stop", "end_turn", "stop_sequence", "max_tokens", "length", "max_output_tokens"):
        if s in ("max_tokens", "length", "max_output_tokens"):
            return "length"
        return "stop"
    if "length" in s:
        return "length"
    if "tool" in s or "function" in s:
        return "tool_calls"
    if "filter" in s or "content" in s:
        return "content_filter"
    if "error" in s:
        return "error"
    return "unknown"


def _classify_exception(exc: Exception) -> GatewayError:
    """Classify a provider/LiteLLM exception into a stable gateway category.

    Heuristics: LiteLLM raises `litellm.exceptions.*` and re-raises provider
    HTTP errors with `status_code` attributes. We classify by status code
    when available, falling back to exception type name matching.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    msg = str(exc)
    msg_lower = msg.lower()

    # Deterministic input moderation errors are sometimes wrapped in a 500 by
    # OpenAI-compatible relays.  Classify by the semantic payload before the
    # status-code branch so the gateway never retries the same rejected image.
    input_safety_markers = (
        "input new_sensitive",
        "image is sensitive",
        "input image is sensitive",
    )
    if any(marker in msg_lower for marker in input_safety_markers) or (
        "1026" in msg_lower and "sensitive" in msg_lower
    ):
        locations = [
            (int(message_index), int(content_index))
            for message_index, content_index in re.findall(
                r"messages\[(\d+)\](?:'s)?\s*content\[(\d+)\]",
                msg,
                flags=re.IGNORECASE,
            )
        ]
        return GatewayInputSafetyError(
            f"input content safety rejection: {msg}",
            cause=exc,
            offending_locations=locations,
        )

    # Inspect only provider errors, never model-generated task explanations.
    # SDKs may keep the backend code in body rather than the exception message.
    quota_detail = (msg_lower + " " + str(getattr(exc, "body", "")).lower()).replace("’", "'")
    quota_markers = (
        "usage_limit_reached", "insufficient_quota", "quota_exceeded",
        "usage limit has been reached", "you've hit your usage limit",
        "you have hit your usage limit", "usage limit reached",
        "you have reached your usage limit",
        "exceeded your current quota", "insufficient credits", "out of credits",
    )
    if any(marker in quota_detail for marker in quota_markers):
        return GatewayQuotaError(f"quota exhausted: {msg}; provider_body={getattr(exc, 'body', None)}", cause=exc)

    # Auth
    if status in (401, 403):
        return GatewayAuthError(f"auth error ({status}): {msg}", cause=exc)
    if "auth" in name or "unauthorized" in msg_lower or "forbidden" in msg_lower:
        return GatewayAuthError(f"auth error: {msg}", cause=exc)

    # Transient: 5xx, 429, timeouts, connection errors
    if status is not None:
        try:
            sc = int(status)
        except (TypeError, ValueError):
            sc = 0
        if sc == 429 or 500 <= sc < 600:
            return GatewayTransientError(f"transient ({sc}): {msg}", cause=exc)
    if "timeout" in name or "timeout" in msg_lower:
        return GatewayTransientError(f"timeout: {msg}", cause=exc)
    if "rate" in name or "rate limit" in msg_lower:
        return GatewayTransientError(f"rate-limit: {msg}", cause=exc)
    if "connection" in name or "connect" in msg_lower:
        return GatewayTransientError(f"connection: {msg}", cause=exc)

    # Budget / length
    if "length" in name or "max_tokens" in msg_lower or "context" in msg_lower and "length" in msg_lower:
        return GatewayBudgetError(f"budget/length: {msg}", cause=exc)

    # Client 4xx (except auth/timeout/rate-limit above) is a bad request:
    # tool_choice + thinking, unknown field, etc. Retrying wastes seconds.
    if status is not None:
        try:
            sc = int(status)
        except (TypeError, ValueError):
            sc = 0
        if 400 <= sc < 500 and sc not in (401, 403, 408, 429):
            return GatewayConfigError(f"config ({sc}): {msg}", cause=exc)
    if "badrequest" in name or "invalidrequest" in name:
        return GatewayConfigError(f"config/argument: {msg}", cause=exc)

    # Default: treat as transient (safer to retry once) unless it's clearly
    # a config/argument problem.
    if "argument" in name or "invalid" in name or "valueerror" in name:
        return GatewayConfigError(f"config/argument: {msg}", cause=exc)
    return GatewayTransientError(f"unclassified (treated transient): {msg}", cause=exc)


def _is_chatgpt_subscription_provider(model_id: str, settings: Settings) -> bool:
    """True if `model_id` routes to LiteLLM's ChatGPT Pro/Max subscription provider.

    Prefers explicit `provider: chatgpt` in MODELS_JSON; falls back to the
    `chatgpt/` model-id prefix when no provider field is set.
    """
    provider = str(settings.provider_for(model_id).get("provider", "")).lower()
    if provider:
        return provider == "chatgpt"
    return model_id.lower().startswith("chatgpt/")


def _build_completion_kwargs(
    model_id: str,
    messages: list[dict[str, Any]],
    response_format: Type[BaseModel] | None,
    tools: list[Any] | None,
    tool_choice: str | dict[str, Any] | None,
    settings: Settings,
    reasoning: ReasoningConfig | None = None,
    *,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Build the kwargs for `litellm.completion` from Settings + caller args."""
    provider = settings.provider_for(model_id)
    chatgpt = _is_chatgpt_subscription_provider(model_id, settings)
    if chatgpt:
        # LiteLLM ChatGPT OAuth reads CHATGPT_TOKEN_DIR; apply before acompletion.
        settings.apply_chatgpt_token_dir()
    configured_reasoning = provider.get("reasoning")
    if reasoning is None and isinstance(configured_reasoning, dict):
        effort = configured_reasoning.get("effort")
        summary = configured_reasoning.get("summary")
        try:
            reasoning = ReasoningConfig(
                effort=effort if effort in {"minimal", "low", "medium", "high", "xhigh"} else None,
                summary=summary if summary in {"auto", "concise", "detailed"} else None,
            )
        except TypeError:
            reasoning = None
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "timeout": GATEWAY_REQUEST_TIMEOUT_S,
    }
    if provider.get("stream") is True:
        kwargs["stream"] = True
    # ChatGPT subscription auth/base are owned by LiteLLM OAuth; do not pass
    # MODELS_JSON api_key/base_url/max_tokens (backend rejects token limits).
    if not chatgpt:
        if "api_key" in provider and provider["api_key"]:
            kwargs["api_key"] = provider["api_key"]
        if "base_url" in provider and provider["base_url"]:
            kwargs["api_base"] = provider["base_url"]
        configured_extra_body = provider.get("extra_body")
        if isinstance(configured_extra_body, dict) and configured_extra_body:
            kwargs["extra_body"] = dict(configured_extra_body)
        allowed_params = provider.get("allowed_openai_params")
        if (
            isinstance(allowed_params, list)
            and allowed_params
            and all(isinstance(param, str) for param in allowed_params)
        ):
            # Explicit per-model capability override for compatible relay aliases
            # missing from LiteLLM's model registry. Keep strict defaults elsewhere.
            kwargs["allowed_openai_params"] = list(allowed_params)
        if max_output_tokens is not None:
            kwargs["max_tokens"] = int(max_output_tokens)
        elif "max_tokens" in provider and provider["max_tokens"]:
            kwargs["max_tokens"] = int(provider["max_tokens"])
    reasoning_supported = bool(provider.get("reasoning_supported", configured_reasoning))
    if reasoning is not None and reasoning_supported:
        if reasoning.effort:
            kwargs["reasoning_effort"] = reasoning.effort
        # LiteLLM provider adapters that support summary controls accept the
        # normalized reasoning object; adapters that do not must leave
        # reasoning_supported false in model configuration.
        if reasoning.summary:
            kwargs["reasoning"] = {"summary": reasoning.summary}
    if response_format is not None:
        schema = _schema_to_json_schema(response_format)
        # LiteLLM exposes a uniform `response_format` kwarg; providers that
        # support structured output honor JSON schema here. For providers
        # that don't, we still pass it; the gateway surfaces parse_risk.
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": response_format.__name__, "schema": schema}}
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
    # Inject a configurable User-Agent for OpenAI-compatible relays and other
    # providers. ChatGPT subscription is the sole exception: Codex expects its
    # own client identity, so we omit ClickClick's forced browser UA.
    if not chatgpt:
        ua = settings.gateway_user_agent or DEFAULT_GATEWAY_USER_AGENT
        kwargs["extra_headers"] = {"User-Agent": ua}
    return kwargs


def _is_anthropic_provider(model_id: str, settings: Settings) -> bool:
    """True if `model_id` routes to Anthropic.

    Prefers the explicit `provider` field in the per-model config; falls back
    to a model-id heuristic (`claude*`) when no provider config is present
    (LiteLLM defaults). Used to decide whether to emit Anthropic-style
    `cache_control` markers.
    """
    provider = str(settings.provider_for(model_id).get("provider", "")).lower()
    if provider:
        return provider == "anthropic"
    return model_id.lower().startswith("claude")


def _as_cached_text_block(text: str) -> list[dict[str, Any]]:
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]


def _apply_cache_control(
    messages: list[dict[str, Any]],
    model_id: str,
    settings: Settings,
    cache_system_prefix: bool,
    *,
    cache_skill_index: bool = False,
) -> list[dict[str, Any]]:
    """Attach Anthropic prefix-cache markers when requested.

    When ``cache_system_prefix`` is True, mark the system message. When
    ``cache_skill_index`` is True, also mark the last contiguous Decision
    Context v2 Skill-index/K message. Cumulative prefix through the stable index,
    generic knowledge, and exact-App knowledge is what Anthropic caches.
    Non-Anthropic providers: no-op.
    """
    if not cache_system_prefix and not cache_skill_index:
        return messages
    if not _is_anthropic_provider(model_id, settings):
        return messages
    out: list[dict[str, Any]] = []
    k_prefixes = (
        "SKILL INDEX",
        "[generic skill:",
        "[foreground_app_core skill:",
        "[foreground_app_workflow skill:",
    )
    k_marker_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
            and isinstance(messages[index].get("content"), str)
            and messages[index].get("content", "").lstrip().startswith(k_prefixes)
        ),
        -1,
    ) if cache_skill_index else -1
    for index, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")
        if cache_system_prefix and role == "system" and isinstance(content, str):
            out.append({
                "role": "system",
                "content": _as_cached_text_block(content),
            })
            continue
        if (
            index == k_marker_index
            and role == "user"
            and isinstance(content, str)
        ):
            out.append({
                "role": "user",
                "content": _as_cached_text_block(content),
            })
            continue
        out.append(msg)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def complete(
    model: str,
    messages: list[dict[str, Any]],
    *,
    response_format: Type[BaseModel] | None = None,
    tools: list[Any] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    cache_system_prefix: bool = False,
    cache_skill_index: bool = False,
    settings: Settings | None = None,
    reasoning: ReasoningConfig | None = None,
    max_retries: int | None = None,
    max_output_tokens: int | None = None,
    attempt_meter: Callable[[str, dict[str, Any]], Any] | None = None,
    stream_sink: StreamSink | None = None,
) -> GatewayResponse:
    """One Chat Completions round-trip (LiteLLM ``acompletion``).

    This is **not** a legacy non-chat Completion API. Callers pass a full
    ``messages`` list (and optional ``tools``). The gateway stays
    **sessionless**; Agent Session owns multi-turn chat state.

    Behavior:
    - Transient (timeout / 5xx / 429): exponential backoff retry up to
      the gateway's fixed transient-error policy.
    - Input content safety rejection: surfaced distinctly without retrying the
      same payload so the session owner can remove rejected image attachments.
    - Auth (401 / 403): no retry, raise `GatewayAuthError`.
    - Malformed output (syntactically valid but schema-non-conforming):
      NOT retried inside the gateway. Returns `GatewayResponse` with
      `parse_risk=True` so the caller can retry once with a nudge and/or
      apply a tolerant fallback parser.
    - Budget/length (`stop_reason=length`): surfaced distinctly; not retried.
    - Tool use (`stop_reason=tool_calls`): ``tool_calls`` populated.

    When ``tools`` and ``tool_choice`` are both supplied, the choice is passed
    through to LiteLLM unchanged. AgentSession defaults to ``required``, with
    a per-model ``auto`` option; it still requires a validated tool submission.

    `cache_system_prefix` / `cache_skill_index`: when True and `model` routes
    to Anthropic, attach ephemeral `cache_control` markers to the system
    message and/or the stable knowledge-prefix user message so the provider
    caches S+K. No-op for non-Anthropic providers. The parameter name is an
    internal provider adapter label, not a model-visible legacy bucket.
    """
    s = settings or get_settings()
    messages = _apply_cache_control(
        messages, model, s, cache_system_prefix,
        cache_skill_index=cache_skill_index,
    )
    kwargs = _build_completion_kwargs(
        model,
        messages,
        response_format,
        tools,
        tool_choice,
        s,
        reasoning,
        max_output_tokens=max_output_tokens,
    )

    # Lazy import so unit tests can mock the gateway without importing litellm.
    import litellm  # type: ignore[import-not-found]

    last_exc: Exception | None = None
    retry_cap = GATEWAY_MAX_RETRIES if max_retries is None else max(0, int(max_retries))
    streaming = kwargs.get("stream") is True
    for attempt in range(retry_cap + 1):
        if attempt_meter is not None:
            metered = attempt_meter("model_call_started", {
                "attempt": attempt + 1,
            })
            if inspect.isawaitable(metered):
                await metered
        t0 = time.monotonic()
        try:
            if streaming:
                await _notify_stream(stream_sink, {
                    "attempt": attempt + 1,
                    "sequence": 0,
                    "status": "started",
                    "text": "",
                    "summary": "",
                    "tool_call_count": 0,
                })
            # litellm.acompletion is the async entry point.
            resp = await litellm.acompletion(**kwargs)
            if streaming:
                effective_reasoning, _ = _effective_reasoning_config(s, model, reasoning)
                resp = await _aggregate_stream(
                    resp,
                    stream_sink=stream_sink,
                    attempt=attempt + 1,
                    allow_reasoning_content=bool(
                        effective_reasoning and effective_reasoning.summary
                    ),
                )
            latency_ms = (time.monotonic() - t0) * 1000.0
            result = _build_response(
                resp, model, response_format, latency_ms,
                reasoning=_effective_reasoning_config(s, model, reasoning),
            )
            result.transport_attempts = attempt + 1
            return result
        except Exception as exc:  # noqa: BLE001
            if streaming:
                await _notify_stream(stream_sink, {
                    "attempt": attempt + 1,
                    "sequence": -1,
                    "status": "failed",
                    "text": "",
                    "summary": "",
                    "tool_call_count": 0,
                })
            last_exc = exc
            classified = _classify_exception(exc)
            if classified.category in ("auth", "quota"):
                classified.transport_attempts = attempt + 1
                raise classified from exc
            if classified.category == "config":
                classified.transport_attempts = attempt + 1
                raise classified from exc
            if classified.category == "budget":
                classified.transport_attempts = attempt + 1
                raise classified from exc
            if classified.category == "content_safety":
                classified.transport_attempts = attempt + 1
                raise classified from exc
            # transient → backoff and retry
            if attempt < retry_cap:
                delay = GATEWAY_RETRY_BASE_DELAY_S * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            classified.transport_attempts = attempt + 1
            raise classified from exc
    # Should not reach here; the loop either returns or raises.
    if last_exc is not None:
        raise _classify_exception(last_exc) from last_exc
    raise GatewayError("complete() exhausted retries with no exception", category="transient")


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _stream_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        text = item if isinstance(item, str) else _value(item, "text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


async def _notify_stream(sink: StreamSink | None, payload: dict[str, Any]) -> None:
    """Best-effort progress reporting must never break model execution."""
    if sink is None:
        return
    try:
        value = sink(payload)
        if inspect.isawaitable(value):
            await value
    except Exception:  # noqa: BLE001
        return


async def _aggregate_stream(
    response: Any,
    *,
    stream_sink: StreamSink | None,
    attempt: int,
    allow_reasoning_content: bool,
) -> Any:
    """Consume LiteLLM chunks into a normal response without leaking tool args."""
    if not hasattr(response, "__aiter__"):
        # Some compatible relays ignore stream=True. Preserve their completed
        # response rather than rejecting an otherwise valid model call.
        return response

    content_parts: list[str] = []
    summary_parts: list[str] = []
    tools: dict[int, dict[str, str]] = {}
    finish_reason: Any = None
    usage: Any = None
    sequence = 0
    last_emit_at = 0.0

    async for chunk in response:
        chunk_usage = _value(chunk, "usage")
        if chunk_usage is not None:
            usage = chunk_usage
        choices = _value(chunk, "choices", []) or []
        if not choices:
            continue
        first = choices[0]
        finish = _value(first, "finish_reason")
        if finish is not None:
            finish_reason = finish
        delta = _value(first, "delta") or _value(first, "message") or {}
        content = _stream_text(_value(delta, "content"))
        if content:
            content_parts.append(content)
        summary = _stream_text(_value(delta, "reasoning_summary"))
        if not summary:
            summary = _stream_text(_value(delta, "summary"))
        if not summary and allow_reasoning_content:
            summary = _stream_text(_value(delta, "reasoning_content"))
        if summary:
            summary_parts.append(summary)
        for fallback_index, raw_call in enumerate(_value(delta, "tool_calls", []) or []):
            raw_index = _value(raw_call, "index", fallback_index)
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = fallback_index
            entry = tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = _value(raw_call, "id") or ""
            fn = _value(raw_call, "function", {}) or {}
            entry["id"] += str(call_id)
            entry["name"] += str(_value(fn, "name", _value(raw_call, "name", "")) or "")
            arguments = _value(fn, "arguments", _value(raw_call, "arguments", ""))
            if isinstance(arguments, str):
                entry["arguments"] += arguments

        now = time.monotonic()
        if (content or summary or tools) and (sequence == 0 or now - last_emit_at >= 0.04):
            sequence += 1
            last_emit_at = now
            await _notify_stream(stream_sink, {
                "attempt": attempt,
                "sequence": sequence,
                "status": "streaming",
                "text": "".join(content_parts),
                "summary": "".join(summary_parts),
                "tool_call_count": len(tools),
            })

    message: dict[str, Any] = {
        "content": "".join(content_parts),
        "tool_calls": [
            {
                "id": item["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": item["name"], "arguments": item["arguments"] or "{}"},
            }
            for index, item in sorted(tools.items())
            if item["name"]
        ],
    }
    if summary_parts:
        message["reasoning_summary"] = "".join(summary_parts)
    aggregate = {
        "choices": [{
            "message": message,
            "finish_reason": finish_reason or ("tool_calls" if tools else "stop"),
        }],
        "usage": usage,
    }
    sequence += 1
    await _notify_stream(stream_sink, {
        "attempt": attempt,
        "sequence": sequence,
        "status": "completed",
        "text": message["content"],
        "summary": message.get("reasoning_summary", ""),
        "tool_call_count": len(message["tool_calls"]),
    })
    return aggregate


def _extract_tool_calls(msg: Any) -> list[ToolCall]:
    """Normalize OpenAI-style tool_calls from a message object or dict."""
    raw_calls = (
        getattr(msg, "tool_calls", None)
        if not isinstance(msg, dict)
        else msg.get("tool_calls")
    )
    if not raw_calls:
        return []
    out: list[ToolCall] = []
    for i, tc in enumerate(raw_calls):
        if isinstance(tc, dict):
            tc_id = str(tc.get("id") or f"call_{i}")
            fn = tc.get("function") or {}
            name = str(fn.get("name") or tc.get("name") or "")
            args = fn.get("arguments", tc.get("arguments", "{}"))
        else:
            tc_id = str(getattr(tc, "id", None) or f"call_{i}")
            fn = getattr(tc, "function", None)
            if fn is not None:
                name = str(getattr(fn, "name", "") or "")
                args = getattr(fn, "arguments", "{}")
            else:
                name = str(getattr(tc, "name", "") or "")
                args = getattr(tc, "arguments", "{}")
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        if name:
            out.append(ToolCall(id=tc_id, name=name, arguments=args))
    return out


def _build_response(
    resp: Any,
    model: str,
    response_format: Type[BaseModel] | None,
    latency_ms: float,
    *,
    reasoning: tuple[ReasoningConfig | None, bool] = (None, False),
) -> GatewayResponse:
    """Extract content + stop_reason + tool_calls from a LiteLLM response.

    LiteLLM normalizes responses to an OpenAI-like shape: `resp.choices[0].message.content`
    and `resp.choices[0].finish_reason`. We tolerate both attribute and dict
    access so a fake/mock provider in tests works equally well.
    """
    content = ""
    stop_reason_raw: Any = "stop"
    usage: dict[str, Any] = {}
    tool_calls: list[ToolCall] = []

    choices = getattr(resp, "choices", None)
    if choices is None and isinstance(resp, dict):
        choices = resp.get("choices")
    if choices:
        first = choices[0]
        msg = getattr(first, "message", None) if not isinstance(first, dict) else first.get("message")
        if msg is not None:
            content = getattr(msg, "content", "") if not isinstance(msg, dict) else (msg.get("content") or "")
            tool_calls = _extract_tool_calls(msg)
        finish = getattr(first, "finish_reason", None) if not isinstance(first, dict) else first.get("finish_reason")
        stop_reason_raw = finish if finish is not None else "stop"

    u = getattr(resp, "usage", None) if not isinstance(resp, dict) else resp.get("usage")
    usage = normalize_usage(u)

    stop_reason = _normalize_stop_reason(stop_reason_raw)
    if tool_calls and stop_reason == "stop":
        # Some providers omit finish_reason=tool_calls; infer from payload.
        stop_reason = "tool_calls"

    parse_risk = False
    if response_format is not None and not tool_calls:
        # Try to validate the content against the requested schema. On
        # failure, surface the raw content with parse_risk=True — the caller
        # retries once with a nudge and/or applies a tolerant fallback parser.
        try:
            payload = json.loads(content) if content and content.strip().startswith("{") else None
            if payload is not None:
                response_format.model_validate(payload)
            else:
                parse_risk = True
        except (json.JSONDecodeError, ValidationError, TypeError):
            parse_risk = True

    reasoning_config, reasoning_supported = reasoning
    summary = (
        _extract_reasoning_summary(
            resp,
            allow_reasoning_content=bool(
                reasoning_config and reasoning_config.summary
            ),
        )
        if reasoning_supported
        else None
    )
    reasoning_result = ReasoningResult(
        status=(
            "not_requested" if reasoning_config is None
            else "supported" if reasoning_supported
            else "unsupported"
        ),
        effort=reasoning_config.effort if reasoning_config else None,
        summary_preference=reasoning_config.summary if reasoning_config else None,
        summary=summary,
    )
    return GatewayResponse(
        content=content or "",
        model=model,
        stop_reason=stop_reason,
        parse_risk=parse_risk,
        usage=usage,
        latency_ms=latency_ms,
        raw=resp,
        tool_calls=tool_calls,
        reasoning=reasoning_result,
    )
