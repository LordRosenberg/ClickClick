"""Focused AgentSession protocol, evidence, and recovery tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.prompt_measurement import measure_json_component
from agent.prompts import render_executor_system
from agent.session import (
    AgentSession,
    _catalog_specs,
    _is_evidence_message_name,
    _prompt_measurements,
    delivered_evidence_digest,
    delivered_history_digest,
    tools_for_role,
)
from agent.skills.library import SkillLibrary
from agent.tool_registry import ToolStatus
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import ExecutorDecisionKind


def _library(tmp_path: Path) -> SkillLibrary:
    skill = tmp_path / "generic" / "session"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: session\ndescription: session\nversion: 0.1.0\n"
        "kind: generic\ntags: [test]\n---\n\nSession test skill.\n",
        encoding="utf-8",
    )
    return SkillLibrary(tmp_path)


def _measurement_summary(component) -> dict[str, object]:
    return component.summary_dict()


def test_delivered_evidence_digest_ignores_transport_identity_only() -> None:
    first = [{
        "role": "user",
        "content": [
            {"type": "text", "text": 'INPUT:{"ui_text":"Result 42","observation_id":"O1"}'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
    }]
    churn = [{
        "role": "user",
        "content": [
            {"type": "text", "text": 'INPUT:{"observation_id":"O9","ui_text":"Result 42"}'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ],
    }]
    changed = json.loads(json.dumps(churn))
    changed[0]["content"][0]["text"] = changed[0]["content"][0]["text"].replace(
        "Result 42", "Result 43",
    )

    baseline = delivered_evidence_digest(first, ["observation"])
    assert delivered_evidence_digest(churn, ["observation"]) == baseline
    assert delivered_evidence_digest(changed, ["observation"]) != baseline


def test_history_digest_hashes_only_named_history_messages() -> None:
    history = {"role": "user", "content": "HISTORY:\noldest -> newest"}
    observation = {"role": "user", "content": "OBSERVATION: same"}
    digest = delivered_history_digest(
        [history, observation], ["history", "observation"],
    )
    assert digest != delivered_history_digest(
        [{**history, "content": "HISTORY:\nchanged"}, observation],
        ["history", "observation"],
    )
    assert digest == delivered_history_digest(
        [history, {**observation, "content": "OBSERVATION: changed"}],
        ["history", "observation"],
    )


def test_temporal_history_is_measured_as_observation_evidence() -> None:
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "start visual history"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]},
        {"role": "user", "content": "CURRENT OBSERVATION:\nending"},
    ]
    names = ["observation_temporal_history", "observation"]
    assert _is_evidence_message_name(names[0])
    measurements = _prompt_measurements(
        role="executor",
        model="chatgpt/gpt-5.4",
        tools=tools_for_role("executor"),
        tool_choice="required",
        wire_messages=messages,
        message_names=names,
        context_state={"visual_evidence_metadata": {"estimated_image_tokens": 42}},
    )
    expected = [
        [{"type": "text", "text": "start visual history"}],
        "CURRENT OBSERVATION:\nending",
    ]
    assert measurements["observation_text"] == _measurement_summary(
        measure_json_component("observation_text", expected),
    )
    assert measurements["image_estimate"]["token_count"] == 42


def test_executor_submit_schema_is_one_discriminator_plus_summary() -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    parameters = submit["function"]["parameters"]
    assert list(parameters["properties"]) == ["decision", "summary", "action"]
    assert parameters["required"] == ["decision", "summary"]
    action_types = {
        branch["properties"]["type"]["enum"][0]
        for branch in parameters["properties"]["action"]["oneOf"]
    }
    assert action_types == {
        "tap", "tap_xy", "type", "replace_text", "swipe", "long_press",
        "scroll", "drag", "key", "launch", "back", "home", "sleep",
    }
    assert not {"complete", "remember", "claim", "replan"} & action_types


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "act", "summary": "wait for loading", "action": {"type": "sleep", "duration_ms": 1}},
        {"decision": "request_review", "summary": "the result is visible"},
        {"decision": "request_replan", "summary": "the route cannot continue"},
    ],
)
def test_executor_submit_json_schema_accepts_all_new_decisions(payload: dict) -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    assert not list(
        Draft202012Validator(submit["function"]["parameters"]).iter_errors(payload)
    )


def test_executor_submit_json_schema_rejects_action_on_boundary() -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    errors = list(Draft202012Validator(
        submit["function"]["parameters"],
    ).iter_errors({
        "decision": "request_review",
        "summary": "ready",
        "action": {"type": "sleep", "duration_ms": 1},
    }))
    assert errors


def test_observe_screen_schema_has_current_and_temporal_only() -> None:
    observe = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "observe_screen"
    )
    current, temporal = observe["function"]["parameters"]["oneOf"]
    assert set(current["properties"]) == {"mode"}
    assert set(temporal["properties"]) == {"mode", "frames", "duration_ms"}


def test_observe_screen_timeout_is_shared() -> None:
    for role in ("planner", "executor", "reviewer"):
        observe = next(
            spec for spec in _catalog_specs(role) if spec.name == "observe_screen"
        )
        assert observe.timeout_ms == 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"decision": "act", "summary": "pause", "action": {"type": "sleep", "duration_ms": 1}},
            ExecutorDecisionKind.ACT,
        ),
        (
            {"decision": "request_review", "summary": "result visible"},
            ExecutorDecisionKind.REQUEST_REVIEW,
        ),
        (
            {"decision": "request_replan", "summary": "route blocked"},
            ExecutorDecisionKind.REQUEST_REPLAN,
        ),
    ],
)
async def test_session_registry_accepts_each_executor_decision(
    monkeypatch, tmp_path: Path, payload: dict, expected: ExecutorDecisionKind,
) -> None:
    session = AgentSession("executor", "m", library=_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())

    async def fake_complete(model, messages, **kwargs):
        del model, messages, kwargs
        return GatewayResponse(
            content="",
            model="m",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit",
                name="submit_executor_step",
                arguments=json.dumps(payload),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run([{"role": "user", "content": "OBS"}])
    assert result.decision.decision == expected
    assert result.decision.summary == payload["summary"]
    assert (result.decision.action is not None) == (expected == ExecutorDecisionKind.ACT)


@pytest.mark.asyncio
async def test_invalid_boundary_action_is_corrected_in_same_invocation(
    monkeypatch, tmp_path: Path,
) -> None:
    session = AgentSession(
        "executor", "m", library=_library(tmp_path), max_total_rounds=2,
    )
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())
    calls = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls
        del model, messages, kwargs
        calls += 1
        payload = (
            {
                "decision": "request_review",
                "summary": "result visible",
                "action": {"type": "sleep", "duration_ms": 1},
            }
            if calls == 1
            else {"decision": "request_review", "summary": "result visible"}
        )
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id=f"submit-{calls}",
                name="submit_executor_step",
                arguments=json.dumps(payload),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run([{"role": "user", "content": "OBS"}])
    assert result.decision.decision == ExecutorDecisionKind.REQUEST_REVIEW
    assert [record.status for record in result.tool_calls] == [
        ToolStatus.INVALID_ARGUMENTS, ToolStatus.SUCCEEDED,
    ]


def test_executor_summary_has_no_rejection_only_maximum() -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    assert "maxLength" not in submit["function"]["parameters"]["properties"]["summary"]
