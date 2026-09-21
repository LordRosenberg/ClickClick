from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.prompt_measurement import (
    build_role_component_report,
    first_difference_path,
    measure_json_component,
    measure_serialized_component,
    measure_text_component,
)
from agent.session import tools_for_role
from shared.config import Settings
from shared.llm_gateway import _build_completion_kwargs
from shared.schemas import ExecutorStepSubmit


def test_role_report_contains_only_supported_roles_and_is_deterministic():
    report = build_role_component_report()
    assert set(report["roles"]) == {"planner", "reviewer", "executor"}
    assert report == build_role_component_report()
    for role in report["roles"].values():
        for measured in role.values():
            assert measured["token_count"] > 0
            assert measured["tokenizer"] == "o200k_base"





def test_json_component_measurement_is_canonical_across_key_orderings():
    first = measure_json_component(
        "component",
        {"b": 1, "a": [{"y": True, "x": 3}, {"x": 4, "y": False}]},
    )
    second = measure_json_component(
        "component",
        {"a": [{"x": 3, "y": True}, {"y": False, "x": 4}], "b": 1},
    )
    assert first == second


def test_executor_submit_source_property_order_reaches_gateway():
    source = ExecutorStepSubmit.model_json_schema()
    assert list(source["properties"]) == [
        "decision", "summary", "action",
    ]
    assert source["required"] == ["decision", "summary"]

    tools = tools_for_role("executor")
    submit = next(
        tool for tool in tools
        if tool["function"]["name"] == "submit_executor_step"
    )
    parameters = submit["function"]["parameters"]
    assert list(parameters["properties"]) == [
        "decision", "summary", "action",
    ]
    assert parameters["required"] == ["decision", "summary"]
    assert "strict" not in submit["function"]

    kwargs = _build_completion_kwargs(
        "chatgpt/gpt-5.4",
        [{"role": "user", "content": "test"}],
        response_format=None,
        tools=tools,
        tool_choice="required",
        settings=Settings(models_json=json.dumps({
            "chatgpt/gpt-5.4": {"provider": "chatgpt"},
        })),
    )
    delivered = next(
        tool for tool in kwargs["tools"]
        if tool["function"]["name"] == "submit_executor_step"
    )
    assert list(delivered["function"]["parameters"]["properties"]) == [
        "decision", "summary", "action",
    ]
    wire = json.dumps(delivered, ensure_ascii=False, separators=(",", ":"))
    measured = measure_serialized_component("executor_submit_wire", wire)
    assert measured.token_count > 0
    assert delivered == submit



@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("openai/deepseek-v4-flash-vision-exp", "openai"),
        ("gpt-5.4", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
    ],
)
@pytest.mark.parametrize(
    "role", ["planner", "reviewer", "executor"],
)
def test_gateway_forwards_static_role_catalog_unchanged(
    model: str, provider: str, role: str,
) -> None:
    tools = tools_for_role(role)
    kwargs = _build_completion_kwargs(
        model,
        [{"role": "user", "content": "test"}],
        response_format=None,
        tools=tools,
        tool_choice="required",
        settings=Settings(models_json=json.dumps({
            model: {"provider": provider},
        })),
    )
    assert kwargs["tools"] == tools


def test_chatgpt_request_prefix_diverges_at_planner_system_before_tool_schemas():
    settings = Settings(models_json=json.dumps({
        "chatgpt/gpt-5.4": {"provider": "chatgpt"},
    }))
    planner_tools = tools_for_role("planner")
    first = _build_completion_kwargs(
        "chatgpt/gpt-5.4",
        [
            {"role": "system", "content": "SYSTEM A"},
            {"role": "user", "content": "OBS"},
        ],
        response_format=None,
        tools=planner_tools,
        tool_choice="required",
        settings=settings,
    )
    second = _build_completion_kwargs(
        "chatgpt/gpt-5.4",
        [
            {"role": "system", "content": "SYSTEM B"},
            {"role": "user", "content": "OBS"},
        ],
        response_format=None,
        tools=planner_tools,
        tool_choice="required",
        settings=settings,
    )
    assert list(first.keys()) == ["model", "messages", "timeout", "tools", "tool_choice"]
    assert list(second.keys()) == list(first.keys())
    assert first_difference_path(first, second) == "$.messages[0].content"
