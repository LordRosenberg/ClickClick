"""Store untrusted Skill hints with references to a verified assisted replay."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from agent.revisable.recall import resolve_source
from agent.skills.script_schema import script_fingerprint
from shared.recovery_store import digest
from shared.schemas import TaskStatus


SCRIPT_SKILL_SIGNAL_KIND = "script_skill_learning_signal"

_Hash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
_EventRef = Annotated[str, StringConstraints(pattern=r"^event:[^@\s]+@[1-9][0-9]*$", max_length=256)]
_ObservationRef = Annotated[str, StringConstraints(pattern=r"^observation:[^@\s]+@[1-9][0-9]*$", max_length=256)]
_HintText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ScriptSkillHint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    condition: _HintText
    rule: _HintText
    evidence: Annotated[tuple[_EventRef, ...], Field(strict=False, min_length=1, max_length=8)]


class ScriptSkillEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    event: _EventRef
    before: _ObservationRef
    after: _ObservationRef
    before_image_ref: str | None = None
    after_image_ref: str | None = None


class ScriptSkillSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[1] = 1
    signal_id: _Hash
    source_task_id: str | None = None
    replay_task_id: str = Field(min_length=1)
    replay_runtime: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    app_id: str = Field(min_length=1)
    app_build: str = Field(pattern=r"^[0-9]+$")
    script_id: str = Field(min_length=1)
    script_revision: int = Field(ge=1)
    base_script_hash: _Hash
    # Digest of the persisted gui_script_run/final payload, including fixture binding.
    assisted_run_hash: _Hash
    failed_step_id: str = Field(min_length=1)
    hint: ScriptSkillHint
    evidence: Annotated[tuple[ScriptSkillEvidence, ...], Field(strict=False, min_length=1, max_length=8)]


def _image_ref(store, source):
    _, observation, _ = resolve_source(store, source)
    ref = observation["payload"].get("image_ref")
    if isinstance(ref, str) and ref:
        try:
            if store.artifacts.resolve(ref).is_file():
                return ref
        except (OSError, ValueError):
            pass
    return None


def record_script_skill_signal(*, raw_hint, task, replay_store, signal_store, source_task_id,
                               base, evidence, saved):
    """Bind an optional hint to native observations without accepting knowledge or a patch."""
    try:
        hint = ScriptSkillHint.model_validate(raw_hint)
    except ValidationError:
        return None, "invalid_hint"
    if any(item.input_value and (item.input_value in hint.condition or item.input_value in hint.rule)
           for item in evidence):
        return None, "hint_contains_demonstrated_input"
    events = {item.source: item for item in evidence}
    if len(set(hint.evidence)) != len(hint.evidence) or not set(hint.evidence) <= events.keys():
        return None, "hint_evidence_unconfirmed"
    if signal_store is None or signal_store.db.read_only:
        return None, "writable_signal_store_unavailable"

    if replay_store.get("gui_script_run", "final")["payload"] != saved:
        return None, "replay_record_changed"
    fixture = saved.get("fixture")
    build = fixture.get("app_build") if isinstance(fixture, dict) else None
    if not isinstance(build, str) or not build.isascii() or not build.isdecimal():
        return None, "app_build_unconfirmed"
    source_id = None
    if signal_store.task_id != task.id:
        source = signal_store.db.get_task(source_task_id) if source_task_id else None
        if (source is None or source.id != signal_store.task_id or source.status != TaskStatus.SUCCEEDED
                or source.instruction != task.instruction):
            return None, "source_task_unconfirmed"
        source_id = source.id
    elif Path(signal_store.db.path).resolve() != Path(replay_store.db.path).resolve():
        return None, "replay_store_unconfirmed"

    # The durable key represents one exact replay and is independent of repeated model wording.
    replay_hash = digest(saved)
    base_hash = script_fingerprint(base)
    signal_id = digest({"replay_task_id": task.id, "assisted_run_hash": replay_hash,
                        "base_script_hash": base_hash, "app_build": build})
    links = []
    for reference in hint.evidence:
        item = events[reference]
        links.append(ScriptSkillEvidence(
            event=reference, before=item.before.source, after=item.after.source,
            before_image_ref=_image_ref(replay_store, item.before.source),
            after_image_ref=_image_ref(replay_store, item.after.source),
        ))
    signal = ScriptSkillSignal(
        signal_id=signal_id, source_task_id=source_id, replay_task_id=task.id,
        replay_runtime=str(Path(replay_store.db.path).resolve().parent), goal=task.instruction,
        app_id=base.app_id, app_build=build, script_id=base.script_id, script_revision=base.revision,
        base_script_hash=base_hash, assisted_run_hash=replay_hash,
        failed_step_id=saved["receipt"]["failed_step_id"], hint=hint, evidence=tuple(links),
    )
    with signal_store.db.transaction():
        previous = signal_store.db.get_agent_record(signal_store.task_id, SCRIPT_SKILL_SIGNAL_KIND, signal_id)
        if previous is not None:
            return ScriptSkillSignal.model_validate(previous["payload"]), "already_recorded"
        signal_store.put(SCRIPT_SKILL_SIGNAL_KIND, signal_id, signal.model_dump(mode="json"))
    return signal, "recorded"
