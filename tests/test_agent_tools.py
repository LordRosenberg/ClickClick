from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image

from agent.decision_role import MutableText
from agent.read_tools import (
    TemporalObservationResult,
    _baseline_eligible,
    _prepare_role_model_image,
    _register_package,
    make_observe_screen_handler,
)
from agent.executor import Executor
from agent.session import AgentSession, _attachments_message, _redact_tool_arguments, tools_for_role
from agent.skills.library import SkillLibrary
from agent.tool_registry import (
    AgentRole,
    AgentToolRegistry,
    AgentToolResult,
    AgentToolSpec,
    EvidenceRecord,
    ToolAttachment,
    ToolCategory,
    ToolExecutionContext,
    ToolStatus,
    redact_value,
    stable_json,
)
from perception.observation import ObservationBuilder, ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall, normalize_usage
from shared.schemas import ActionResult, AgentState, CanonicalUI, FocusedElementEvidence, InteractionStateEvidence, ObservationMode, UIElement


def _png(path: Path) -> bytes:
    Image.new("RGB", (32, 64), "white").save(path)
    return path.read_bytes()


def _empty_library(tmp_path: Path) -> SkillLibrary:
    (tmp_path / "generic").mkdir(parents=True, exist_ok=True)
    return SkillLibrary(tmp_path)


def _executor_payload(action: dict, act: str, **overrides) -> dict:
    payload = {
        "decision": "act",
        "action": action,
        "summary": act,
    }
    payload.update(overrides)
    return payload


def test_role_catalog_is_stable_and_contains_only_read_or_terminal_tools():
    first = stable_json(tools_for_role("executor"))
    second = stable_json(tools_for_role("executor"))
    assert first == second
    assert [tool["function"]["name"] for tool in tools_for_role("executor")] == [
        "inspect_image_regions",
        "load_skill",
        "observe_screen",
        "search_installed_apps",
        "submit_executor_step",
    ]
    assert not any(
        tool["function"]["name"] in {"tap", "swipe", "launch", "back"}
        for tool in tools_for_role("executor")
    )


def test_model_tool_projection_omits_observability_noise():
    result = AgentToolResult(
        status=ToolStatus.TIMEOUT,
        summary="snapshot observation failed: /opt/homebrew/bin/adb -s device screencap",
        data={
            "mode": "snapshot", "status": "timeout", "failed_stage": "screencap",
            "observation_id": "diagnostic-observation",
            "provider_state": {"device": "device", "ring": {"frame_count": 0}},
            "stage_timings": [{"stage": "collector", "elapsed_ms": 1200}],
            "elapsed_ms": 2100, "remaining_ms": 400, "frame_count": 0,
        },
        provider_status="failed:screencap",
        evidence_refs=["diagnostic:capture-timeout"],
        artifact_refs=["trees/internal.json"],
        actionable_observation_id="diagnostic-observation",
        error="adb command failed with private provider detail",
    )

    wire = result.model_metadata("observe_screen")

    assert wire == {
        "status": "timeout",
        "summary": "snapshot observation timeout at screencap",
        "data": {"mode": "snapshot", "status": "timeout", "failed_stage": "screencap"},
    }
    assert "provider_state" not in str(wire)
    # Full trace/console metadata is intentionally unaffected.
    assert result.metadata()["data"]["provider_state"]["device"] == "device"


@pytest.mark.parametrize("status", [ToolStatus.PRECONDITION_NOT_MET, ToolStatus.INVALID_ARGUMENTS])
@pytest.mark.parametrize("recoverable", [True, False])
def test_observation_protocol_feedback_preserves_recovery_without_diagnostics(status, recoverable):
    result = AgentToolResult(
        status=status,
        summary="observe_screen already succeeded; submit the role decision now",
        data={"reason": "observe_screen_already_succeeded", "recoverable": recoverable,
              "provider_state": {"private": "diagnostic"}},
        error="observe_screen_already_succeeded",
        actionable_observation_id="stale-observation",
        evidence_refs=["stale-evidence"],
    )
    wire = result.model_metadata("observe_screen")
    assert wire == {
        "status": status.value,
        "summary": "observe_screen already succeeded; submit the role decision now",
        "data": {"reason": "observe_screen_already_succeeded", "recoverable": recoverable},
        "error": "observe_screen_already_succeeded",
    }


