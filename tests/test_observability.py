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
from shared.schemas import (
    Action, ActionPipeline, ActionPipelineStage, ActionResult, AgentState, CanonicalUI,
    ExecutorDecisionKind,
    ExecutorStepSubmit,
    SubgoalContractBody,
    ExecutorStep, FocusedElementEvidence, InteractionStateEvidence,
    ObservationMode, PlannerDecision, PlannerMode, ReviewerDecision,
    ReviewerAcceptedProgress, ReviewerVerdict, TaskStatus, UIElement,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer, fake_task_scope


def _plan(subgoal: str, remaining: list[str] | None = None) -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=subgoal,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{subgoal} is established"],
        ),
        plan=[subgoal, *(remaining or [])],
        target_requirement_ref="final_ui_state:1",
    )


def _accepted(digest: str = "accepted-packet") -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.ACCEPT,
        accepted_progress=[ReviewerAcceptedProgress(
            requirement_ref="final_ui_state:1",
            statement="the current subgoal is established",
            evidence_handles=["current"],
        )],
        reason="the current subgoal boundary is accepted",
        evidence_handles=["current"],
        packet_digest=digest,
    )


def _done(digest: str = "done-packet") -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.DONE,
        reason="the whole task is established",
        evidence_handles=["current"],
        packet_digest=digest
    )


def _stack(tmp_path: Path):
    db = Database(tmp_path / "obs.db")
    arts = ArtifactStore(tmp_path / "arts")
    traces = TraceWriter(db, arts)
    planner = FakePlanner([
        _plan("launch demo", ["tap play"]),
        _plan("tap play"),
    ])
    reviewer = FakeReviewer(
        [_accepted(), _done()], task_scope=fake_task_scope(),
    )
    executor = FakeExecutor(
        [
            Action(type="launch", app="com.example.demo"),
            ExecutorStepSubmit(
                decision="request_review", summary="demo app is open",
            ).to_step(),
            Action(type="tap", index=0),
            ExecutorStepSubmit(
                decision="request_review", summary="video is playing",
            ).to_step(),
        ]
    )
    orch = Orchestrator(
        db,
        traces,
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        max_steps=10,
        artifacts=arts,
    )
    return db, arts, orch, traces


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


