"""TraceWriter for full-chain observability."""

from __future__ import annotations

import time
from typing import Any

from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import LogLevel, TraceEvent

# Forward-declared to avoid an import cycle (control_api imports agent).
EventBusLike = Any  # control_api.sse.EventBus


class TraceWriter:
    """Persist LLM/action/UI/loop events with optional large payload refs.

    Supported `kind` values: `task_scope`, `planner_decision`,
    `reviewer_decision`, `executor_tick`, `loop_tick` (closed-loop event types),
    plus the `llm` / `ui` / `system`
    kinds. Executor model/action/result data is consolidated into one
    `executor_tick` event.
    """

    def __init__(
        self,
        db: Database,
        artifacts: ArtifactStore,
        bus: EventBusLike | None = None,
    ) -> None:
        self.db = db
        self.artifacts = artifacts
        self.bus = bus

    # Structured tick kinds whose payloads the Console timeline renders
    # inline (decision roles / executor_tick / loop_tick). These MUST NOT be
    # ref-ified: the timeline API expands `**e.payload` into the tick view, so
    # replacing the payload with `{"ref": ...}` would leave the frontend with a
    # bare reference instead of the SoM screenshot, semantic tree, action
    # details, or decision-role LLM input/output. Large binary payloads,
    # SoM PNGs) are still stored as artifacts and referenced by `*_ref` fields
    # inside the payload; only the wholesale payload→ref swap is suppressed.
    INLINE_PAYLOAD_KINDS = frozenset({
        "task_scope", "planner_decision", "reviewer_decision",
        "executor_tick", "loop_tick",
        "agent_tool_started", "agent_tool_finished", "agent_tool_failed",
        "agent_llm_round_finished",
        "agent_input_safety_degraded",
        "agent_tool_call_recovery",
        "harness_event",
    })

    def write(
        self,
        task_id: str,
        *,
        kind: str = "system",
        level: LogLevel = LogLevel.INFO,
        message: str = "",
        node_id: str | None = None,
        step_seq: int | None = None,
        payload: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        payload_bytes: bytes | None = None,
        payload_suffix: str = ".bin",
        artifact_kind: str = "llm",
    ) -> TraceEvent:
        """Append a TraceEvent, storing large blobs on disk when provided."""
        ref = payload_ref
        if payload_bytes is not None:
            ref = self.artifacts.save_bytes(artifact_kind, payload_bytes, suffix=payload_suffix)
        elif (
            ref is None
            and payload is not None
            and len(str(payload)) > 2000
            and kind not in self.INLINE_PAYLOAD_KINDS
        ):
            # Only ref-ify large payloads for non-structured kinds (e.g. raw
            # `llm` blobs). Structured tick kinds stay inline so the Console
            # timeline can render them without a second fetch.
            ref = self.artifacts.save_json(artifact_kind, payload)
            payload = {"ref": ref}

        event = TraceEvent(
            task_id=task_id,
            node_id=node_id,
            step_seq=step_seq,
            kind=kind,  # type: ignore[arg-type]
            level=level,
            message=message,
            payload_ref=ref,
            payload=payload or {},
            ts=time.time(),
        )
        self.db.add_trace(event)
        # Notify live SSE subscribers (same event loop). No bus → no-op,
        # which keeps unit tests that never wire a bus compatible.
        if self.bus is not None:
            self.bus.publish(task_id, event.model_dump())
        return event
