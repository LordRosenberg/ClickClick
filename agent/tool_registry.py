"""Stable role-scoped Agent tool registry and generic invocation records."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentRole(str, Enum):
    PLANNER = "planner"
    REVIEWER = "reviewer"
    EXECUTOR = "executor"


class ToolCategory(str, Enum):
    KNOWLEDGE = "knowledge"
    OBSERVATION = "observation"
    DEVICE_DISCOVERY = "device_discovery"
    TERMINAL = "terminal"


class ToolStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INVALID_ARGUMENTS = "invalid_arguments"
    PRECONDITION_NOT_MET = "precondition_not_met"


_SECRET_KEY = re.compile(
    r"(?:password|passwd|passphrase|secret|token|resolution[_-]?ticket|api[_-]?key|authorization|cookie)",
    re.IGNORECASE,
)
_DATA_URL = re.compile(r"^data:image/[^;]+;base64,", re.IGNORECASE)


def redact_value(
    value: Any,
    *,
    key: str = "",
    max_string: int | None = 1000,
    max_items: int | None = 100,
) -> Any:
    """Return a JSON-safe copy without credentials or image bytes.

    ``None`` disables presentation truncation for exact persisted protocol
    evidence. Bounded diagnostic callers retain the explicit defaults.
    """
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(k): redact_value(
                v,
                key=str(k),
                max_string=max_string,
                max_items=max_items,
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        values = value if max_items is None else value[:max_items]
        return [
            redact_value(
                v,
                key=key,
                max_string=max_string,
                max_items=max_items,
            )
            for v in values
        ]
    if isinstance(value, bytes):
        return f"[binary:{len(value)} bytes]"
    if isinstance(value, BaseModel):
        return redact_value(
            value.model_dump(),
            key=key,
            max_string=max_string,
            max_items=max_items,
        )
    if isinstance(value, str):
        if _DATA_URL.match(value):
            return "[image attachment omitted]"
        return (
            value
            if max_string is None or len(value) <= max_string
            else value[:max_string] + "…"
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_value(
        str(value),
        key=key,
        max_string=max_string,
        max_items=max_items,
    )


def redact_exact_values(value: Any, exact_values: list[str] | tuple[str, ...]) -> Any:
    """Replace exact ephemeral secret values without guessing semantic fields."""
    secrets = [item for item in exact_values if isinstance(item, str) and item]
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, Mapping):
        return {
            key: redact_exact_values(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_exact_values(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_exact_values(item, secrets) for item in value)
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


class AgentToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    category: ToolCategory
    roles: tuple[AgentRole, ...]
    parameters: dict[str, Any]
    timeout_ms: int = Field(default=10_000, ge=1, le=120_000)
    updates_action_context: bool = False

    def chat_completion_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolAttachment(BaseModel):
    """Multimodal evidence; binary content is intentionally excluded from dumps."""

    label: str
    kind: str = "image"
    mime_type: str = "image/png"
    artifact_ref: str | None = None
    timestamp_ms: float | None = None
    actionable_coordinate_reference: bool = False
    indexed_targets_available: bool = True
    observation_id: str = ""
    coordinate_space_id: str = ""
    image_size: tuple[int, int] | None = None
    frame_geometry: tuple[int, int] | None = None
    rotation_degrees: int = 0
    crop_box: tuple[float, float, float, float] | None = None
    transform_id: str = ""
    element_set_id: str = ""
    content: bytes | str | None = Field(default=None, exclude=True, repr=False)


class EvidenceRecord(BaseModel):
    evidence_ref: str
    observation_id: str | None = None
    status: str = "complete"
    provider: str = ""
    artifact_refs: list[str] = Field(default_factory=list)


class AgentToolResult(BaseModel):
    status: ToolStatus = ToolStatus.SUCCEEDED
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    attachments: list[ToolAttachment] = Field(default_factory=list)
    # Internal replacement for AgentSession's dynamic observation bucket.
    # It may be text-only when multimodal attachments are feature-disabled.
    replacement_attachments: list[ToolAttachment] | None = Field(
        default=None, exclude=True, repr=False,
    )
    evidence: EvidenceRecord | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    provider_status: str | None = None
    error: str | None = None
    actionable_observation_id: str | None = None
    terminal_value: Any = Field(default=None, exclude=True, repr=False)
    def metadata(self) -> dict[str, Any]:
        payload = self.model_dump(exclude={"attachments"})
        payload["attachments"] = [a.model_dump() for a in self.attachments]
        return redact_value(payload)

    def model_metadata(self, tool_name: str = "") -> dict[str, Any]:
        """Return the compact, decision-relevant payload sent to the model.

        ``metadata()`` remains the full observability projection. Provider
        diagnostics, artifact bookkeeping, attachment descriptors, timings,
        and empty/default fields are intentionally kept off the provider wire.
        Observation evidence is described by the following attachment message,
        so its tool result carries only the state needed to choose the next
        action. A resolver ticket is the one exception: it is capability data
        the model must echo once and is restored only on the provider wire.
        """
        summary = self.summary
        if tool_name == "observe_screen" and self.status != ToolStatus.SUCCEEDED:
            mode = str(self.data.get("mode") or "screen")
            failed_stage = str(self.data.get("failed_stage") or "")
            summary = f"{mode} observation {self.status.value}"
            if failed_stage:
                summary += f" at {failed_stage}"
        payload: dict[str, Any] = {
            "status": self.status.value,
            "summary": summary,
        }
        if tool_name == "observe_screen":
            keep = {"mode", "status", "failed_stage", "frame_count"}
            if self.status == ToolStatus.SUCCEEDED:
                keep.update({
                    "observation_id", "coordinate_reference", "actionable",
                    "index_actionable",
                })
            compact_data = {
                key: value for key, value in self.data.items()
                if key in keep
                and (
                    key == "index_actionable"
                    or value not in (None, "", [], {}, False)
                )
            }
        else:
            compact_data = {
                key: value for key, value in self.data.items()
                if value not in (None, "", [], {})
            }
        if compact_data:
            payload["data"] = compact_data
        if self.actionable_observation_id and not (
            tool_name == "observe_screen" and self.status != ToolStatus.SUCCEEDED
        ):
            payload["actionable_observation_id"] = self.actionable_observation_id
        if self.evidence_refs and not (
            tool_name == "observe_screen" and self.status != ToolStatus.SUCCEEDED
        ):
            payload["evidence_refs"] = list(self.evidence_refs)
        if self.error and not (
            tool_name == "observe_screen" and self.status != ToolStatus.SUCCEEDED
        ):
            payload["error"] = self.error

        raw = payload
        safe = redact_value(raw)

        def restore(source: Any, target: Any) -> None:
            if isinstance(source, dict) and isinstance(target, dict):
                for key, value in source.items():
                    if key == "resolution_ticket" and isinstance(value, str):
                        target[key] = value
                    elif key in target:
                        restore(value, target[key])
            elif isinstance(source, list) and isinstance(target, list):
                for left, right in zip(source, target):
                    restore(left, right)

        restore(raw, safe)
        return safe


class NormalizedUsage(BaseModel):
    input_tokens: int | None = None
    cached_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cache_hit: bool | None = None


class LLMRoundRecord(BaseModel):
    round_id: str
    order: int
    role: AgentRole
    invocation_id: str
    request_ref: str | None = None
    response_ref: str | None = None
    stop_reason: str = "unknown"
    latency_ms: float = 0.0
    usage: NormalizedUsage = Field(default_factory=NormalizedUsage)
    message_count: int = 0
    image_count: int = 0
    stable_prefix_hash: str = ""
    tool_catalog_hash: str = ""
    model: str = ""
    response_content_count: int = 0
    attachment_count: int = 0
    prompt_measurements: dict[str, Any] = Field(default_factory=dict)
    reasoning_status: str = "not_requested"
    reasoning_effort: str | None = None
    reasoning_summary_preference: str | None = None
    reasoning_summary: str | None = None


class ToolCallRecord(BaseModel):
    call_id: str
    order: int
    role: AgentRole
    invocation_id: str
    llm_round_order: int = 0
    name: str
    category: ToolCategory
    status: ToolStatus
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    # Full redacted AgentToolResult observability payload returned locally.
    local_result: dict[str, Any] = Field(default_factory=dict)
    # Exact compact payload serialized into the provider-wire tool message.
    model_result: dict[str, Any] = Field(default_factory=dict)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    started_at_ms: float = 0.0
    elapsed_ms: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    provider_status: str | None = None
    error: str | None = None
    observation_transition: dict[str, Any] | None = None


@dataclass
class ToolExecutionContext:
    role: AgentRole
    invocation_id: str
    state: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[dict[str, Any], ToolExecutionContext], AgentToolResult | Awaitable[AgentToolResult]]


@dataclass(frozen=True)
class _Registration:
    spec: AgentToolSpec
    handler: ToolHandler


class AgentToolRegistry:
    """Immutable-by-use registry with stable role filtering and hashing."""

    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}

    def register(self, spec: AgentToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._registrations:
            raise ValueError(f"duplicate agent tool: {spec.name}")
        self._registrations[spec.name] = _Registration(spec=spec, handler=handler)

    def specs_for_role(self, role: AgentRole | str) -> tuple[AgentToolSpec, ...]:
        resolved = AgentRole(role)
        return tuple(
            reg.spec
            for name, reg in sorted(self._registrations.items())
            if resolved in reg.spec.roles
        )

    def catalog_for_role(self, role: AgentRole | str) -> list[dict[str, Any]]:
        return [spec.chat_completion_schema() for spec in self.specs_for_role(role)]

    def catalog_hash(self, role: AgentRole | str) -> str:
        return stable_hash(self.catalog_for_role(role))

    def spec(self, name: str, role: AgentRole | str) -> AgentToolSpec:
        reg = self._registrations.get(name)
        resolved = AgentRole(role)
        if reg is None or resolved not in reg.spec.roles:
            raise KeyError(f"unknown tool for {resolved.value}: {name}")
        return reg.spec

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> AgentToolResult:
        try:
            reg = self._registrations[name]
        except KeyError:
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS,
                summary=f"unknown tool: {name}",
                error="unknown_tool",
            )
        if context.role not in reg.spec.roles:
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS,
                summary=f"tool {name} is not available to {context.role.value}",
                error="role_not_allowed",
            )
        try:
            produced = reg.handler(arguments, context)
            if inspect.isawaitable(produced):
                produced = await asyncio.wait_for(produced, reg.spec.timeout_ms / 1000)
            if not isinstance(produced, AgentToolResult):
                raise TypeError(f"handler returned {type(produced).__name__}")
            return produced
        except asyncio.TimeoutError:
            return AgentToolResult(
                status=ToolStatus.TIMEOUT,
                summary=f"{name} timed out after {reg.spec.timeout_ms}ms",
                error="timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return AgentToolResult(
                status=ToolStatus.FAILED,
                summary=f"{name} failed",
                error=str(exc)[:500],
            )


def count_message_images(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"image", "image_url"}:
                count += 1
    return count


def monotonic_ms() -> float:
    return time.monotonic() * 1000
