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
from agent.tool_registry import AgentToolResult, ToolStatus, stable_json
from shared.llm_gateway import GatewayError, GatewayResponse, ToolCall
from shared.schemas import AgentState, ExecutorDecisionKind


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


def test_sequence_history_is_measured_as_observation_evidence() -> None:
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": "start visual history"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
        ]},
        {"role": "user", "content": "CURRENT OBSERVATION:\nending"},
    ]
    names = ["observation_sequence_history", "observation"]
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


def test_executor_submit_schema_is_flat_portable_action_superset() -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    parameters = submit["function"]["parameters"]
    assert list(parameters["properties"]) == [
        "decision", "summary", "action",
    ]
    assert parameters["required"] == ["decision", "summary"]
    assert "one very short action intent" in parameters["properties"]["summary"]["description"]
    action = parameters["properties"]["action"]
    assert action["required"] == ["type"]
    assert action["additionalProperties"] is False
    action_types = set(action["properties"]["type"]["enum"])
    assert action_types == {
        "tap", "tap_xy", "skill_authorized_action", "type", "replace_text", "swipe", "long_press",
        "scroll", "drag", "key", "launch", "back", "home", "sleep",
    }
    assert not {"complete", "remember", "claim", "replan"} & action_types
    assert action["properties"]["key"]["enum"] == [
        "back", "home", "enter", "menu", "delete", "search", "volume_up",
        "volume_down", "power", "media_pause",
    ]


def test_executor_action_schema_rejects_key_chords() -> None:
    from agent.session import executor_action_variants_schema

    validate = Draft202012Validator(executor_action_variants_schema())
    assert validate.is_valid({"type": "key", "key": "delete"})
    assert not validate.is_valid({"type": "key", "key": "CTRL+A"})


def test_skill_authorized_action_schema_is_closed_and_has_one_target() -> None:
    from jsonschema import Draft202012Validator

    from agent.session import executor_action_variants_schema

    schema = executor_action_variants_schema()
    validate = Draft202012Validator(schema)
    assert validate.is_valid({
        "type": "skill_authorized_action",
        "skill_action_id": "demo.inspect",
        "index": 3,
    })
    assert validate.is_valid({
        "type": "skill_authorized_action",
        "skill_action_id": "demo.inspect",
        "x": 12,
        "y": 34,
    })
    rejected = [
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect"},
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect", "x": 12},
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect", "index": 3, "x": 12, "y": 34},
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect", "index": 3, "sequence": ["tap", "back"]},
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect", "index": 3, "key": "back"},
        {"type": "skill_authorized_action", "skill_action_id": "demo.inspect", "index": 3, "template": "tap_capture_key"},
    ]
    assert all(not validate.is_valid(item) for item in rejected)


def test_pixel_verification_prompt_and_schema_share_one_shortlist_contract() -> None:
    prompt = render_executor_system()
    tool = next(
        item["function"] for item in tools_for_role("executor")
        if item["function"]["name"] == "inspect_image_regions"
    )

    assert prompt.count("Use `inspect_image_regions` only to verify") == 1
    assert "whole relevant candidate group" in prompt
    assert "page-wide search" in prompt
    assert "Available Colors" not in prompt
    assert tool["parameters"]["properties"]["regions"]["maxItems"] == 32
    assert tool["parameters"]["properties"]["pairs"]["maxItems"] == 6
    assert "compare" in tool["parameters"]["properties"]
    assert tool["parameters"]["required"] == ["observation_id"]


def test_skill_tool_descriptions_do_not_promise_missing_app_knowledge() -> None:
    planner_tools = {
        tool["function"]["name"]: tool["function"]
        for tool in tools_for_role("planner")
    }
    load_description = planner_tools["load_skill"]["parameters"]["properties"][
        "skill_id"
    ]["description"]

    assert "search_skills" not in planner_tools
    assert "generic Skill index" in load_description
    assert "load_skill" in {
        tool["function"]["name"] for tool in tools_for_role("executor")
    }


@pytest.mark.asyncio
async def test_group_comparison_passes_session_scope_and_keeps_diagnostics_local(monkeypatch, tmp_path):
    session = AgentSession("executor", "m", library=_library(tmp_path), max_total_rounds=2)
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())
    args = {
        "observation_id": "obs",
        "targets": [{"index": i} for i in range(24)],
        "regions": [{"bounds": [0, 0, 1, 1]} for _ in range(3)],
        "compare": {"references": "all_regions", "candidates": "all_targets"},
    }
    schema = next(t["function"]["parameters"] for t in tools_for_role("executor")
                  if t["function"]["name"] == "inspect_image_regions")
    Draft202012Validator(schema).validate(args)
    assert not Draft202012Validator(schema).is_valid({"observation_id": "obs"})
    rounds = 0
    executed = 0

    async def inspect(received, context):
        nonlocal executed
        assert received == args
        executed += 1
        return AgentToolResult(data={"comparison": {"chosen": "index:15"}},
                               diagnostics={"private_matrix": [[1, 2, 3]]})

    async def fake_complete(model, messages, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            call = ToolCall(id="inspect", name="inspect_image_regions", arguments=json.dumps(args))
        else:
            assert "private_matrix" not in str(messages)
            assert "index:15" in str(messages)
            call = ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps({
                "decision": "request_review", "summary": "comparison complete",
            }))
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[call])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run([{"role": "user", "content": "OBS"}],
                               handlers={"inspect_image_regions": inspect})
    assert executed == 1
    assert result.tool_calls[0].status == ToolStatus.SUCCEEDED
    assert result.tool_calls[0].local_result["diagnostics"]["private_matrix"] == [[1, 2, 3]]
    assert "diagnostics" not in result.tool_calls[0].model_result


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


