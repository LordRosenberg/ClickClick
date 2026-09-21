"""Observability: new closed-loop event kinds, replay reconstruction, level filter."""

import asyncio
from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from control_api.services import ObservabilityQueries, task_execution_elapsed_ms
from control_api.sse import EventBus
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import Action, ActionPipeline, ActionPipelineStage, ActionResult, AgentState, CanonicalUI, ExecutorDecisionKind, ExecutorStepSubmit, ExecutorStep, FocusedElementEvidence, InteractionStateEvidence, ObservationMode, TaskStatus, UIElement
from tests.fake_agents import FakeExecutor


def test_task_execution_elapsed_uses_terminal_update_or_current_time(tmp_path: Path):
    db = Database(tmp_path / "elapsed.db")
    task = db.create_task("timed task")
    db._conn.execute(
        "UPDATE tasks SET status=?, created_at=?, updated_at=? WHERE id=?",
        (TaskStatus.SUCCEEDED.value, 10.0, 13.5, task.id),
    )
    db._conn.commit()
    terminal = db.get_task(task.id)
    assert terminal is not None
    assert task_execution_elapsed_ms(terminal, now=99.0) == 3500

    db._conn.execute(
        "UPDATE tasks SET status=? WHERE id=?",
        (TaskStatus.RUNNING.value, task.id),
    )
    db._conn.commit()
    running = db.get_task(task.id)
    assert running is not None
    assert task_execution_elapsed_ms(running, now=15.0) == 5000


