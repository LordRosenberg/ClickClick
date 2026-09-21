"""Query helpers: replay, step debug, failed list, task detail (observability)."""

from __future__ import annotations

import math
import time
from typing import Any

from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import LogLevel, TaskRecord, TaskStatus, TraceEvent


OBSERVATION_PROJECTION_FIELDS = (
    "som_ref",
    "tree_ref",
    "observation_mode",
    "gap_reasons",
    "estimated_tokens",
    "observation_id",
    "captured_monotonic_ms",
    "frame_geometry",
    "coordinate_actionable",
    "index_actionable",
)

GROUNDING_PROJECTION_FIELDS = (
    "evidence_tier",
    "foreground_app_id",
    "foreground_activity",
    "foreground_identity_required",
    "foreground_identity_attempts",
    "foreground_identity_conflict",
    "top_window_package",
    "top_window_type",
    "top_window_layer",
    "top_window_display_id",
    "tree_application_package",
    "tree_application_window_layer",
    "provider",
    "tree_provider",
    "pixel_provider",
    "provider_attempts",
    "tree_provider_attempts",
    "fallback_edges",
    "cancelled_capture_tasks",
    "tree_providers_exhausted",
    "complete",
    "coordinate_compatible",
    "grounding_barrier",
    "coherence_status",
    "generation",
    "tree_generation",
    "action_boundary_generation",
    "action_frame_boundary_id",
    "frame_geometry",
)


def observation_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy one complete, replayable observation envelope from a role tick."""
    projection = {key: payload.get(key) for key in OBSERVATION_PROJECTION_FIELDS}
    if payload.get("model_image_ref"):
        projection["model_image_ref"] = payload["model_image_ref"]
    capture = payload.get("capture")
    if isinstance(capture, dict) and capture:
        projection["grounding"] = {
            key: capture.get(key)
            for key in GROUNDING_PROJECTION_FIELDS
            if key in capture
        }
    return projection


def _with_agent_rounds(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the current Console payload shape without replay synthesis."""
    out = dict(payload)
    out["agent_rounds"] = list(out.get("agent_rounds") or [])
    return out