def test_generated_submit_schema_drops_titles_defaults_and_model_docstrings():
    schema = next(
        tool["function"]["parameters"] for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )

    def keys(value):
        if isinstance(value, dict):
            yield from value.keys()
            for item in value.values():
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    all_keys = set(keys(schema))
    assert "title" not in all_keys
    assert "default" not in all_keys
    serialized = stable_json(tools_for_role("executor"))
    # Keep the focused Executor catalog bounded and free of removed witness schema.
    assert len(serialized) < 8_000
    assert "CompletionWitnessSubmit" not in serialized
    assert "completion_witness" not in serialized


def test_registry_rejects_duplicates_and_unknown_role_tool():
    registry = AgentToolRegistry()
    spec = AgentToolSpec(
        name="x", description="x", category=ToolCategory.KNOWLEDGE,
        roles=(AgentRole.PLANNER,), parameters={"type": "object"},
    )
    registry.register(spec, lambda a, c: AgentToolResult())
    with pytest.raises(ValueError):
        registry.register(spec, lambda a, c: AgentToolResult())
    with pytest.raises(KeyError):
        registry.spec("x", AgentRole.EXECUTOR)


def test_redaction_never_inlines_secret_or_binary():
    redacted = redact_value({
        "password": "hunter2", "api_token": "abc", "image": b"\x00" * 20,
        "url": "data:image/png;base64,AAAA",
    })
    text = json.dumps(redacted)
    assert "hunter2" not in text and "abc" not in text and "AAAA" not in text
    assert "REDACTED" in text and "binary:20" in text


def test_password_type_submit_is_redacted_from_generic_tool_trace():
    package = ObservationPackage(
        ui=CanonicalUI(elements=[UIElement(index=2, states={"password": True})]),
        mode=ObservationMode.TREE_ONLY, text_for_llm="", image_for_llm=None,
        annotated_png=None, gap_reasons=[], interaction_state=InteractionStateEvidence(
            focused_element=FocusedElementEvidence(index=2),
            focused_editable=FocusedElementEvidence(index=2),
        ),
    )
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR, invocation_id="i", state={"active_package": package},
    )
    redacted = _redact_tool_arguments(
        "submit_executor_step", {"action": {"type": "type", "text": "super-secret"}}, context,
    )
    assert "super-secret" not in json.dumps(redacted)
    assert redacted["action"]["text"] == "[REDACTED]"


@pytest.mark.parametrize(
    "role", [AgentRole.PLANNER, AgentRole.REVIEWER, AgentRole.EXECUTOR],
)
def test_in_role_observation_invalid_image_fails_closed(monkeypatch, role):
    package = ObservationPackage(
        ui=CanonicalUI(elements=[UIElement(index=1, text="ready")]),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=b"invalid-image",
        clean_png=b"invalid-image",
        annotated_png=None,
        gap_reasons=[],
        index_actionable=True,
    )
    context = ToolExecutionContext(
        role=role,
        invocation_id="invalid-role-image",
        state={"model": "chatgpt/gpt-5.4"},
    )
    monkeypatch.setattr(
        "agent.read_tools.compress_for_model",
        lambda *args, **kwargs: (b"invalid-image", (0, 0), (0, 0)),
    )
    monkeypatch.setattr(
        "agent.read_tools.prepare_som_for_model",
        lambda *args, **kwargs: (b"invalid-image", (0, 0), (0, 0)),
    )

    metadata = _prepare_role_model_image(context, package)

    assert package.image_for_llm is None
    assert package.model_image_width == 0
    assert package.model_image_height == 0
    assert package.mode == ObservationMode.TREE_ONLY
    assert metadata["image_delivered"] is False
    assert "visual_evidence_metadata" not in context.state