def test_metrics_union_raw_and_folded_role_rounds_once(tmp_path: Path):
    db = Database(tmp_path / "scope-metrics.db")
    arts = ArtifactStore(tmp_path / "scope-metrics-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("measure scope")

    review_round = {
        "round_id": "contract-round-1",
        "invocation_id": "contract-invocation-1",
        "role": "reviewer",
        "phase": "review",
        "latency_ms": 10,
        "usage": {
            "input_tokens": 100,
            "cached_read_tokens": 20,
            "output_tokens": 10,
        },
    }
    normal_round = {
        "round_id": "planner-round-1",
        "invocation_id": "planner-invocation-1",
        "role": "planner",
        "latency_ms": 20,
        "usage": {
            "input_tokens": 200,
            "cached_read_tokens": 40,
            "output_tokens": 20,
        },
    }
    traces.write(
        task.id, kind="agent_llm_round_finished", step_seq=0,
        payload=review_round,
    )
    for role in ("reviewer", "reviewer", "planner"):
        traces.write(
            task.id,
            kind="harness_event",
            step_seq=0,
            message="role_invocation_started",
            payload={"role": role},
        )
    traces.write(
        task.id, kind="agent_llm_round_finished", step_seq=0,
        payload=review_round,
    )
    traces.write(
        task.id, kind="reviewer_decision", step_seq=0,
        payload={
            "status": "reviewered",
            "agent_rounds": [review_round],
            "role_elapsed_ms": 14,
        },
    )
    traces.write(
        task.id,
        kind="task_scope",
        step_seq=0,
        payload={"phase": "task_scope", "agent_rounds": [review_round]},
    )
    traces.write(
        task.id,
        kind="agent_llm_round_finished",
        step_seq=0,
        payload={
            **review_round,
            "round_id": "obsolete-scope-round",
            "role": "reviewer",
            "phase": "reviewer_scope",
        },
    )
    traces.write(
        task.id, kind="planner_decision", step_seq=0,
        payload={
            "mode": "execute",
            "agent_rounds": [normal_round],
            "role_elapsed_ms": 27,
        },
    )

    queries = ObservabilityQueries(db, arts)
    timeline = queries.timeline(task.id)
    metrics = timeline["metrics"]

    assert metrics["round_count"] == 2
    assert metrics["input_tokens_total"] == 300
    assert metrics["output_tokens_total"] == 30
    assert metrics["cache_read_tokens_total"] == 60
    assert metrics["by_role"]["reviewer"]["round_count"] == 1
    assert metrics["by_role"]["planner"]["round_count"] == 1
    assert [call["phase"] for call in timeline["calls"]] == [
        "boundary", "planning",
    ]
    assert [call["elapsed_ms"] for call in timeline["calls"]] == [14, 27]
    assert timeline["calls"][0]["metrics"]["input_tokens_total"] == 100
    assert timeline["calls"][0]["llm_elapsed_ms"] == 10
    assert metrics["role_calls"] == {
        "reviewer": 1, "planner": 1, "executor": 0,
    }
    assert metrics["provider_requests"] == {
        "reviewer": 2, "planner": 1, "executor": 0,
    }
    assert all(
        event["kind"] != "task_scope"
        and event["payload"].get("phase") not in {"task_scope", "reviewer_scope"}
        for event in queries.traces(task.id)
    )
    assert all("harness_events" not in call for call in timeline["calls"])


def test_orphan_invocation_preserves_its_exact_first_round_image(tmp_path: Path):
    db = Database(tmp_path / "orphan-image.db")
    arts = ArtifactStore(tmp_path / "orphan-image-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("orphan model image")
    traces.write(
        task.id, kind="system", step_seq=3, message="role_invocation_started",
        payload={"role": "executor", "round": 0, "phase": "execution"},
    )
    traces.write(
        task.id, kind="agent_llm_round_started", step_seq=3,
        payload={
            "round_id": "orphan-round", "invocation_id": "orphan-invocation",
            "role": "executor", "order": 0, "latency_ms": 12, "usage": {},
            "input_observation_id": "obs-exact",
            "input_model_image_ref": "model-images/exact.jpg",
            "input_captured_monotonic_ms": 42.5,
        },
    )

    call = next(
        item for item in ObservabilityQueries(db, arts).timeline(task.id)["calls"]
        if item["role"] == "executor"
    )
    assert call["executor"]["incomplete"] is True
    assert call["observation"] == {
        "model_image_ref": "model-images/exact.jpg",
        "som_ref": None,
        "tree_ref": None,
        "observation_mode": None,
        "observation_id": "obs-exact",
        "captured_monotonic_ms": 42.5,
        "coordinate_actionable": False,
        "index_actionable": False,
    }


def test_invalid_submit_metrics_key_call_ids_by_invocation(tmp_path: Path):
    db = Database(tmp_path / "invalid-invocations.db")
    arts = ArtifactStore(tmp_path / "invalid-invocations-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("same provider call id in separate invocations")

    common = {
        "call_id": "call_1",
        "category": "terminal",
        "status": "invalid_arguments",
    }
    traces.write(
        task.id,
        kind="agent_tool_failed",
        step_seq=0,
        payload={
            **common,
            "role": "reviewer",
            "phase": "review",
            "invocation_id": "contract-invocation",
        },
    )
    traces.write(
        task.id,
        kind="agent_tool_failed",
        step_seq=0,
        payload={
            **common,
            "role": "reviewer",
            "invocation_id": "reviewer-invocation",
        },
    )

    metrics = ObservabilityQueries(db, arts).timeline(task.id)["metrics"]

    assert metrics["invalid_submits"] == {
        "planner": 0, "reviewer": 2, "executor": 0,
    }


# --- artifact persistence + timeline (observability-recording) ------------


class _CapturingExecutor:
    """Minimal executor that records packages and returns scripted actions.

    Uses the real ObservationPackage from the Orchestrator (built from a
    FixtureDriver frame) so artifact persistence can be exercised.
    """

    def __init__(self, actions):
        self.driver = None
        self.artifacts = None
        self.model = "fake"
        self._raw = list(actions)
        self._idx = 0
        self.seen_packages: list = []

    async def act_once(
        self,
        subgoal,
        package=None,
        prior_result="",
        *,
        task_id="",
        **kwargs,
    ):
        self.seen_packages.append(package)
        entry = self._raw[self._idx] if self._idx < len(self._raw) else Action(type="sleep", duration_ms=10)
        self._idx += 1
        action, done = (entry, False) if not isinstance(entry, tuple) else entry
        ui = package.ui if package is not None else CanonicalUI()
        if done:
            step = ExecutorStep(
                decision=ExecutorDecisionKind.REQUEST_REVIEW,
                review_reason="boundary_ready",
                summary=f"{getattr(action, 'type', 'step')} complete",
                result="ok",
            )
        else:
            step = ExecutorStep(
                decision=ExecutorDecisionKind.ACT,
                action=action,
                summary=action.type,
                result="ok",
            )
        return step, ActionResult(success=True, message="ok"), ui, ObservationMode.TREE_ONLY, {}

    async def aclose(self):
        return None


def test_timeline_executor_projection_preserves_replay_identity_and_geometry(tmp_path: Path):
    db = Database(tmp_path / "projection.db")
    arts = ArtifactStore(tmp_path / "projection-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("projection")
    planner_observation = {
        "som_ref": "som/planner.png",
        "tree_ref": "trees/planner.json",
        "observation_mode": "tree_only",
        "gap_reasons": [],
        "estimated_tokens": 10,
        "observation_id": "obs_planner",
        "captured_monotonic_ms": 10,
        "frame_geometry": [100, 200],
    }
    executor_observation = {
        "som_ref": "som/executor.png",
        "tree_ref": "trees/executor.json",
        "observation_mode": "tree_plus_image",
        "gap_reasons": ["visual_required"],
        "estimated_tokens": 20,
        "observation_id": "obs_executor",
        "captured_monotonic_ms": 20,
        "frame_geometry": [1200, 2670],
        "coordinate_actionable": True,
        "index_actionable": True,
    }
    traces.write(
        task.id, kind="planner_decision", step_seq=1,
        payload=planner_observation,
    )
    traces.write(
        task.id, kind="executor_tick", step_seq=1,
        payload={**executor_observation, "effective_action": {"type": "tap_xy", "x": 600, "y": 1335}},
    )

    timeline = ObservabilityQueries(db, arts).timeline(task.id)
    observation = timeline["steps"][0]["observation"]

    assert observation == executor_observation
    executor_call = next(call for call in timeline["calls"] if call["role"] == "executor")
    assert executor_call["observation"] == executor_observation


def test_timeline_preserves_repeated_same_step_role_calls(tmp_path: Path):
    db = Database(tmp_path / "repeated-role.db")
    arts = ArtifactStore(tmp_path / "repeated-role-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("retry planning")
    for ordinal in (1, 2):
        traces.write(
            task.id, kind="planner_decision", step_seq=3,
            payload={
                "decision": {"mode": "execute", "next_subgoal": f"attempt {ordinal}"},
                "agent_rounds": [{
                    "invocation_id": f"planner-inv-{ordinal}",
                    "round_id": f"planner-round-{ordinal}",
                    "usage": {"input_tokens": 10},
                }],
            },
        )
    timeline = ObservabilityQueries(db, arts).timeline(task.id)
    calls = [call for call in timeline["calls"] if call["role"] == "planner"]
    assert [call["call_key"] for call in calls] == ["3:planner:1", "3:planner:2"]
    assert [call["planner"]["decision"]["next_subgoal"] for call in calls] == [
        "attempt 1", "attempt 2",
    ]
    assert timeline["metrics"]["role_calls"]["planner"] == 2


def test_timeline_historical_observation_keeps_geometry_unknown(tmp_path: Path):
    db = Database(tmp_path / "historical-geometry.db")
    arts = ArtifactStore(tmp_path / "historical-geometry-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("historical")
    traces.write(
        task.id, kind="executor_tick", step_seq=1,
        payload={"som_ref": "som/legacy.png", "tree_ref": "trees/legacy.json"},
    )

    observation = ObservabilityQueries(db, arts).timeline(task.id)["steps"][0]["observation"]

    assert observation["frame_geometry"] is None
    assert observation["observation_id"] is None


def test_timeline_projects_grounding_facts_without_model_prompt_fields(tmp_path: Path):
    db = Database(tmp_path / "grounding.db")
    arts = ArtifactStore(tmp_path / "grounding-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("grounding")
    traces.write(
        task.id,
        kind="executor_tick",
        step_seq=1,
        payload={
            "tree_ref": "trees/current.json",
            "capture": {
                "foreground_app_id": "com.example",
                "foreground_activity": ".Main",
                "top_window_package": "com.android.systemui",
                "provider_attempts": [{"provider": "primary", "status": "ok"}],
                "fallback_edges": [],
                "cancelled_capture_tasks": [],
                "tree_providers_exhausted": False,
                "grounding_barrier": "aligned",
                "generation": 4,
                "tree_generation": 9,
                "action_boundary_generation": 4,
                "action_frame_boundary_id": 10,
                "frame_geometry": [1200, 2670],
                "unrelated_internal_detail": "not projected",
            },
        },
    )

    grounding = ObservabilityQueries(db, arts).timeline(task.id)["steps"][0][
        "observation"
    ]["grounding"]
    assert grounding == {
        "foreground_app_id": "com.example",
        "foreground_activity": ".Main",
        "top_window_package": "com.android.systemui",
        "provider_attempts": [{"provider": "primary", "status": "ok"}],
        "fallback_edges": [],
        "cancelled_capture_tasks": [],
        "tree_providers_exhausted": False,
        "grounding_barrier": "aligned",
        "generation": 4,
        "tree_generation": 9,
        "action_boundary_generation": 4,
        "action_frame_boundary_id": 10,
        "frame_geometry": [1200, 2670],
    }


def test_timeline_metrics_aggregate_tokens_rounds_and_cache_reporting(tmp_path: Path):
    db = Database(tmp_path / "metrics.db")
    arts = ArtifactStore(tmp_path / "metrics-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("metrics")
    db.update_task(task.id, status=TaskStatus.SUCCEEDED, state=task.state)

    traces.write(
        task.id,
        kind="planner_decision",
        step_seq=1,
        payload={
            "agent_rounds": [
                {
                    "invocation_id": "m-inv",
                    "round_id": "m-1",
                    "latency_ms": 10,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cached_read_tokens": 20,
                        "cache_write_tokens": 5,
                    },
                },
                {
                    "invocation_id": "m-inv",
                    "round_id": "m-2",
                    "latency_ms": 20,
                    "usage": {
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "cached_read_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                },
            ],
            "tool_calls": [{
                "call_id": "m-invalid",
                "category": "terminal",
                "status": "invalid_arguments",
            }],
        },
    )
    traces.write(
        task.id,
        kind="executor_tick",
        step_seq=1,
        payload={
            "agent_rounds": [
                {
                    "invocation_id": "e-inv",
                    "round_id": "e-1",
                    "latency_ms": 30,
                    "usage": {
                        "input_tokens": 300,
                        "output_tokens": 30,
                        "cached_read_tokens": 60,
                        "cache_write_tokens": 15,
                    },
                },
                {
                    "invocation_id": "e-inv",
                    "round_id": "e-2",
                    "latency_ms": 40,
                    "usage": {
                        "input_tokens": 400,
                        "output_tokens": 40,
                        "cached_read_tokens": 0,
                        "cache_write_tokens": 0,
                    },
                },
            ],
            "tool_calls": [{
                "call_id": "e-invalid",
                "category": "terminal",
                "status": "rejected",
            }],
            "action": {"type": "tap", "index": 0},
            "action_result": {"success": True, "message": "ok"},
            "action_pipeline": {
                "driver_success": True,
                "dispatch_suppressed": False,
                "stages": [{"stage": "dispatched"}],
            },
            "observation_mode": "tree+image",
            "capture": {"evidence_tier": "grounded"},
        },
    )

    timeline = ObservabilityQueries(db, arts).timeline(task.id)
    metrics = timeline["metrics"]

    assert metrics["runtime_terminal_status"] == {
        "status": "succeeded",
        "failure_reason": None,
        "semantic_true_success": "not_assessed",
    }
    assert metrics["outer_steps"] == 1
    assert metrics["role_calls"] == {
        "planner": 1, "reviewer": 0, "executor": 1,
    }
    assert metrics["role_rounds"] == {
        "planner": 2, "reviewer": 0, "executor": 2,
    }
    assert metrics["round_count"] == 4
    assert metrics["input_tokens_total"] == 1000
    assert metrics["input_tokens_p95"] == 400
    assert metrics["output_tokens_total"] == 100
    assert metrics["output_tokens_p95"] == 40
    assert metrics["llm_latency_ms_total"] == 100
    assert metrics["llm_latency_ms_p95"] == 40
    assert metrics["cache_read_tokens_total"] == 80
    assert metrics["cache_write_tokens_total"] == 20
    assert metrics["cache_eligible_input_tokens_total"] == 1000
    assert metrics["cache_read_ratio"] == pytest.approx(80 / 1000)
    assert metrics["cache_write_ratio"] == pytest.approx(20 / 1000)
    assert metrics["device_actions"] == 1
    assert metrics["invalid_submits"] == {
        "planner": 1, "reviewer": 0, "executor": 1,
    }
    # Only the Executor call owns this observation; it is not copied onto the
    # co-located Planner call.
    assert metrics["observation_modes"] == {"tree+image": 1}
    assert metrics["observation_tiers"] == {"grounded": 1}
    assert metrics["by_role"]["planner"]["cache_reporting"] == {
        "read_reported_rounds": 2,
        "write_reported_rounds": 2,
        "input_rounds": 2,
        "complete": True,
    }
    assert metrics["by_role"]["executor"]["cache_reporting"] == {
        "read_reported_rounds": 2,
        "write_reported_rounds": 2,
        "input_rounds": 2,
        "complete": True,
    }


def test_timeline_metrics_leave_missing_cache_data_unavailable(tmp_path: Path):
    db = Database(tmp_path / "metrics-unavailable.db")
    arts = ArtifactStore(tmp_path / "metrics-unavailable-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("metrics unavailable")

    traces.write(
        task.id,
        kind="planner_decision",
        step_seq=1,
        payload={
            "agent_rounds": [
                {
                    "invocation_id": "m-inv",
                    "round_id": "m-1",
                    "usage": {"input_tokens": 120},
                }
            ]
        },
    )

    metrics = ObservabilityQueries(db, arts).timeline(task.id)["metrics"]

    assert metrics["cache_read_tokens_total"] is None
    assert metrics["cache_write_tokens_total"] is None
    assert metrics["cache_eligible_input_tokens_total"] is None
    assert metrics["cache_read_ratio"] is None
    assert metrics["cache_write_ratio"] is None
    assert metrics["cache_reporting"] == {
        "read_reported_rounds": 0,
        "write_reported_rounds": 0,
        "input_rounds": 1,
        "complete": False,
    }


def test_usage_metrics_do_not_treat_partial_cache_reporting_as_complete():
    from control_api.services import _usage_metrics

    metrics = _usage_metrics([
        {"usage": {"input_tokens": 100, "cached_read_tokens": 40}},
        {"usage": {"input_tokens": 200}},
    ])

    assert metrics["cache_read_tokens_total"] is None
    assert metrics["cache_eligible_input_tokens_total"] is None
    assert metrics["cache_read_ratio"] is None
    assert metrics["cache_reporting"] == {
        "read_reported_rounds": 1,
        "write_reported_rounds": 0,
        "input_rounds": 2,
        "complete": False,
    }


# --- SSE live streaming (observation-console) -------------------------------


@pytest.mark.asyncio
async def test_sse_subscriber_cap_enforced_on_bus(tmp_path: Path):
    """The EventBus itself enforces the per-task subscriber cap."""
    import control_api.sse as sse_mod

    bus = sse_mod.EventBus(max_subscribers=2)
    _, u1 = bus.subscribe("t")
    _, u2 = bus.subscribe("t")
    with pytest.raises(sse_mod.TooManySubscribers):
        bus.subscribe("t")
    u1()
    u2()
    assert bus.subscriber_count("t") == 0


# ---------------------------------------------------------------------------
# payload ref-ification exemption for structured tick kinds
# (perception-robustness)
# ---------------------------------------------------------------------------


def _big_payload() -> dict:
    """A payload whose string form exceeds the 2000-char ref threshold."""
    return {"blob": "x" * 3000, "kept": "yes"}


def test_structured_tick_payloads_stay_inline(tmp_path: Path):
    db = Database(tmp_path / "ref.db")
    arts = ArtifactStore(tmp_path / "arts")
    traces = TraceWriter(db, arts)
    big = _big_payload()
    for kind in ("planner_decision", "reviewer_decision", "executor_tick", "loop_tick"):
        traces.write("t1", kind=kind, step_seq=0, payload=big)
    events = {e.kind: e for e in db.list_traces("t1")}
    for kind in ("planner_decision", "reviewer_decision", "executor_tick", "loop_tick"):
        ev = events[kind]
        # Payload is NOT replaced with {"ref": ...}; the original keys survive.
        assert "blob" in ev.payload
        assert "kept" in ev.payload
        assert ev.payload.get("kept") == "yes"
        assert "ref" not in ev.payload


def test_llm_kind_payload_still_refified(tmp_path: Path):
    db = Database(tmp_path / "ref2.db")
    arts = ArtifactStore(tmp_path / "arts2")
    traces = TraceWriter(db, arts)
    big = _big_payload()
    traces.write("t2", kind="llm", step_seq=0, payload=big)
    ev = db.list_traces("t2")[0]
    # Non-structured kind still gets ref-ified.
    assert "ref" in ev.payload
    assert "blob" not in ev.payload
    assert ev.payload_ref is not None


def test_small_structured_payload_unchanged(tmp_path: Path):
    db = Database(tmp_path / "ref3.db")
    arts = ArtifactStore(tmp_path / "arts3")
    traces = TraceWriter(db, arts)
    traces.write("t3", kind="executor_tick", step_seq=0, payload={"action": "tap"})
    ev = db.list_traces("t3")[0]
    assert ev.payload == {"action": "tap"}
    assert ev.payload_ref is None


@pytest.mark.asyncio
async def test_llm_stream_event_is_live_only(tmp_path: Path):
    db = Database(tmp_path / "stream.db")
    arts = ArtifactStore(tmp_path / "stream-arts")
    bus = EventBus()
    queue, unsubscribe = bus.subscribe("stream-task")
    traces = TraceWriter(db, arts, bus)

    traces.write(
        "stream-task", kind="agent_llm_stream", step_seq=2,
        payload={"round_id": "r1", "text": "partial", "sequence": 1},
    )
    event = await asyncio.wait_for(queue.get(), timeout=0.1)
    unsubscribe()

    assert event["kind"] == "agent_llm_stream"
    assert event["payload"]["text"] == "partial"
    assert db.list_traces("stream-task") == []


# ---------------------------------------------------------------------------
# Planner LLM input/output refs in trace + timeline
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# step-timing-display: timing fields in trace + timeline + task totals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_timing_missing_fields_render_gracefully(tmp_path: Path):
    """Historical tasks recorded before timing fields exist still load:
    timeline returns without errors, steps simply lack the timing keys.
    """
    db = Database(tmp_path / "no-time.db")
    arts = ArtifactStore(tmp_path / "no-arts")
    from shared.schemas import LogLevel, TraceEvent

    # Pre-populate a no-timing task with a legacy Executor event.
    db._conn.execute(
        "INSERT INTO tasks(id, instruction, status, current_node_id, failure_reason, "
        "state_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("no-time", "old", "succeeded", None, None, "{}", 1.0, 1.0),
    )
    db._conn.execute(
        "INSERT INTO traces(task_id, node_id, step_seq, kind, level, message, "
        "payload_ref, payload_json, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        ("no-time", None, 0, "executor_tick", "INFO", "tap -> ok",
         None, '{"action":{"type":"tap","index":0},"action_result":{"success":true,'
         '"message":"ok"},"window_phase":"x","phase_change":"stay"}', 1.0),
    )
    db._conn.commit()

    q = ObservabilityQueries(db, arts)
    tl = q.timeline("no-time")
    assert tl["steps"], "no-timing task still produces a step"
    step0 = tl["steps"][0]
    assert "total_elapsed_ms" not in tl
    assert "llm_total_elapsed_ms" not in tl
    # No timing key was lifted onto the step.
    for key in ("step_started_at", "step_finished_at",
                "step_elapsed_ms", "llm_elapsed_ms"):
        assert key not in step0, (
            f"historical step should not carry synthesized timing key: {key}"
        )


def test_tool_call_recovery_event_persists_inline(tmp_path: Path):
    db = Database(tmp_path / "recovery.db")
    arts = ArtifactStore(tmp_path / "recovery-arts")
    writer = TraceWriter(db, arts)

    event = writer.write(
        "task",
        kind="agent_tool_call_recovery",
        step_seq=3,
        message="agent_tool_call_recovery",
        payload={"role": "executor", "reason": "content_without_tool_calls", "retry": 1},
    )

    assert event.kind == "agent_tool_call_recovery"
    stored = db.list_traces("task")
    assert len(stored) == 1
    assert stored[0].payload["reason"] == "content_without_tool_calls"
    assert stored[0].payload_ref is None