def test_metrics_union_raw_scope_and_folded_legacy_role_rounds_once(tmp_path: Path):
    db = Database(tmp_path / "scope-metrics.db")
    arts = ArtifactStore(tmp_path / "scope-metrics-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("measure scope")

    scope_round = {
        "round_id": "scope-round-1",
        "invocation_id": "scope-invocation-1",
        "role": "reviewer",
        "phase": "task_scope",
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
        payload=scope_round,
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
        payload=scope_round,
    )
    traces.write(
        task.id, kind="task_scope", step_seq=0,
        payload={
            "status": "scope_authored",
            "scope_attempt_id": "scope-attempt-1",
            "agent_rounds": [scope_round],
            "role_elapsed_ms": 14,
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

    timeline = ObservabilityQueries(db, arts).timeline(task.id)
    metrics = timeline["metrics"]

    assert metrics["round_count"] == 2
    assert metrics["input_tokens_total"] == 300
    assert metrics["output_tokens_total"] == 30
    assert metrics["cache_read_tokens_total"] == 60
    assert metrics["by_role"]["reviewer"]["round_count"] == 1
    assert metrics["by_role"]["planner"]["round_count"] == 1
    assert metrics["by_phase"]["reviewer_scope"]["round_count"] == 1
    assert metrics["by_phase"]["reviewer_scope"]["input_tokens_total"] == 100
    assert metrics["scope_call_count"] == 1
    assert [call["phase"] for call in timeline["calls"]] == ["scope", "planning"]
    assert [call["elapsed_ms"] for call in timeline["calls"]] == [14, 27]
    assert timeline["calls"][0]["metrics"]["input_tokens_total"] == 100
    assert timeline["calls"][0]["llm_elapsed_ms"] == 10
    assert metrics["role_calls"] == {"planner": 1, "reviewer": 1, "executor": 0}
    assert metrics["provider_requests"] == {"planner": 1, "reviewer": 2, "executor": 0}
    assert all("harness_events" not in call for call in timeline["calls"])


def test_invalid_submit_metrics_key_call_ids_by_invocation(tmp_path: Path):
    db = Database(tmp_path / "invalid-invocations.db")
    arts = ArtifactStore(tmp_path / "invalid-invocations-arts")
    traces = TraceWriter(db, arts)
    task = db.create_task("same provider call id in separate invocations")

    common = {
        "call_id": "call_1",
        "role": "reviewer",
        "category": "terminal",
        "status": "invalid_arguments",
    }
    traces.write(
        task.id,
        kind="agent_tool_failed",
        step_seq=0,
        payload={
            **common,
            "phase": "task_scope",
            "invocation_id": "scope-invocation",
        },
    )
    traces.write(
        task.id,
        kind="agent_tool_failed",
        step_seq=0,
        payload={
            **common,
            "invocation_id": "reviewer-invocation",
        },
    )

    metrics = ObservabilityQueries(db, arts).timeline(task.id)["metrics"]

    assert metrics["invalid_submits"] == {"planner": 0, "reviewer": 2, "executor": 0}


@pytest.mark.asyncio
async def test_closed_loop_event_kinds_persisted(tmp_path: Path):
    db, arts, orch, _ = _stack(tmp_path)
    tid = await orch.start_task("打开 demo 并播放视频")
    task = db.get_task(tid)
    assert task.status == TaskStatus.SUCCEEDED

    events = db.list_traces(tid)
    kinds = {e.kind for e in events}
    assert "task_scope" in kinds
    assert "planner_decision" in kinds
    assert "reviewer_decision" in kinds
    assert "executor_tick" in kinds
    assert "loop_tick" in kinds

    planner_events = [
        e for e in events
        if e.kind == "planner_decision"
    ]
    assert planner_events
    scope = [event for event in events if event.kind == "task_scope"]
    assert len(scope) == 1
    focused = [
        event for event in events
        if event.kind in {
            "task_scope", "planner_decision", "reviewer_decision", "executor_tick",
        }
    ]
    assert all(isinstance(event.payload.get("role_elapsed_ms"), int) for event in focused)
    timeline = ObservabilityQueries(db, arts).timeline(tid)
    assert timeline["task_scope"]["kind"] == "task_scope"
    assert timeline["task_scope"]["phase"] == "task_scope"
    assert timeline["calls"][0]["phase"] == "scope"
    assert timeline["calls"][0]["reviewer"]["kind"] == "task_scope"
    assert timeline["calls"][0]["observation"] is None
    assert any(step["planner"] for step in timeline["steps"])
    assert any(step["reviewer"] for step in timeline["steps"])
    replay = ObservabilityQueries(db, arts).replay(tid)
    assert [
        event["kind"] for event in replay["loop_events"]
        if event["kind"] == "task_scope"
    ] == ["task_scope"]
    assert "next_subgoal" in planner_events[0].payload["decision"]
    assert "mode" in planner_events[0].payload["decision"]

    # Executor ticks carry exact action/observation bindings without a Harness
    # screen-meaning classifier.
    et = [e for e in events if e.kind == "executor_tick"]
    assert et
    assert "action" in et[0].payload
    assert "window_phase" not in et[0].payload
    assert "phase_change" not in et[0].payload
    assert "minor_change" not in et[0].payload
    assert "action_result" in et[0].payload
    assert "som_ref" in et[0].payload
    assert "tree_ref" in et[0].payload


@pytest.mark.asyncio
async def test_replay_reconstructs_loop(tmp_path: Path):
    db, arts, orch, _ = _stack(tmp_path)
    tid = await orch.start_task("打开 demo 并播放视频")
    q = ObservabilityQueries(db, arts)
    payload = q.replay(tid)
    assert payload["status"] == "succeeded"
    assert payload["step_number"] == 4
    assert payload["steps"]
    # loop_events reconstruct the focused-role sequence.
    loop_events = payload["loop_events"]
    kinds = {e["kind"] for e in loop_events}
    assert {"planner_decision", "reviewer_decision", "executor_tick", "loop_tick"}.issubset(kinds)


@pytest.mark.asyncio
async def test_step_debug_payload(tmp_path: Path):
    db, arts, orch, _ = _stack(tmp_path)
    tid = await orch.start_task("打开 demo 并播放视频")
    q = ObservabilityQueries(db, arts)
    steps = db.list_steps(tid)
    assert steps
    s0 = steps[0]
    dbg = q.step_debug(tid, s0["node_id"], s0["seq"])
    assert dbg["action"]
    assert dbg.get("action") is not None or dbg.get("act")


@pytest.mark.asyncio
async def test_level_filter(tmp_path: Path):
    db, arts, orch, _ = _stack(tmp_path)
    tid = await orch.start_task("打开 demo 并播放视频")
    from shared.schemas import LogLevel

    infos = db.list_traces(tid, LogLevel.INFO)
    assert infos
    for e in infos:
        assert e.level == LogLevel.INFO


@pytest.mark.asyncio
async def test_failed_task_guardrail_in_traces(tmp_path: Path):
    db = Database(tmp_path / "fail.db")
    arts = ArtifactStore(tmp_path / "arts")
    traces = TraceWriter(db, arts)
    planner = FakePlanner([_plan("go")])
    reviewer = FakeReviewer([], task_scope=fake_task_scope())
    executor = FakeExecutor([Action(type="tap", index=0)] * 50)
    orch = Orchestrator(
        db, traces, lambda: planner, lambda: reviewer, lambda: executor,
        max_steps=3, artifacts=arts,
    )
    tid = await orch.start_task("loop forever")
    task = db.get_task(tid)
    assert task.status == TaskStatus.FAILED
    assert task.failure_reason == "max_steps_exhausted"
    q = ObservabilityQueries(db, arts)
    failed = q.failed_tasks()
    assert any(f["id"] == tid for f in failed)
    errors = q.traces(tid, "ERROR")
    assert errors


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


def _real_stack(tmp_path: Path):
    """A stack with a FixtureDriver + real ObservationBuilder + artifacts."""
    db = Database(tmp_path / "rec.db")
    arts = ArtifactStore(tmp_path / "arts")
    traces = TraceWriter(db, arts)
    driver = FixtureDriver()
    planner = FakePlanner([_plan("tap play")])
    reviewer = FakeReviewer([_done()], task_scope=fake_task_scope())
    executor = FakeExecutor([
        Action(type="tap", index=0),
        ExecutorStepSubmit(
            decision="request_review", summary="video playing",
        ).to_step(),
    ])
    orch = Orchestrator(
        db, traces, lambda: planner, lambda: reviewer, lambda: executor, driver,
        max_steps=10, artifacts=arts,
    )
    return db, arts, orch


@pytest.mark.asyncio
async def test_som_and_tree_artifacts_persisted_per_tick(tmp_path: Path):
    """2.4 — SoM PNG and tree JSON are persisted; StepReport refs are non-null."""
    db, arts, orch = _real_stack(tmp_path)
    tid = await orch.start_task("tap play")

    som_files = list((arts.root / "som").glob("*.png"))
    tree_files = list((arts.root / "trees").glob("*.json"))
    assert som_files, "no SoM PNG persisted"
    assert tree_files, "no tree JSON persisted"

    steps = db.list_steps(tid)
    assert steps
    # At least one step should carry non-null annotated_ref and tree_ref.
    with_som = [s for s in steps if s.get("annotated_ref")]
    with_tree = [s for s in steps if s.get("tree_ref")]
    assert with_som, "no step has annotated_ref"
    assert with_tree, "no step has tree_ref"
    # The refs resolve to real files.
    assert arts.resolve(with_som[0]["annotated_ref"]).exists()
    assert arts.resolve(with_tree[0]["tree_ref"]).exists()


@pytest.mark.asyncio
async def test_executor_tick_is_single_event(tmp_path: Path):
    """3.4 — one executor tick emits exactly one `executor_tick` trace, no
    legacy `executor_step` or per-tick `action`."""
    db, arts, orch = _real_stack(tmp_path)
    tid = await orch.start_task("tap play")
    events = db.list_traces(tid)
    kinds = {e.kind for e in events}
    assert "executor_tick" in kinds
    assert "executor_step" not in kinds
    assert "action" not in kinds
    # Each executor tick appears exactly once per step_seq it covers.
    et = [e for e in events if e.kind == "executor_tick"]
    seqs = [e.step_seq for e in et]
    assert len(seqs) == len(set(seqs)), "duplicate executor_tick per step"


@pytest.mark.asyncio
async def test_timeline_returns_step_grouped_steps(tmp_path: Path):
    """Timeline keeps folded steps and exact focused-role calls."""
    db, arts, orch = _real_stack(tmp_path)
    tid = await orch.start_task("step grouped")
    q = ObservabilityQueries(db, arts)
    payload = q.timeline(tid)
    assert payload["task_id"] == tid
    assert payload["instruction"] == "step grouped"
    assert payload["steps"], "timeline must return at least one step"
    assert "progress" in payload
    assert "planning_memory" not in payload

    seqs = [s["step_seq"] for s in payload["steps"] if s["step_seq"] is not None]
    assert len(seqs) == len(set(seqs)), "step_seq must be unique across steps"
    call_keys = [c["call_key"] for c in payload["calls"]]
    assert len(call_keys) == len(set(call_keys)), "call_key must be unique"
    for c in payload["calls"]:
        assert c["role"] in ("reviewer", "planner", "executor")
        assert "observation" in c
        assert c[c["role"]] is not None
        assert all(c.get(other) is None for other in {"reviewer", "planner", "executor"} - {c["role"]})

    for s in payload["steps"]:
        assert set(s.keys()) >= {
            "step_seq", "observation", "reviewer", "planner", "executor", "loop",
        }, (
            f"step missing required keys: {sorted(s.keys())}"
        )
        if s["loop"] is not None:
            assert s["loop"]["step_seq"] == s["step_seq"]
    assert payload["metrics"]["runtime_terminal_status"]["semantic_true_success"] == "not_assessed"


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
    assert metrics["role_calls"] == {"planner": 1, "reviewer": 0, "executor": 1}
    assert metrics["role_rounds"] == {"planner": 2, "reviewer": 0, "executor": 2}
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
    assert metrics["invalid_submits"] == {"planner": 1, "reviewer": 0, "executor": 1}
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


@pytest.mark.parametrize("action_type", ["type", "replace_text"])
def _executor_pipeline_trace_redacts_password_submitted_and_dispatched(
    tmp_path: Path, action_type: str,
):
    from perception.observation import ObservationPackage
    db = Database(tmp_path / "redact.db")
    arts = ArtifactStore(tmp_path / "redact-arts")
    traces = TraceWriter(db, arts)
    orch = Orchestrator(
        db, traces,
        lambda: FakePlanner([]),
        lambda: FakeReviewer([], task_scope=fake_task_scope()),
        lambda: FakeExecutor([]),
        artifacts=arts,
    )
    secret = Action(type=action_type, text="super-secret")
    step = ExecutorStep(
        action=secret,
        action_pipeline=ActionPipeline(stages=[
            ActionPipelineStage(stage="submitted", action=secret,
                                coordinate_space="none", reason="model_submitted"),
            ActionPipelineStage(stage="dispatched", action=secret,
                                coordinate_space="device", reason="driver_dispatched"),
        ]),
    )
    ui = CanonicalUI(elements=[UIElement(
        index=7, role="EditText", bounds=[0, 0, 100, 40],
        states={"password": True},
    )])
    package = ObservationPackage(
        ui=ui, mode=ObservationMode.TREE_ONLY, text_for_llm="tree",
        image_for_llm=None, annotated_png=None, gap_reasons=[],
        interaction_state=InteractionStateEvidence(
            focused_element=FocusedElementEvidence(index=7, password=True),
            focused_editable=FocusedElementEvidence(index=7, password=True),
        ),
    )
    orch._trace_executor_step(
        "task", AgentState(instruction="redact", step_number=0), step,
        ActionResult(success=True, message="ok"),
        ui, ObservationMode.TREE_ONLY, {}, package,
    )
    payload = db.list_traces("task")[0].payload
    assert payload["action"]["text"] is None
    assert all(s["action"].get("text") is None for s in payload["action_pipeline"]["stages"])
    assert "super-secret" not in str(payload)


def _delivered_interaction_envelope_has_role_trace_parity_and_replays_safely(
    tmp_path: Path,
):
    from perception.observation import ObservationPackage

    envelope = {
        "focused_element": {
            "index": 7,
            "role": "EditText",
            "editable": True,
            "password": True,
            "value_state": "redacted",
        },
        "focused_editable": {
            "index": 7,
            "role": "EditText",
            "editable": True,
            "password": True,
            "value_state": "redacted",
        },
        "keyboard": None,
    }
    db = Database(tmp_path / "interaction-envelope.db")
    arts = ArtifactStore(tmp_path / "interaction-envelope-arts")
    traces = TraceWriter(db, arts)
    orch = Orchestrator(
        db, traces,
        lambda: FakePlanner([]),
        lambda: FakeReviewer([], task_scope=fake_task_scope()),
        lambda: FakeExecutor([]),
        artifacts=arts,
    )
    task = db.create_task("role-symmetric interaction evidence")
    orch._trace_decision(
        task.id,
        AgentState(instruction=task.instruction, step_number=1),
        "planner_decision",
        _plan("edit"),
        {},
        observation_refs={"interaction_state": envelope},
    )
    package = ObservationPackage(
        ui=CanonicalUI(elements=[]),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="tree",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        interaction_state=InteractionStateEvidence(
            focused_element=FocusedElementEvidence(
                index=7,
                text="must-not-appear",
                password=True,
                value_available=True,
            ),
            focused_editable=FocusedElementEvidence(
                index=7,
                text="must-not-appear",
                password=True,
                value_available=True,
            ),
        ),
    )
    orch._trace_executor_step(
        task.id,
        AgentState(instruction=task.instruction, step_number=1),
        ExecutorStep(action=Action(type="tap", index=1)),
        ActionResult(success=True, message="ok"),
        package.ui,
        ObservationMode.TREE_ONLY,
        {"interaction_state": envelope},
        package,
    )

    events = db.list_traces(task.id)
    planner_payload = next(e.payload for e in events if e.kind == "planner_decision")
    executor_payload = next(e.payload for e in events if e.kind == "executor_tick")
    assert planner_payload["interaction_state"] == envelope
    assert executor_payload["interaction_state"] == envelope
    assert "must-not-appear" not in str(planner_payload)
    assert "must-not-appear" not in str(executor_payload)

    replay = ObservabilityQueries(db, arts).replay(task.id)
    replay_payloads = [
        event["payload"] for event in replay["loop_events"]
        if event["kind"] in {"planner_decision", "executor_tick"}
    ]
    assert [payload["interaction_state"] for payload in replay_payloads] == [
        envelope,
        envelope,
    ]


# --- SSE live streaming (observation-console) -------------------------------


def _sse_stack(tmp_path: Path, *, db_name: str = "sse.db"):
    """Stack with an EventBus wired into TraceWriter for SSE tests.

    Pass ``db_name="clickclick.db"`` (and set CLICKCLICK_DATA_DIR=tmp_path) so
    a ``create_app`` built over the same data dir sees the same task rows.
    """
    db = Database(tmp_path / db_name)
    arts = ArtifactStore(tmp_path / "arts")
    bus = EventBus()
    traces = TraceWriter(db, arts, bus=bus)
    planner = FakePlanner([_plan("tap play")])
    reviewer = FakeReviewer([_done()], task_scope=fake_task_scope())
    executor = FakeExecutor([(Action(type="tap", index=0), True)])
    orch = Orchestrator(
        db, traces, lambda: planner, lambda: reviewer, lambda: executor,
        max_steps=10, artifacts=arts,
    )
    return db, arts, bus, orch


@pytest.mark.asyncio
async def test_sse_bus_publish_delivers_to_subscriber(tmp_path: Path):
    """EventBus.publish fans an event out to every subscriber queue."""
    db, arts, bus, orch = _sse_stack(tmp_path)
    tid = "bus-test"
    queue, unsubscribe = bus.subscribe(tid)
    bus.publish(tid, {"kind": "system", "message": "hello"})
    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert ev["message"] == "hello"
    assert bus.subscriber_count(tid) == 1
    unsubscribe()
    assert bus.subscriber_count(tid) == 0


@pytest.mark.asyncio
async def test_sse_tracewriter_publishes_to_bus(tmp_path: Path):
    """TraceWriter.write publishes each event to the bus for live subscribers."""
    db, arts, bus, _ = _sse_stack(tmp_path)
    tid = "tw-test"
    queue, unsubscribe = bus.subscribe(tid)
    traces = TraceWriter(db, arts, bus=bus)
    traces.write(tid, kind="loop_tick", message="tick 1")
    ev = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert ev["kind"] == "loop_tick"
    assert ev["message"] == "tick 1"
    # No bus → no publish, no exception (test compat).
    plain = TraceWriter(db, arts)
    plain.write(tid, kind="system", message="no bus")
    unsubscribe()


@pytest.mark.asyncio
async def test_sse_http_stream_replays_then_closes_for_terminal_task(tmp_path: Path):
    """GET /api/tasks/{id}/stream replays persisted ticks then closes for a
    finished task (so the client can finalize without an infinite stream)."""
    import json as _json
    import os

    from httpx import ASGITransport, AsyncClient

    from control_api.main import create_app

    db, arts, bus, orch = _sse_stack(tmp_path, db_name="clickclick.db")
    tid = await orch.start_task("tap play")
    # Task is terminal (succeeded) at this point.

    os.environ["CLICKCLICK_DATA_DIR"] = str(tmp_path)
    os.environ["CLICKCLICK_ENABLE_SKILL_MINER"] = "false"
    os.environ["CLICKCLICK_USE_FIXTURE_DRIVER"] = "true"
    try:
        app = create_app(
            planner_factory=lambda: FakePlanner([_plan("wait")]),
            reviewer_factory=lambda: FakeReviewer([_done()], task_scope=fake_task_scope()),
            executor_factory=lambda: FakeExecutor([Action(type="sleep", duration_ms=1)]),
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # The terminal-task stream closes after replay, so aread() returns.
            resp = await client.get(f"/api/tasks/{tid}/stream")
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            seen_kinds: set[str] = set()
            for block in resp.text.split("\n\n"):
                for line in block.splitlines():
                    if line.startswith("data: "):
                        ev = _json.loads(line[len("data: "):])
                        seen_kinds.add(ev.get("kind", ""))
            assert {"planner_decision", "reviewer_decision", "executor_tick", "loop_tick"}.issubset(seen_kinds), (
                f"replay should include all tick kinds, got {seen_kinds}"
            )
    finally:
        del os.environ["CLICKCLICK_DATA_DIR"]
        del os.environ["CLICKCLICK_ENABLE_SKILL_MINER"]
        del os.environ["CLICKCLICK_USE_FIXTURE_DRIVER"]


@pytest.mark.asyncio
async def test_sse_disconnect_cleans_up_queue(tmp_path: Path):
    """Unsubscribing removes the queue so no resources leak."""
    db, arts, bus, orch = _sse_stack(tmp_path)
    tid = await orch.start_task("tap play")
    _, unsubscribe = bus.subscribe(tid)
    assert bus.subscriber_count(tid) == 1
    unsubscribe()
    assert bus.subscriber_count(tid) == 0
    # Publishing after cleanup is a no-op (no subscribers).
    bus.publish(tid, {"kind": "system", "message": "no one listening"})


@pytest.mark.asyncio
async def test_sse_subscriber_cap_returns_429(tmp_path: Path):
    """Over the per-task subscriber cap, the stream endpoint returns 429."""
    import os

    from httpx import ASGITransport, AsyncClient

    from control_api.main import create_app

    db, arts, bus, orch = _sse_stack(tmp_path, db_name="clickclick.db")
    tid = await orch.start_task("tap play")

    os.environ["CLICKCLICK_DATA_DIR"] = str(tmp_path)
    os.environ["CLICKCLICK_ENABLE_SKILL_MINER"] = "false"
    os.environ["CLICKCLICK_USE_FIXTURE_DRIVER"] = "true"
    import control_api.sse as sse_mod

    original = sse_mod.MAX_SUBSCRIBERS_PER_TASK
    sse_mod.MAX_SUBSCRIBERS_PER_TASK = 2
    try:
        app = create_app(
            planner_factory=lambda: FakePlanner([_plan("wait")]),
            reviewer_factory=lambda: FakeReviewer([_done()], task_scope=fake_task_scope()),
            executor_factory=lambda: FakeExecutor([Action(type="sleep", duration_ms=1)]),
        )
        # Occupy both subscriber slots on the app's own bus so the next
        # subscribe inside the endpoint is rejected with 429.
        _, u1 = app.state.bus.subscribe(tid)
        _, u2 = app.state.bus.subscribe(tid)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/tasks/{tid}/stream")
            assert resp.status_code == 429
        u1()
        u2()
    finally:
        sse_mod.MAX_SUBSCRIBERS_PER_TASK = original
        del os.environ["CLICKCLICK_DATA_DIR"]
        del os.environ["CLICKCLICK_ENABLE_SKILL_MINER"]
        del os.environ["CLICKCLICK_USE_FIXTURE_DRIVER"]


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


# ---------------------------------------------------------------------------
# Planner LLM input/output refs in trace + timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_decision_trace_carries_llm_refs(tmp_path: Path):

    class _RefPlanner(FakePlanner):
        async def decide(self, *a, **k):  # type: ignore[override]
            d, refs, obs = await super().decide(*a, **k)
            # Override the refs with concrete values to verify they flow through.
            return d, {
                **refs,
                "llm_input_ref": "llm/in-abc.json",
                "llm_output_ref": "llm/out-def.json",
            }, obs

    planner = _RefPlanner([_plan("inspect result")])
    reviewer = FakeReviewer([_done()], task_scope=fake_task_scope())
    executor = FakeExecutor([
        ExecutorStepSubmit(
            decision="request_review", summary="inspect result is complete",
        ).to_step(),
    ])
    db = Database(tmp_path / "mref.db")
    arts = ArtifactStore(tmp_path / "marts")
    traces = TraceWriter(db, arts)
    orch = Orchestrator(
        db, traces, lambda: planner, lambda: reviewer, lambda: executor,
        max_steps=10, artifacts=arts,
    )
    tid = await orch.start_task("planner refs")
    md = [
        e for e in db.list_traces(tid)
        if e.kind == "planner_decision"
    ]
    assert md
    assert md[0].payload.get("llm_input_ref") == "llm/in-abc.json"
    assert md[0].payload.get("llm_output_ref") == "llm/out-def.json"

    # The timeline API surfaces them via **e.payload spread.
    q = ObservabilityQueries(db, arts)
    tl = q.timeline(tid)
    # New shape: steps[] (not legacy flat ticks[]).
    planner_steps = [s for s in tl["steps"] if s["planner"] is not None]
    assert planner_steps, "planner_decision should appear inside some step"
    assert planner_steps[0]["planner"]["llm_input_ref"] == "llm/in-abc.json"
    assert planner_steps[0]["planner"]["llm_output_ref"] == "llm/out-def.json"


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