@pytest.mark.parametrize(
    "role", [AgentRole.PLANNER, AgentRole.REVIEWER, AgentRole.EXECUTOR],
)
def test_trace_only_annotation_never_becomes_role_image(tmp_path: Path, role):
    png = _png(tmp_path / f"trace-{role.value}.png")
    package = ObservationPackage(
        ui=CanonicalUI(),
        mode=ObservationMode.IMAGE_ONLY,
        text_for_llm="tree unavailable",
        image_for_llm=None,
        clean_png=None,
        annotated_png=png,
        gap_reasons=["tree_unusable"],
        frame_width=32,
        frame_height=64,
        actionable=True,
        index_actionable=False,
    )
    context = ToolExecutionContext(
        role=role,
        invocation_id="trace-only",
        state={"model": "chatgpt/gpt-5.4"},
    )

    metadata = _prepare_role_model_image(context, package)

    assert package.image_for_llm is None
    assert package.accepted is False
    assert package.actionable is False
    assert package.acceptance_reason == "role_image_unavailable"
    assert metadata["image_delivered"] is False
    assert _baseline_eligible(
        package, driver=object(), generation=0, max_age_ms=10_000,
    ) == (False, "incomplete")


@pytest.mark.asyncio
async def test_temporal_trace_only_frames_are_unavailable_and_preserve_prior_o(
    tmp_path: Path,
):
    png = _png(tmp_path / "trace-temporal.png")

    def trace_only(observation_id: str) -> ObservationPackage:
        return ObservationPackage(
            ui=CanonicalUI(app_id="com.example"),
            mode=ObservationMode.IMAGE_ONLY,
            text_for_llm="tree unavailable",
            image_for_llm=None,
            clean_png=None,
            annotated_png=png,
            gap_reasons=["tree_unusable"],
            observation_id=observation_id,
            frame_width=32,
            frame_height=64,
            actionable=True,
            index_actionable=False,
        )

    start = trace_only("obs-trace-start")
    ending = trace_only("obs-trace-end")

    class Driver:
        async def observe_temporal(self, request):
            del request
            return TemporalObservationResult(
                status="complete", frames=[start, ending], ending=ending,
                provider="test", provider_status="complete",
            )

    prior = ObservationPackage(
        ui=CanonicalUI(app_id="com.example"),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="prior",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        observation_id="obs-prior",
        accepted=True,
    )
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR,
        invocation_id="trace-only-temporal",
        state={"active_package": prior, "model": "chatgpt/gpt-5.4"},
    )
    handler = make_observe_screen_handler(
        driver=Driver(), builder=ObservationBuilder(), artifacts=None,
        baseline_package=prior,
    )

    result = await handler({"mode": "sequence"}, context)

    assert result.status == ToolStatus.UNAVAILABLE
    assert result.replacement_attachments == []
    assert result.actionable_observation_id is None
    assert context.state["active_package"].observation_id == "obs-prior"
    assert "visual_evidence_metadata" not in context.state


def test_actionable_temporal_frames_bypass_contact_sheet_coordinate_rewrite(tmp_path: Path):
    png = _png(tmp_path / "sheet.png")
    message = _attachments_message("observe_screen", [
        ToolAttachment(label="start", content=png),
        ToolAttachment(label="end", content=png, actionable_coordinate_reference=True),
    ], contact_sheet=True)
    assert message is not None
    content = message["content"]
    assert sum(block.get("type") == "image_url" for block in content) == 2
    text = " ".join(block.get("text", "") for block in content)
    assert "start" in text and "end" in text and "contact sheet" not in text


@pytest.mark.asyncio
async def test_registry_timeout_is_a_typed_soft_failure():
    registry = AgentToolRegistry()
    spec = AgentToolSpec(
        name="slow", description="slow", category=ToolCategory.OBSERVATION,
        roles=(AgentRole.EXECUTOR,), parameters={"type": "object"}, timeout_ms=1,
    )

    async def slow(args, context):
        await asyncio.sleep(0.02)
        return AgentToolResult()

    registry.register(spec, slow)
    result = await registry.execute(
        "slow", {}, ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )
    assert result.status == ToolStatus.TIMEOUT


@pytest.mark.asyncio
async def test_terminal_submit_remains_available_after_read_tool_failure(monkeypatch, tmp_path: Path):
    session = AgentSession("executor", "m", library=_empty_library(tmp_path))
    session.reset_lifecycle("t")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    call_count = 0

    async def failed(args, context):
        return AgentToolResult(status=ToolStatus.FAILED, summary="provider rejected", error="rejected")

    async def fake_complete(model, messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="o", name="observe_screen", arguments='{"mode":"snapshot"}'),
            ])
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "bounded fallback",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}], handlers={"observe_screen": failed},
    )
    assert result.decision.action and result.decision.action.type == "sleep"
    assert result.tool_calls[0].status == ToolStatus.FAILED
    assert result.tool_calls[-1].status == ToolStatus.SUCCEEDED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}, True),
        ({"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 0}, False),
        ({"input_tokens": 100, "output_tokens": 10}, None),
    ],
)
def test_usage_cache_hit_is_tristate(raw, expected):
    assert normalize_usage(raw)["cache_hit"] is expected


