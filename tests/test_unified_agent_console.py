"""Unified AgentSession telemetry exposed by the Console artifacts."""

import json
from pathlib import Path

import pytest

from agent.session import AgentSession
from agent.tool_registry import AgentToolResult
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall


def _planner_payload() -> dict:
    return {
        "mode": "execute",
        "target_requirement_ref": "final_ui_state:1",
        "next_subgoal": "establish the requested next state",
        "completion_contract": {
            "success_conditions": ["the requested next state is established"],
            "disqualifying_clauses": [],
        },
        "plan": ["establish the requested next state"],
    }


@pytest.mark.asyncio
async def test_round_artifacts_are_retrievable_redacted_and_deduplicated(
    monkeypatch, tmp_path: Path,
):
    async def fake_complete(*args, **kwargs):
        return GatewayResponse(
            content="visible assistant text",
            model="test-model",
            stop_reason="tool_calls",
            usage={"input_tokens": 20, "cached_read_tokens": 10, "output_tokens": 4},
            latency_ms=12.5,
            tool_calls=[ToolCall(
                id="submit-1",
                name="submit_planner_decision",
                arguments=json.dumps(_planner_payload()),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    session = AgentSession("planner", "test-model", settings=Settings(gateway_max_retries=0))
    session.set_stable_system("system")
    session.freeze_allow_dirs(["generic"])
    result = await session.run(
        [{"role": "user", "content": [
            {"type": "text", "text": "password=secret"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}],
        observation_message_names=["observation"],
        artifacts=artifacts,
    )

    round_record = result.llm_rounds[0]
    assert round_record.request_ref and round_record.response_ref
    request = json.loads(artifacts.read_text(round_record.request_ref))
    response = json.loads(artifacts.read_text(round_record.response_ref))
    assert request["round_id"] == response["round_id"] == round_record.round_id
    assert request["messages"][-1]["content"][1]["attachment_metadata"]["binary"] == "omitted"
    assert [section["name"] for section in request["message_sections"]] == [
        "system", "observation",
    ]
    assert "AAAA" not in artifacts.read_text(round_record.request_ref)
    assert response["content"] == "visible assistant text"
    assert response["private_reasoning"] == "not_persisted"
    assert response["usage"]["cached_read_tokens"] == 10
    assert result.tool_calls[0].local_result["summary"] == "accepted"
    assert result.tool_calls[0].model_result == {"status": "succeeded", "summary": "accepted"}
    assert artifacts.save_json("llm", response) == round_record.response_ref
    artifacts.save_json("llm", {"older": True})
    assert artifacts.prune("llm", max_files=1) == 2


@pytest.mark.asyncio
async def test_next_round_records_exact_named_tool_messages(monkeypatch, tmp_path: Path):
    calls = 0

    async def fake_complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return GatewayResponse(
                content="need current screen", model="test-model",
                stop_reason="tool_calls", usage={}, latency_ms=1,
                tool_calls=[ToolCall(
                    id="observe-1", name="observe_screen",
                    arguments=json.dumps({"mode": "current"}),
                )],
            )
        return GatewayResponse(
            content="done", model="test-model", stop_reason="tool_calls",
            usage={}, latency_ms=1,
            tool_calls=[ToolCall(
                id="submit-1", name="submit_planner_decision",
                arguments=json.dumps(_planner_payload()),
            )],
        )

    async def observe_screen(args, context):
        del args, context
        return AgentToolResult(summary="fresh screen", data={"observation_id": "O2"})

    monkeypatch.setattr("agent.session.complete", fake_complete)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    session = AgentSession("planner", "test-model", settings=Settings(gateway_max_retries=0))
    session.set_stable_system("system")
    session.freeze_allow_dirs(["generic"])
    result = await session.run(
        [{"role": "user", "content": "observation O1"}],
        observation_message_names=["observation"],
        handlers={"observe_screen": observe_screen},
        artifacts=artifacts,
        context_state={
            "active_package": object(),
            "render_observation_bucket": lambda _package: (
                [{"role": "user", "content": "CURRENT OBSERVATION:\n{}"}],
                ["observation"],
            ),
        },
    )

    assert result.tool_calls[0].model_result == {
        "status": "succeeded",
        "summary": "Current screen evidence is replaced by the following observation.",
    }
    assert result.tool_calls[0].local_result["data"] == {"observation_id": "O2"}
    second_request = json.loads(artifacts.read_text(result.llm_rounds[1].request_ref))
    by_name = {
        section["name"]: second_request["messages"][section["message_index"]]
        for section in second_request["message_sections"]
    }
    assert by_name["assistant_tools"]["tool_calls"][0]["function"]["name"] == "observe_screen"
    assert json.loads(by_name["model_result[observe_screen]"]["content"]) == result.tool_calls[0].model_result


def test_artifact_path_authorization(tmp_path: Path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        artifacts.resolve("../../outside")
