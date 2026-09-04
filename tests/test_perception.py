"""Accessibility-only perception and observation packaging."""

import io

import pytest
from PIL import Image

from perception.normalizer import normalize_a11y_tree
from perception.observation import (
    FRAME_GATE_DEGRADED_KEY,
    ObservationBuilder,
    TREE_CAPTURE_INCOMPLETE,
)
from agent.action_observation import observation_acceptance_reason
from shared.schemas import ObservationMode
from perception.image_utils import expected_role_image_delivery


def test_role_image_delivery_expectation_is_structural_and_shared() -> None:
    image_only = expected_role_image_delivery(
        role="executor",
        accepted=True,
        tree_available=False,
        index_actionable=False,
        pixels_available=True,
    )
    indexed = expected_role_image_delivery(
        role="executor",
        accepted=True,
        tree_available=True,
        index_actionable=True,
        pixels_available=True,
    )

    assert image_only == {
        "would_attach": True,
        "mode": "image-only",
        "visual_kind": "clean",
        "profile_id": "image-only-calibrated-1080-q75",
        "long_edge": 1080,
        "jpeg_quality": 75,
    }
    assert indexed["mode"] == "tree+image"
    assert indexed["visual_kind"] == "som"
    assert indexed["profile_id"] == "tree-som-1080-q75"


TREE = {
    "class": "Root",
    "_capture": {"complete": True, "coordinate_compatible": True},
    "children": [
        {
            "class": "android.widget.Button",
            "text": "OK",
            "clickable": True,
            "bounds": [10, 10, 100, 80],
        },
        {
            "class": "android.widget.TextView",
            "text": "label",
            "clickable": False,
            "bounds": [10, 100, 200, 140],
        },
    ],
}