def test_executor_submit_json_schema_leaves_boundary_rules_to_harness() -> None:
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
    assert not errors


def test_observe_screen_schema_has_snapshot_and_sequence_only() -> None:
    observe = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "observe_screen"
    )
    parameters = observe["function"]["parameters"]
    assert parameters["properties"]["mode"]["enum"] == ["snapshot", "sequence"]
    assert set(parameters["properties"]) == {"mode", "frames", "duration_ms"}
    assert parameters["required"] == ["mode"]
    assert parameters["additionalProperties"] is False


def test_observe_screen_timeout_is_shared() -> None:
    for role in ("planner", "executor", "reviewer"):
        observe = next(
            spec for spec in _catalog_specs(role) if spec.name == "observe_screen"
        )
        assert observe.timeout_ms == 10_000


def test_role_session_retains_general_protocol_recovery_budget(tmp_path: Path) -> None:
    session = AgentSession("executor", "m", library=_library(tmp_path))
    assert session.max_total_rounds == 8


@pytest.mark.asyncio
async def test_planner_receives_stable_generic_skill_index(
    monkeypatch, tmp_path: Path,
) -> None:
    role = "planner"
    session = AgentSession(role, "m", library=_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    captured: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        del model, kwargs
        captured.append(messages)
        if role == "planner":
            arguments = {
                "decision": "review",
                "reason": "Inspect the current evidence",
            }
            name = "submit_planner_decision"
        else:
            arguments = {
                "decision": "request_review",
                "summary": "Result is visible",
            }
            name = "submit_executor_step"
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit", name=name, arguments=json.dumps(arguments),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    if role == "planner":
        context_state = {
            "planner_criteria": {"final_ui_state:1": "Results remain visible"},
            "planner_criterion_categories": {
                "final_ui_state:1": "final_ui_state",
            },
        }
    else:
        context_state = {}
    await session.run(
        [{"role": "user", "content": "OBS"}],
        context_state=context_state,
    )

    assert captured[0][0]["content"] == "S\n"
    assert captured[0][1]["content"].startswith("GENERIC SKILL INDEX")
    assert "session — kind=generic" in captured[0][1]["content"]
    assert captured[0][2]["content"] == "OBS"


@pytest.mark.asyncio
async def test_executor_receives_no_generic_index(monkeypatch, tmp_path: Path) -> None:
    session = AgentSession("executor", "m", library=_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    captured: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        del model, kwargs
        captured.append(messages)
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit", name="submit_executor_step", arguments=json.dumps({
                    "decision": "request_review",
                        "summary": "Result is visible",
                }),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    await session.run([{"role": "user", "content": "OBS"}])
    assert [message["content"] for message in captured[0][:2]] == ["S\n", "OBS"]
    assert all(
        "GENERIC SKILL INDEX" not in str(message.get("content") or "")
        for message in captured[0]
    )


