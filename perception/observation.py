"""Build tree-first LLM observations from one current tree/pixel capture."""

from __future__ import annotations

import io
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from perception.filters import DetailedFilter, TreeFilter
from perception.normalizer import (
    normalize_a11y_tree,
    render_semantic_tree,
)
from perception.som import render_som
from shared.schemas import (
    CanonicalUI,
    InteractionStateEvidence,
    ObservationMode,
)
from perception.input_evidence import build_interaction_state


# Rough vision-token penalty for an attached annotated screenshot.
_IMAGE_TOKEN_PENALTY = 1200

# Soft-degrade marker stamped on raw trees by AndroidDriver.get_frame Frame Gate.
FRAME_GATE_DEGRADED_KEY = "_frame_gate_degraded"
TREE_CAPTURE_INCOMPLETE = "tree_capture_incomplete"
TREE_UNUSABLE = "tree_unusable"


def mechanical_tree_usable(
    capture_meta: dict[str, Any], gap_reasons: list[str] | tuple[str, ...],
) -> bool:
    """Return the single typed, model-free tree usability fact."""
    return bool(
        TREE_UNUSABLE not in gap_reasons
        and capture_meta.get("complete") is True
        and capture_meta.get("coordinate_compatible") is True
        and capture_meta.get("tree_providers_exhausted", False) is False
    )
@dataclass
class ObservationPackage:
    """Observation handed to the executor model (and mirrored into Trace)."""

    ui: CanonicalUI
    mode: ObservationMode
    text_for_llm: str
    image_for_llm: bytes | None
    annotated_png: bytes | None
    gap_reasons: list[str]
    # Current unannotated pixels retained only in the active in-memory
    # package. Decision roles and Executor derive their model images from
    # this same frame so pixels, indices, and coordinate transforms align.
    clean_png: bytes | None = None
    estimated_tokens: int = 0
    # Artifact refs populated by the Orchestrator when it persists the frame.
    # `som_ref` points at the annotated SoM PNG under `som/`; `tree_ref` points
    # at the rendered semantic-tree JSON under `trees/`. None when no
    # ArtifactStore is wired (e.g. unit tests with no artifacts).
    som_ref: str | None = None
    tree_ref: str | None = None
    interaction_state: InteractionStateEvidence | None = None
    observation_id: str = field(default_factory=lambda: f"obs_{uuid.uuid4().hex}")
    coordinate_space_id: str = field(default_factory=lambda: f"space_{uuid.uuid4().hex}")
    actionable: bool = True
    # Accessibility indices require a complete tree aligned to the current
    # pixels. Coordinate grounding can remain actionable without them.
    index_actionable: bool = True
    captured_monotonic_ms: float = field(default_factory=lambda: time.monotonic() * 1000)
    frame_width: int = 0
    frame_height: int = 0
    model_image_width: int = 0
    model_image_height: int = 0
    rotation_degrees: int = 0
    crop_box: tuple[float, float, float, float] | None = None
    transform_id: str = ""
    evidence_ref: str | None = None
    capture_meta: dict[str, Any] = field(default_factory=dict)
    transaction_id: str = ""
    accepted: bool = True
    acceptance_reason: str = "accepted"
    observation_source: str = "fresh_current"

    def __post_init__(self) -> None:
        if not self.transform_id:
            self.transform_id = f"transform_{self.coordinate_space_id}"


def estimate_tokens(text: str, image_bytes: bytes | None) -> int:
    """Rough token estimate: ~4 chars/token for text + fixed image penalty.

    Observation-side only; never influences how the model is called.
    """
    text_tokens = len(text) // 4
    return text_tokens + (_IMAGE_TOKEN_PENALTY if image_bytes else 0)


def decoded_image_size(image: bytes | None) -> tuple[int, int]:
    """Return decoded pixel geometry, or ``(0, 0)`` for absent/invalid bytes."""
    if not image:
        return (0, 0)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as parsed:
            width, height = int(parsed.width), int(parsed.height)
            parsed.load()
            return (width, height)
    except Exception:  # noqa: BLE001
        return (0, 0)


def assess_tree(ui: CanonicalUI, *, frame_gate_degraded: bool = False) -> list[str]:
    """Return only severe, deterministic tree-quality reasons."""
    reasons: list[str] = []
    if not ui.capture_complete or frame_gate_degraded:
        reasons.append(TREE_CAPTURE_INCOMPLETE)
    if not has_model_visible_tree_content(ui):
        reasons.append(TREE_UNUSABLE)
    return reasons


