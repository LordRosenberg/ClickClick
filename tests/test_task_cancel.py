"""Operator force-cancel: soft boundary exit, hard asyncio cancel, API, failed-list."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from control_api.services import ObservabilityQueries
from driver.fixture import FixtureDriver
from driver.pool import FIXTURE_SERIAL
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    Action,
    AgentState,
    SubgoalContractBody,
    PlannerDecision,
    PlannerMode,
    TaskStatus,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer, fake_task_scope


class BoundPlanner(FakePlanner):
    async def decide(self, state, package, **kwargs):
        decision, refs, observation_refs = await super().decide(state, package, **kwargs)
        refs["active_package"] = package
        observation_refs["observation_id"] = package.observation_id
        return decision, refs, observation_refs


def execute() -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal="keep going",
        completion_contract=SubgoalContractBody(
            success_conditions=["finished"],
        ),
        plan=["keep going"],
        target_requirement_ref="final_ui_state:1",
    )


class _HangingDriver(FixtureDriver):
    """Blocks forever inside get_frame so hard-cancel can interrupt."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        self.entered.set()
        await asyncio.Event().wait()
        return await super().get_frame()


class _GateDriver(FixtureDriver):
    """Releases one frame at a time so soft-cancel can land between ticks."""

    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()
        self._gate.set()
        self.frames = 0

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        await self._gate.wait()
        self.frames += 1
        # After the first frame, hold until the test releases — gives soft
        # cancel a window before tick 2 observe.
        if self.frames == 1:
            self._gate.clear()
        return await super().get_frame()


def _stack(tmp_path: Path, planner: FakePlanner, executor: FakeExecutor, *, driver=None, **kw):
    db = Database(tmp_path / "cancel.db")
    arts = ArtifactStore(tmp_path / "arts")
    traces = TraceWriter(db, arts)
    orch = Orchestrator(
        db,
        traces,
        lambda: planner,
        lambda: FakeReviewer([], task_scope=fake_task_scope()),
        lambda: executor,
        driver=driver or FixtureDriver(),
        max_steps=kw.get("max_steps", 20),
        role_call_retry_n=0,
        artifacts=arts,
    )
    return db, traces, orch


@pytest.mark.asyncio
async def test_soft_cancel_at_loop_boundary(tmp_path: Path):
    planner = BoundPlanner([execute()])
    # Never signal done — loop would otherwise keep ticking.
    executor = FakeExecutor([Action(type="sleep", duration_ms=1)] * 20)
    driver = _GateDriver()
    db, traces, orch = _stack(tmp_path, planner, executor, driver=driver)

    record = db.create_task("cancel me", AgentState(instruction="cancel me"))
    run = asyncio.create_task(orch.run_task(record.id))

    # Wait until tick 1 has observed once, then request cancel before tick 2.
    for _ in range(50):
        if driver.frames >= 1:
            break
        await asyncio.sleep(0.01)
    assert driver.frames >= 1
    orch.request_cancel(record.id)
    driver.release()

    status = await asyncio.wait_for(run, timeout=2.0)
    assert status == TaskStatus.CANCELLED
    task = db.get_task(record.id)
    assert task.status == TaskStatus.CANCELLED
    assert task.failure_reason == "cancelled_by_operator"
    msgs = [t.message for t in db.list_traces(record.id)]
    assert any("task_cancelled mode=soft" in (m or "") for m in msgs)

    q = ObservabilityQueries(db, ArtifactStore(tmp_path / "arts"))
    assert all(f["id"] != record.id for f in q.failed_tasks())


@pytest.mark.asyncio
async def test_hard_cancel_while_blocked_on_observe(tmp_path: Path):
    planner = BoundPlanner([execute()])
    executor = FakeExecutor([Action(type="sleep", duration_ms=1)])
    driver = _HangingDriver()
    db, _traces, orch = _stack(tmp_path, planner, executor, driver=driver)

    record = db.create_task("hang", AgentState(instruction="hang"))
    run = asyncio.create_task(orch.run_task(record.id))
    await asyncio.wait_for(driver.entered.wait(), timeout=2.0)

    orch.request_cancel(record.id)
    run.cancel()  # simulate Control API hard-cancel after grace
    status = await asyncio.wait_for(run, timeout=2.0)
    assert status == TaskStatus.CANCELLED
    task = db.get_task(record.id)
    assert task.status == TaskStatus.CANCELLED
    msgs = [t.message for t in db.list_traces(record.id)]
    assert any("task_cancelled mode=hard" in (m or "") for m in msgs)


