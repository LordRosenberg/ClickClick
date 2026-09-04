"""Current-only role images, compact decision context, and visual telemetry."""

from __future__ import annotations

import base64
import hashlib
import io
import json

import pytest
from PIL import Image, ImageDraw

from agent.executor import Executor
from agent.planner import Planner
from perception.image_utils import estimate_image_input_tokens
from perception.observation import ObservationPackage
from perception.som import (
    A11Y_COLOR,
    MIN_FONT_SIZE,
    _effective_font_size,
    _measure_label,
    _outline_width,
    render_som,
)
from shared.config import Settings
from shared.artifacts import ArtifactStore
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import (
    ActiveTaskCompletionContract,
    ActionResult,
    AgentState,
    CanonicalUI,
    SubgoalContractBody,
    TaskContractBody,
    ObservationMode,
    UIElement,
)


def _png(width: int = 1440, height: int = 3200) -> bytes:
    image = Image.new("RGB", (width, height), (245, 245, 245))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _package() -> ObservationPackage:
    clean = _png()
    target = UIElement(
        index=7,
        role="Button",
        text="Search",
        bounds=[100, 200, 500, 360],
        interactable=True,
    )
    ui = CanonicalUI(
        app_id="com.example",
        activity="ResultsActivity",
        elements=[target],
        semantic_tree=[target],
        page_summary="Search",
    )
    return ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="[7] Button Search clickable",
        image_for_llm=render_som(clean, [target]),
        annotated_png=render_som(clean, [target]),
        clean_png=clean,
        gap_reasons=[],
        frame_width=1440,
        frame_height=3200,
        observation_id="obs-current",
    )


def _request_images(messages: list[dict]) -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") != "image_url":
                continue
            url = block["image_url"]["url"]
            media_type = url.split(";", 1)[0].removeprefix("data:")
            images.append((media_type, base64.b64decode(url.split(",", 1)[1])))
    return images


def _cyan_pixels(image_bytes: bytes, tolerance: int = 35) -> int:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    count = 0
    for y in range(image.height):
        for x in range(image.width):
            pixel = image.getpixel((x, y))
            if all(abs(pixel[channel] - A11Y_COLOR[channel]) <= tolerance for channel in range(3)):
                count += 1
    return count


class _Driver:
    async def act(self, action):
        return ActionResult(success=True, message=action.type)


@pytest.mark.asyncio
async def test_planner_receives_one_clean_1080_current_image(monkeypatch, tmp_path):
    captured: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        captured.append(messages)
        return GatewayResponse(
            content="",
            model=model,
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="planner-submit",
                name="submit_planner_decision",
                arguments=json.dumps({
                    "mode": "execute",
                    "target_requirement_ref": "final_ui_state:1",
                    "next_subgoal": "inspect the visible result",
                    "completion_contract": {
                        "success_conditions": [
                            "the requested current screen is visible",
                        ],
                        "disqualifying_clauses": [],
                    },
                    "plan": ["inspect the visible result"],
                }),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    planner = Planner(
        driver=_Driver(), artifacts=artifacts,
        model="chatgpt/gpt-5.4", settings=Settings(),
    )
    decision, refs, observation_refs = await planner.decide(
        AgentState(
            instruction="inspect",
            task_completion_contract=ActiveTaskCompletionContract(
                contract_id="precommitted-visual-contract",
                revision=1,
                body=TaskContractBody(
                    final_ui_state=["the requested screen is currently visible"],
                ),
            ),
        ),
        _package(),
        task_id="visual-planner",
    )

    assert decision.mode.value == "execute"
    images = _request_images(captured[0])
    assert len(images) == 1
    media_type, image_bytes = images[0]
    assert media_type == "image/jpeg"
    assert Image.open(io.BytesIO(image_bytes)).size == (486, 1080)
    assert _cyan_pixels(image_bytes) == 0
    metadata = observation_refs["visual_evidence"]
    image_artifact_ref = metadata.pop("image_artifact_ref")
    assert hashlib.sha256(artifacts.read_bytes(image_artifact_ref)).hexdigest() == (
        hashlib.sha256(image_bytes).hexdigest()
    )
    assert "active_skills" in refs
    assert observation_refs["capture"] == {}
    assert metadata == {
        "role": "planner",
        "observation_id": "obs-current",
        "visual_kind": "clean",
        "image_delivered": True,
        "delivered_image_byte_count": len(image_bytes),
        "delivered_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "source_pixel_byte_count": len(_png()),
        "source_pixel_sha256": hashlib.sha256(_png()).hexdigest(),
        "model_image_width": 486,
        "model_image_height": 1080,
        "current_only": True,
        "estimated_image_tokens": 653,
        "image_profile_id": "tree-som-1080-q75",
        "image_profile_long_edge": 1080,
        "image_profile_jpeg_quality": 75,
    }
    rendered_text = "\n".join(
        str(block.get("text") or "")
        for message in captured[0]
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else [{"text": message.get("content")}]
        )
    )
    assert "executor_completed" not in rendered_text
    assert '"role":"Button","raw_text":"Search"' in rendered_text
    assert '"image_size":[486,1080]' not in rendered_text
    assert "[7]" not in rendered_text


