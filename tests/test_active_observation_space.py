"""Regression coverage for observation-bound visual actions."""

from __future__ import annotations

import io
import base64
import json

import pytest
from PIL import Image

from agent.executor import Executor
from agent.observation_space import (
    ObservationRegistry,
    ObservationTransform,
    make_registry_entry,
    transform_action,
    validate_action_bounds,
)
from agent.read_tools import TemporalObservationResult
from agent.session import _visual_submission_error
from agent.tool_registry import AgentRole, ToolExecutionContext
from perception.observation import ObservationPackage
from shared.config import Settings
from shared.llm_gateway import GatewayError, GatewayResponse, ToolCall
from shared.schemas import (
    Action,
    ActionResult,
    CanonicalUI,
    ExecutorStep,
    ObservationMode,
    UIElement,
)


def _png(width: int, height: int, color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def _package(
    observation_id: str,
    width: int,
    height: int,
    *,
    color: str = "white",
) -> ObservationPackage:
    image = _png(width, height, color)
    return ObservationPackage(
        ui=CanonicalUI(
            app_id="com.example", activity=".Main",
            elements=[UIElement(index=7, bounds=[10, 20, 30, 40], clickable=True)],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="[7] Button target",
        image_for_llm=image,
        annotated_png=image,
        gap_reasons=[],
        observation_id=observation_id,
        frame_width=width,
        frame_height=height,
    )


def _executor_payload(action: dict, act: str, **overrides) -> dict:
    payload = {
        "decision": "act",
        "action": action,
        "summary": act,
    }
    payload.update(overrides)
    return payload


class _MixedSizeDriver:
    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.later = _png(1200, 2670, "blue")

    async def get_frame(self):
        return {
            "class": "Root", "package": "com.example",
            "bounds": [0, 0, 1200, 2670],
            "_capture": {
                "provider": "fixture",
                "complete": True,
                "coordinate_compatible": True,
                "frame_geometry": [1200, 2670],
            },
            "children": [{
                "class": "Button", "bounds": [10, 20, 30, 40], "clickable": True,
            }],
        }, self.later

    async def current_foreground_identity(self, *, timeout_s=None):
        return {
            "package": "com.example", "activity": ".Main",
            "component": "com.example/.Main", "sources": [], "conflict": False,
        }

    async def act(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return ActionResult(success=True, message="ok")


def test_image_only_basis_rejects_index_but_keeps_coordinates_available():
    registry = ObservationRegistry()
    entry = make_registry_entry(
        observation_id="obs-image-only",
        coordinate_space_id="space-image-only",
        ui=CanonicalUI(
            app_id="com.example", activity=".Main",
            elements=[
                UIElement(index=7, bounds=[10, 20, 30, 40], clickable=True),
            ],
        ),
        actionable=True,
        index_actionable=False,
        captured_monotonic_ms=1,
        model_image_size=(540, 1080),
        frame_geometry=(1080, 2160),
    )
    registry.register(entry, make_active=True)
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR,
        invocation_id="image-only",
        state={
            "observation_registry": registry,
            "active_observation_id": entry.observation_id,
        },
    )
    indexed = ExecutorStep(
        action=Action(type="tap", index=7),
        basis_observation_id=entry.observation_id,
    )
    coordinate = ExecutorStep(
        action=Action(type="tap_xy", x=100, y=200),
        basis_observation_id=entry.observation_id,
    )

    reason, _detail = _visual_submission_error(
        indexed, context=context, epsilon=1e-6,
    )
    assert reason == "index_unavailable_for_observation"
    assert _visual_submission_error(
        coordinate, context=context, epsilon=1e-6,
    )[0] == ""


@pytest.mark.asyncio
async def test_tool_observation_replaces_baseline_and_owns_transform(monkeypatch):
    driver = _MixedSizeDriver()
    executor = Executor(driver, model="m", settings=Settings())
    baseline = _package("obs-initial", 485, 1080)
    baseline.captured_monotonic_ms = 0
    calls = 0
    seen_second = ""

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, seen_second
        calls += 1
        if calls == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="observe", name="observe_screen", arguments='{"mode":"current"}'),
            ])
        seen_second = json.dumps(messages, ensure_ascii=False)
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "tap_xy", "x": 240, "y": 400},
                    "tap later frame",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    step, result, *_ = await executor.act_once("tap", baseline)

    assert calls == 2
    assert "observation_id: obs-initial" not in seen_second
    assert seen_second.count('"type": "image_url"') == 1
    assert '\\"image_size\\":[485,1080]' in seen_second.replace(" ", "")
    assert result.success is True
    assert len(driver.actions) == 1
    assert driver.actions[0].x == pytest.approx(240 * 1200 / 485)
    assert driver.actions[0].y == pytest.approx(400 * 2670 / 1080)
    assert step.basis_observation_id != "obs-initial"
    assert step.action_pipeline is not None
    assert step.action_pipeline.source_geometry == [485, 1080]
    assert step.action_pipeline.target_geometry == [1200, 2670]
    assert step.action_pipeline.transform_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attach_image", "image_size", "expected_model_size"),
    [
        (False, (648, 1440), "485x1080"),
        (True, (1200, 2670), "485x1080"),
    ],
)
async def test_current_read_replaces_baseline_with_fresh_model_pixels(
    monkeypatch, attach_image: bool, image_size: tuple[int, int], expected_model_size: str,
):
    driver = _MixedSizeDriver()
    baseline_image = _png(*image_size)
    baseline = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.example", activity=".Main",
            elements=[
                UIElement(index=7, bounds=[10, 20, 30, 40], clickable=True),
            ],
        ),
        mode=(ObservationMode.TREE_PLUS_IMAGE if attach_image else ObservationMode.TREE_ONLY),
        text_for_llm="[7] Button target",
        image_for_llm=baseline_image if attach_image else None,
        annotated_png=baseline_image,
        gap_reasons=[],
        frame_width=1200,
        frame_height=2670,
    )
    calls = 0
    second_request = ""

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, second_request
        calls += 1
        if calls == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="observe", name="observe_screen", arguments='{"mode":"current"}'),
            ])
        second_request = json.dumps(messages, ensure_ascii=False)
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    step, result, *_ = await Executor(driver, model="m", settings=Settings()).act_once(
        "inspect", baseline,
    )

    assert calls == 2
    assert result.success is True
    assert step.action is not None and step.action.type == "sleep"
    assert "observation identity is immutable" not in second_request
    expected_width, expected_height = expected_model_size.split("x")
    compact_request = second_request.replace(" ", "")
    assert f'\\"image_size\\":[{expected_width},{expected_height}]' in compact_request
    assert second_request.count('"type": "image_url"') == 1


