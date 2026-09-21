from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from agent.action_observation import interaction_ack_for_batch
from agent.decision_context import render_executor_observation_v2
from agent.read_tools import make_inspect_image_regions_handler
from agent.tool_registry import AgentRole, ToolExecutionContext
from driver.accessibility import (
    AccessibilityEventBatch,
    AccessibilityInteractionEvent,
    decode_event_batch,
)
from perception.observation import ObservationPackage
from shared.schemas import Action, ActionTargetSnapshot, AgentState, CanonicalUI, ObservationMode, UIElement


def _event(*, sequence: int = 2, bounds=(10, 20, 30, 40)):
    return AccessibilityInteractionEvent(
        sequence=sequence, monotonic_ms=123.0, event_type=1,
        package="com.example", window_id=7, source_class="android.widget.Button",
        resource_id="com.example:id/next", bounds=bounds, clickable=True,
        long_clickable=False, scrollable=False, editable=False, password=False,
    )


def test_event_batch_decoder_is_strict_and_preserves_order():
    raw_event = {
        "sequence": 2, "monotonic_ms": 123, "event_type": 1,
        "package": "com.example", "window_id": 7,
        "source_class": "android.widget.Button", "resource_id": "com.example:id/next",
        "bounds": [10, 20, 30, 40], "clickable": True,
        "long_clickable": False, "scrollable": False, "editable": False,
        "password": False,
    }
    decoded = decode_event_batch({
        "schema_version": 1, "oldest_sequence": 2, "current_sequence": 2,
        "complete_coverage": True, "events": [raw_event],
    })
    assert decoded.events == (_event(),)
    with pytest.raises(Exception, match="strictly ordered"):
        decode_event_batch({
            "schema_version": 1, "oldest_sequence": 2, "current_sequence": 2,
            "complete_coverage": True, "events": [raw_event, raw_event],
        })
    with pytest.raises(Exception, match="unsupported fields"):
        decode_event_batch({
            "schema_version": 1, "oldest_sequence": 2, "current_sequence": 2,
            "complete_coverage": True, "events": [{**raw_event, "text": "secret"}],
        })


def test_ack_matches_mechanically_and_never_promotes_semantics():
    target = ActionTargetSnapshot(
        package="com.example", window_id=7, source_class="Button",
        resource_id="next", bounds=[10, 20, 30, 40],
    )
    batch = AccessibilityEventBatch(2, 2, True, (_event(),))
    assert interaction_ack_for_batch(Action(type="tap_xy", x=20, y=30), target, batch) == (
        "confirmed", "accessibility_event",
    )
    unrelated = AccessibilityEventBatch(2, 2, True, (_event(bounds=(50, 60, 70, 80)),))
    assert interaction_ack_for_batch(Action(type="tap_xy", x=20, y=30), target, unrelated) == (
        "unobserved", None,
    )
    incomplete = AccessibilityEventBatch(2, 2, False, ())
    assert interaction_ack_for_batch(Action(type="tap_xy", x=20, y=30), target, incomplete) == (
        "unavailable", None,
    )


@pytest.mark.asyncio
async def test_multi_region_tool_reads_clean_pixels_and_compares_colors():
    image = Image.new("RGB", (20, 10), "red")
    for x in range(10, 20):
        for y in range(10):
            image.putpixel((x, y), (0, 255, 0))
    out = BytesIO(); image.save(out, format="PNG")
    package = ObservationPackage(
        ui=CanonicalUI(app_id="com.example"), mode=ObservationMode.IMAGE_ONLY,
        text_for_llm="", image_for_llm=b"annotated", annotated_png=b"annotated",
        clean_png=out.getvalue(), gap_reasons=[], observation_id="obs-current",
        frame_width=20, frame_height=10, model_image_width=20, model_image_height=10,
    )
    context = ToolExecutionContext(AgentRole.EXECUTOR, "i", {
        "active_package": package, "active_observation_id": "obs-current",
    })
    result = await make_inspect_image_regions_handler()({
        "observation_id": "obs-current",
        "regions": [
            {"bounds": [0, 0, 10, 10], "inset_ratio": 0.1},
            {"bounds": [10, 0, 20, 10], "inset_ratio": 0.1},
        ],
        "metrics": ["median_rgb", "dominant_rgb", "lab"],
        "pairs": [["region:1", "region:2"]],
    }, context)
    assert result.data["regions"]["region:1"]["median_rgb"] == [255, 0, 0]
    assert result.data["regions"]["region:2"]["median_rgb"] == [0, 255, 0]
    assert result.data["delta_e_2000"][0][2] > 80
    assert set(result.data["regions"]["region:1"]) == {
        "median_rgb", "dominant_rgb", "lab", "sample_bounds",
    }
    with pytest.raises(ValueError, match="stale"):
        await make_inspect_image_regions_handler()({
            "observation_id": "old", "regions": [{"bounds": [0, 0, 1, 1]}],
            "metrics": ["median_rgb"],
        }, context)