def test_usage_normalizes_reasoning_tokens_from_completion_details():
    usage = normalize_usage({
        "prompt_tokens": 100,
        "completion_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 24},
    })
    assert usage["reasoning_tokens"] == 24


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_choice", [None, "auto"])
async def test_session_packages_tool_metadata_then_ordered_multimodal_attachments(
    monkeypatch, tmp_path: Path, configured_choice,
):
    provider = {} if configured_choice is None else {"tool_choice": configured_choice}
    session = AgentSession(
        "executor", "m", library=_empty_library(tmp_path),
        settings=Settings(_env_file=None, models_json=json.dumps({"m": provider})),
    )
    png = _png(tmp_path / "temporal-frame.png")
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    seen: list[list[dict]] = []
    seen_tools: list[list[dict]] = []
    seen_tool_choices: list[object] = []
    events: list[tuple[str, dict]] = []

    async def handler(args, context):
        assert args == {"mode": "sequence"}
        return AgentToolResult(
            summary="frames",
            data={"mode": "sequence"},
            actionable_observation_id="obs-end",
            attachments=[
                ToolAttachment(label="start", content=png, observation_id="obs-start"),
                ToolAttachment(
                    label="end", content=png, observation_id="obs-end",
                    actionable_coordinate_reference=True,
                ),
            ],
            replacement_attachments=[
                ToolAttachment(label="start", content=png, observation_id="obs-start"),
                ToolAttachment(
                    label="end", content=png, observation_id="obs-end",
                    actionable_coordinate_reference=True,
                ),
            ],
        )

    calls = 0
    async def fake_complete(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        seen.append(list(messages))
        seen_tools.append(kwargs["tools"])
        seen_tool_choices.append(kwargs["tool_choice"])
        if calls == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="o", name="observe_screen", arguments='{"mode":"sequence"}'),
            ])
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}], handlers={"observe_screen": handler},
        context_state={
            "active_package": object(),
            "render_observation_bucket": lambda _package: (
                [{"role": "user", "content": [
                    {"type": "text", "text": "CURRENT OBSERVATION:\n{}\nstart"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64,b25l",
                    }},
                    {"type": "text", "text": "end"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64,dHdv",
                    }},
                ]}],
                ["observation"],
            ),
        },
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )
    assert len(result.llm_rounds) == 2 and len(result.tool_calls) == 2
    assert seen_tools[1] == seen_tools[0]
    assert seen_tool_choices == [configured_choice or "required"] * 2
    second = seen[1]
    tool_message = next(message for message in second if message.get("role") == "tool")
    assert "base64" not in str(tool_message["content"])
    attachment_message = next(message for message in second if isinstance(message.get("content"), list))
    labels = [block.get("text", "") for block in attachment_message["content"] if block.get("type") == "text"]
    assert any("start" in label for label in labels) and any("end" in label for label in labels)
    assert "obs-start" not in str(attachment_message)
    assert [kind for kind, _ in events] == [
        "agent_llm_round_started",
        "agent_llm_round_finished", "agent_tool_started", "agent_tool_finished",
        "agent_llm_round_started",
        "agent_llm_round_finished", "agent_tool_started", "agent_tool_finished",
    ]
    assert all(payload.get("role") == "executor" for _, payload in events)
    assert all(payload.get("invocation_id") for _, payload in events)


