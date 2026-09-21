"""Deterministic offline measurement for prompt components and request envelopes.

This module is intentionally non-semantic. It measures exact rendered prompt
text and canonical tool-schema JSON with the frozen GPT-5.4/o200k tokenizer,
and it compares exact request envelope structure without inferring task
meaning.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

GPT_5_4_MODEL_ID = "chatgpt/gpt-5.4"
GPT_5_4_TOKENIZER = "o200k_base"


@dataclass(frozen=True)
class PromptComponentMeasurement:
    name: str
    serialization: str
    digest_sha256: str
    char_count: int
    byte_count: int
    token_count: int
    tokenizer: str = GPT_5_4_TOKENIZER

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("serialization", None)
        return payload


def _canonicalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _canonicalize(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON rejects non-finite floats")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical prompt value: {type(value)!r}")


def canonical_component_json(value: Any) -> str:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@lru_cache(maxsize=1)
def _o200k_encoding() -> Any:
    import tiktoken

    return tiktoken.get_encoding(GPT_5_4_TOKENIZER)


def measure_serialized_component(name: str, serialized: str) -> PromptComponentMeasurement:
    utf8 = serialized.encode("utf-8")
    return PromptComponentMeasurement(
        name=name,
        serialization=serialized,
        digest_sha256=hashlib.sha256(utf8).hexdigest(),
        char_count=len(serialized),
        byte_count=len(utf8),
        token_count=len(_o200k_encoding().encode(serialized)),
    )


def measure_text_component(name: str, text: str) -> PromptComponentMeasurement:
    return measure_serialized_component(name, text)


def measure_json_component(name: str, value: Any) -> PromptComponentMeasurement:
    return measure_serialized_component(name, canonical_component_json(value))


def first_difference_path(left: Any, right: Any, path: str = "$") -> str | None:
    """Return the first exact structural divergence path, or None if identical."""
    if isinstance(left, BaseModel):
        left = left.model_dump(mode="json")
    if isinstance(right, BaseModel):
        right = right.model_dump(mode="json")
    if left == right:
        return None
    if type(left) is not type(right):
        return path
    if isinstance(left, dict):
        left_keys = list(left.keys())
        right_keys = list(right.keys())
        limit = min(len(left_keys), len(right_keys))
        for index in range(limit):
            if left_keys[index] != right_keys[index]:
                return f"{path}.{left_keys[index]}"
        if len(left_keys) != len(right_keys):
            extra_index = limit
            if extra_index < len(left_keys):
                return f"{path}.{left_keys[extra_index]}"
            return f"{path}.{right_keys[extra_index]}"
        for key in left_keys:
            child = first_difference_path(left[key], right[key], f"{path}.{key}")
            if child is not None:
                return child
        return path
    if isinstance(left, list):
        limit = min(len(left), len(right))
        for index in range(limit):
            child = first_difference_path(left[index], right[index], f"{path}[{index}]")
            if child is not None:
                return child
        if len(left) != len(right):
            return f"{path}[{limit}]"
        return path
    return path


def build_role_component_report() -> dict[str, Any]:
    from agent.prompts import (
        render_executor_system,
        render_planner_system,
        render_reviewer_system,
    )
    from agent.session import tools_for_role

    planner_role = measure_text_component(
        "planner.role_policy",
        render_planner_system(),
    )
    reviewer_role = measure_text_component(
        "reviewer.role_policy",
        render_reviewer_system(),
    )
    executor_role = measure_text_component(
        "executor.role_policy",
        render_executor_system(),
    )
    planner_tools = measure_json_component(
        "planner.tool_schemas",
        tools_for_role("planner"),
    )
    reviewer_tools = measure_json_component(
        "reviewer.tool_schemas",
        tools_for_role("reviewer"),
    )
    executor_tools = measure_json_component(
        "executor.tool_schemas",
        tools_for_role("executor"),
    )
    return {
        "model": GPT_5_4_MODEL_ID,
        "tokenizer": GPT_5_4_TOKENIZER,
        "roles": {
            "planner": {
                "role_policy": planner_role.as_dict(),
                "tool_schemas": planner_tools.as_dict(),
            },
            "reviewer": {
                "role_policy": reviewer_role.as_dict(),
                "tool_schemas": reviewer_tools.as_dict(),
            },
            "executor": {
                "role_policy": executor_role.as_dict(),
                "tool_schemas": executor_tools.as_dict(),
            },
        },
    }
