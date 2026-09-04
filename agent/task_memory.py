"""TaskMemory helpers and canonical action-audit projections."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from shared.schemas import (
    Action,
    AgentState,
    ExecutorStep,
    FactEntry,
    MemoryEvent,
    ProgressEntry,
    ReviewerDecision,
    SubmittedActionSnapshot,
    TaskMemory,
)

DispatchStatus = Literal["dispatched", "rejected", "failed"]
TEXT_ACTION_TYPES = frozenset({"type", "replace_text"})
REDACTED_TEXT_INTENT = "[text intent redacted]"
DEVICE_ACTION_TYPES = frozenset({
    "tap",
    "tap_xy",
    "type",
    "replace_text",
    "swipe",
    "long_press",
    "scroll",
    "drag",
    "key",
    "launch",
    "back",
    "home",
    "sleep",
})


def dispatch_status_for(step: ExecutorStep) -> DispatchStatus | None:
    """Project only transport/validation truth into model-facing memory."""
    pipeline = step.action_pipeline
    if pipeline is not None and pipeline.dispatch_suppressed:
        return "rejected"
    receipt = step.action_receipt
    if receipt is not None:
        return "dispatched" if receipt.dispatch_succeeded else "failed"
    if pipeline is not None and pipeline.driver_success is not None:
        return "dispatched" if pipeline.driver_success else "failed"
    return None


def _submitted_action_for_step(step: ExecutorStep) -> Action | None:
    pipeline = step.action_pipeline
    if pipeline is not None and "action" in (pipeline.missing_required_fields or []):
        return None
    if pipeline is not None:
        for stage in pipeline.stages:
            if stage.stage == "submitted" and stage.action is not None:
                return stage.action
    return step.action


def _submitted_action_family(action: Action | None) -> str:
    action_type = (action.type if action is not None else "").strip()
    return action_type if action_type in DEVICE_ACTION_TYPES else ""


def _submitted_action_snapshot_for_step(
    step: ExecutorStep,
    action: Action | None,
) -> SubmittedActionSnapshot | None:
    if step.submitted_action_snapshot is not None:
        return step.submitted_action_snapshot
    if action is None:
        return None
    redact_text = bool(
        step.target_snapshot is not None
        and step.target_snapshot.password
        and action.type in TEXT_ACTION_TYPES
    )
    image_size: tuple[int, int] | None = None
    if any(getattr(action, key) is not None for key in ("x", "y", "x2", "y2")):
        source_geometry = list(
            getattr(step.action_pipeline, "source_geometry", None) or []
        )
        if (
            len(source_geometry) == 2
            and all(isinstance(value, int) and value > 0 for value in source_geometry)
        ):
            image_size = (source_geometry[0], source_geometry[1])
    return SubmittedActionSnapshot(
        type=action.type,
        index=action.index,
        x=action.x,
        y=action.y,
        x2=action.x2,
        y2=action.y2,
        text=None if redact_text else action.text,
        text_redacted=bool(redact_text and action.text is not None),
        key=action.key,
        app=action.app,
        direction=action.direction,
        duration_ms=action.duration_ms,
        image_size=image_size,
    )


def _action_signature(step: ExecutorStep) -> str:
    """Return a deterministic mechanical signature without semantic inference."""
    snapshot = step.submitted_action_snapshot
    if snapshot is not None:
        payload = snapshot.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )
        text = payload.pop("text", None)
        if text is not None:
            payload["text_sha256"] = _exact_text_sha256(str(text))
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    action = step.action
    if action is None:
        return "none"
    payload: dict[str, Any] = {"type": action.type}
    if action.index is not None:
        payload["index"] = action.index
    for key in ("x", "y", "x2", "y2"):
        value = getattr(action, key)
        if value is not None:
            payload[key] = round(float(value), 3)
    if action.text is not None:
        payload["text_sha256"] = _exact_text_sha256(action.text)
    for key in ("key", "app", "direction", "duration_ms"):
        value = getattr(action, key)
        if value is not None:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _exact_text_sha256(value: str | None) -> str:
    """Hash exact internal text bytes without exposing or interpreting them."""
    return hashlib.sha256(
        (value or "").encode("utf-8", errors="surrogatepass"),
    ).hexdigest()


def _role_attempt_family(
    action_type: str, submitted_action_type: str = "",
) -> str:
    """Prefer an exact submitted text family when either record is textual."""
    effective = (action_type or "").strip()
    submitted = (submitted_action_type or "").strip()
    if submitted in TEXT_ACTION_TYPES:
        return submitted
    return effective


def _role_attempt_summary(
    action_type: str,
    summary: str,
    *,
    submitted_action_type: str = "",
) -> str:
    """Fail closed when either submitted or effective action is textual."""
    effective = (action_type or "").strip()
    submitted = (submitted_action_type or "").strip()
    if (
        effective in TEXT_ACTION_TYPES
        or submitted in TEXT_ACTION_TYPES
    ):
        return REDACTED_TEXT_INTENT
    return summary


def upsert_fact(
    memory: TaskMemory,
    key: str,
    value: str,
    *,
    source: Literal["reviewer"] = "reviewer",
    step: int = 0,
    evidence_handles: list[str] | None = None,
    packet_digest: str = "",
) -> None:
    entry = FactEntry(
        value=value,
        source=source,
        step=step,
        evidence_handles=list(evidence_handles or []),
        packet_digest=packet_digest,
    )
    if memory.facts.get(key) == entry:
        return
    memory.facts[key] = entry
    memory.events.append(MemoryEvent(
        kind="fact", summary=f"{key}={value}", step=step, outcome="stored",
    ))


def _reviewer_progress_id(packet_digest: str, ordinal: int) -> str:
    """Derive a compact replay-stable handle from exact runtime provenance."""
    seed = f"{packet_digest}:{ordinal}".encode("utf-8")
    return f"p:{hashlib.sha256(seed).hexdigest()[:12]}:{ordinal + 1}"


def apply_reviewer_progress(
    state: AgentState,
    decision: ReviewerDecision,
    step: int,
) -> dict[str, list[str]]:
    """Mechanically apply one Reviewer verdict to canonical progress.

    The function validates exact ids/provenance and is replay-idempotent. It
    does not decide whether a statement is true, useful, equivalent, or
    sufficient for the task.
    """
    packet_digest = decision.packet_digest.strip()
    if not packet_digest:
        raise ValueError("Reviewer progress requires a packet digest")
    accepted_step = int(step)
    if accepted_step < 0:
        raise ValueError("Reviewer progress step cannot be negative")
    cited_handles = set(decision.evidence_handles)
    superseded_ids = list(decision.superseded_progress_ids)
    if len(superseded_ids) != len(set(superseded_ids)):
        raise ValueError("superseded progress ids must be unique")

    memory = ensure_memory(state)
    existing = {entry.progress_id: entry for entry in memory.progress}
    positions = {
        entry.progress_id: index for index, entry in enumerate(memory.progress)
    }
    if len(existing) != len(memory.progress):
        raise ValueError("canonical progress contains duplicate progress ids")
    unknown = sorted(set(superseded_ids) - set(existing))
    if unknown:
        raise ValueError(
            "Reviewer superseded unknown progress ids: " + ", ".join(unknown)
        )

    additions: list[ProgressEntry] = []
    accepted_ids: list[str] = []
    for ordinal, accepted in enumerate(decision.accepted_progress):
        handles = list(accepted.evidence_handles)
        if not set(handles).issubset(cited_handles):
            raise ValueError(
                "accepted progress cites a handle absent from Reviewer evidence_handles"
            )
        progress_id = _reviewer_progress_id(packet_digest, ordinal)
        expected = ProgressEntry(
            progress_id=progress_id,
            requirement_ref=accepted.requirement_ref,
            statement=accepted.statement,
            evidence_handles=handles,
            packet_digest=packet_digest,
            accepted_step=accepted_step,
            source_subgoal=state.current_subgoal,
        )
        prior = existing.get(progress_id)
        if prior is not None:
            stable_fields = {
                "progress_id",
                "requirement_ref",
                "statement",
                "evidence_handles",
                "packet_digest",
                "accepted_step",
                "source_subgoal",
            }
            if any(
                getattr(prior, field) != getattr(expected, field)
                for field in stable_fields
            ):
                raise ValueError(
                    f"stable progress id collision for {progress_id}"
                )
        else:
            additions.append(expected)
            existing[progress_id] = expected
        accepted_ids.append(progress_id)

    for progress_id in superseded_ids:
        entry = existing[progress_id]
        prior_digest = entry.superseded_by_packet_digest
        if prior_digest and prior_digest != packet_digest:
            raise ValueError(
                f"progress {progress_id} was already superseded by another packet"
            )

    for progress_id in superseded_ids:
        entry = existing[progress_id]
        if not entry.superseded_by_packet_digest:
            updated = ProgressEntry.model_validate({
                **entry.model_dump(mode="json"),
                "superseded_by_packet_digest": packet_digest,
                "superseded_step": accepted_step,
            })
            memory.progress[positions[progress_id]] = updated
            existing[progress_id] = updated
    memory.progress.extend(additions)
    remembered_keys: list[str] = []
    for fact in decision.remembered_facts:
        if not set(fact.evidence_handles).issubset(cited_handles):
            raise ValueError(
                "remembered fact cites a handle absent from Reviewer evidence_handles"
            )
        upsert_fact(
            memory,
            fact.key,
            fact.value,
            source="reviewer",
            step=accepted_step,
            evidence_handles=fact.evidence_handles,
            packet_digest=packet_digest,
        )
        remembered_keys.append(fact.key)
    return {
        "accepted_progress_ids": accepted_ids,
        "superseded_progress_ids": superseded_ids,
        "remembered_fact_keys": remembered_keys,
    }


def record_attempt(
    memory: TaskMemory,
    step_record: ExecutorStep,
    *,
    subgoal: str,
    step: int,
    lineage_id: str = "",
    refs: list[str] | None = None,
) -> None:
    """Append one typed attempt event without model-authored bookkeeping."""
    submitted_action = _submitted_action_for_step(step_record)
    submitted_snapshot = _submitted_action_snapshot_for_step(
        step_record,
        submitted_action,
    )
    submitted_action_type = _submitted_action_family(submitted_action)
    action_type = (
        step_record.action.type
        if step_record.action is not None
        else step_record.decision.value
    )
    status = dispatch_status_for(step_record)
    event_refs = list(dict.fromkeys([
        *(str(value) for value in (refs or []) if str(value).strip()),
        *(str(value) for value in (step_record.evidence_refs or []) if str(value).strip()),
    ]))
    receipt = step_record.action_receipt
    post_dispatch_observation = "not_applicable"
    if status == "dispatched" and receipt is not None:
        post_dispatch_observation = (
            "accepted" if receipt.observation_accepted else "missing"
        )
    raw_intent = step_record.summary or ""
    password_text_action = bool(
        submitted_action_type in TEXT_ACTION_TYPES
        and step_record.target_snapshot is not None
        and step_record.target_snapshot.password
    )
    summary = raw_intent.strip() or action_type
    memory.events.append(MemoryEvent(
        kind="attempt",
        summary=_role_attempt_summary(
            action_type,
            summary,
            submitted_action_type=submitted_action_type,
        ),
        model_intent=(REDACTED_TEXT_INTENT if password_text_action else summary),
        subgoal=(subgoal or "").strip(),
        step=int(step),
        lineage_id=lineage_id,
        action_type=action_type,
        submitted_action_type=submitted_action_type,
        submitted_action=submitted_snapshot,
        target=step_record.target_snapshot,
        intent_sha256=_exact_text_sha256(raw_intent),
        action_signature=_action_signature(step_record),
        dispatch_status=status,
        post_dispatch_observation=post_dispatch_observation,
        basis_observation_id=step_record.basis_observation_id,
        post_observation_id=(receipt.observation_id if receipt is not None else ""),
        refs=event_refs,
    ))


def ensure_memory(state: AgentState) -> TaskMemory:
    if getattr(state, "task_memory", None) is None:
        state.task_memory = TaskMemory()
    return state.task_memory