@pytest.mark.asyncio
async def test_session_rejects_repeated_observe_without_changing_catalog(
    monkeypatch, tmp_path: Path,
):
    session = AgentSession("executor", "m", library=_empty_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    handler_calls = 0
    request_tools: list[list[dict]] = []

    async def handler(args, context):
        nonlocal handler_calls
        handler_calls += 1
        assert args == {"mode": "snapshot"}
        return AgentToolResult(
            summary="current",
            data={"mode": "snapshot"},
            actionable_observation_id="obs-current",
        )

    calls = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        request_tools.append(kwargs["tools"])
        if calls == 1:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="observe-1", name="observe_screen",
                        arguments='{"mode":"snapshot"}',
                    ),
                    ToolCall(
                        id="observe-2", name="observe_screen",
                        arguments='{"mode":"snapshot"}',
                    ),
                ],
            )
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit", name="submit_executor_step",
                arguments=json.dumps(_executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait",
                )),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}],
        handlers={"observe_screen": handler},
        context_state={
            "active_package": object(),
            "render_observation_bucket": lambda _package: (
                [{"role": "user", "content": "CURRENT OBSERVATION:\n{}"}],
                ["observation"],
            ),
        },
    )

    assert handler_calls == 1
    assert request_tools[1] == request_tools[0]
    assert [record.status for record in result.tool_calls] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.PRECONDITION_NOT_MET,
        ToolStatus.SUCCEEDED,
    ]
    assert result.tool_calls[1].error == "observe_screen_already_succeeded"
    wire = result.tool_calls[1].model_result
    assert wire["error"] == "observe_screen_already_succeeded"
    assert "submit the role decision now" in wire["summary"]


@pytest.mark.asyncio
async def test_repeated_observe_on_second_round_can_submit_with_normal_role_budget(
    monkeypatch, tmp_path: Path,
):
    session = AgentSession("executor", "m", library=_empty_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    handler_calls = 0
    request_tools: list[list[dict]] = []
    request_choices: list[object] = []

    async def handler(args, context):
        nonlocal handler_calls
        handler_calls += 1
        return AgentToolResult(
            summary="current",
            data={"mode": "snapshot"},
            actionable_observation_id="obs-current",
        )

    calls = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls
        calls += 1
        request_tools.append(kwargs["tools"])
        request_choices.append(kwargs["tool_choice"])
        if calls <= 2:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls",
                tool_calls=[ToolCall(
                    id=f"observe-{calls}", name="observe_screen",
                    arguments='{"mode":"snapshot"}',
                )],
            )
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="submit", name="submit_executor_step",
                arguments=json.dumps(_executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait",
                )),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}],
        handlers={"observe_screen": handler},
        context_state={
            "active_package": object(),
            "render_observation_bucket": lambda _package: (
                [{"role": "user", "content": "CURRENT OBSERVATION:\n{}"}],
                ["observation"],
            ),
        },
    )

    assert handler_calls == 1
    assert len(result.llm_rounds) == 3
    assert request_tools[0] == request_tools[1] == request_tools[2]
    assert request_choices == ["required", "required", "required"]
    assert [record.status for record in result.tool_calls] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.PRECONDITION_NOT_MET,
        ToolStatus.SUCCEEDED,
    ]
    assert all(
        "recovery_round" not in round_
        for round_ in result.request_snapshot["rounds"]
    )


class _Frames:
    def __init__(self, png: bytes):
        self.png = png
        self.calls = 0

    async def get_frame(self):
        self.calls += 1
        return {
            "class": "Root", "package": "com.example",
            "bounds": "[0,0][32,64]", "children": [],
            "_capture": {
                "complete": False,
                "tree_providers_exhausted": True,
            },
        }, self.png

    async def current_foreground_identity(self, *, timeout_s=None):
        return {
            "package": "com.example", "activity": ".Main",
            "component": "com.example/.Main", "sources": [], "conflict": False,
        }


@pytest.mark.asyncio
async def test_observe_screen_current_and_temporal_fallback_are_global_and_actionable(tmp_path: Path):
    png = _png(tmp_path / "frame.png")
    driver = _Frames(png)
    baseline = ObservationPackage(
        ui=CanonicalUI(), mode=ObservationMode.TREE_ONLY, text_for_llm="",
        image_for_llm=None, annotated_png=png, gap_reasons=[], frame_width=32, frame_height=64,
    )
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(), artifacts=None, baseline_package=baseline,
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i")
    current = await handler({"mode": "snapshot"}, context)
    assert current.status == ToolStatus.SUCCEEDED
    assert current.data["frame_geometry"] == [32, 64]
    assert current.actionable_observation_id
    assert "purpose" not in current.data and "verdict" not in current.data
    temporal = await handler({"mode": "sequence", "frames": 3, "duration_ms": 1}, context)
    assert temporal.status == ToolStatus.SUCCEEDED
    assert temporal.data["coordinate_reference"] == "end"
    assert len(temporal.data["frames"]) == 3
    assert temporal.data["status"] == "complete"
    assert "temporal_complete" not in temporal.data
    assert sum(bool(a.actionable_coordinate_reference) for a in temporal.attachments if a.kind == "image") == 1