@pytest.mark.asyncio
async def test_temporal_start_frame_cannot_override_runtime_ending_basis(monkeypatch):
    driver = _MixedSizeDriver()
    start = _package("obs-start", 200, 400, color="red")
    ending = _package("obs-end", 200, 400, color="green")

    async def observe_temporal(request):
        return TemporalObservationResult(
            status="complete", frames=[start, ending], ending=ending,
            provider="test", provider_status="complete",
        )

    driver.observe_temporal = observe_temporal  # type: ignore[attr-defined]
    executor = Executor(driver, model="chatgpt/gpt-5.4", settings=Settings())
    calls = 0
    second_messages: list[dict] = []

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, second_messages
        calls += 1
        if calls == 1:
            return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
                ToolCall(id="observe", name="observe_screen", arguments='{"mode":"temporal"}'),
            ])
        second_messages = list(messages)
        return GatewayResponse(content="", model="chatgpt/gpt-5.4", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "tap", "index": 7}, "tap",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    step, result, _ui, _mode, refs = await executor.act_once(
        "tap", _package("obs-initial", 100, 200),
    )
    image_urls = [
        block["image_url"]["url"]
        for message in second_messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "image_url"
    ]
    decoded_colors = []
    for url in image_urls:
        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")
        decoded_colors.append(image.getpixel((image.width // 2, image.height // 2)))

    assert len(image_urls) == 2
    assert decoded_colors[0][0] > decoded_colors[0][1]  # start is red
    assert decoded_colors[1][1] > decoded_colors[1][0]  # ending is green
    assert "visual history only" in json.dumps(second_messages, ensure_ascii=False)
    assert refs["delivered_evidence_digest"]
    assert refs["visual_evidence"]["temporal_frame_count"] == 2
    assert refs["agent_rounds"][1]["prompt_measurements"]["image_estimate"][
        "token_count"
    ] == refs["visual_evidence"]["estimated_image_tokens"]
    assert refs["agent_rounds"][1]["prompt_measurements"]["observation_text"] is not None
    assert len(driver.actions) == 1
    assert result.success is True
    assert step.basis_observation_id == "obs-end"
    assert step.action_pipeline is not None
    assert step.action_pipeline.index_set_id == "elements_obs-end"
    assert any(stage.stage == "dispatched" for stage in step.action_pipeline.stages)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["current", "temporal"])
async def test_failed_in_role_refresh_preserves_prior_o_basis_and_visual_metadata(
    monkeypatch, mode: str,
):
    class Driver(_MixedSizeDriver):
        async def get_frame(self):
            return {
                "class": "Root",
                "_capture": {
                    "complete": True,
                    "coordinate_compatible": True,
                    "tree_providers_exhausted": True,
                    "frame_geometry": [200, 400],
                },
                "children": [],
            }, b"invalid-image"

    driver = Driver()
    baseline = _package("obs-prior", 200, 400, color="yellow")
    if mode == "temporal":
        invalid_start = ObservationPackage(
            ui=CanonicalUI(app_id="com.example"),
            mode=ObservationMode.IMAGE_ONLY,
            text_for_llm="invalid start",
            image_for_llm=b"invalid-image",
            clean_png=b"invalid-image",
            annotated_png=b"invalid-image",
            gap_reasons=["tree_unusable"],
            observation_id="obs-invalid-start",
            frame_width=200,
            frame_height=400,
            actionable=True,
            index_actionable=False,
        )
        ending = _package("obs-candidate-end", 200, 400, color="green")

        async def observe_temporal(request):
            del request
            return TemporalObservationResult(
                status="complete",
                frames=[invalid_start, ending],
                ending=ending,
                provider="test",
                provider_status="complete",
            )

        driver.observe_temporal = observe_temporal  # type: ignore[attr-defined]

    calls = 0
    second_messages: list[dict] = []

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, second_messages
        del model, kwargs
        calls += 1
        if calls == 1:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls",
                tool_calls=[ToolCall(
                    id="observe", name="observe_screen",
                    arguments=json.dumps({"mode": mode}),
                )],
            )
        second_messages = list(messages)
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
    step, result, _ui, _obs_mode, refs = await Executor(
        driver, model="chatgpt/gpt-5.4", settings=Settings(),
    ).act_once("inspect", baseline)

    assert result.success is True
    assert refs["active_package"].observation_id == "obs-prior"
    assert refs["visual_evidence"]["observation_id"] == "obs-prior"
    assert step.action_pipeline is not None
    assert step.action_pipeline.active_observation_id == "obs-prior"
    assert any(
        entry["observation_id"] == "obs-prior"
        for entry in refs["observation_registry"]
    )
    assert refs["tool_calls"][0]["status"] == "unavailable"
    assert json.dumps(second_messages).count('"type": "image_url"') == 1


@pytest.mark.asyncio
async def test_accepted_nonactionable_current_refresh_replaces_o_without_action_basis(
    monkeypatch,
):
    class Driver(_MixedSizeDriver):
            async def get_frame(self):
                return {
                    "class": "Root", "package": "com.example",
                    "_capture": {"complete": True, "coordinate_compatible": True},
                "children": [{
                    "class": "TextView",
                    "text": "NEW_NONACTIONABLE_CURRENT",
                    "bounds": [0, 0, 100, 40],
                }],
            }, None

    driver = Driver()
    baseline = _package("obs-prior", 200, 400, color="yellow")
    calls = 0
    second_messages: list[dict] = []

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, second_messages
        del model, kwargs
        calls += 1
        if calls == 1:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls",
                tool_calls=[ToolCall(
                    id="observe", name="observe_screen",
                    arguments='{"mode":"current"}',
                )],
            )
        second_messages = list(messages)
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
    step, result, _ui, _mode, refs = await Executor(
        driver, model="m", settings=Settings(),
    ).act_once("inspect", baseline)

    assert result.success is True
    assert step.basis_observation_id == ""
    assert step.action_pipeline is not None
    assert step.action_pipeline.active_observation_id == ""
    assert refs["active_package"].text_for_llm.find("NEW_NONACTIONABLE_CURRENT") >= 0
    assert refs["active_package"].actionable is False
    assert refs["observation_registry"][0]["observation_id"] == "obs-prior"
    assert "NEW_NONACTIONABLE_CURRENT" in json.dumps(second_messages)
    assert json.dumps(second_messages).count('"type": "image_url"') == 0


