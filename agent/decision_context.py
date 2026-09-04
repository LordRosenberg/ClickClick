"""Deterministic Decision Context v2 role projections.

This module selects and orders exact runtime/model-authored facts. It never
classifies UI meaning, action equivalence, progress, effectiveness, or task
completion.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from agent.task_memory import ensure_memory
from agent.completion_contract import (
    model_visible_contract_body,
)
from agent.tool_registry import stable_hash
from perception.input_evidence import (
    build_interaction_state,
    editability_evidence,
    element_identity,
    interaction_envelope,
)
from perception.observation import ObservationPackage
from perception.observation import has_model_visible_tree_content
from shared.schemas import AgentState, CanonicalUI, MemoryEvent, UIElement


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _contract_body(bound: Any) -> dict[str, Any] | None:
    body = getattr(bound, "body", None)
    if body is None:
        return None
    return model_visible_contract_body(body)


def task_contract_projection(state: AgentState) -> dict[str, Any] | None:
    """Project stable ordered refs for the immutable task contract."""
    body = _contract_body(state.task_completion_contract)
    if body is None:
        return None
    accepted_refs = {
        entry.requirement_ref
        for entry in ensure_memory(state).progress
        if entry.effective and entry.requirement_ref
    }
    active_ref = (
        state.active_completion_contract.target_requirement_ref
        if state.active_completion_contract is not None
        else ""
    )
    for group in ("must_happen", "final_ui_state", "answer"):
        projected = []
        for index, text in enumerate(body.get(group) or [], start=1):
            requirement_ref = f"{group}:{index}"
            status = (
                "accepted"
                if requirement_ref in accepted_refs
                else "active" if requirement_ref == active_ref else "pending"
            )
            projected.append({
                "requirement_ref": requirement_ref,
                "status": status,
                "text": text,
            })
        body[group] = projected
    return body


def task_requirement_categories(state: AgentState) -> dict[str, str]:
    """Return exact immutable requirement refs and categories."""
    body = task_contract_projection(state) or {}
    return {
        str(item["requirement_ref"]): group
        for group in ("must_happen", "final_ui_state", "answer")
        for item in body.get(group) or []
    }


def _accepted_memory(state: AgentState) -> dict[str, Any]:
    memory = ensure_memory(state)
    facts = {
        key: entry.value
        for key, entry in sorted(
            memory.facts.items(),
            key=lambda item: (int(item[1].step or 0), item[0]),
        )
    }
    progress = [
        {
            **({"requirement_ref": entry.requirement_ref} if entry.requirement_ref else {}),
            "statement": entry.statement,
        }
        for entry in memory.progress
        if entry.effective
    ]
    payload: dict[str, Any] = {}
    if facts:
        payload["remembered_facts"] = facts
    if progress:
        payload["progress"] = progress
    return payload


def accepted_progress_projection(state: AgentState) -> list[dict[str, Any]]:
    """Project Reviewer-accepted progress without exposing planning history."""
    rows: list[dict[str, Any]] = []
    for entry in ensure_memory(state).progress:
        if not entry.effective:
            continue
        rows.append({
            "progress_id": entry.progress_id,
            **({"requirement_ref": entry.requirement_ref} if entry.requirement_ref else {}),
            "statement": entry.statement,
        })
    return rows


def _reviewer_accepted_knowledge(
    state: AgentState,
) -> tuple[dict[str, Any], list[str]]:
    memory = _accepted_memory(state)
    handles: list[str] = []
    facts: dict[str, Any] = {}
    for key, value in dict(memory.get("remembered_facts") or {}).items():
        handle = f"fact:{key}"
        facts[key] = {"value": value, "evidence_handle": handle}
        handles.append(handle)
    payload: dict[str, Any] = {}
    if facts:
        payload["remembered_facts"] = facts
    return payload, handles


def _active_lineage_ids(state: AgentState) -> list[str]:
    return list(dict.fromkeys(
        value.strip()
        for value in state.active_timeline_lineage_ids
        if value and value.strip()
    ))


def _ordered_attempts(state: AgentState) -> list[MemoryEvent]:
    active_ids = set(_active_lineage_ids(state))
    if not active_ids:
        return []
    indexed = [
        (position, event)
        for position, event in enumerate(ensure_memory(state).events)
        if event.kind == "attempt" and event.lineage_id in active_ids
    ]
    indexed.sort(key=lambda item: (int(item[1].step or 0), item[0]))
    return [event for _position, event in indexed]


def _post_action_capture(event: MemoryEvent, current_observation_id: str) -> dict[str, Any]:
    status = {
        "accepted": "available",
        "missing": "unavailable",
        "not_applicable": "not_applicable",
    }.get(event.post_dispatch_observation, "unavailable")
    return {
        "status": status,
        **(
            {"result_observation_handle": "current"}
            if current_observation_id
            and event.post_observation_id == current_observation_id
            else {}
        ),
    }


def _semantic_action(event: MemoryEvent) -> dict[str, Any]:
    """Project action meaning without historical locators."""
    if event.submitted_action is None:
        return {"type": event.submitted_action_type or event.action_type}
    payload = event.submitted_action.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    return {
        key: value
        for key, value in payload.items()
        if key in {
            "type", "text", "text_redacted", "key", "app", "direction",
            "duration_ms",
        }
    }


def _semantic_target(event: MemoryEvent) -> dict[str, Any] | None:
    """Project exact source semantics while omitting replay-only geometry."""
    if event.target is None:
        return None
    payload = event.target.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    target = {
        key: value
        for key, value in payload.items()
        if key in {
            "role", "raw_text", "raw_a11y_label", "raw_hint",
            "raw_fields_redacted", "editability", "focused", "password",
        }
    }
    return target or None


def action_timeline_rows(
    state: AgentState,
    *,
    current_observation_id: str = "",
) -> list[dict[str, Any]]:
    """Return the one active timeline; interpretation remains model-owned."""
    rows: list[dict[str, Any]] = []
    for event in _ordered_attempts(state):
        row: dict[str, Any] = {
            "intent": event.model_intent or event.summary or event.action_type,
            "submitted_action": _semantic_action(event),
            "dispatch": event.dispatch_status or "not_dispatched",
            "post_action_capture": _post_action_capture(
                event, current_observation_id,
            ),
        }
        target = _semantic_target(event)
        if target is not None:
            row["target_at_submission"] = target
        rows.append(row)
    return rows


def planner_action_timeline_rows(state: AgentState) -> list[dict[str, Any]]:
    """Project active attempt facts needed for recovery, without action locators."""
    return [
        {
            "intent": row["intent"],
            "submitted_action": row["submitted_action"],
            "dispatch": row["dispatch"],
            "post_action_capture": row["post_action_capture"],
        }
        for row in action_timeline_rows(state)
    ]


def _observation_capabilities(package: ObservationPackage) -> dict[str, Any]:
    image_available = bool(package.image_for_llm)
    tree_semantics_available = has_model_visible_tree_content(package.ui)
    coordinate_available = bool(
        package.actionable
        and image_available
        and package.model_image_width > 0
        and package.model_image_height > 0
    )
    evidence_tier = str(package.capture_meta.get("evidence_tier") or "")
    if not evidence_tier:
        if package.mode.value == "image-only":
            evidence_tier = "image_only" if image_available else "unavailable"
        elif tree_semantics_available and image_available:
            evidence_tier = (
                "indexed_tree_image"
                if package.index_actionable else "nonindexed_tree_image"
            )
        elif tree_semantics_available:
            evidence_tier = "tree_only"
        elif image_available:
            evidence_tier = "image_only"
        else:
            evidence_tier = "unavailable"
    return {
        "evidence_tier": evidence_tier,
        "index_actions_available": bool(
            package.actionable
            and package.index_actionable
            and tree_semantics_available
            and package.ui.elements
        ),
        "coordinate_actions_available": coordinate_available,
        **(
            {"image_size": [package.model_image_width, package.model_image_height]}
            if image_available
            and package.model_image_width > 0
            and package.model_image_height > 0
            else {}
        ),
        **(
            {"gap_reason": package.gap_reasons[0]}
            if package.gap_reasons else {}
        ),
    }


def _decision_observation_capabilities(package: ObservationPackage) -> dict[str, Any]:
    """Expose evidence quality only; non-acting roles cannot use locators."""
    capabilities = _observation_capabilities(package)
    return {
        key: capabilities[key]
        for key in ("evidence_tier", "gap_reason")
        if key in capabilities
    }


def _semantic_interaction_state(package: ObservationPackage) -> dict[str, Any]:
    """Project focus semantics for non-acting roles without locators."""
    payload = interaction_envelope(package.interaction_state)
    for key in ("focused_element", "focused_editable"):
        focus = payload.get(key)
        if isinstance(focus, dict):
            focus.pop("index", None)
    return payload


def _short_role(role: str) -> str:
    return (role or "View").rsplit(".", 1)[-1] or "View"


def _raw_fields(
    element: UIElement,
    *,
    editability: str | None = None,
    projected_focused_editable_identity: str = "",
) -> dict[str, str]:
    if bool((element.states or {}).get("password")):
        return {"raw_fields_redacted": True}
    editability = editability or editability_evidence(element)
    if (
        projected_focused_editable_identity
        and bool((element.states or {}).get("focused"))
        and editability == "editable"
        and element_identity(element) == projected_focused_editable_identity
    ):
        return {}
    preserve_empty_text = editability == "editable"
    fields = {
        "raw_text": element.text,
        "raw_a11y_label": element.desc,
        "raw_hint": element.hint,
    }
    return {
        name: value
        for name, value in fields.items()
        if value or (name == "raw_text" and preserve_empty_text)
    }


def semantic_tree_projection(
    elements: Iterable[UIElement],
    *,
    projected_focused_editable_identity: str = "",
) -> list[dict[str, Any]]:
    """Preserve hierarchy/provenance while removing action coordinates and ids."""
    elements = list(elements)
    if not projected_focused_editable_identity:
        interaction = build_interaction_state(CanonicalUI(
            semantic_tree=elements,
            elements=[element for element in elements if element.interactable],
        ))
        if interaction.focused_editable is not None:
            projected_focused_editable_identity = (
                interaction.focused_editable.identity
            )
    rows: list[dict[str, Any]] = []
    for element in elements:
        states = element.states or {}
        editability = editability_evidence(element)
        structural_states = {
            name: True
            for name in (
                "clickable",
                "checkable",
                "editable",
                "focusable",
                "focused",
                "selected",
                "checked",
                "scrollable",
                "password",
            )
            if (
                element.clickable if name == "clickable" else bool(states.get(name))
            )
        }
        row: dict[str, Any] = {
            "depth": max(0, int(element.depth or 0)),
            "role": _short_role(element.role),
            **_raw_fields(
                element,
                editability=editability,
                projected_focused_editable_identity=(
                    projected_focused_editable_identity
                ),
            ),
        }
        if editability == "conflict":
            row["editability"] = "conflict"
        if structural_states:
            row["states"] = structural_states
        if element.window_wrapper:
            row["window"] = True
        rows.append(row)
    return rows


def decision_semantic_tree_projection(
    package: ObservationPackage,
) -> list[dict[str, Any]]:
    """Project the capture-bound foreground window and active overlays.

    Window selection uses only ownership and focus metadata already attached
    to the accepted observation. The full canonical tree remains untouched for
    Executor grounding, artifacts, and replay.
    """
    tree = package.ui.semantic_tree
    foreground = package.ui.app_id.strip()
    exact_window_ids = {
        int(row["window_id"])
        for row in package.capture_meta.get("tree_ownership_candidates") or []
        if row.get("source_kind") == "window"
        and row.get("packages") == [foreground]
        and row.get("window_id") is not None
    }
    active_window_ids = {
        int(element.window_id)
        for element in tree
        if element.window_wrapper
        and element.window_id is not None
        and any(
            bool((element.states or {}).get(key))
            for key in ("active", "focused")
        )
    }
    retained_window_ids = exact_window_ids | active_window_ids
    if not retained_window_ids:
        retained = tree
    else:
        retained = [
            element for element in tree
            if element.window_id in retained_window_ids
        ] or tree
    focused_editable = (
        package.interaction_state.focused_editable
        if package.interaction_state is not None
        else None
    )
    rows = semantic_tree_projection(
        retained,
        projected_focused_editable_identity=(
            focused_editable.identity if focused_editable is not None else ""
        ),
    )
    meaningful_states = {
        "checkable", "checked", "editable", "focused", "password",
        "scrollable", "selected",
    }
    return [
        row
        for row in rows
        if (
            any(key.startswith("raw_") for key in row)
            or meaningful_states.intersection((row.get("states") or {}).keys())
        )
    ]


def _boundary_candidate(
    _state: AgentState,
    *,
    executor_report: str,
) -> dict[str, Any] | None:
    if not executor_report:
        return None
    return {"summary": executor_report}


def _boundary_review(state: AgentState) -> dict[str, Any] | None:
    fact = state.recovery_state.boundary_review
    if fact is None:
        return None
    payload = fact.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"package_digest"},
    )
    return {
        key: value
        for key, value in payload.items()
        if key in {"type", "source"} or value not in ("", [], {})
    }


def render_executor_task_anchor(state: AgentState) -> str:
    payload = {
        "original_instruction": state.instruction,
        "task_contract": task_contract_projection(state),
        "current_subgoal": state.current_subgoal,
        **({"target_requirement_ref": (
            state.active_completion_contract.target_requirement_ref
        )} if state.active_completion_contract is not None
        and state.active_completion_contract.target_requirement_ref else {}),
        "active_subgoal_contract": _contract_body(
            state.active_completion_contract,
        ),
        **({"last_reviewer_feedback": {
            "verdict": state.recovery_state.last_reviewer_verdict.value,
            "reason": state.recovery_state.last_reviewer_reason,
        }} if state.recovery_state.last_reviewer_verdict is not None else {}),
    }
    return "TASK ANCHOR:\n" + _json(payload)


def render_planner_task_anchor(
    state: AgentState,
    *,
    deviation: str = "",
) -> str:
    active_attempts = planner_action_timeline_rows(state)
    accepted_memory = _accepted_memory(state)
    accepted_progress = list(accepted_memory.get("progress") or [])
    accepted_knowledge = {
        key: value
        for key, value in accepted_memory.items()
        if key == "remembered_facts"
    }
    payload = {
        "original_instruction": state.instruction,
        "task_contract": task_contract_projection(state),
        **({"accepted_progress": accepted_progress} if accepted_progress else {}),
        **accepted_knowledge,
        **({"plan": state.plan} if state.plan else {}),
        **({
            "last_reviewed_subgoal": state.current_subgoal,
        } if state.active_completion_contract is not None else {}),
        **(
            {
                "last_reviewer_feedback": {
                    "verdict": state.recovery_state.last_reviewer_verdict.value,
                    "reason": (
                        state.recovery_state.last_reviewer_reason
                        or deviation.strip()
                    ),
                },
            }
            if state.recovery_state.last_reviewer_verdict is not None
            else (
                {"current_deviation_or_blocker": deviation.strip()}
                if deviation.strip()
                else {}
            )
        ),
        **({"active_action_timeline": active_attempts} if active_attempts else {}),
    }
    return "PLANNER TASK ANCHOR:\n" + _json(payload)


def render_executor_history_v2(state: AgentState) -> str:
    accepted_memory = _accepted_memory(state)
    timeline = action_timeline_rows(state)
    accepted_progress = list(accepted_memory.get("progress") or [])
    remembered_facts = dict(accepted_memory.get("remembered_facts") or {})
    payload = {
        **({"accepted_progress": accepted_progress} if accepted_progress else {}),
        **({"remembered_facts": remembered_facts} if remembered_facts else {}),
        **({"active_action_timeline": timeline} if timeline else {}),
    }
    return "ACCEPTED RESULTS AND ACTIVE ATTEMPTS:\n" + _json(payload) if payload else ""


def render_executor_observation_v2(package: ObservationPackage) -> str:
    foreground = package.ui.app_id.strip()
    if not foreground:
        raise ValueError("exact foreground application is required for Executor input")
    tree = package.text_for_llm
    foreground_line = f"foreground_package={foreground}"
    if tree == foreground_line:
        tree = ""
    elif tree.startswith(foreground_line + "\n"):
        tree = tree[len(foreground_line) + 1:]
    return "CURRENT OBSERVATION:\n" + _json({
        "foreground_package": foreground,
        "capabilities": _observation_capabilities(package),
        "focused_interaction": interaction_envelope(package.interaction_state),
        "semantic_tree": tree,
    })


def render_planner_observation_v2(package: ObservationPackage) -> str:
    foreground = package.ui.app_id.strip()
    if not foreground:
        raise ValueError("exact foreground application is required for Planner input")
    return "CURRENT OBSERVATION:\n" + _json({
        "foreground_package": foreground,
        "capabilities": _decision_observation_capabilities(package),
        "focused_interaction": _semantic_interaction_state(package),
        "semantic_tree": decision_semantic_tree_projection(package),
    })


def _task_audit_required(state: AgentState, *, terminal_review: bool) -> bool:
    if terminal_review:
        return True
    body = getattr(state.task_completion_contract, "body", None)
    if body is None:
        return False
    return bool(
        getattr(body, "disqualifying_clauses", [])
        or getattr(body, "must_happen", [])
    )


def task_action_timeline_rows(
    state: AgentState,
    *,
    current_observation_id: str = "",
) -> list[dict[str, Any]]:
    """Project the one canonical task event stream in exact event order."""
    indexed = [
        (position, event)
        for position, event in enumerate(ensure_memory(state).events)
        if event.kind == "attempt"
    ]
    indexed.sort(key=lambda item: (int(item[1].step or 0), item[0]))
    rows: list[dict[str, Any]] = []
    for _position, event in indexed:
        action = _semantic_action(event)
        dispatched = event.dispatch_status or "not_dispatched"
        if dispatched != "dispatched":
            # Boundary requests are model reports, not device effects.
            action = {"type": str(action.get("type") or event.action_type)}
        row: dict[str, Any] = {
            "subgoal": event.subgoal,
            "submitted_action": action,
            "dispatch": dispatched,
            "post_action_capture": _post_action_capture(
                event, current_observation_id,
            ),
        }
        if dispatched == "dispatched":
            row["intent"] = event.model_intent or event.summary or event.action_type
        target = _semantic_target(event)
        if target is not None:
            row["target_at_submission"] = target
        rows.append(row)
    return rows


def reviewer_packet_payload(
    state: AgentState,
    package: ObservationPackage,
    *,
    executor_report: str = "",
    boundary_reason: str = "",
    terminal_review: bool = False,
    current_evidence: bool = True,
    review_requirement_ref: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Build one exact Reviewer packet and its citeable evidence handles."""
    foreground = package.ui.app_id.strip()
    if not foreground:
        raise ValueError("exact foreground application is required for Reviewer input")

    observation_key = "current" if current_evidence else "pre_action_observation"
    observation_handle = "current" if current_evidence else "before-action"
    handles = [observation_handle]
    if package.evidence_ref:
        handles.append(package.evidence_ref)
    audit_required = _task_audit_required(state, terminal_review=terminal_review)
    result_observation_id = package.observation_id if current_evidence else ""
    active_events = action_timeline_rows(
        state,
        current_observation_id=result_observation_id,
    )
    task_events = task_action_timeline_rows(
        state,
        current_observation_id=result_observation_id,
    )
    if audit_required:
        for index, row in enumerate(task_events, start=1):
            row["evidence_handle"] = f"task-action:{index}"
            handles.append(row["evidence_handle"])
    else:
        for index, row in enumerate(active_events, start=1):
            row["evidence_handle"] = f"action:{index}"
            handles.append(row["evidence_handle"])

    progress = accepted_progress_projection(state)
    for index, row in enumerate(progress, start=1):
        row["evidence_handle"] = f"progress:{index}"
        handles.append(row["evidence_handle"])
    accepted_knowledge, knowledge_handles = _reviewer_accepted_knowledge(state)
    handles.extend(knowledge_handles)

    candidate = _boundary_candidate(state, executor_report=executor_report.strip())
    review = _boundary_review(state)
    if candidate is not None:
        handles.append("executor-report")
    planner_current_review = boundary_reason.strip() == "planner_review_requested"
    if (boundary_reason and not planner_current_review) or review is not None:
        handles.append("boundary")

    payload: dict[str, Any] = {
        "original_instruction": state.instruction,
        observation_key: {
            "evidence_handle": observation_handle,
            **(
                {"observation_evidence_handle": package.evidence_ref}
                if package.evidence_ref else {}
            ),
            "foreground_package": foreground,
            "capabilities": _decision_observation_capabilities(package),
            "focused_interaction": _semantic_interaction_state(package),
            "semantic_tree": decision_semantic_tree_projection(package),
        },
        **({"accepted_progress": progress} if progress else {}),
        **accepted_knowledge,
        **({"task_action_audit": task_events} if audit_required else {
            "active_subgoal_actions": active_events,
        }),
        **({"executor_report": {
            "evidence_handle": "executor-report", **candidate,
        }}
           if candidate is not None else {}),
        **({"review_trigger": {
            "requirement_ref": review_requirement_ref,
        }} if planner_current_review else {}),
        **({"boundary": {
            "evidence_handle": "boundary",
            **({"reason": boundary_reason.strip()}
               if boundary_reason.strip() and not planner_current_review else {}),
            **({"runtime_fact": review} if review is not None else {}),
        }} if (boundary_reason.strip() and not planner_current_review)
        or review is not None else {}),
        **({"active_subgoal_boundary": {
            "current_subgoal": state.current_subgoal,
            **({"target_requirement_ref": (
                state.active_completion_contract.target_requirement_ref
            )} if state.active_completion_contract.target_requirement_ref else {}),
            "completion_contract": (
                _contract_body(state.active_completion_contract) or {}
            ),
        }} if state.active_completion_contract is not None
        and not planner_current_review else {}),
        **({"last_reviewer_feedback": {
            "verdict": state.recovery_state.last_reviewer_verdict.value,
            "reason": state.recovery_state.last_reviewer_reason,
        }} if state.recovery_state.last_reviewer_verdict is not None else {}),
        "task_contract": task_contract_projection(state),
    }
    return payload, handles