@pytest.mark.asyncio
async def test_sequence_reuses_registered_baseline_without_mutating_its_identity(tmp_path: Path):
    import time

    png = _png(tmp_path / "frame.png")
    driver = _Frames(png)
    baseline = ObservationBuilder().build(await driver.get_frame())
    baseline.captured_monotonic_ms = time.monotonic() * 1000
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="reuse")
    _prepare_role_model_image(context, baseline)
    _register_package(context, baseline, actionable=True)
    registry = context.state["observation_registry"]
    original = registry.get(baseline.observation_id).deterministic_json()
    original_time = baseline.captured_monotonic_ms
    handler = make_observe_screen_handler(
        driver=driver, builder=ObservationBuilder(), artifacts=None, baseline_package=baseline,
    )
    result = await handler({"mode": "sequence", "frames": 2, "duration_ms": 300}, context)
    assert result.status == ToolStatus.SUCCEEDED
    assert registry.get(baseline.observation_id).deterministic_json() == original
    assert baseline.actionable is True
    assert baseline.captured_monotonic_ms == original_time
    start = registry.get(result.data["frames"][0]["observation_id"])
    assert start.observation_id != baseline.observation_id
    assert start.captured_monotonic_ms == original_time
    assert start.actionable is False
    assert registry.active_observation_id == result.actionable_observation_id
    assert registry.active_observation_id != baseline.observation_id