@pytest.mark.asyncio
async def test_region_inspection_insets_noise_limits_and_collapsed_mapping():
    image = Image.new("RGB", (30, 10), "black")
    for x in range(2, 28):
        for y in range(2, 8):
            image.putpixel((x, y), (255 if x < 20 else 254, 0, 0))
    out = BytesIO(); image.save(out, format="PNG")
    package = ObservationPackage(
        ui=CanonicalUI(app_id="com.example"), mode=ObservationMode.IMAGE_ONLY,
        text_for_llm="", image_for_llm=b"annotated", annotated_png=b"annotated",
        clean_png=out.getvalue(), gap_reasons=[], observation_id="obs-current",
        frame_width=30, frame_height=10, model_image_width=30, model_image_height=10,
    )
    context = ToolExecutionContext(AgentRole.EXECUTOR, "i", {
        "active_package": package, "active_observation_id": "obs-current",
    })
    handler = make_inspect_image_regions_handler()
    args = {
        "observation_id": "obs-current",
        "regions": [
            {"bounds": [0, 0, 20, 10], "inset_ratio": 0.2},
            {"bounds": [20, 0, 30, 10], "inset_ratio": 0.2},
        ],
        "metrics": ["median_rgb", "dominant_rgb", "lab"],
        "pairs": [["region:1", "region:2"]],
    }
    result = await handler(args, context)
    assert result.data["regions"]["region:1"]["median_rgb"] == [255, 0, 0]
    assert result.data["regions"]["region:2"]["median_rgb"] == [254, 0, 0]
    assert 0 < result.data["delta_e_2000"][0][2] < 1
    assert "sample_count" not in str(result.data)
    assert "rgb_variance" not in str(result.data)

    with pytest.raises(ValueError, match="duplicate"):
        await handler(args, context)
    assert context.state["image_region_inspection_calls"] == 1
    second = {**args, "metrics": ["median_rgb"]}
    await handler(second, context)
    with pytest.raises(ValueError, match="allowance"):
        await handler({**args, "metrics": ["dominant_rgb"]}, context)

    too_many = ToolExecutionContext(AgentRole.EXECUTOR, "i", {
        "active_package": package, "active_observation_id": "obs-current",
    })
    with pytest.raises(ValueError, match="1..12"):
        await make_inspect_image_regions_handler()({
            "observation_id": "obs-current",
            "regions": [
                {"bounds": [0, 0, 1, 1]} for i in range(13)
            ],
            "metrics": ["median_rgb"],
        }, too_many)

    tiny = Image.new("RGB", (1, 1), "red")
    tiny_out = BytesIO(); tiny.save(tiny_out, format="PNG")
    tiny_package = ObservationPackage(
        ui=CanonicalUI(app_id="com.example"), mode=ObservationMode.IMAGE_ONLY,
        text_for_llm="", image_for_llm=b"x", annotated_png=None,
        clean_png=tiny_out.getvalue(),
        gap_reasons=[], observation_id="tiny", frame_width=100, frame_height=100,
        model_image_width=100, model_image_height=100,
    )
    tiny_context = ToolExecutionContext(AgentRole.EXECUTOR, "i", {
        "active_package": tiny_package, "active_observation_id": "tiny",
    })
    with pytest.raises(ValueError, match="collapses"):
        await make_inspect_image_regions_handler()({
            "observation_id": "tiny",
            "regions": [{"bounds": [0, 0, 1, 1]}],
            "metrics": ["median_rgb"],
        }, tiny_context)
