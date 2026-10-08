"""User evidence is passive; personal research needs a separate bounded request."""
from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from agent.skills.learning import TERMINAL, mechanical_signals, source
from shared.schemas import Action

ActionKind = Action.model_fields["type"].annotation
ACTION_KINDS = set(get_args(ActionKind))


class LearningPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    local_recording: StrictBool = True
    contribution_enabled: StrictBool = False


class PersonalLearningRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accept_model_cost: StrictBool = False
    allow_device_operations: StrictBool = False
    max_calls: int = Field(default=24, ge=12, le=64, strict=True)
    max_actions: int = Field(default=30, ge=1, le=100, strict=True)
    max_seconds: int = Field(default=900, ge=30, le=1800, strict=True)

    @property
    def authorized(self) -> bool:
        return self.accept_model_cost and self.allow_device_operations


Signal = Literal["task_failed", "repeated_action_without_visible_change",
    "multiple_replans", "multi_step_navigation_return"]


class ClueCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: int = Field(ge=0, le=1000000, strict=True)
    model_requests: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    actions: int | None = Field(default=None, ge=0, le=1000000, strict=True)
    replans: int = Field(ge=0, le=1000000, strict=True)


class ContributionReport(BaseModel):
    """An allowlist, not a model-produced redaction or replayable trajectory."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    report_id: uuid.UUID
    apps: list[str] = Field(max_length=16)
    outcome: Literal["succeeded", "failed", "cancelled"]
    signals: list[Signal] = Field(max_length=4)
    counts: ClueCounts
    action_types: dict[ActionKind, Annotated[int, Field(ge=0, le=1000000, strict=True)]] = Field(max_length=40)

    @classmethod
    def checked(cls, value):
        report = cls.model_validate(value)
        if len(report.apps) != len(set(report.apps)) or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", app)
            or len(app) > 200 for app in report.apps
        ):
            raise ValueError("invalid normalized app package")
        if len(report.signals) != len(set(report.signals)):
            raise ValueError("duplicate signals")
        if any(type(n) is not int or not 0 <= n <= 1000000 for n in report.action_types.values()):
            raise ValueError("invalid action counts")
        return report


def preferences(db) -> LearningPreferences:
    return LearningPreferences.model_validate(db.get_local_preference("skill_learning") or {})


def save_preferences(db, value: LearningPreferences) -> dict:
    db.set_local_preference("skill_learning", value.model_dump())
    return value.model_dump()


def record_local_clue(task, store) -> dict:
    if task.status not in TERMINAL:
        return {"ok": False, "reason": "source_task_not_terminal"}
    if not preferences(store.db).local_recording:
        return {"ok": True, "skipped": True, "reason": "local_recording_disabled"}
    existing = store.db.get_agent_record(task.id, "learning_clue", "terminal")
    if existing:
        return {"ok": True, "recorded": True, "version": existing["version"]}
    rows, observations = store.records("event"), store.records("observation")
    signals, replans, _, _ = mechanical_signals(task, rows, observations)
    action_types = Counter()
    for row in rows:
        action = row["payload"].get("submitted_action") or row["payload"].get("action") or {}
        kind = action.get("type")
        if kind in ACTION_KINDS:
            action_types[kind] += 1
    apps = sorted({row["payload"].get("app") for row in observations
        if isinstance(row["payload"].get("app"), str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", row["payload"]["app"])
        and len(row["payload"]["app"]) <= 200})
    report = ContributionReport.checked({"report_id": str(uuid.uuid4()), "apps": apps[:16],
        "outcome": task.status.value, "signals": signals,
        "counts": {"events": len(rows), "model_requests": task.state.role_invocation_count if task.state else None,
            "actions": task.state.revisable.execution_count if task.state else None, "replans": replans},
        "action_types": dict(action_types)})
    # Native source references remain local and are never included in the report.
    row = store.put("learning_clue", "terminal", {"report": report.model_dump(mode="json"),
        "source_refs": [source(row) for row in rows[-4:]], "omitted_apps": max(0, len(apps)-16),
        "status": "unvalidated_clue", "research_calls": 0, "research_actions": 0})
    return {"ok": True, "recorded": True, "version": row["version"]}


def export_contribution(task_id, db) -> dict:
    if not preferences(db).contribution_enabled:
        raise PermissionError("contribution_disabled")
    row = db.get_agent_record(task_id, "learning_clue", "terminal")
    if not row:
        raise ValueError("local_clue_not_found")
    # Explicit reconstruction also drops any extra private keys in stored data.
    saved = row["payload"]["report"]
    allowed = {key: saved[key] for key in ContributionReport.model_fields if key in saved}
    counts = allowed.get("counts", {})
    allowed["counts"] = {key: counts[key] for key in ClueCounts.model_fields if key in counts}
    return ContributionReport.checked(allowed).model_dump(mode="json")
