"""Invocation-local observation identity, transforms, and element namespaces."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas import Action, CanonicalUI, UIElement

COORDINATE_ROUNDING_EPSILON = 1e-6


class ObservationTransform(BaseModel):
    """Immutable model-image to driver-frame transform metadata."""

    model_config = ConfigDict(frozen=True)

    transform_id: str
    model_image_size: tuple[int, int]
    frame_geometry: tuple[int, int]
    rotation_degrees: int = 0
    crop_box: tuple[float, float, float, float] | None = None

    def _crop(self) -> tuple[float, float, float, float]:
        if self.crop_box is not None:
            return self.crop_box
        width, height = self.frame_geometry
        return (0.0, 0.0, float(width), float(height))

    def transform_point(self, x: float, y: float) -> tuple[float, float]:
        """Map one model-image pixel through the declared crop/rotation."""
        model_w, model_h = self.model_image_size
        left, top, right, bottom = self._crop()
        if model_w <= 0 or model_h <= 0 or right <= left or bottom <= top:
            raise ValueError("unknown_coordinate_geometry")

        if (
            self.rotation_degrees % 360 == 0
            and self.crop_box is None
            and self.model_image_size == self.frame_geometry
        ):
            return (float(x), float(y))

        # Pixel-space ratios preserve the existing resize convention: a point
        # is scaled by target-axis/model-axis. Rotation is then inverted into
        # the source crop. Endpoints are validated separately before dispatch.
        u = float(x) / float(model_w)
        v = float(y) / float(model_h)
        rotation = self.rotation_degrees % 360
        if rotation == 0:
            source_u, source_v = u, v
        elif rotation == 90:
            source_u = v
            source_v = float(model_w - 1 - x) / float(model_w)
        elif rotation == 180:
            source_u = float(model_w - 1 - x) / float(model_w)
            source_v = float(model_h - 1 - y) / float(model_h)
        elif rotation == 270:
            source_u = float(model_h - 1 - y) / float(model_h)
            source_v = u
        else:
            raise ValueError("unsupported_rotation")
        return (
            left + source_u * (right - left),
            top + source_v * (bottom - top),
        )


class ObservationRegistryEntry(BaseModel):
    """One immutable action basis owned by an invocation."""

    model_config = ConfigDict(frozen=True)

    observation_id: str
    coordinate_space_id: str
    actionable: bool
    index_actionable: bool = True
    captured_monotonic_ms: float = 0.0
    model_image_size: tuple[int, int] | None = None
    frame_geometry: tuple[int, int] | None = None
    rotation_degrees: int = 0
    crop_box: tuple[float, float, float, float] | None = None
    transform: ObservationTransform | None = None
    element_set_id: str
    elements: tuple[UIElement, ...] = Field(default_factory=tuple)

    def deterministic_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        )

    def persistence_projection(self) -> dict[str, Any]:
        """Return replay metadata with password values removed at the source."""
        payload = self.model_dump(mode="json")
        for element in payload.get("elements", []):
            if not isinstance(element, dict):
                continue
            states = element.get("states") or {}
            if not isinstance(states, dict) or not bool(states.get("password")):
                continue
            element.update({
                "text": "",
                "desc": "",
                "hint": "",
                "ctx": "",
                "raw_fields_redacted": True,
            })
        return payload

    def ui(self) -> CanonicalUI:
        return CanonicalUI(elements=list(self.elements))


class ObservationRegistry:
    """Mutable container whose entries remain immutable once registered."""

    def __init__(self) -> None:
        self._entries: dict[str, ObservationRegistryEntry] = {}
        self.active_observation_id: str = ""

    def register(self, entry: ObservationRegistryEntry, *, make_active: bool = False) -> None:
        existing = self._entries.get(entry.observation_id)
        if existing is not None and existing != entry:
            raise ValueError(f"observation identity is immutable: {entry.observation_id}")
        self._entries[entry.observation_id] = entry
        if make_active:
            if not entry.actionable:
                raise ValueError("non-actionable observation cannot become active")
            self.active_observation_id = entry.observation_id

    def get(self, observation_id: str) -> ObservationRegistryEntry | None:
        return self._entries.get(observation_id)

    def clear_active(self) -> None:
        """Clear the action basis while retaining immutable evidence entries."""
        self.active_observation_id = ""

    def values(self) -> tuple[ObservationRegistryEntry, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))

    def deterministic_json(self) -> str:
        payload = {
            "active_observation_id": self.active_observation_id,
            "entries": [entry.model_dump(mode="json") for entry in self.values()],
        }
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def make_registry_entry(
    *,
    observation_id: str,
    coordinate_space_id: str,
    ui: CanonicalUI,
    actionable: bool,
    index_actionable: bool = True,
    captured_monotonic_ms: float,
    model_image_size: tuple[int, int] | None,
    frame_geometry: tuple[int, int] | None,
    rotation_degrees: int = 0,
    crop_box: tuple[float, float, float, float] | None = None,
    transform_id: str = "",
) -> ObservationRegistryEntry:
    model_size = model_image_size if model_image_size and all(v > 0 for v in model_image_size) else None
    frame_size = frame_geometry if frame_geometry and all(v > 0 for v in frame_geometry) else None
    transform = None
    if model_size is not None and frame_size is not None:
        transform = ObservationTransform(
            transform_id=transform_id or f"transform_{coordinate_space_id}",
            model_image_size=model_size,
            frame_geometry=frame_size,
            rotation_degrees=rotation_degrees,
            crop_box=crop_box,
        )
    return ObservationRegistryEntry(
        observation_id=observation_id,
        coordinate_space_id=coordinate_space_id,
        actionable=actionable,
        index_actionable=bool(index_actionable),
        captured_monotonic_ms=captured_monotonic_ms,
        model_image_size=model_size,
        frame_geometry=frame_size,
        rotation_degrees=rotation_degrees,
        crop_box=crop_box,
        transform=transform,
        element_set_id=f"elements_{observation_id}",
        elements=tuple(ui.elements),
    )


COORDINATE_ACTION_TYPES = frozenset({"tap_xy", "swipe", "drag"})
INDEX_ACTION_TYPES = frozenset({"tap"})


def action_uses_coordinates(action: Action) -> bool:
    return action.type in COORDINATE_ACTION_TYPES or (
        action.type in {"long_press", "skill_authorized_action"}
        and action.index is None
    )


def action_uses_index(action: Action) -> bool:
    return action.type in INDEX_ACTION_TYPES or (
        action.type in {
            "long_press", "replace_text", "skill_authorized_action",
        }
        and action.index is not None
    )


def transform_action(action: Action, transform: ObservationTransform) -> Action:
    """Apply one basis-owned transform to all submitted coordinate pairs."""
    data = action.model_dump()
    if action.x is not None and action.y is not None:
        data["x"], data["y"] = transform.transform_point(action.x, action.y)
    if action.x2 is not None and action.y2 is not None:
        data["x2"], data["y2"] = transform.transform_point(action.x2, action.y2)
    return Action.model_validate(data)


def validate_action_bounds(
    action: Action,
    *,
    width: int,
    height: int,
    epsilon: float,
) -> tuple[Action | None, bool, dict[str, Any]]:
    """Validate coordinates, normalizing rounding-only boundary drift."""
    if width <= 0 or height <= 0:
        return None, False, {"valid": False, "reason": "unknown_coordinate_geometry"}
    data = action.model_dump()
    normalized = False
    evidence: dict[str, Any] = {
        "valid": True,
        "bounds": [0, 0, width - 1, height - 1],
        "epsilon": epsilon,
        "coordinates": {},
    }
    axes = (("x", width), ("x2", width), ("y", height), ("y2", height))
    for field_name, axis_size in axes:
        raw = getattr(action, field_name)
        if raw is None:
            continue
        value = float(raw)
        maximum = float(axis_size - 1)
        evidence["coordinates"][field_name] = value
        if -epsilon <= value < 0.0:
            value = 0.0
            normalized = True
        elif maximum < value <= maximum + epsilon:
            value = maximum
            normalized = True
        elif value < 0.0 or value > maximum or not math.isfinite(value):
            evidence.update({"valid": False, "reason": "coordinate_out_of_bounds"})
            return None, normalized, evidence
        data[field_name] = value
    evidence["normalized"] = normalized
    return Action.model_validate(data), normalized, evidence


def validate_surface_containment(action: Action, entry: ObservationRegistryEntry | None) -> str:
    """Validate a declared confined drag after transformation to device coordinates.

    A straight segment inside a rectangular surface is contained iff both endpoints
    are contained. This does not infer a target from proximity or classify task intent.
    """
    if action.surface_index is None:
        return ""
    if action.type != "drag":
        return "surface_index_requires_drag"
    if entry is None or not entry.actionable or not entry.index_actionable:
        return "surface_index_unavailable"
    matches = [e for e in entry.elements if e.index == action.surface_index and e.interactable]
    if len(matches) != 1 or len(matches[0].bounds) != 4:
        return "unknown_surface_index"
    x1, y1, x2, y2 = matches[0].bounds
    for x, y in ((action.x, action.y), (action.x2, action.y2)):
        if x is None or y is None or not (x1 <= x < x2 and y1 <= y < y2):
            return "drag_outside_surface"
    return ""
