"""HTTP golden-path coverage for the focused three-role runtime."""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from shared.schemas import (
    Action,
    ExecutorStepSubmit,
    SubgoalContractBody,
    PlannerDecision,
    PlannerMode,
    ReviewerDecision,
    ReviewerVerdict,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer, fake_task_scope


class BoundPlanner(FakePlanner):
    async def decide(self, state, package, **kwargs):
        decision, refs, observation_refs = await super().decide(state, package, **kwargs)
        refs["active_package"] = package
        observation_refs["observation_id"] = package.observation_id
        return decision, refs, observation_refs


class BoundReviewer(FakeReviewer):
    async def decide(self, state, package, **kwargs):
        decision, refs, observation_refs = await super().decide(state, package, **kwargs)
        refs["active_package"] = package
        observation_refs["observation_id"] = package.observation_id
        return decision, refs, observation_refs


def execute(name: str) -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=name,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{name} complete"],
        ),
        plan=[name],
        target_requirement_ref="final_ui_state:1",
    )


def done() -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.DONE,
        reason="requested result is visible",
        evidence_handles=["current"],
        packet_digest="packet-done"
    )


async def wait_terminal(client: AsyncClient, task_id: str) -> dict:
    detail = {}
    for _ in range(100):
        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        if detail["status"] in {"succeeded", "failed", "cancelled"}:
            return detail
        await asyncio.sleep(0.05)
    raise AssertionError(f"task did not terminate: {detail}")


@pytest.mark.asyncio
async def test_golden_path_open_act_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")

    planner = BoundPlanner([execute("play the fixture video")])
    reviewer = BoundReviewer([done()], task_scope=fake_task_scope())
    executor = FakeExecutor([
        (Action(type="launch", app="com.example.demo"), False),
        (Action(type="tap", index=0), False),
        ExecutorStepSubmit(
            decision="request_review", summary="video is playing",
        ).to_step(),
    ])

    from control_api.main import create_app

    app = create_app(
        planner_factory=lambda: planner,
        reviewer_factory=lambda: reviewer,
        executor_factory=lambda: executor,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/api/health")).json()["runtime"] == (
            "reviewer-planner-executor-loop"
        )
        serial = (await client.get("/api/devices")).json()[0]["serial"]
        created = await client.post(
            "/api/tasks",
            json={
                "instruction": "打开 demo app，找到播放按钮并播放视频",
                "device_serials": [serial],
            },
        )
        assert created.status_code == 200
        task_id = created.json()["tasks"][0]["id"]
        task = await wait_terminal(client, task_id)

        assert task["status"] == "succeeded"
        assert task["step_number"] == 3
        assert len(reviewer.seen_boundary_states) == 1

        replay = (await client.get(f"/api/tasks/{task_id}/replay")).json()
        assert len(replay["steps"]) == 3
        kinds = {event["kind"] for event in replay["loop_events"]}
        assert {"planner_decision", "executor_tick", "reviewer_decision"}.issubset(kinds)

        step = replay["steps"][0]
        debug = (
            await client.get(
                f"/api/tasks/{task_id}/steps/{step['node_id']}/{step['seq']}/debug"
            )
        ).json()
        assert debug["action"] or debug["summary"]
        assert not debug.get("thought")


@pytest.mark.asyncio
async def test_max_steps_failure_is_listed_and_debuggable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")

    planner = BoundPlanner([execute("keep acting")])
    reviewer = BoundReviewer([], task_scope=fake_task_scope())
    executor = FakeExecutor([Action(type="tap", index=0)] * 4)

    from control_api.main import create_app

    app = create_app(
        planner_factory=lambda: planner,
        reviewer_factory=lambda: reviewer,
        executor_factory=lambda: executor,
    )
    app.state.orchestrator.max_steps = 2
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        serial = (await client.get("/api/devices")).json()[0]["serial"]
        response = await client.post(
            "/api/tasks",
            json={"instruction": "bounded run", "device_serials": [serial]},
        )
        task_id = response.json()["tasks"][0]["id"]
        task = await wait_terminal(client, task_id)

        assert task["status"] == "failed"
        assert task["failure_reason"] == "max_steps_exhausted"
        failed = (await client.get("/api/tasks/failed/list")).json()
        assert any(item["id"] == task_id for item in failed)
        errors = (await client.get(f"/api/tasks/{task_id}/traces?level=ERROR")).json()
        assert errors