@pytest.mark.asyncio
async def test_session_projects_safe_attempt_scoped_stream_events(
    monkeypatch, tmp_path: Path,
) -> None:
    session = AgentSession("executor", "m", library=_library(tmp_path))
    session.reset_lifecycle("task")
    session.set_stable_system("S")
    events: list[tuple[str, dict]] = []

    async def fake_complete(model, messages, **kwargs):
        del model, messages
        await kwargs["stream_sink"]({
            "attempt": 2, "sequence": 7, "status": "streaming",
            "text": "latest output", "summary": "private detail",
            "tool_call_count": 1, "arguments": '{"secret":"never emit"}',
        })
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit", name="submit_executor_step", arguments=json.dumps({
                    "decision": "request_review",
                        "summary": "Result is visible",
                }),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    await session.run(
        [{"role": "user", "content": "OBS"}],
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    live = next(payload for kind, payload in events if kind == "agent_llm_stream")
    assert live["attempt"] == 2
    assert live["sequence"] == 7
    assert live["text"] == "latest output"
    assert "summary" not in live
    assert "arguments" not in live


def test_reviewer_submit_schema_uses_current_decision_and_source_fields():
    from shared.revisable import Review
    from agent.revisable.tools import tool_schema
    tool = next(t for t in tools_for_role("reviewer") if t["function"]["name"] == "submit_reviewer_decision")
    assert tool["function"]["parameters"] == tool_schema(Review)
    assert "source_refs" in tool["function"]["parameters"]["properties"]
    assert "reason_proofs" not in tool["function"]["parameters"]["properties"]


def test_every_model_visible_tool_schema_has_closed_objects_and_valid_references():
    import jsonschema
    for role in ("planner", "reviewer", "executor"):
        for tool in tools_for_role(role):
            schema = tool["function"]["parameters"]
            jsonschema.Draft202012Validator.check_schema(schema)
            def check(node):
                if isinstance(node, dict):
                    if node.get("type") == "object":
                        assert node.get("additionalProperties") is False
                        assert isinstance(node.get("required"), list)
                    if "$ref" in node:
                        assert node["$ref"].removeprefix("#/$defs/") in schema["$defs"]
                    for child in node.values(): check(child)
                elif isinstance(node, list):
                    for child in node: check(child)
            check(schema)


@pytest.mark.parametrize("role", ["planner", "reviewer", "executor"])
def test_role_tool_catalog_is_byte_stable(role: str) -> None:
    assert stable_json(tools_for_role(role)) == stable_json(tools_for_role(role))


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
async def test_round_start_preserves_image_binding_before_budget_failure(
    monkeypatch, tmp_path: Path,
) -> None:
    session = AgentSession("executor", "m", library=_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())
    events: list[tuple[str, dict]] = []

    async def fake_complete(model, messages, **kwargs):
        del model, messages, kwargs
        raise GatewayError("provider budget exhausted", category="budget")

    async def event_sink(kind, payload):
        events.append((kind, payload))

    monkeypatch.setattr("agent.session.complete", fake_complete)
    with pytest.raises(GatewayError, match="budget exhausted"):
        await session.run(
            [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ]}],
            context_state={"visual_evidence_metadata": {
                "observation_id": "obs-budget",
                "image_artifact_ref": "model-images/budget.png",
                "visual_kind": "som",
                "captured_monotonic_ms": 9.0,
            }},
            event_sink=event_sink,
        )

    started = next(payload for kind, payload in events if kind == "agent_llm_round_started")
    assert started["input_observation_id"] == "obs-budget"
    assert started["input_model_image_ref"] == "model-images/budget.png"


@pytest.mark.asyncio
async def test_broad_parallel_region_inspection_is_atomic_and_cannot_loop(
    monkeypatch, tmp_path: Path,
) -> None:
    session = AgentSession(
        "executor", "m", library=_library(tmp_path), max_total_rounds=3,
    )
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())
    rounds = 0
    executed = 0

    async def inspect(_args, _context):
        nonlocal executed
        executed += 1
        return AgentToolResult(summary="should not execute")

    async def fake_complete(model, messages, **kwargs):
        nonlocal rounds
        del model, kwargs
        rounds += 1
        if rounds < 3:
            calls = [
                ToolCall(
                    id=f"inspect-{rounds}-{index}",
                    name="inspect_image_regions",
                    arguments=json.dumps({
                        "observation_id": "obs",
                        "regions": [{"bounds": [0, 0, 1, 1]}],
                        "metrics": ["median_rgb"],
                    }),
                )
                for index in range(2)
            ]
        else:
            assert "evidence gathering is closed" in str(messages).lower()
            calls = [ToolCall(
                id="submit",
                name="submit_executor_step",
                arguments=json.dumps({
                    "decision": "request_replan",
                    "summary": "exact pixels cannot be grounded from the shortlist",
                }),
            )]
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls", tool_calls=calls,
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "OBS"}],
        handlers={"inspect_image_regions": inspect},
    )

    assert executed == 0
    assert result.decision.decision == ExecutorDecisionKind.REQUEST_REPLAN
    assert [record.status for record in result.tool_calls[:-1]] == [
        ToolStatus.PRECONDITION_NOT_MET,
        ToolStatus.PRECONDITION_NOT_MET,
        ToolStatus.PRECONDITION_NOT_MET,
        ToolStatus.PRECONDITION_NOT_MET,
    ]
    assert result.tool_calls[2].error == "repeated_image_region_scope_violation"


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


@pytest.mark.asyncio
async def test_retired_contract_fields_are_rejected():
    from shared.schemas import ExecutorStepSubmit
    from pydantic import ValidationError
    for field, value in (("review_reason", "boundary_ready"), ("triggered_condition_refs", ["condition:1"])):
        with pytest.raises(ValidationError):
            ExecutorStepSubmit.model_validate({"decision": "request_review", "summary": "ready", field: value})


def test_executor_summary_has_no_rejection_only_maximum() -> None:
    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    assert "maxLength" not in submit["function"]["parameters"]["properties"]["summary"]