@pytest.mark.asyncio
async def test_executor_receives_one_single_pass_som_1080_image(monkeypatch):
    captured: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        captured.append(messages)
        return GatewayResponse(
            content="",
            model=model,
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="executor-submit",
                name="submit_executor_step",
                arguments=json.dumps({
                    "decision": "request_replan",
                    "summary": "inspect current result surface",
                }),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    executor = Executor(_Driver(), model="chatgpt/gpt-5.4", settings=Settings())
    _step, result, _ui, _mode, refs = await executor.act_once(
        "show results", _package(), task_id="visual-executor",
    )

    assert result.success is True
    images = _request_images(captured[0])
    assert len(images) == 1
    media_type, image_bytes = images[0]
    assert media_type == "image/jpeg"
    assert Image.open(io.BytesIO(image_bytes)).size == (486, 1080)
    assert _cyan_pixels(image_bytes) > 0
    assert refs["visual_evidence"] == {
        "role": "executor",
        "observation_id": "obs-current",
        "visual_kind": "som",
        "image_delivered": True,
        "delivered_image_byte_count": len(image_bytes),
        "delivered_image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "source_pixel_byte_count": len(_png()),
        "source_pixel_sha256": hashlib.sha256(_png()).hexdigest(),
        "model_image_width": 486,
        "model_image_height": 1080,
        "current_only": True,
        "estimated_image_tokens": 653,
        "image_profile_id": "tree-som-1080-q75",
        "image_profile_long_edge": 1080,
        "image_profile_jpeg_quality": 75,
    }


@pytest.mark.asyncio
async def test_executor_image_only_mode_receives_clean_coordinate_guidance(monkeypatch):
    captured: list[list[dict]] = []

    async def fake_complete(model, messages, **kwargs):
        captured.append(messages)
        return GatewayResponse(
            content="",
            model=model,
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="executor-submit",
                name="submit_executor_step",
                arguments=json.dumps({
                    "decision": "request_replan",
                    "summary": "inspect visible result",
                }),
            )],
        )

    package = _package()
    package.mode = ObservationMode.IMAGE_ONLY
    package.index_actionable = False
    monkeypatch.setattr("agent.session.complete", fake_complete)
    executor = Executor(_Driver(), model="chatgpt/gpt-5.4", settings=Settings())

    _step, result, _ui, mode, refs = await executor.act_once(
        "judge visible result", package, task_id="visual-image-only",
    )

    assert result.success is True
    assert mode == ObservationMode.IMAGE_ONLY
    image_bytes = _request_images(captured[0])[0][1]
    assert _cyan_pixels(image_bytes) == 0
    rendered = "\n".join(
        str(block.get("text") or "")
        for message in captured[0]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert '"evidence_tier":"image_only"' in rendered
    assert '"mode":"image-only"' not in rendered
    assert '"index_actions_available":false' in rendered
    assert '"coordinate_actions_available":true' in rendered
    assert refs["visual_evidence"]["visual_kind"] == "clean"
    assert refs["visual_evidence"]["image_profile_id"] == (
        "image-only-calibrated-1080-q75"
    )


def test_1080_som_reference_style_and_supported_token_estimate():
    assert MIN_FONT_SIZE == 12
    assert _outline_width(486, 1080) == 2
    assert _effective_font_size("123", [0, 0, 12, 12], 486, 1080) >= 12
    image = Image.new("RGB", (100, 100))
    draw = ImageDraw.Draw(image)
    font_size = _effective_font_size("7", [0, 0, 40, 40], 486, 1080)
    _width, _height, padding = _measure_label(draw, "7", None)
    assert font_size >= 12
    assert padding == 2
    assert estimate_image_input_tokens("chatgpt/gpt-5.4", 486, 1080) == 653
    assert estimate_image_input_tokens("unknown-model", 486, 1080) is None


def test_role_prompts_require_usable_results_before_completion():
    from agent.prompts import render_executor_system, render_reviewer_system

    executor = " ".join(render_executor_system().lower().split())
    reviewer = " ".join(render_reviewer_system().lower().split())
    assert "request_review" in executor
    assert "loading" in executor
    assert "current ui proves state" in executor
    assert "do not plan future" in executor
    assert "must not operate the device or plan future work" in reviewer
    assert "remembered_facts" in reviewer