def reviewer_protocol_metadata(
    state: AgentState,
    *,
    terminal_review: bool,
) -> dict[str, Any]:
    """Project only exact ref/source/dispatch facts used by submit validation."""
    audit_required = _task_audit_required(state, terminal_review=terminal_review)
    events = (
        task_action_timeline_rows(state)
        if audit_required
        else action_timeline_rows(state)
    )
    prefix = "task-action" if audit_required else "action"
    dispatched_handles = [
        f"{prefix}:{index}"
        for index, row in enumerate(events, start=1)
        if row.get("dispatch") == "dispatched"
    ]
    categories = task_requirement_categories(state)
    progress = accepted_progress_projection(state)
    progress_bindings = [
        {
            "progress_id": row.get("progress_id", ""),
            "evidence_handle": f"progress:{index}",
            "requirement_ref": row.get("requirement_ref", ""),
        }
        for index, row in enumerate(progress, start=1)
    ]
    return {
        "reviewer_requirement_categories": categories,
        "reviewer_dispatched_action_handles": dispatched_handles,
        "reviewer_progress_bindings": progress_bindings,
        "reviewer_has_active_subgoal": state.active_completion_contract is not None,
    }


def render_reviewer_packet(
    state: AgentState,
    package: ObservationPackage,
    *,
    executor_report: str = "",
    boundary_reason: str = "",
    terminal_review: bool = False,
    current_evidence: bool = True,
    review_requirement_ref: str = "",
) -> tuple[str, str, list[str]]:
    """Render a Reviewer packet and retain its digest for internal binding."""
    payload, handles = reviewer_packet_payload(
        state,
        package,
        executor_report=executor_report,
        boundary_reason=boundary_reason,
        terminal_review=terminal_review,
        current_evidence=current_evidence,
        review_requirement_ref=review_requirement_ref,
    )
    digest = stable_hash({
        "packet": payload,
        "observation_id": package.observation_id,
    })
    return "REVIEW PACKET:\n" + _json(payload), digest, handles
