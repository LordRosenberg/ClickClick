"""Explicit continuation keeps prior failures immutable; no real device/model calls."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from shared.schemas import AgentState, TaskStatus
from tests.test_task_pause import app
from tests.test_task_pause import reply
from tests.test_task_steering import runtime


def budget_failure(app, dimension="model_calls"):
    state = AgentState(instruction="continue existing work")
    state.role_invocation_count = 1
    state.step_number = 2
    state.revisable.execution_count = 1
    state.revisable.limits.model_calls = 1 if dimension == "model_calls" else 100
    state.revisable.limits.executor_decisions = 2 if dimension == "executor_decisions" else 200
    state.revisable.limits.device_actions = 1 if dimension == "device_actions" else 10
    state.revisable.limits.deadline_at = time.time() + (-1 if dimension == "task_time" else 300)
    task = app.state.db.create_task(state.instruction, state, device_serial="fixture")
    assert app.state.orchestrator._budget_fail(task.id, state, dimension) == TaskStatus.FAILED
    return app.state.db.get_task(task.id)


async def drain(app):
    await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=False)


@pytest.mark.parametrize("finish", ["success", "budget_failure", "cancelled"])
async def test_attempt_history_counters_and_retry_after_next_terminal(app, finish):
    db, orch = app.state.db, app.state.orchestrator
    task = budget_failure(app)
    original = db.get_agent_record(task.id, "execution_attempt", "1")
    starts = []

    async def run(tid):
        starts.append(tid)
        state = db.get_task(tid).state
        assert db.get_task(tid).failure_reason is None
        assert (state.step_number, state.role_invocation_count, state.revisable.execution_count) == (2, 1, 1)
        assert state.revisable.execution_attempt == 2
        state.role_invocation_count += 1
        if finish == "success":
            return orch._succeed(tid, state, "verified")
        if finish == "cancelled":
            return orch._cancel(tid, state, mode="soft")
        return orch._budget_fail(tid, state, "model_calls")

    orch.run_task = run
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        projected = (await client.get(f"/api/tasks/{task.id}")).json()
        assert projected["status"] == "failed" and projected["can_resume"]
        assert not db.busy_serials()
        assert (await client.post(f"/api/tasks/{task.id}/resume")).json()["resumed"] is False
        wrong = {"request_key": "wrong", "budget": {"device_actions": 1}}
        assert (await client.post(f"/api/tasks/{task.id}/resume", json=wrong)).json()["status"] == "failed"
        renewal = {"request_key": "continue", "budget": {"model_calls": 1}}
        responses = await asyncio.gather(*(client.post(f"/api/tasks/{task.id}/resume", json=renewal) for _ in range(2)))
        assert sum(r.json()["resumed"] for r in responses) == 1
        await drain(app)
        assert starts == [task.id]
        assert db.get_agent_record(task.id, "execution_attempt", "1") == original
        final = db.get_task(task.id)
        if finish == "success":
            assert final.failure_reason is None
        assert final.state.role_invocation_count == 2
        assert final.state.revisable.limits.model_calls == 2
        second = db.get_agent_record(task.id, "execution_attempt", "2")
        assert second["payload"]["status"] == {"success": "succeeded", "budget_failure": "failed", "cancelled": "cancelled"}[finish]
        if finish == "success":
            assert second["payload"]["failure_reason"] is None
        replay = (await client.post(f"/api/tasks/{task.id}/resume", json=renewal)).json()
        assert replay["replayed"] and not replay["resumed"]
        assert starts == [task.id]
        assert db.get_task(task.id).state.revisable.execution_attempt == 2
        assert (await client.post(f"/api/tasks/{task.id}/resume", json={
            **renewal, "budget": {"model_calls": 2},
        })).status_code == 409
        compact = (await client.get(f"/api/assistant/tasks/{task.id}")).json()
        assert [a["status"] for a in compact["execution_attempts"]] == ["failed", second["payload"]["status"]]


@pytest.mark.parametrize("reason", ["planner_inconclusive:budget cannot fit", "review_inconclusive:unknown", "plan_runtime:RuntimeError:uncertain device effect"])
async def test_ordinary_failure_keeps_state_but_never_resumes(app, reason):
    db, orch = app.state.db, app.state.orchestrator
    state = AgentState(instruction="original")
    state.role_invocation_count = 4
    task = db.create_task(state.instruction, state, device_serial="fixture")
    orch._fail(task.id, state, reason)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = (await client.post(f"/api/tasks/{task.id}/resume", json={
            "request_key": "not-budget", "budget": {"model_calls": 10},
        })).json()
        assert not response["resumed"] and response["status"] == "failed"
        assert not db.get_agent_record(task.id, "budget_extension", "not-budget")
        projection = (await client.get(f"/api/tasks/{task.id}")).json()
        assert not projection["can_resume"]
    saved = db.get_agent_record(task.id, "execution_attempt", "1")["payload"]
    assert saved["failure_reason"] == reason
    assert saved["task"]["state"]["role_invocation_count"] == 4


async def test_device_reassignment_and_cleanup_block_budget_resume(app):
    db = app.state.db
    task = budget_failure(app)
    other = db.create_task("another owner", device_serial="fixture")
    body = {"request_key": "renew", "budget": {"model_calls": 2}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/tasks/{task.id}/resume", json=body)).status_code == 409
        assert not db.get_agent_record(task.id, "budget_extension", "renew")
        db.update_task(other.id, status=TaskStatus.CANCELLED)
        gate = asyncio.Event()
        cleanup = asyncio.create_task(gate.wait())
        app.state.running_tasks[task.id] = cleanup
        try:
            assert (await client.post(f"/api/tasks/{task.id}/resume", json=body)).status_code == 409
            assert not db.get_agent_record(task.id, "budget_extension", "renew")
        finally:
            gate.set()
            await cleanup
            app.state.running_tasks.pop(task.id, None)


async def test_budget_checkpoint_survives_backend_reopen(app):
    from control_api.main import create_app
    task = budget_failure(app)
    original = app.state.db.get_agent_record(task.id, "execution_attempt", "1")
    app.state.db.close()
    replacement = create_app()
    starts = []
    async def run(tid):
        starts.append(tid)
        saved = replacement.state.db.get_task(tid)
        assert saved.state.role_invocation_count == 1
        return replacement.state.orchestrator._succeed(tid, saved.state, "done")
    replacement.state.orchestrator.run_task = run
    try:
        async with AsyncClient(transport=ASGITransport(app=replacement), base_url="http://test") as client:
            assert (await client.post(f"/api/tasks/{task.id}/resume", json={
                "request_key": "reopen", "budget": {"model_calls": 2},
            })).json()["resumed"]
            await drain(replacement)
        assert starts == [task.id]
        assert replacement.state.db.get_agent_record(task.id, "execution_attempt", "1") == original
    finally:
        replacement.state.db.close()


async def test_prediction_round_failure_cannot_extend_or_resume(app):
    task = budget_failure(app, "prediction_rounds")
    assert not task.state.revisable.checkpoint_resumable
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.post(f"/api/tasks/{task.id}/resume")).json()
        assert result["status"] == "failed" and not result["resumed"]


@pytest.mark.parametrize("dimension,increment", [
    ("executor_decisions", "executor_decisions"), ("device_actions", "device_actions"),
    ("task_time", "seconds"),
])
async def test_explicit_renewal_of_each_dimension_keeps_original_limits(app, dimension, increment):
    db, orch = app.state.db, app.state.orchestrator
    task = budget_failure(app, dimension)
    original = db.get_agent_record(task.id, "execution_attempt", "1")
    async def run(tid):
        return orch._succeed(tid, db.get_task(tid).state, "done")
    orch.run_task = run
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert not (await client.post(f"/api/tasks/{task.id}/resume")).json()["resumed"]
        assert (await client.post(f"/api/tasks/{task.id}/resume", json={
            "request_key": "renew", "budget": {increment: 30},
        })).json()["resumed"]
        await drain(app)
    final = db.get_task(task.id)
    field = "deadline_at" if increment == "seconds" else increment
    assert getattr(final.state.revisable.limits, field) == original["payload"]["limits"][field] + 30
    assert db.get_agent_record(task.id, "execution_attempt", "1") == original
    assert (final.state.step_number, final.state.role_invocation_count, final.state.revisable.execution_count) == (2, 1, 1)


async def test_invalid_budget_checkpoint_cannot_resume(app):
    db, orch = app.state.db, app.state.orchestrator
    task = db.create_task("broken continuation", device_serial="fixture")
    task.state.revisable.dialogue_refs = ["missing-dialogue"]
    orch._budget_fail(task.id, task.state, "model_calls")
    saved = db.get_task(task.id)
    assert saved.failure_reason.startswith("budget_checkpoint_invalid:")
    assert not saved.state.revisable.checkpoint_resumable
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert not (await client.post(f"/api/tasks/{task.id}/resume")).json()["resumed"]


def test_official_androidworld_budget_failure_remains_not_done():
    from evaluation.androidworld.official_trajectory import agent_session_done
    assert not agent_session_done({"status": "failed", "failure_reason": "role_invocation_limit_exhausted",
                                   "stop_reason": "budget_exhausted"})
    assert agent_session_done({"status": "failed", "failure_reason": "planner_inconclusive:original verdict"})


async def test_runtime_continuation_keeps_receipts_and_observes_again(runtime, monkeypatch):
    from shared.task_budgets import BudgetIncrement, ResumeRequest, extend_budget
    orch, db, _, driver = runtime
    state = AgentState(instruction="Go home")
    state.revisable.limits.executor_decisions = 1
    task = db.create_task(state.instruction, state, device_serial="fixture")
    resumed = False
    original_observation = None

    async def complete(model, messages, *, tools, **kwargs):
        nonlocal original_observation
        kwargs["attempt_meter"]("model_call_started", {})
        if any(t["function"]["name"] == "submit_planner_decision" for t in tools):
            if resumed:
                return reply("submit_planner_decision", {"decision": "complete", "reason": "Home visible"})
            return reply("submit_planner_decision", {"decision": "execute", "reason": "Go home",
                         "plan": {"current_stage": {"goal": "Go home"}}})
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        observation = update["current_observation_id"]
        if resumed:
            assert observation != original_observation
            assert "action_result" in json.dumps(messages)
            return reply("submit_executor_step", {"decision": "finish", "summary": "Home visible", "observation_id": observation})
        original_observation = observation
        return reply("submit_executor_step", {"decision": "act", "summary": "Go home", "observation_id": observation, "action": {"type": "home"}})

    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.FAILED
    assert len(driver.actions) == 1
    original_step = db.list_steps(task.id)[0]
    original_attempt = db.get_agent_record(task.id, "execution_attempt", "1")
    saved = db.get_task(task.id)
    assert saved.state.revisable.dialogue_refs
    assert await orch.run_task(task.id) == TaskStatus.FAILED  # no implicit continuation
    extend_budget(db, saved, ResumeRequest(request_key="explicit", budget=BudgetIncrement(executor_decisions=3)),
                  default_decisions=orch.max_steps, default_calls=orch.max_role_invocations)
    saved.state.revisable.execution_attempt += 1
    db.update_task(task.id, state=saved.state, status=TaskStatus.QUEUED, failure_reason=None)
    resumed = True
    assert await orch.run_task(task.id) == TaskStatus.SUCCEEDED
    final = db.get_task(task.id)
    assert len(driver.actions) == final.state.revisable.execution_count == 1
    assert final.state.role_invocation_count > original_attempt["payload"]["model_calls"]
    assert db.list_steps(task.id)[0] == original_step
    assert db.get_agent_record(task.id, "execution_attempt", "1") == original_attempt
    assert db.get_agent_record(task.id, "execution_attempt", "2")["payload"]["status"] == "succeeded"