@pytest.mark.asyncio
async def test_observe_screen_current_ignores_temporal_provider_defaults(tmp_path: Path):
    png = _png(tmp_path / "frame.png")
    baseline = ObservationPackage(
        ui=CanonicalUI(), mode=ObservationMode.TREE_ONLY, text_for_llm="",
        image_for_llm=None, annotated_png=png, gap_reasons=[], frame_width=32, frame_height=64,
    )
    handler = make_observe_screen_handler(
        driver=_Frames(png), builder=ObservationBuilder(), artifacts=None,
        baseline_package=baseline,
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i")
    result = await handler(
        {"mode": "snapshot", "frames": 2, "duration_ms": 800},
        context,
    )
    assert result.status == ToolStatus.SUCCEEDED
    assert result.data["mode"] == "snapshot"
    assert context.state["_observe_attempt_count"] == 1


@pytest.mark.asyncio
async def test_temporal_provider_uses_current_sampling_window(tmp_path: Path):
    png = _png(tmp_path / "frame.png")
    ending = ObservationPackage(
        ui=CanonicalUI(), mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm="tree",
        image_for_llm=png, annotated_png=png, gap_reasons=[], frame_width=32, frame_height=64,
    )

    class Provider(_Frames):
        request = None

        async def observe_temporal(self, request):
            self.request = request
            return TemporalObservationResult(
                status="complete", frames=[ending, ending], ending=ending,
                provider="scrcpy", provider_status="ring_buffer",
            )

    provider = Provider(png)
    handler = make_observe_screen_handler(
        driver=provider, builder=ObservationBuilder(), artifacts=None, baseline_package=ending,
    )
    result = await handler(
        {"mode": "sequence", "frames": 2},
        ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )
    assert result.status == ToolStatus.SUCCEEDED
    assert provider.request.frames == 2
    assert result.provider_status == "ring_buffer"


@pytest.mark.asyncio
async def test_observe_screen_rejects_removed_anchor_argument(tmp_path: Path):
    png = _png(tmp_path / "frame.png")
    baseline = ObservationPackage(
        ui=CanonicalUI(), mode=ObservationMode.TREE_ONLY, text_for_llm="",
        image_for_llm=None, annotated_png=png, gap_reasons=[],
    )
    handler = make_observe_screen_handler(
        driver=_Frames(png), builder=ObservationBuilder(), artifacts=None,
        baseline_package=baseline,
    )

    result = await handler(
        {"mode": "sequence", "anchor": "last_action"},
        ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i"),
    )

    assert result.status == ToolStatus.INVALID_ARGUMENTS


@pytest.mark.asyncio
async def test_read_tool_policy_is_fixed_and_nonconfigurable(tmp_path: Path):
    png = _png(tmp_path / "frame.png")
    baseline = ObservationPackage(
        ui=CanonicalUI(), mode=ObservationMode.TREE_ONLY, text_for_llm="",
        image_for_llm=None, annotated_png=png, gap_reasons=[],
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i")
    handler = make_observe_screen_handler(
        driver=_Frames(png), builder=ObservationBuilder(), artifacts=None, baseline_package=baseline,
    )
    result = await handler({"mode": "snapshot"}, context)
    assert result.status == ToolStatus.SUCCEEDED
    assert result.attachments


@pytest.mark.asyncio
async def test_submit_only_vs_observe_invocation_measurements(monkeypatch, tmp_path: Path):
    async def run(with_tool: bool):
        session = AgentSession("executor", "m", library=_empty_library(tmp_path / ("tool" if with_tool else "plain")))
        session.reset_lifecycle("t")
        session.freeze_allow_dirs(["generic"])
        session.set_stable_system("S")
        calls = 0

        async def fake_complete(model, messages, **kwargs):
            nonlocal calls
            calls += 1
            if with_tool and calls == 1:
                return GatewayResponse(
                    content="", model="m", stop_reason="tool_calls", latency_ms=5,
                    usage={"input_tokens": 100, "cached_read_tokens": 50, "output_tokens": 5, "total_tokens": 105, "cache_hit": True},
                    tool_calls=[ToolCall(id="o", name="observe_screen", arguments='{"mode":"snapshot"}')],
                )
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls", latency_ms=4,
                usage={"input_tokens": 110, "cached_read_tokens": 60, "output_tokens": 4, "total_tokens": 114, "cache_hit": True},
                tool_calls=[ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(
                    _executor_payload({"type": "sleep", "duration_ms": 1}, "wait")
                ))],
            )

        async def observe(args, context):
            return AgentToolResult(
                summary="current", actionable_observation_id="obs",
                attachments=[ToolAttachment(label="end", content=b"image", actionable_coordinate_reference=True)],
            )

        monkeypatch.setattr("agent.session.complete", fake_complete)
        return await session.run(
            [{"role": "user", "content": "O"}], handlers={"observe_screen": observe},
            context_state={
                "active_package": object(),
                "render_observation_bucket": lambda _package: (
                    [{"role": "user", "content": [
                        {"type": "text", "text": "CURRENT OBSERVATION:\n{}"},
                        {"type": "image_url", "image_url": {
                            "url": "data:image/png;base64,aW1hZ2U=",
                        }},
                    ]}],
                    ["observation"],
                ),
            },
        )

    plain = await run(False)
    observed = await run(True)
    assert (len(plain.llm_rounds), len(plain.tool_calls)) == (1, 1)
    assert (len(observed.llm_rounds), len(observed.tool_calls)) == (2, 2)
    assert observed.llm_rounds[1].usage.cached_read_tokens == 60
    assert observed.llm_rounds[1].image_count == 1
    assert sum(round.latency_ms for round in observed.llm_rounds) > sum(round.latency_ms for round in plain.llm_rounds)


@pytest.mark.asyncio
async def test_legacy_model_basis_is_ignored_and_runtime_binds_active_observation(monkeypatch, tmp_path: Path):
    class Driver(_Frames):
        def __init__(self, png):
            super().__init__(png)
            self.actions = []

        async def act(self, action):
            self.actions.append(action)
            return ActionResult(success=True, message="ok")

    png = _png(tmp_path / "frame.png")
    driver = Driver(png)
    executor = Executor(driver, model="m", settings=Settings())
    payload = _executor_payload(
        {"type": "tap", "index": 0}, "tap",
    )

    async def fake_complete(model, messages, **kwargs):
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(payload)),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    package = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.example", activity=".Main",
            elements=[UIElement(index=0, bounds=[0, 0, 10, 10], clickable=True)],
        ),
        mode=ObservationMode.TREE_ONLY, text_for_llm="tree", image_for_llm=None,
        annotated_png=None, gap_reasons=[], observation_id="obs-active",
    )
    step, *_ = await executor.act_once("tap", package)
    assert len(driver.actions) == 1
    assert step.basis_observation_id == "obs-active"
    assert step.action_pipeline and step.action_pipeline.dispatch_suppressed is False