@pytest.mark.asyncio
async def test_accepted_nonactionable_temporal_ending_replaces_o_without_duplication(
    monkeypatch,
):
    driver = _MixedSizeDriver()
    start = _package("obs-start", 200, 400, color="red")
    ending_image = _png(200, 400, color="green")
    ending = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.example",
            semantic_tree=[UIElement(index=0, text="NONACTIONABLE_END")],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="NONACTIONABLE_END",
        image_for_llm=ending_image,
        clean_png=ending_image,
        annotated_png=ending_image,
        gap_reasons=[],
        observation_id="obs-end-nonactionable",
        frame_width=200,
        frame_height=400,
        actionable=False,
        index_actionable=False,
    )

    async def observe_temporal(request):
        del request
        return TemporalObservationResult(
            status="complete", frames=[start, ending], ending=ending,
            provider="test", provider_status="complete",
        )

    driver.observe_temporal = observe_temporal  # type: ignore[attr-defined]
    calls = 0
    second_messages: list[dict] = []

    async def fake_complete(model, messages, **kwargs):
        nonlocal calls, second_messages
        del model, kwargs
        calls += 1
        if calls == 1:
            return GatewayResponse(
                content="", model="m", stop_reason="tool_calls",
                tool_calls=[ToolCall(
                    id="observe", name="observe_screen",
                    arguments='{"mode":"temporal"}',
                )],
            )
        second_messages = list(messages)
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
    step, result, _ui, _mode, refs = await Executor(
        driver, model="chatgpt/gpt-5.4", settings=Settings(),
    ).act_once("inspect", _package("obs-prior", 200, 400, color="yellow"))

    assert result.success is True
    assert refs["active_package"].observation_id == "obs-end-nonactionable"
    assert refs["visual_evidence"]["observation_id"] == "obs-end-nonactionable"
    assert step.basis_observation_id == ""
    assert step.action_pipeline is not None
    assert step.action_pipeline.active_observation_id == ""
    assert json.dumps(second_messages).count('"type": "image_url"') == 2
    assert json.dumps(second_messages).count("NONACTIONABLE_END") == 1


