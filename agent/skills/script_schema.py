"""Versioned declarative input for the deterministic GUI script runner."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agent.skills.script_visual import VisualTemplate
from agent.skills.script_visual_object import VisualObject

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Label = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Timeout = Annotated[float, Field(gt=0, le=120, allow_inf_nan=False)]


class _ScriptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ScriptParameter(_ScriptModel):
    type: Literal["string"] = "string"
    max_length: int = Field(ge=1, le=4096, default=1024)


class SelectorFields(_ScriptModel):
    resource_id: Label | None = None
    role: Label | None = None
    text: Label | None = None
    text_parameter: Identifier | None = None
    desc: Label | None = None
    hint: Label | None = None
    context: Label | None = None
    checked: bool | None = None
    enabled: bool | None = None
    selected: bool | None = None

    @model_validator(mode="after")
    def check_fields(self):
        if not any(value is not None for value in
                   (self.resource_id, self.role, self.text, self.text_parameter, self.desc, self.hint, self.context)):
            raise ValueError("selector must specify semantic fields")
        if self.text is not None and self.text_parameter is not None:
            raise ValueError("selector text and text_parameter are mutually exclusive")
        return self


class RelativeSelector(_ScriptModel):
    anchor: SelectorFields
    relation: Literal["nearest", "above", "below", "left_of", "right_of"] = "nearest"


class SemanticSelector(SelectorFields):
    ancestor: SelectorFields | None = None
    relative_to: RelativeSelector | None = None
    occurrence: int | None = Field(default=None, ge=0)
    expected_count: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def check_disambiguation(self):
        if self.occurrence is not None:
            if self.relative_to is not None:
                raise ValueError("use either a relative anchor or a recorded occurrence")
            if self.expected_count is None and self.ancestor is None:
                raise ValueError("a recorded occurrence needs an expected count or an ancestor scope")
            if self.expected_count is not None and self.occurrence >= self.expected_count:
                raise ValueError("occurrence must be below expected_count")
        return self


class TargetCondition(_ScriptModel):
    type: Literal["target_present", "target_absent"]
    target: SemanticSelector
    visual: VisualTemplate | None = None


class AppCondition(_ScriptModel):
    type: Literal["app_is"]
    app_id: Identifier


class ActivityCondition(_ScriptModel):
    type: Literal["activity_is"]
    activity: Label


class VisualCondition(_ScriptModel):
    type: Literal["visual_present", "visual_absent"]
    target: VisualTemplate


class VisualObjectCondition(_ScriptModel):
    type: Literal["visual_object_present", "visual_object_absent"]
    target: VisualObject


ScriptCondition = Annotated[
    TargetCondition | AppCondition | ActivityCondition | VisualCondition | VisualObjectCondition,
    Field(discriminator="type"),
]


class TapAction(_ScriptModel):
    type: Literal["tap"]
    target: SemanticSelector
    visual: VisualTemplate | None = None
    object: VisualObject | None = None


class VisualTapAction(_ScriptModel):
    type: Literal["tap_visual"]
    target: VisualTemplate
    object: VisualObject | None = None


class LongPressAction(_ScriptModel):
    type: Literal["long_press"]
    target: SemanticSelector
    duration_ms: int = Field(ge=100, le=3000, default=1000)


class TypeAction(_ScriptModel):
    type: Literal["type", "replace_text"]
    target: SemanticSelector
    parameter: Identifier


class ScrollAction(_ScriptModel):
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"]


class SimpleAction(_ScriptModel):
    type: Literal["launch", "back", "wait"]


ScriptAction = Annotated[
    TapAction | VisualTapAction | LongPressAction | TypeAction | ScrollAction | SimpleAction,
    Field(discriminator="type"),
]

Conditions = Annotated[tuple[ScriptCondition, ...], Field(min_length=1, max_length=24, strict=False)]


class ScriptStep(_ScriptModel):
    id: Identifier
    before: Conditions
    blocked_when: Conditions | None = None
    action: ScriptAction
    after: Conditions
    retry_when: Conditions | None = None
    timeout_seconds: Timeout | None = None

    @model_validator(mode="after")
    def check_object_action(self):
        if not isinstance(self.action, (TapAction, VisualTapAction)) or self.action.object is None:
            return self
        if sum(part.role == "control" for part in self.action.object.parts) != 1:
            raise ValueError("visual object taps require exactly one control")
        outcomes = tuple(condition.target for condition in self.after
                         if isinstance(condition, VisualObjectCondition)
                         and condition.type == "visual_object_present"
                         and any(part.role == "outcome" for part in condition.target.parts))
        if not outcomes:
            raise ValueError("visual object taps require a positive object outcome")
        if any(not any(isinstance(condition, VisualObjectCondition)
                       and condition.type == "visual_object_absent" and condition.target == outcome
                       for condition in self.before) for outcome in outcomes):
            raise ValueError("the visual object outcome must be checked as absent before tapping")
        return self

    @property
    def timeout(self) -> float:
        if self.timeout_seconds is not None:
            return self.timeout_seconds
        return 15.5 if self.action.type == "launch" else 12.0


def _parameter_references(selector: SemanticSelector):
    if selector.text_parameter is not None:
        yield selector.text_parameter
    if selector.ancestor is not None and selector.ancestor.text_parameter is not None:
        yield selector.ancestor.text_parameter
    if selector.relative_to is not None and selector.relative_to.anchor.text_parameter is not None:
        yield selector.relative_to.anchor.text_parameter


class GuiScript(_ScriptModel):
    schema_version: Literal[1] = 1
    script_id: Identifier
    revision: int = Field(ge=1, default=1)
    app_id: Identifier
    platform: Literal["android", "ios"] = "android"
    parameters: dict[Identifier, ScriptParameter] = Field(default_factory=dict, max_length=32)
    initial: Conditions
    steps: tuple[ScriptStep, ...] = Field(min_length=1, max_length=128, strict=False)
    final: Conditions

    @model_validator(mode="after")
    def check_references(self):
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("script step IDs must be unique")
        conditions = [*self.initial, *self.final]
        references = set()
        for step in self.steps:
            conditions.extend((*step.before, *step.after, *(step.retry_when or ()), *(step.blocked_when or ())))
            if isinstance(step.action, (TapAction, LongPressAction, TypeAction)):
                references.update(_parameter_references(step.action.target))
            if isinstance(step.action, (TapAction, VisualTapAction)) and step.action.object is not None:
                references.update(part.text_parameter for part in step.action.object.parts
                                  if part.text_parameter is not None)
            if isinstance(step.action, TypeAction):
                references.add(step.action.parameter)
            if step.action.type == "launch" and "." not in self.app_id:
                raise ValueError("launch requires an exact app identifier")
        for condition in conditions:
            if isinstance(condition, TargetCondition):
                references.update(_parameter_references(condition.target))
            elif isinstance(condition, VisualObjectCondition):
                references.update(part.text_parameter for part in condition.target.parts
                                  if part.text_parameter is not None)
        if references - self.parameters.keys():
            raise ValueError("script references an undeclared parameter")
        if any(template.sha256 is None for template in visual_templates(self)):
            raise ValueError("script visual assets require pinned SHA-256 hashes")
        return self

    def bind_parameters(self, values: dict[str, str]) -> dict[str, str]:
        if values.keys() != self.parameters.keys():
            raise ValueError("script parameters must exactly match the declared inputs")
        for name, definition in self.parameters.items():
            value = values[name]
            if not isinstance(value, str) or not value or len(value) > definition.max_length:
                raise ValueError(f"invalid string argument for script parameter {name}")
        return dict(values)


def visual_templates(script: GuiScript):
    def conditions_templates(conditions):
        for item in conditions:
            if isinstance(item, VisualCondition):
                yield item.target
            elif isinstance(item, VisualObjectCondition):
                yield item.target.layout
            elif isinstance(item, TargetCondition) and item.visual is not None:
                yield item.visual

    for conditions in (script.initial, script.final):
        yield from conditions_templates(conditions)
    for step in script.steps:
        if isinstance(step.action, VisualTapAction):
            yield step.action.target
        elif isinstance(step.action, TapAction) and step.action.visual is not None:
            yield step.action.visual
        if isinstance(step.action, (TapAction, VisualTapAction)) and step.action.object is not None:
            yield step.action.object.layout
        for conditions in (step.before, step.after, step.retry_when or (), step.blocked_when or ()):
            yield from conditions_templates(conditions)


def _fingerprint_projection(value):
    if isinstance(value, dict):
        return {key: _fingerprint_projection(item) for key, item in value.items()
                if item is not None or key not in
                {"visual", "relative_to", "occurrence", "expected_count", "fallback_roi", "object"}}
    if isinstance(value, list):
        return [_fingerprint_projection(item) for item in value]
    return value


def script_fingerprint(script: GuiScript) -> str:
    """Identify an exact validated revision independent of its JSON formatting."""
    canonical = json.dumps(_fingerprint_projection(script.model_dump(mode="json")), sort_keys=True,
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