@pytest.mark.asyncio
async def test_prefix_hash_changes_only_after_loaded_skill_enters_next_outer_prefix(monkeypatch, tmp_path: Path):
    skill_dir = tmp_path / "generic" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\nversion: 1.0.0\nkind: generic\n---\n\nbody\n",
        encoding="utf-8",
    )
    session = AgentSession("planner", "m", library=SkillLibrary(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system("S")
    phase = 0
    call_in_phase = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal call_in_phase
        call_in_phase += 1
        if phase == 0 and call_in_phase == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="l", name="load_skill", arguments='{"skill_id":"demo"}'),
            ])
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id=f"s{phase}", name="submit_planner_decision", arguments=json.dumps({
                "decision": "execute", "reason": "Wait for readiness",
                "plan": {"current_stage": {"goal": "wait"}},
            })),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    first = await session.run([{"role": "user", "content": "O"}])
    assert len(first.llm_rounds) == 2
    assert len(first.request_snapshot["rounds"]) == 2
    second_round = json.dumps(first.request_snapshot["rounds"][1], ensure_ascii=False)
    assert "load_skill" in second_round
    assert "skill:demo" in second_round
    assert first.llm_rounds[0].stable_prefix_hash == first.llm_rounds[1].stable_prefix_hash
    assert first.llm_rounds[0].tool_catalog_hash == first.llm_rounds[1].tool_catalog_hash
    phase = 1
    call_in_phase = 0
    second = await session.run([{"role": "user", "content": "O2"}])
    assert second.llm_rounds[0].tool_catalog_hash == first.llm_rounds[0].tool_catalog_hash
    assert second.llm_rounds[0].stable_prefix_hash != first.llm_rounds[0].stable_prefix_hash


@pytest.mark.asyncio
async def test_executor_input_artifact_uses_target_k_wire(monkeypatch, tmp_path: Path):
    skill_dir = tmp_path / "skills" / "apps" / "app.a"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: app_a\ndescription: app a\nversion: 1.0.0\napp: app.a\nkind: app_core\n---\n\nAPP A BODY\n",
        encoding="utf-8",
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")

    class Driver:
        async def act(self, action):
            return ActionResult(success=True, message="ok")

        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "app.a", "activity": ".Main",
                "component": "app.a/.Main", "conflict": False,
            }

        async def get_frame(self):
            return (
                {"class": "Root", "text": "ready", "children": [],
                 "_capture": {"complete": True, "coordinate_compatible": True}},
                _png(tmp_path / "transaction-frame.png"),
            )

    executor = Executor(Driver(), artifacts=artifacts, model="m", settings=Settings())
    executor._session = AgentSession(  # noqa: SLF001
        "executor", "m", library=SkillLibrary(tmp_path / "skills"),
    )
    executor.configure_skills(
        frozen_skill_dirs=["generic", "apps/app.a"],
        target_app="app.a",
        workflow_ids=[],
    )

    async def fake_complete(model, messages, **kwargs):
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait; loading",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)

    def package(app_id: str) -> ObservationPackage:
        return ObservationPackage(
            ui=CanonicalUI(app_id=app_id), mode=ObservationMode.TREE_ONLY,
            text_for_llm="tree", image_for_llm=None, annotated_png=None, gap_reasons=[],
        )

    *_, active_refs = await executor.act_once("wait", package("app.a"), task_id="task-one")
    *_, inactive_refs = await executor.act_once("wait", package("app.b"), task_id="task-one")
    active = artifacts.read_text(str(active_refs["llm_input_ref"]))
    inactive = artifacts.read_text(str(inactive_refs["llm_input_ref"]))
    assert "APP A BODY" in active
    assert "GENERIC SKILL INDEX" not in active
    assert "APP A BODY" in inactive
    assert "GENERIC SKILL INDEX" not in inactive
    assert '"tool_catalog"' in active and '"rounds"' in active