def _to_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _usage_metrics(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    inputs: list[int] = []
    outputs: list[int] = []
    latencies: list[int] = []
    cache_reads: list[int] = []
    cache_writes: list[int] = []

    for item in rounds:
        usage = item.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        input_tokens = _to_int(usage.get("input_tokens"))
        if input_tokens is not None:
            inputs.append(input_tokens)
        output_tokens = _to_int(usage.get("output_tokens"))
        if output_tokens is not None:
            outputs.append(output_tokens)
        latency_ms = _to_int(item.get("latency_ms"))
        if latency_ms is not None:
            latencies.append(latency_ms)
        cache_read = _to_int(usage.get("cached_read_tokens"))
        cache_write = _to_int(usage.get("cache_write_tokens"))
        if cache_read is not None:
            cache_reads.append(cache_read)
        if cache_write is not None:
            cache_writes.append(cache_write)

    input_rounds = len(inputs)
    input_total = sum(inputs) if inputs else None
    read_complete = input_rounds > 0 and len(cache_reads) == input_rounds
    write_complete = input_rounds > 0 and len(cache_writes) == input_rounds
    cache_read_total = sum(cache_reads) if read_complete else None
    cache_write_total = sum(cache_writes) if write_complete else None
    eligible_input_total = input_total if read_complete else None
    cache_read_ratio = (
        cache_read_total / eligible_input_total
        if cache_read_total is not None and eligible_input_total
        else None
    )
    cache_write_ratio = (
        cache_write_total / input_total
        if cache_write_total is not None and input_total
        else None
    )
    return {
        "round_count": len(rounds),
        "input_tokens_total": input_total,
        "input_tokens_p95": _p95(inputs),
        "output_tokens_total": sum(outputs) if outputs else None,
        "output_tokens_p95": _p95(outputs),
        "llm_latency_ms_total": sum(latencies) if latencies else None,
        "llm_latency_ms_p95": _p95(latencies),
        "cache_read_tokens_total": cache_read_total,
        "cache_write_tokens_total": cache_write_total,
        "cache_eligible_input_tokens_total": eligible_input_total,
        "cache_read_ratio": cache_read_ratio,
        "cache_write_ratio": cache_write_ratio,
        "cache_reporting": {
            "read_reported_rounds": len(cache_reads),
            "write_reported_rounds": len(cache_writes),
            "input_rounds": input_rounds,
            "complete": read_complete,
        },
    }


def _dedupe_rounds(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate canonical provider rounds when traces are projected twice."""
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rounds:
        round_id = str(item.get("round_id") or "").strip()
        if round_id and round_id in seen:
            continue
        if round_id:
            seen.add(round_id)
        output.append(item)
    return output


def call_key_for(step_seq: int | None, role: str, ordinal: int = 1) -> str:
    """Stable key for one persisted role call.

    ``step_seq`` is an action counter, not a role-call counter: Planner and
    Reviewer may each run more than once before the next device action.  The
    ordinal prevents those valid calls from overwriting one another.
    """
    seq_part = "null" if step_seq is None else str(step_seq)
    return f"{seq_part}:{role}:{ordinal}"


def _call_phase(role: str, payload: dict[str, Any]) -> str:
    if role == "reviewer":
        return "boundary"
    return "planning" if role == "planner" else "execution"


def _call_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    rounds = [
        item for item in payload.get("agent_rounds") or []
        if isinstance(item, dict)
    ]
    return _usage_metrics(_dedupe_rounds(rounds))


def task_execution_elapsed_ms(task: TaskRecord, *, now: float | None = None) -> int:
    """Return task wall time from creation to terminal update/current time."""
    terminal = task.status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
    end = task.updated_at if terminal else (time.time() if now is None else now)
    return max(0, int((end - task.created_at) * 1000))


def expand_role_calls(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive Console navigation rows from folded timeline steps.

    Each Planner, Reviewer, and Executor record is independently selectable.
    Loop-only steps are skipped; co-located role calls share the parent
    observation and loop snapshot without being merged into one decision.
    """
    calls: list[dict[str, Any]] = []
    occurrences: dict[tuple[int | None, str], int] = {}
    for step in steps:
        role_payloads = [
            (role, step.get(role))
            for role in ("reviewer", "planner", "executor")
        ]
        if all(payload is None for _role, payload in role_payloads):
            continue
        seq = step.get("step_seq")
        observation = step.get("observation")
        for role, payload in role_payloads:
            if payload is None:
                continue
            occurrence_key = (seq, role)
            ordinal = occurrences.get(occurrence_key, 0) + 1
            occurrences[occurrence_key] = ordinal
            metrics = _call_metrics(payload)
            calls.append(
                {
                    "call_key": call_key_for(seq, role, ordinal),
                    "step_seq": seq,
                    "role": role,
                    "phase": _call_phase(role, payload),
                    "observation": observation,
                    "reviewer": payload if role == "reviewer" else None,
                    "planner": payload if role == "planner" else None,
                    "executor": payload if role == "executor" else None,
                    "elapsed_ms": payload.get("role_elapsed_ms"),
                    "llm_elapsed_ms": metrics.get("llm_latency_ms_total"),
                    "metrics": metrics,
                    "wall_started_at": step.get("wall_started_at"),
                }
            )
    return calls


class ObservabilityQueries:
    """Console-oriented retrieval over SQLite + artifact refs."""

    def __init__(self, db: Database, artifacts: ArtifactStore) -> None:
        self.db = db
        self.artifacts = artifacts

    @staticmethod
    def _supported_traces(events: list[TraceEvent]) -> list[TraceEvent]:
        """Drop obsolete Reviewer-scope telemetry instead of reinterpreting it."""
        return [
            event
            for event in events
            if event.kind != "task_scope"
            and str((event.payload or {}).get("phase") or "")
            not in {"task_scope", "reviewer_scope"}
        ]

    @staticmethod
    def _metrics(
        task: TaskRecord,
        steps: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        traces: list[TraceEvent],
    ) -> dict[str, Any]:
        rounds_by_role: dict[str, list[dict[str, Any]]] = {
            "planner": [],
            "reviewer": [],
            "executor": [],
        }
        role_calls = {
            "planner": 0, "reviewer": 0, "executor": 0,
        }
        provider_requests = {
            "planner": 0, "reviewer": 0, "executor": 0,
        }
        for event in traces:
            payload = event.payload or {}
            if (
                event.kind in {"harness_event", "system"}
                and event.message == "role_invocation_started"
            ):
                role = str(payload.get("role") or "")
                if role in provider_requests:
                    provider_requests[role] += 1
            elif event.kind == "agent_llm_round_finished":
                role = str(payload.get("role") or "")
                if role in rounds_by_role:
                    rounds_by_role[role].append(payload)

        # Always union folded role-call rounds for mixed historical/new traces;
        # canonical round ids remove duplicates below.
        for call in calls:
            role = str(call.get("role") or "")
            if role not in rounds_by_role:
                continue
            role_payload = call.get(role) or {}
            rounds = role_payload.get("agent_rounds") or []
            if isinstance(rounds, list):
                rounds_by_role[role].extend(
                    item for item in rounds if isinstance(item, dict)
                )

        for call in calls:
            role = str(call.get("role") or "")
            if role in role_calls:
                role_calls[role] += 1
        for role, rounds in rounds_by_role.items():
            rounds_by_role[role] = _dedupe_rounds(rounds)
        all_rounds = [
            round_payload
            for role in ("planner", "reviewer", "executor")
            for round_payload in rounds_by_role[role]
        ]
        by_role = {
            role: {
                "call_count": role_calls[role],
                **_usage_metrics(rounds),
            }
            for role, rounds in rounds_by_role.items()
        }
        invalid_submits = {
            "planner": 0, "reviewer": 0, "executor": 0,
        }
        seen_tool_calls: set[tuple[str, str]] = set()
        tool_sources: list[tuple[str, dict[str, Any], str]] = []
        for call in calls:
            role = str(call.get("role") or "")
            if role not in invalid_submits:
                continue
            payload = call.get(role) or {}
            step_seq = call.get("step_seq")
            tool_sources.append((
                role,
                payload,
                f"legacy:{step_seq}:{role}:normal",
            ))
        for event in traces:
            if event.kind not in {"agent_tool_finished", "agent_tool_failed"}:
                continue
            payload = event.payload or {}
            role = str(payload.get("role") or "")
            if role in invalid_submits:
                phase = str(payload.get("phase") or "normal")
                tool_sources.append((
                    role,
                    {"tool_calls": [payload]},
                    f"legacy:{event.step_seq}:{role}:{phase}",
                ))
        for role, payload, legacy_invocation in tool_sources:
            for tool_call in payload.get("tool_calls") or []:
                call_id = str(tool_call.get("call_id") or "").strip()
                invocation_id = str(
                    tool_call.get("invocation_id") or legacy_invocation
                ).strip()
                call_key = (invocation_id, call_id)
                if call_id and call_key in seen_tool_calls:
                    continue
                if call_id:
                    seen_tool_calls.add(call_key)
                if (
                    str(tool_call.get("category") or "") == "terminal"
                    and str(tool_call.get("status") or "")
                    in {"invalid_arguments", "invalid", "rejected"}
                ):
                    invalid_submits[role] += 1

        device_actions = 0
        observation_modes: dict[str, int] = {}
        observation_tiers: dict[str, int] = {}
        for call in calls:
            if call.get("step_seq") is None:
                continue
            executor = call.get("executor") or {}
            pipeline = executor.get("action_pipeline") or {}
            stages = pipeline.get("stages") or []
            if (
                not pipeline.get("dispatch_suppressed")
                and pipeline.get("driver_success") is True
                and any(stage.get("stage") == "dispatched" for stage in stages)
            ):
                device_actions += 1
            observation = call.get("observation") or {}
            mode = str(observation.get("observation_mode") or "").strip()
            if mode:
                observation_modes[mode] = observation_modes.get(mode, 0) + 1
            grounding = observation.get("grounding") or {}
            tier = str(grounding.get("evidence_tier") or "").strip()
            if tier:
                observation_tiers[tier] = observation_tiers.get(tier, 0) + 1
        return {
            "runtime_terminal_status": {
                "status": task.status.value,
                "failure_reason": task.failure_reason,
                "semantic_true_success": "not_assessed",
            },
            "outer_steps": len(
                [
                    step for step in steps
                    if step.get("step_seq") is not None
                    and any(
                        step.get(role) is not None
                        for role in ("planner", "reviewer", "executor")
                    )
                ]
            ),
            "role_calls": role_calls,
            "provider_requests": provider_requests,
            "role_rounds": {
                role: by_role[role]["round_count"]
                for role in ("planner", "reviewer", "executor")
            },
            "device_actions": device_actions,
            "invalid_submits": invalid_submits,
            "observation_modes": observation_modes,
            "observation_tiers": observation_tiers,
            **_usage_metrics(all_rounds),
            "by_role": by_role,
            "by_phase": {
            },
        }

    def task_detail(self, task: TaskRecord) -> dict[str, Any]:
        """Serialize a TaskRecord for the API, projecting closed-loop fields."""
        return {
            "id": task.id,
            "instruction": task.instruction,
            "status": task.status.value,
            "current_node_id": task.current_node_id,
            "current_subgoal": task.current_subgoal,
            "plan": task.plan,
            "step_number": task.step_number,
            "failure_reason": task.failure_reason,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "execution_elapsed_ms": task_execution_elapsed_ms(task),
            "device_serial": task.device_serial,
            "state": task.state.model_dump() if task.state else None,
        }

    def failed_tasks(self) -> list[dict[str, Any]]:
        """Return terminal failed tasks for the Console list."""
        tasks = self.db.list_tasks(status=TaskStatus.FAILED)
        return [
            {
                "id": t.id,
                "instruction": t.instruction,
                "failure_reason": t.failure_reason,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
                "execution_elapsed_ms": task_execution_elapsed_ms(t),
                "current_node_id": t.current_node_id,
                "current_subgoal": t.current_subgoal,
                "step_number": t.step_number,
                "device_serial": t.device_serial,
            }
            for t in tasks
        ]

    def replay(self, task_id: str) -> dict[str, Any]:
        """Ordered replay payload reconstructing the closed-loop sequence."""
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        steps = self.db.list_steps(task_id)
        # Reconstruct the focused-role sequence from traces for replay.
        traces = self._supported_traces(self.db.list_traces(task_id))
        loop_events = [
            event.model_dump() for event in traces
            if event.kind in {
                        "planner_decision",
                "reviewer_decision",
                "executor_tick",
                "loop_tick",
            }
        ]
        return {
            "task_id": task_id,
            "status": task.status.value,
            "instruction": task.instruction,
            "plan": task.plan,
            "current_subgoal": task.current_subgoal,
            "step_number": task.step_number,
            "failure_reason": task.failure_reason,
            "steps": steps,
            "loop_events": loop_events,
        }

    def timeline(self, task_id: str) -> dict[str, Any]:
        """Aggregated timeline: task metadata + step-grouped records + role calls.

        Returns the Planner→Executor loop with optional Reviewer calls grouped by `step_seq`
        into folded `steps` (shared observation / loop / step timing), plus a
        derived `calls` list with one navigable entry per focused role
        role tick. Console selection uses `call_key` (`step_seq`+`role`), not
        bare `step_seq`.

        Each `Step` carries:
          - observation (shared; executor, reviewer, then planner precedence)
          - reviewer (ReviewerDecision, optional)
          - planner (PlannerDecision, optional)
          - executor (ExecutorTick, optional)
          - loop (LoopTick, optional; attached to the step whose own step_seq
            equals the loop_tick's step_seq — effective plan/subgoal context
            written after deterministic role application)

        """
        task = self.db.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        traces = self._supported_traces(self.db.list_traces(task_id))
        wall_starts: dict[int, float] = {}
        for event in traces:
            if event.step_seq is None:
                continue
            previous = wall_starts.get(event.step_seq)
            wall_starts[event.step_seq] = (
                event.ts if previous is None else min(previous, event.ts)
            )

        steps_by_seq: dict[int | None, dict[str, Any]] = {}
        calls: list[dict[str, Any]] = []
        call_occurrences: dict[tuple[int | None, str], int] = {}
        def _bucket(seq: int | None) -> dict[str, Any]:
            b = steps_by_seq.get(seq)
            if b is None:
                b = {
                    "step_seq": seq,
                    "observation": None,
                    "observation_role": "",
                    "reviewer": None,
                    "planner": None,
                    "executor": None,
                    "loop": None,
                }
                steps_by_seq[seq] = b
            return b
        def _merge_observation(bucket: dict[str, Any], src: dict[str, Any]) -> None:
            """Select the highest-priority exact role envelope deterministically."""
            if not src:
                return
            cur = bucket.get("observation")
            role = str(src.get("role_hint") or "")
            priorities = {"planner": 1, "reviewer": 2, "executor": 3}
            current_role = str(bucket.get("observation_role") or "")
            if cur is None or priorities.get(role, 0) > priorities.get(current_role, 0):
                bucket["observation"] = observation_projection(src)
                bucket["observation_role"] = role
                return
            # Lower-priority packets may fill missing transport metadata only.
            for key in OBSERVATION_PROJECTION_FIELDS:
                if cur.get(key) in (None, [], "") and src.get(key) not in (None, [], ""):
                    cur[key] = src.get(key)

        def _append_role_call(
            seq: int | None,
            role: str,
            role_payload: dict[str, Any],
            observation_payload: dict[str, Any],
            event_ts: float | None,
        ) -> None:
            occurrence_key = (seq, role)
            ordinal = call_occurrences.get(occurrence_key, 0) + 1
            call_occurrences[occurrence_key] = ordinal
            observation = observation_projection(observation_payload)
            if not any(value not in (None, [], "", {}) for value in observation.values()):
                observation = None
            metrics = _call_metrics(role_payload)
            elapsed_ms = role_payload.get("role_elapsed_ms")
            wall_started_at = (
                event_ts - (float(elapsed_ms) / 1000.0)
                if event_ts is not None and isinstance(elapsed_ms, (int, float))
                else None
            )
            calls.append(
                {
                    "call_key": call_key_for(seq, role, ordinal),
                    "step_seq": seq,
                    "role": role,
                    "phase": _call_phase(role, role_payload),
                    "observation": observation,
                    "reviewer": role_payload if role == "reviewer" else None,
                    "planner": role_payload if role == "planner" else None,
                    "executor": role_payload if role == "executor" else None,
                    "elapsed_ms": elapsed_ms,
                    "llm_elapsed_ms": metrics.get("llm_latency_ms_total"),
                    "metrics": metrics,
                    "wall_started_at": wall_started_at,
                    "_ts": event_ts,
                }
            )

        for e in traces:
            seq = e.step_seq
            payload = e.payload or {}
            if e.kind in {"planner_decision", "reviewer_decision"}:
                bucket = _bucket(seq)
                normalized = _with_agent_rounds(payload)
                decision = normalized.get("decision")
                decision_fields = decision if isinstance(decision, dict) else {}
                role = "planner" if e.kind == "planner_decision" else "reviewer"
                role_payload = {
                    "kind": e.kind,
                    "step_seq": seq,
                    "message": e.message,
                    "level": e.level.value,
                    **decision_fields,
                    **normalized,
                }
                bucket[role] = role_payload
                _append_role_call(seq, role, role_payload, payload, e.ts)
                _merge_observation(bucket, {**payload, "role_hint": role})
            elif e.kind == "executor_tick":
                payload = _with_agent_rounds(payload)
                if payload.get("effective_action") is None and payload.get("action") is not None:
                    payload["effective_action"] = payload.get("action")
                if payload.get("action_origin") is None:
                    payload["action_origin"] = "model"
                bucket = _bucket(seq)
                role_payload = {
                    "kind": "executor_tick",
                    "step_seq": seq,
                    "message": e.message,
                    "level": e.level.value,
                    **payload,
                }
                bucket["executor"] = role_payload
                _append_role_call(seq, "executor", role_payload, payload, e.ts)
                _merge_observation(bucket, {**payload, "role_hint": "executor"})
            elif e.kind == "loop_tick":
                # Loop_tick(N) attaches to step N's `loop` slot (effective
                # context after deterministic role application for that step).
                bucket = _bucket(seq)
                bucket["loop"] = {
                    "kind": "loop_tick",
                    "step_seq": seq,
                    "message": e.message,
                    "level": e.level.value,
                    **payload,
                }
        # --- Orphan role invocations --------------------------------------
        # A role call that dies at the gateway (e.g. "AgentSession tool loop
        # exhausted without submit") persists only its in-flight agent_*
        # events — no planner_decision / reviewer_decision / executor_tick is
        # ever written for it. Live SSE folds those events into the calls
        # stream (web upsertLiveRoleEvent), so unless the replay does the
        # same, the failed call silently vanishes from the post-terminal
        # timeline. Reconstruct one call per invocation, mirroring the
        # frontend merge, so the timeline strictly reflects the actual
        # invocation sequence.
        owned_invocations: set[str] = set()
        for call in calls:
            tick = call.get(call["role"])
            if not isinstance(tick, dict):
                continue
            for item in list(tick.get("agent_rounds") or []) + list(tick.get("tool_calls") or []):
                if isinstance(item, dict) and item.get("invocation_id"):
                    owned_invocations.add(str(item["invocation_id"]))

        invocations: dict[str, dict[str, Any]] = {}
        invocation_order: list[str] = []
        # role_invocation_started(round=0) immediately precedes the first
        # agent event of each invocation and carries the phase label; pair
        # them in trace order (invocations never interleave within a task).
        pending_start: tuple[int | None, str] | None = None
        pending_phase: str | None = None
        last_invocation_at: dict[tuple[int | None, str], str] = {}

        for e in traces:
            payload = e.payload or {}
            if e.kind == "system":
                message = e.message or ""
                if message == "role_invocation_started" and payload.get("round") == 0:
                    pending_start = (
                        e.step_seq,
                        str(payload.get("role") or ""),
                    )
                    pending_phase = str(payload.get("phase") or "")
                elif "gateway error" in message:
                    role = str(payload.get("role") or "")
                    inv_id = last_invocation_at.get((e.step_seq, role))
                    if inv_id is not None and inv_id in invocations:
                        invocations[inv_id]["error"] = {
                            "message": message,
                            "level": e.level.value,
                            "will_retry": bool(payload.get("will_retry")),
                        }
                continue
            if e.kind not in {
                "agent_llm_round_started",
                "agent_llm_round_finished",
                "agent_tool_started",
                "agent_tool_finished",
                "agent_tool_failed",
            }:
                continue
            inv_id = str(payload.get("invocation_id") or "")
            role = str(payload.get("role") or "")
            if not inv_id or role not in (
                "reviewer", "planner", "executor",
            ):
                continue
            inv = invocations.get(inv_id)
            if inv is None:
                inv = {
                    "role": role,
                    "step_seq": e.step_seq,
                    "phase": None,
                    "agent_rounds": [],
                    "tool_calls": [],
                    "first_ts": e.ts,
                    "error": None,
                }
                if pending_start is not None and pending_start == (e.step_seq, role):
                    inv["phase"] = pending_phase
                    pending_start = None
                    pending_phase = None
                invocations[inv_id] = inv
                invocation_order.append(inv_id)
            last_invocation_at[(e.step_seq, role)] = inv_id
            if e.kind in {"agent_llm_round_started", "agent_llm_round_finished"}:
                rounds = inv["agent_rounds"]
                round_id = payload.get("round_id")
                for index, existing in enumerate(rounds):
                    if existing.get("round_id") == round_id:
                        rounds[index] = payload
                        break
                else:
                    rounds.append(payload)
            else:
                tool_calls = inv["tool_calls"]
                tool_call_id = payload.get("call_id")
                for index, existing in enumerate(tool_calls):
                    if existing.get("call_id") == tool_call_id:
                        tool_calls[index] = payload
                        break
                else:
                    tool_calls.append(payload)

        kind_by_role = {
            "reviewer": "reviewer_decision",
            "planner": "planner_decision",
            "executor": "executor_tick",
        }
        for inv_id in invocation_order:
            if inv_id in owned_invocations:
                continue
            inv = invocations[inv_id]
            role = inv["role"]
            seq = inv["step_seq"]
            occurrence_key = (seq, role)
            ordinal = call_occurrences.get(occurrence_key, 0) + 1
            call_occurrences[occurrence_key] = ordinal
            error = inv["error"] if isinstance(inv["error"], dict) else None
            tick: dict[str, Any] = {
                "kind": (
                    kind_by_role[role]
                ),
                "step_seq": seq,
                "message": (
                    str(error.get("message"))
                    if error
                    else "invocation ended without a terminal submit"
                ),
                "level": (
                    str(error.get("level")) if error else LogLevel.INFO.value
                ),
                "role": role,
                "agent_rounds": inv["agent_rounds"],
                "tool_calls": inv["tool_calls"],
                # No decision event exists for this invocation; the call is
                # reconstructed from in-flight agent_* events.
                "incomplete": True,
            }
            if error:
                tick["error"] = error
            metrics = _call_metrics(tick)
            first_round = min(
                inv["agent_rounds"], key=lambda item: int(item.get("order") or 0),
                default=None,
            )
            orphan_observation = None
            if isinstance(first_round, dict) and first_round.get("input_observation_id"):
                model_image_ref = first_round.get("input_model_image_ref")
                orphan_observation = {
                    "model_image_ref": model_image_ref,
                    "som_ref": None,
                    "tree_ref": None,
                    "observation_mode": (
                        None if model_image_ref else "tree-only"
                    ),
                    "observation_id": first_round.get("input_observation_id"),
                    "captured_monotonic_ms": first_round.get(
                        "input_captured_monotonic_ms"
                    ),
                    "coordinate_actionable": False,
                    "index_actionable": False,
                }
            calls.append(
                {
                    "call_key": call_key_for(seq, role, ordinal),
                    "step_seq": seq,
                    "role": role,
                    "phase": _call_phase(role, tick),
                    "observation": orphan_observation,
                    "reviewer": tick if role == "reviewer" else None,
                    "planner": tick if role == "planner" else None,
                    "executor": tick if role == "executor" else None,
                    "elapsed_ms": None,
                    "llm_elapsed_ms": metrics.get("llm_latency_ms_total"),
                    "metrics": metrics,
                    "wall_started_at": wall_starts.get(seq),
                    "_ts": inv["first_ts"],
                }
            )

        # Chronological merge: decision calls were appended in trace order,
        # orphan calls afterwards — stable-sort by first event ts so the
        # calls stream reflects the actual invocation sequence.
        calls.sort(key=lambda c: (c.get("_ts") is None, c.get("_ts") or 0.0))
        for call in calls:
            call.pop("_ts", None)

        # Stable order: by step_seq ascending, with NULL-seq runtime lifecycle
        # ticks kept last; they are mechanical status, not true-success labels.
        ordered_steps: list[dict[str, Any]] = []
        for seq in sorted(k for k in steps_by_seq.keys() if k is not None):
            ordered_steps.append(steps_by_seq[seq])
        if None in steps_by_seq:
            ordered_steps.append(steps_by_seq[None])

        for s in ordered_steps:
            if s.get("step_seq") is None:
                continue
            wall_started_at = wall_starts.get(s["step_seq"])
            if wall_started_at is not None:
                s["wall_started_at"] = wall_started_at
        # A step is an action counter, so several role calls can legitimately
        # share it.  Keep the folded step for overview display, but enrich the
        # trace-ordered calls rather than regenerating them from one slot per
        # role (which would silently lose Planner/Reviewer retries).
        steps_index = {step.get("step_seq"): step for step in ordered_steps}
        for call in calls:
            parent = steps_index.get(call.get("step_seq")) or {}
            if call.get("wall_started_at") is None:
                call["wall_started_at"] = parent.get("wall_started_at")
        metrics = self._metrics(task, ordered_steps, calls, traces)
        return {
            "task_id": task_id,
            "status": task.status.value,
            "instruction": task.instruction,
            "device_serial": task.device_serial,
            "plan": task.plan,
            "current_subgoal": task.current_subgoal,
            "next_role": task.state.revisable.next_role if task.state else None,
            "revisable": (
                task.state.revisable.model_dump(
                    include={"revision", "plan", "completed_stage_ids", "next_role", "feedback"}
                )
                if task.state and task.state.revisable.plan else None
            ),
            "role_invocation_count": (
                task.state.role_invocation_count if task.state else None
            ),
            "step_number": task.step_number,
            "failure_reason": task.failure_reason,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "execution_elapsed_ms": task_execution_elapsed_ms(task),
            "steps": ordered_steps,
            "calls": calls,
            "metrics": metrics,
        }

    def step_debug(self, task_id: str, node_id: str, seq: int) -> dict[str, Any]:
        """Per-step debug payload for tree/SoM/LLM inspection.

        `node_id` slot now carries the current_subgoal identifier.
        """
        steps = self.db.list_steps(task_id)
        match = next(
            (s for s in steps if s.get("node_id") == node_id and s.get("seq") == seq),
            None,
        )
        if not match:
            raise KeyError(f"{task_id}/{node_id}/{seq}")
        capture: dict[str, Any] = {}
        for event in reversed(self.db.list_traces(task_id)):
            if event.step_seq != seq or event.kind not in {
                "executor_tick", "planner_decision", "reviewer_decision",
            }:
                continue
            candidate = (event.payload or {}).get("capture")
            if isinstance(candidate, dict):
                capture = candidate
                break
        return {
            "task_id": task_id,
            "node_id": node_id,
            "seq": seq,
            "observation_mode": match.get("observation_mode"),
            "tree_ref": match.get("tree_ref"),
            "annotated_ref": match.get("annotated_ref"),
            "llm_input_ref": match.get("llm_input_ref"),
            "llm_output_ref": match.get("llm_output_ref"),
            "action": match.get("action"),
            "summary": match.get("summary"),
            "reason": match.get("reason"),
            "observation_digest": match.get("observation_digest"),
            "grounding": observation_projection({"capture": capture}).get(
                "grounding", {}
            ),
        }

    def traces(self, task_id: str, level: str | None = None) -> list[dict[str, Any]]:
        """Retrieve supported focused-role, Executor, and loop traces."""
        lvl = LogLevel(level) if level else None
        events = self._supported_traces(self.db.list_traces(task_id, lvl))
        return [event.model_dump() for event in events]