@pytest.mark.asyncio
async def test_api_cancel_happy_path_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")
    monkeypatch.setenv("CLICKCLICK_ENABLE_SKILL_MINER", "false")
    monkeypatch.setenv("CLICKCLICK_TASK_CANCEL_HARD_TIMEOUT_S", "0.05")

    from control_api.main import create_app

    # Slow enough that cancel wins before natural completion.
    planner = BoundPlanner([execute()])
    reviewer = FakeReviewer([], task_scope=fake_task_scope())
    executor = FakeExecutor([(Action(type="sleep", duration_ms=200), False)] * 30)

    class SlowDriver(FixtureDriver):
        async def get_frame(self) -> tuple[dict[str, Any], bytes]:
            await asyncio.sleep(0.2)
            return await super().get_frame()

    # Inject via pool by patching after app create is hard; use factories +
    # fixture pool serial. Instead, hang on executor by sleeping in Fake —
    # FakeExecutor doesn't sleep. Use SlowDriver through Orchestrator driver
    # by monkeypatching DriverPool.get after create_app.

    app = create_app(
        planner_factory=lambda: planner,
        reviewer_factory=lambda: reviewer,
        executor_factory=lambda: executor,
    )
    orch = app.state.orchestrator
    slow = SlowDriver()
    orch.driver = slow
    # Pool path: override get to return slow driver for fixture key.
    original_get = app.state.driver_pool.get

    def _get(key: str):
        if key in (FIXTURE_SERIAL, "fixture"):
            return slow
        return original_get(key)

    app.state.driver_pool.get = _get  # type: ignore[method-assign]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        devices = (await client.get("/api/devices")).json()
        key = devices[0]["key"]
        created = await client.post(
            "/api/tasks",
            json={"instruction": "long run", "device_serials": [key]},
        )
        assert created.status_code == 200
        tid = created.json()["tasks"][0]["id"]

        cancel = await client.post(f"/api/tasks/{tid}/cancel")
        assert cancel.status_code == 200
        body = cancel.json()
        assert body["task_id"] == tid
        assert body["cancel_requested"] is True

        # Wait until terminal.
        status = None
        for _ in range(80):
            detail = (await client.get(f"/api/tasks/{tid}")).json()
            status = detail["status"]
            if status == "cancelled":
                break
            await asyncio.sleep(0.05)
        assert status == "cancelled"

        # Device free.
        devices2 = (await client.get("/api/devices")).json()
        entry = next(d for d in devices2 if d["key"] == key)
        assert entry["busy"] is False

        # Idempotent.
        again = await client.post(f"/api/tasks/{tid}/cancel")
        assert again.status_code == 200
        assert again.json()["already_terminal"] is True
        assert again.json()["status"] == "cancelled"

        failed = (await client.get("/api/tasks/failed/list")).json()
        assert all(f["id"] != tid for f in failed)

        missing = await client.post("/api/tasks/no-such/cancel")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_api_cancel_orphan_running_without_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """After API restart, DB can stay ``running`` with no asyncio Task.

    Cancel MUST terminalize immediately so the Console unsticks.
    """
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")
    monkeypatch.setenv("CLICKCLICK_ENABLE_SKILL_MINER", "false")

    from control_api.main import create_app

    app = create_app(
        planner_factory=lambda: FakePlanner([]),
        reviewer_factory=lambda: FakeReviewer([], task_scope=fake_task_scope()),
        executor_factory=lambda: FakeExecutor([]),
    )
    db = app.state.db
    orphan = db.create_task(
        "stuck after restart",
        AgentState(instruction="stuck after restart"),
        device_serial=FIXTURE_SERIAL,
    )
    db.update_task(orphan.id, status=TaskStatus.RUNNING)
    assert orphan.id in {v for v in db.busy_serials().values()}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/tasks/{orphan.id}/cancel")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "cancelled"
        assert body["already_terminal"] is True

        detail = (await client.get(f"/api/tasks/{orphan.id}")).json()
        assert detail["status"] == "cancelled"

        devices = (await client.get("/api/devices")).json()
        entry = next(d for d in devices if d["serial"] == FIXTURE_SERIAL)
        assert entry["busy"] is False

        failed = (await client.get("/api/tasks/failed/list")).json()
        assert all(f["id"] != orphan.id for f in failed)

    msgs = [t.message for t in db.list_traces(orphan.id)]
    assert any("operator_cancel_requested" in (m or "") for m in msgs)
    assert any("task_cancelled mode=orphan" in (m or "") for m in msgs)