@pytest.mark.asyncio
async def test_temporal_start_pixels_are_bound_into_delivered_evidence_digest(monkeypatch):
    async def run(start_color: str) -> str:
        driver = _MixedSizeDriver()
        start = _package("obs-start", 200, 400, color=start_color)
        ending = _package("obs-end", 200, 400, color="green")

        async def observe_temporal(request):
            del request
            return TemporalObservationResult(
                status="complete", frames=[start, ending], ending=ending,
                provider="test", provider_status="complete",
            )

        driver.observe_temporal = observe_temporal  # type: ignore[attr-defined]
        calls = 0

        async def fake_complete(model, messages, **kwargs):
            nonlocal calls
            del model, messages, kwargs
            calls += 1
            if calls == 1:
                return GatewayResponse(
                    content="", model="m", stop_reason="tool_calls",
                    tool_calls=[ToolCall(
                        id="observe", name="observe_screen",
                        arguments='{"mode":"temporal"}',
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
        _step, _result, _ui, _mode, refs = await Executor(
            driver, model="chatgpt/gpt-5.4", settings=Settings(),
        ).act_once("inspect", _package("obs-prior", 200, 400, color="yellow"))
        return refs["delivered_evidence_digest"]

    assert await run("red") != await run("blue")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("basis", "index", "reason"),
    [
        ("obs-active", 99, "mismatched_observation_basis"),
    ],
)
async def test_missing_or_mismatched_basis_never_dispatches(monkeypatch, basis, index, reason):
    driver = _MixedSizeDriver()
    executor = Executor(driver, model="m", settings=Settings())
    payload = {
        "decision": "act", "action": {"type": "tap", "index": index},
        "summary": "tap",
    }
    if basis:
        payload["basis_observation_id"] = basis

    async def fake_complete(model, messages, **kwargs):
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="submit", name="submit_executor_step", arguments=json.dumps(payload)),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    with pytest.raises(GatewayError, match="tool loop exhausted"):
        await executor.act_once("tap", _package("obs-active", 100, 200))
    assert driver.actions == []


def test_registry_serialization_is_deterministic_and_identity_is_immutable():
    ui = CanonicalUI(elements=[UIElement(index=1, bounds=[0, 0, 2, 2])])
    entries = [
        make_registry_entry(
            observation_id=obs, coordinate_space_id=f"space-{obs}", ui=ui,
            actionable=True, captured_monotonic_ms=1.0,
            model_image_size=(100, 200), frame_geometry=(200, 400),
        )
        for obs in ("obs-b", "obs-a")
    ]
    first, second = ObservationRegistry(), ObservationRegistry()
    first.register(entries[0])
    first.register(entries[1], make_active=True)
    second.register(entries[1], make_active=True)
    second.register(entries[0])
    assert first.deterministic_json() == second.deterministic_json()
    changed = entries[1].model_copy(update={"coordinate_space_id": "different"})
    with pytest.raises(ValueError, match="immutable"):
        first.register(changed)


@pytest.mark.parametrize(
    ("transform", "point", "expected"),
    [
        (
            ObservationTransform(
                transform_id="identity", model_image_size=(100, 50), frame_geometry=(100, 50),
            ),
            (40, 20), (40, 20),
        ),
        (
            ObservationTransform(
                transform_id="crop", model_image_size=(100, 50), frame_geometry=(300, 200),
                crop_box=(10, 20, 210, 120),
            ),
            (50, 25), (110, 70),
        ),
        (
            ObservationTransform(
                transform_id="rotate", model_image_size=(100, 200), frame_geometry=(200, 100),
                rotation_degrees=90,
            ),
            (0, 0), (0, 99),
        ),
    ],
)
def test_identity_crop_and_rotation_transforms(transform, point, expected):
    action = transform_action(Action(type="tap_xy", x=point[0], y=point[1]), transform)
    assert action.x == pytest.approx(expected[0])
    assert action.y == pytest.approx(expected[1])


def test_rounding_epsilon_normalizes_only_tiny_drift():
    action = Action(type="swipe", x=-0.0000005, y=20, x2=99.0000005, y2=49)
    bounded, normalized, evidence = validate_action_bounds(
        action, width=100, height=50, epsilon=0.000001,
    )
    assert normalized is True and bounded is not None
    assert (bounded.x, bounded.x2) == (0.0, 99.0)
    assert evidence["valid"] is True

    rejected, _, evidence = validate_action_bounds(
        Action(type="tap_xy", x=-0.01, y=10), width=100, height=50, epsilon=0.000001,
    )
    assert rejected is None
    assert evidence["reason"] == "coordinate_out_of_bounds"