def _png() -> bytes:
    image = Image.new("RGB", (200, 200), color=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_normalize_assigns_indices_per_frame():
    first = normalize_a11y_tree(TREE, app_id="a")
    second = normalize_a11y_tree(TREE, app_id="a")
    assert [element.index for element in first.elements] == [0]
    assert [element.index for element in second.elements] == [0]
    assert first.elements[0].text == "OK"


def test_tree_only_observation_retains_trace_som():
    package = ObservationBuilder().build((TREE, _png()), app_id="demo")
    assert package.mode == ObservationMode.TREE_ONLY
    assert package.image_for_llm is None
    assert package.annotated_png is not None
    assert package.gap_reasons == []


def test_explicit_image_uses_accessibility_marks_only():
    package = ObservationBuilder().build(
        (TREE, _png()), app_id="demo", will_send_image=True,
    )
    assert package.mode == ObservationMode.TREE_PLUS_IMAGE
    assert package.image_for_llm is not None
    assert package.index_actionable is True
    assert [element.text for element in package.ui.elements] == ["OK"]
    assert package.gap_reasons == []


def test_explicit_image_without_screenshot_stays_tree_only():
    package = ObservationBuilder().build(
        (TREE, None), app_id="demo", will_send_image=True,
    )
    assert package.mode == ObservationMode.TREE_ONLY
    assert package.image_for_llm is None


def test_valid_tree_with_invalid_pixels_degrades_to_tree_only():
    tree = {
        **TREE,
        "_capture": {
            "complete": True,
            "coordinate_compatible": True,
            "frame_geometry": [200, 200],
        },
    }
    package = ObservationBuilder().build(
        (tree, b"invalid-image"), app_id="demo", will_send_image=True,
    )

    assert package.mode == ObservationMode.TREE_ONLY
    assert package.clean_png is None
    assert package.image_for_llm is None
    assert package.annotated_png is None
    assert package.index_actionable is True
    assert package.capture_meta["pixel_bytes_present"] is True
    assert package.capture_meta["pixel_decode_valid"] is False
    assert package.capture_meta["pixel_decode_failure"] == "invalid_or_zero_geometry"
    assert observation_acceptance_reason(package) == ""


@pytest.mark.parametrize(
    "pixels",
    [None, b"invalid-image", pytest.param(_png()[: len(_png()) // 2], id="truncated-png")],
)
def test_unusable_tree_without_decodable_pixels_is_rejected(pixels):
    tree = {
        "class": "Root",
        "_capture": {
            "complete": True,
            "coordinate_compatible": True,
            "tree_providers_exhausted": True,
            "frame_geometry": [200, 200],
        },
        "children": [],
    }
    package = ObservationBuilder().build(
        (tree, pixels), app_id="demo", will_send_image=True,
    )

    assert package.mode == ObservationMode.TREE_ONLY
    assert package.clean_png is None
    assert package.actionable is False
    assert package.capture_meta["pixel_decode_valid"] is False
    assert observation_acceptance_reason(package) == "grounding_evidence_unavailable"


def test_decode_telemetry_is_overwritten_when_raw_tree_is_reused():
    tree = {
        **TREE,
        "_capture": {
            "complete": True,
            "coordinate_compatible": True,
            "frame_geometry": [200, 200],
        },
    }
    builder = ObservationBuilder()
    builder.build((tree, b"invalid-image"), app_id="demo")
    package = builder.build((tree, _png()), app_id="demo")

    assert package.capture_meta["pixel_bytes_present"] is True
    assert package.capture_meta["pixel_decode_valid"] is True
    assert "pixel_decode_failure" not in package.capture_meta


def test_deferred_role_image_does_not_mislabel_unusable_tree_as_tree_only():
    tree = {
        "class": "Root",
        "_capture": {
            "complete": False,
            "coordinate_compatible": False,
            "tree_providers_exhausted": True,
        },
        "children": [],
    }
    prepared = ObservationBuilder().prepare((tree, _png()), frame_geometry=(200, 200))

    package = ObservationBuilder().package(prepared, attach_image=False)

    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.index_actionable is False
    assert package.clean_png is not None
    assert package.image_for_llm is None


def test_usable_tree_without_indexed_elements_remains_tree_evidence():
    tree = {
        "class": "Root",
        "_capture": {"complete": True, "coordinate_compatible": True},
        "children": [{
            "class": "android.widget.TextView",
            "text": "Static content",
            "clickable": False,
            "bounds": [10, 10, 180, 40],
        }],
    }

    package = ObservationBuilder().build((tree, _png()), app_id="demo")

    assert package.mode == ObservationMode.TREE_ONLY
    assert package.index_actionable is False
    assert package.image_for_llm is None

def test_explicit_image_does_not_invent_gap_reasons_across_ticks():
    builder = ObservationBuilder()
    first = builder.build((TREE, _png()), app_id="demo", will_send_image=True)
    second = builder.build((TREE, _png()), app_id="demo", will_send_image=True)
    assert first.gap_reasons == []
    assert second.gap_reasons == []


def test_frame_gate_degraded_forces_image_and_tree_reason():
    tree = {"class": "hierarchy", "children": [], FRAME_GATE_DEGRADED_KEY: True}
    package = ObservationBuilder().build(
        (tree, _png()), app_id="demo", will_send_image=False,
    )
    assert TREE_CAPTURE_INCOMPLETE in package.gap_reasons
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.image_for_llm is not None
    assert package.image_for_llm == package.clean_png
    assert package.index_actionable is False


def test_dynamic_filter_uses_explicit_portrait_geometry():
    tree = {
        "class": "Root",
        "_capture": {"complete": True, "coordinate_compatible": True},
        "bounds": [0, 0, 1440, 3200],
        "children": [{
            "class": "android.widget.Button",
            "text": "Bottom",
            "clickable": True,
            "bounds": [100, 2900, 600, 3100],
        }],
    }
    prepared = ObservationBuilder().prepare((tree, None), frame_geometry=(1440, 3200))
    assert any(element.text == "Bottom" for element in prepared.ui.elements)


def test_dynamic_filter_uses_explicit_landscape_geometry():
    tree = {
        "class": "Root",
        "_capture": {"complete": True, "coordinate_compatible": True},
        "bounds": [0, 0, 2670, 1200],
        "children": [{
            "class": "android.widget.Button",
            "text": "Right",
            "clickable": True,
            "bounds": [2400, 100, 2600, 300],
        }],
    }
    prepared = ObservationBuilder().prepare((tree, None), frame_geometry=(2670, 1200))
    assert any(element.text == "Right" for element in prepared.ui.elements)


def test_dynamic_filter_unknown_geometry_does_not_prune_by_guessed_screen():
    tree = {
        "class": "Root",
        "_capture": {"complete": True, "coordinate_compatible": True},
        "children": [{
            "class": "android.widget.Button",
            "text": "Unknown geometry",
            "clickable": True,
            "bounds": [2400, 100, 2600, 300],
        }],
    }
    prepared = ObservationBuilder().prepare((tree, None))
    assert any(element.text == "Unknown geometry" for element in prepared.ui.elements)