def has_model_visible_tree_content(ui: CanonicalUI) -> bool:
    """Return whether the rendered tree contains a model-visible UI fact.

    Provider completeness and raw node counts are insufficient: filtering can
    leave only synthetic WindowSet/window wrappers. This check uses only
    retained structural/raw source presence and never classifies UI meaning.
    """
    return any(
        bool(
            element.interactable
            or element.text
            or element.desc
            or element.hint
            or any((element.states or {}).get(name) for name in (
                "focused", "selected", "checked", "editable", "scrollable",
            ))
        )
        for element in ui.semantic_tree
        if not element.window_wrapper
    )


@dataclass(frozen=True)
class PreparedObservation:
    raw_tree: dict
    screenshot: bytes | None
    ui: CanonicalUI
    gap_reasons: list[str]
    frame_geometry: tuple[int, int] | None = None
    rotation_degrees: int = 0
    crop_box: tuple[float, float, float, float] | None = None


class ObservationBuilder:
    """Normalize and package one driver-supplied tree/image pair."""

    def __init__(
        self,
        detailed_filter: TreeFilter | None = None,
    ) -> None:
        # Explicit custom filters stay fixed for deterministic tests. The
        # production default is rebuilt from each capture's real geometry.
        self.detailed_filter = detailed_filter

    def build(
        self,
        frame: tuple[dict, bytes | None],
        *,
        app_id: str = "",
        activity: str = "",
        will_send_image: bool = False,
        frame_geometry: tuple[int, int] | None = None,
        rotation_degrees: int | None = None,
        crop_box: tuple[float, float, float, float] | None = None,
    ) -> ObservationPackage:
        """Compatibility convenience; production Harness chooses attachment."""
        prepared = self.prepare(
            frame,
            app_id=app_id,
            activity=activity,
            frame_geometry=frame_geometry,
            rotation_degrees=rotation_degrees,
            crop_box=crop_box,
        )
        return self.package(
            prepared,
            attach_image=bool(prepared.gap_reasons) or will_send_image,
        )

    def prepare(
        self,
        frame: tuple[dict, bytes | None],
        *,
        app_id: str = "",
        activity: str = "",
        frame_geometry: tuple[int, int] | None = None,
        rotation_degrees: int | None = None,
        crop_box: tuple[float, float, float, float] | None = None,
    ) -> PreparedObservation:
        """Normalize once and return the pure tree-quality assessment."""
        raw_tree, screenshot = frame
        capture = raw_tree.setdefault("_capture", {}) if isinstance(raw_tree, dict) else {}
        if not isinstance(capture, dict):
            capture = {}
            if isinstance(raw_tree, dict):
                raw_tree["_capture"] = capture
        declared_geometry = frame_geometry or tuple(capture.get("frame_geometry") or ())
        if len(declared_geometry) != 2 or any(int(value) <= 0 for value in declared_geometry):
            declared_geometry = decoded_image_size(screenshot)
        decoded_geometry = decoded_image_size(screenshot)
        if screenshot is not None:
            capture["pixel_decode_valid"] = all(value > 0 for value in decoded_geometry)
            capture["pixel_bytes_present"] = True
            if not capture["pixel_decode_valid"]:
                capture["pixel_decode_failure"] = "invalid_or_zero_geometry"
                screenshot = None
            else:
                capture.pop("pixel_decode_failure", None)
        else:
            capture["pixel_bytes_present"] = False
            capture["pixel_decode_valid"] = False
            capture.pop("pixel_decode_failure", None)
        trusted_geometry = (
            (int(declared_geometry[0]), int(declared_geometry[1]))
            if len(declared_geometry) == 2
            and all(int(value) > 0 for value in declared_geometry)
            else None
        )
        tree_filter = self.detailed_filter or DetailedFilter(screen=trusted_geometry)
        frame_gate_degraded = bool(
            isinstance(raw_tree, dict) and raw_tree.get(FRAME_GATE_DEGRADED_KEY)
        )
        normalize_started = time.monotonic()
        ui = normalize_a11y_tree(
            raw_tree,
            app_id=app_id,
            activity=activity,
            allow_tree_app_fallback=False,
            tree_filter=tree_filter,
        )
        if isinstance(raw_tree, dict):
            capture = raw_tree.setdefault("_capture", {})
            if isinstance(capture, dict):
                capture["normalization_elapsed_ms"] = round(
                    (time.monotonic() - normalize_started) * 1000.0, 3
                )
                capture["semantic_node_count"] = len(ui.semantic_tree)
                capture["actionable_node_count"] = len(ui.elements)
        reasons = assess_tree(ui, frame_gate_degraded=frame_gate_degraded)
        declared_crop = crop_box or capture.get("crop_box")
        parsed_crop = (
            tuple(float(value) for value in declared_crop)
            if isinstance(declared_crop, (list, tuple)) and len(declared_crop) == 4
            else None
        )
        return PreparedObservation(
            raw_tree,
            screenshot,
            ui,
            reasons,
            frame_geometry=trusted_geometry or (0, 0),
            rotation_degrees=int(
                capture.get("rotation_degrees", 0)
                if rotation_degrees is None else rotation_degrees
            ),
            crop_box=parsed_crop,
        )

    def package(
        self, prepared: PreparedObservation, *, attach_image: bool
    ) -> ObservationPackage:
        """Package a prepared observation without adding lifecycle policy."""
        raw_tree = prepared.raw_tree
        screenshot = prepared.screenshot
        ui = prepared.ui
        reasons = list(prepared.gap_reasons)
        image_geometry = decoded_image_size(screenshot)
        pixels_decodable = bool(
            screenshot is not None and all(value > 0 for value in image_geometry)
        )
        frame_geometry = prepared.frame_geometry or image_geometry
        render_kwargs: dict[str, tuple[int, int]] = {}
        if all(value > 0 for value in (*frame_geometry, *image_geometry)):
            render_kwargs = {"src_size": frame_geometry, "dst_size": image_geometry}

        capture_meta = (
            dict(raw_tree.get("_capture") or {}) if isinstance(raw_tree, dict) else {}
        )
        tree_usable = bool(
            not reasons and mechanical_tree_usable(capture_meta, reasons)
        )
        tree_index_actionable = bool(
            tree_usable
            and ui.elements
            and capture_meta.get("tree_index_eligible", True) is not False
        )

        annotated: bytes | None = None
        image_for_llm: bytes | None = None
        mode = ObservationMode.TREE_ONLY

        if attach_image and pixels_decodable:
            if tree_usable:
                annotated = render_som(
                    screenshot,
                    ui.elements if tree_index_actionable else [],
                    **render_kwargs,
                )
                image_for_llm = annotated
                mode = ObservationMode.TREE_PLUS_IMAGE
            else:
                # Unavailable/unaligned trees cannot safely label current
                # pixels. Keep the frame clean and make coordinates the only
                # visual grounding path.
                annotated = screenshot
                image_for_llm = screenshot
                mode = ObservationMode.IMAGE_ONLY
        elif pixels_decodable:
            # Trace/replay always gets an accessibility SoM when a shot exists,
            # including when the LLM itself receives tree-only evidence.
            annotated = render_som(
                screenshot,
                ui.elements if tree_index_actionable else [],
                **render_kwargs,
            )
            if not tree_usable:
                # Role attachment is deferred, but evidence capability is not:
                # downstream roles must prepare a clean coordinate image rather
                # than treat an unusable tree as available.
                mode = ObservationMode.IMAGE_ONLY

        if not tree_usable:
            # The raw tree remains in trace input, but untyped/incomplete or
            # coordinate-incompatible semantics cannot become role evidence.
            ui = CanonicalUI(
                platform=ui.platform,
                app_id=ui.app_id,
                activity=ui.activity,
                capture_provider=ui.capture_provider,
                capture_complete=False,
                capture_generation=ui.capture_generation,
                capture_reasons=list(ui.capture_reasons),
            )

        text = render_semantic_tree(
            ui,
            include_action_indexes=tree_index_actionable,
        )
        est = estimate_tokens(text, image_for_llm)
        ui.estimated_tokens = est

        frame_width, frame_height = frame_geometry
        model_image_width, model_image_height = decoded_image_size(image_for_llm)
        captured_monotonic_ms = float(
            capture_meta.get("pixel_monotonic_ms") or time.monotonic() * 1000.0
        )
        return ObservationPackage(
            ui=ui,
            mode=mode,
            text_for_llm=text,
            image_for_llm=image_for_llm,
            annotated_png=annotated,
            gap_reasons=reasons,
            clean_png=screenshot,
            estimated_tokens=est,
            interaction_state=build_interaction_state(ui),
            captured_monotonic_ms=captured_monotonic_ms,
            frame_width=frame_width,
            frame_height=frame_height,
            model_image_width=model_image_width,
            model_image_height=model_image_height,
            actionable=bool(
                tree_index_actionable
                or (
                    pixels_decodable
                    and frame_width > 0
                    and frame_height > 0
                )
            ),
            index_actionable=tree_index_actionable,
            capture_meta=capture_meta,
            rotation_degrees=prepared.rotation_degrees,
            crop_box=prepared.crop_box,
        )
