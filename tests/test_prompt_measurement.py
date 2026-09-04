from __future__ import annotations

import json
from pathlib import Path

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


def test_tool_schema_component_snapshot_matches_current_catalogs():
    report = build_role_component_report()
    expected = {
        ("planner", "role_policy"): (401, "ee20591153ec37b4de1641f904ec8ccd20ee8c8eb8889f246666f618bdbd6085"),
        ("planner", "tool_schemas"): (764, "a65d4a3dcf759364c605931c9a3841720ec9ef780a64cea1a96b0985e55f38b1"),
        ("reviewer", "role_policy"): (731, "c5dfe691db0819fe75ead550d9c54bb7c56ff6c2e3ab2e00bc8de6176a7b0de2"),
        ("reviewer", "tool_schemas"): (667, "f79f5231ae0a3aa00e4e2cc5a6e7b987685bb539ec96c55ef5f5e6e0ee6534f1"),
        ("reviewer_scope", "role_policy"): (544, "669c0f16db918749416a8e60e2999f8823fce38919b3fef46cfb51ead78b3fc2"),
        ("reviewer_scope", "tool_schemas"): (135, "f976992c634f59f9c2c87c4d6212decbe1991c1cb44d0f37f78ba67d26296c83"),
        ("executor", "role_policy"): (487, "7daac2003b73f3230e7010ee64c3a46761a5fd0763e4a63a95eea169f9201215"),
        ("executor", "tool_schemas"): (1401, "5bd4347a9ec7d409491562f1c5b781e8deb64acdc0e12797071f3a8814b69b22"),
    }
    for (role, component), (tokens, digest) in expected.items():
        measured = report["roles"][role][component]
        assert measured["token_count"] == tokens
        assert measured["digest_sha256"] == digest
        assert measured["tokenizer"] == "o200k_base"


def test_executor_delete_only_candidate_has_only_the_frozen_removals():
    baseline = Path("agent/prompts/executor_system.md").read_text(encoding="utf-8")
    candidate = Path(
        "evaluation/prompt_variants/executor_stable_delete_only_v1.md"
    ).read_text(encoding="utf-8")
    assert candidate != baseline
    measurement = measure_text_component("executor.delete_only", candidate)
    assert measurement.token_count == 1064
    assert measurement.digest_sha256 == (
        "0ecdd645064aacb9e82b381f8836b47e9a051b0e8a8b1a9100dafbb803a5675c"
    )


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
    assert list(source["properties"]) == ["decision", "summary", "action"]
    assert source["required"] == ["decision", "summary"]

    tools = tools_for_role("executor")
    submit = next(
        tool for tool in tools
        if tool["function"]["name"] == "submit_executor_step"
    )
    parameters = submit["function"]["parameters"]
    assert list(parameters["properties"]) == ["decision", "summary", "action"]
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
    assert measured.token_count == 1002
    assert measured.char_count == 4243
    assert measured.digest_sha256 == (
        "19e1b57a121789fe6d121694733a9f9765156357c9207fb420f6d25275927d30"
    )


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
