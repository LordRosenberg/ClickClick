"""User-update races and global budget stops with simulated providers/devices."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from agent.runtime import create_orchestrator
from agent.revisable.store import TaskStore
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.hitl import HitlConfig, HumanInputBroker, InputConflict
from shared.schemas import AgentState, TaskStatus
from shared.steering import SteeringInbox, SteeringRequest
from shared.task_budgets import BudgetIncrement, ResumeRequest, extend_budget
from shared.task_control import InvocationLease, TaskSteered, begin_dispatch, current_lease
from tests.test_task_pause import app, reply  # shared fixture backend, no device/model calls


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="test", jev_mode="off")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    orch = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                               artifacts=artifacts, driver=driver)
    yield orch, db, artifacts, driver
    db.close()


def test_adjustments_survive_reopen_and_keep_original(runtime):
    _, db, artifacts, _ = runtime
    task = db.create_task("original")
    inbox = SteeringInbox(db, task.id)
    request = SteeringRequest(instruction="new requirement", request_key="same/@key")
    assert inbox.accept(request) == inbox.accept(request)
    with pytest.raises(InputConflict):
        inbox.accept(request.model_copy(update={"instruction": "different"}))
    assert len(inbox.records()) == 1
    assert db.get_task(task.id).instruction == "original"
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    state = db.get_task(task.id).state
    store = TaskStore(db, artifacts, task.id)
    assert store.context(state, "obs")["user_adjustments"][0]["instruction"] == "new requirement"
    state.revisable.summary = "old summary"
    state.revisable.dialogue_refs = []
    assert store.context(state, "obs")["instruction_revision"] == 1
    db.close()
    reopened = Database(runtime[0].settings.db_path)
    try:
        assert SteeringInbox(reopened, task.id).projection()[0]["instruction"] == "new requirement"
        assert reopened.get_task(task.id).instruction == "original"
    finally:
        reopened.close()


def test_disabled_context_does_not_gain_control_fields(runtime):
    _, db, artifacts, _ = runtime
    task = db.create_task("original")
    context = TaskStore(db, artifacts, task.id).context(task.state, "obs")
    assert "user_adjustments" not in context and "instruction_revision" not in context


async def test_disabled_runtime_does_not_start_steering_waiter(runtime, monkeypatch):
    orch, db, _, _ = runtime
    task = db.create_task("original")
    def forbidden():
        raise AssertionError("Disabled task started an intervention waiter")
    monkeypatch.setattr("agent.revisable.orchestrator.SteeringControl", forbidden)
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        assert "user_adjustments" not in json.dumps(messages)
        return reply("submit_planner_decision", {"decision": "complete", "reason": "done"})
    monkeypatch.setattr("agent.session.complete", complete)
    assert await orch.run_task(task.id) == TaskStatus.SUCCEEDED
    assert db.get_task(task.id).state.role_invocation_count == 1
    assert not orch.steering_controls


def test_revoked_lease_prevents_dispatch_and_rolls_back_transaction(runtime):
    _, db, artifacts, _ = runtime
    task = db.create_task("original")
    token = current_lease.set(InvocationLease(1, 2, revoked=True))
    try:
        with pytest.raises(TaskSteered):
            begin_dispatch()
        with pytest.raises(TaskSteered), db.transaction():
            TaskStore(db, artifacts, task.id).put("note", "stale", {"text": "obsolete"})
        assert db._transaction_depth == 0
        assert db.get_agent_record(task.id, "note", "stale") is None
    finally:
        current_lease.reset(token)


def test_discarded_dialogue_is_not_recalled_and_adjustment_order_is_stable(runtime):
    _, db, artifacts, _ = runtime
    task = db.create_task("original")
    store = TaskStore(db, artifacts, task.id)
    lease = InvocationLease(1, 1)
    token = current_lease.set(lease)
    try:
        store.put("dialogue", "obsolete", {"step": 0, "ref": "diagnostic-only"})
    finally:
        current_lease.reset(token)
    db.append_agent_record(task.id, "obsolete_invocation", lease.identity, {})
    assert not store.records("dialogue")
    with pytest.raises(ValueError):
        store.get("dialogue", "obsolete")
    assert db.get_agent_record(task.id, "dialogue", "obsolete")  # diagnostic remains
    inbox = SteeringInbox(db, task.id)
    inbox.accept(SteeringRequest(instruction="first", request_key="1"))
    inbox.accept(SteeringRequest(instruction="second", request_key="2"))
    inbox.applied(1, "first applied later")
    assert [r["payload"]["revision"] for r in inbox.records()] == [1, 2]


@pytest.mark.parametrize("dimension", ["executor_decisions", "model_calls", "device_actions", "task_time"])
async def test_budget_failure_retains_resumable_checkpoint(runtime, monkeypatch, dimension):
    orch, db, _, driver = runtime
    state = AgentState(instruction="Go home")
    if dimension == "executor_decisions":
        state.revisable.limits.executor_decisions = 1
        state.step_number = 1
    elif dimension == "model_calls":
        state.revisable.limits.model_calls = 1
        state.role_invocation_count = 1
    elif dimension == "device_actions":
        state.revisable.limits.device_actions = 0
        state.revisable.budget_kind = "device_actions"
    else:
        state.revisable.limits.deadline_at = time.time() - 1
    task = db.create_task(state.instruction, state)
    assert await orch.run_task(task.id) == TaskStatus.FAILED
    saved = db.get_task(task.id)
    assert saved.failure_reason
    assert saved.state.revisable.stop_reason == "budget_exhausted"
    assert saved.state.revisable.budget_kind == dimension
    assert saved.state.revisable.checkpoint_version == 1
    assert saved.state.revisable.checkpoint_resumable
    assert db.get_agent_record(task.id, "execution_attempt", "1")["payload"]["status"] == "failed"
    assert not driver.actions


def test_extensions_are_cumulative_and_idempotent(runtime):
    _, db, _, _ = runtime
    state = AgentState(instruction="goal")
    state.revisable.limits.model_calls = 10
    state.role_invocation_count = 10
    task = db.create_task(state.instruction, state)
    body = ResumeRequest(request_key="renew", budget=BudgetIncrement(model_calls=20))
    assert extend_budget(db, task, body, default_decisions=200, default_calls=1000)
    assert not extend_budget(db, task, body, default_decisions=200, default_calls=1000)
    assert task.state.revisable.limits.model_calls == 30
    assert task.state.role_invocation_count == 10
    with pytest.raises(ValueError, match="conflict"):
        extend_budget(db, task, ResumeRequest(request_key="renew", budget=BudgetIncrement(model_calls=1)),
                      default_decisions=200, default_calls=1000)


async def test_inference_is_preempted_before_late_action(runtime, monkeypatch):
    orch, db, artifacts, driver = runtime
    task = db.create_task("Go home")
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    old_waiting, planner_updated, release_old = asyncio.Event(), asyncio.Event(), asyncio.Event()
    old_returned = asyncio.Event()

    async def complete(model, messages, *, tools, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        names = [t["function"]["name"] for t in tools]
        if "submit_planner_decision" in names:
            if SteeringInbox(db, task.id).pending():
                planner_updated.set()
                return reply("submit_planner_decision", {"decision": "complete", "reason": "User now wants no device action"})
            return reply("submit_planner_decision", {"decision": "execute", "reason": "Go home",
                         "plan": {"current_stage": {"goal": "Go home"}}})
        old_waiting.set()
        try:
            await release_old.wait()
        except asyncio.CancelledError:
            await release_old.wait()  # provider deliberately ignores cancellation
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        old_returned.set()
        return reply("submit_executor_step", {"decision": "act", "summary": "obsolete home",
                     "observation_id": update["current_observation_id"], "action": {"type": "home"}})

    monkeypatch.setattr("agent.session.complete", complete)
    runner = asyncio.create_task(orch.run_task(task.id))
    await asyncio.wait_for(old_waiting.wait(), 10)
    SteeringInbox(db, task.id).accept(SteeringRequest(instruction="Do not act", request_key="change"))
    orch.steering_controls[task.id].notify()
    await asyncio.wait_for(planner_updated.wait(), 5)
    assert not release_old.is_set()  # new Planner ran before old provider returned
    assert await asyncio.wait_for(runner, 5) == TaskStatus.SUCCEEDED
    release_old.set()
    await asyncio.wait_for(old_returned.wait(), 5)
    await asyncio.sleep(0)
    assert not driver.actions
    assert not db.list_agent_records(task.id, "dialogue")
    assert db.get_task(task.id).state.role_invocation_count == 3


async def test_disabled_api_is_noop_and_paused_update_stays_paused(app):
    db = app.state.db
    task = db.create_task("original", device_serial="fixture")
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=False))
    app.state.settings.hitl_enabled = True  # Global changes cannot switch this task.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"instruction": "change", "request_key": "key"}
        result = (await client.post(f"/api/assistant/tasks/{task.id}/steer", json=body)).json()
        assert result["status"] == "disabled" and "Console" in result["message"]
        assert result["settings_url"].endswith("/setup")
        assert not SteeringInbox(db, task.id).records()
        HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
        # configure is immutable, so create an explicitly enabled second task.
        other = db.create_task("enabled", device_serial="fixture")
        HumanInputBroker(db).configure(other.id, HitlConfig(enabled=True))
        db.update_task(other.id, status=TaskStatus.PAUSED)
        result = await client.post(f"/api/assistant/tasks/{other.id}/steer", json=body)
        assert result.status_code == 200
        assert db.get_task(other.id).status == TaskStatus.PAUSED
        assert len(SteeringInbox(db, other.id).pending()) == 1


async def test_update_during_dispatched_action_preserves_receipt(runtime, monkeypatch):
    orch, db, _, driver = runtime
    task = db.create_task("Go home")
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    dispatched, release, replanned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_act = driver.act

    async def act(action):
        dispatched.set()
        await release.wait()
        return await original_act(action)

    driver.act = act

    async def complete(model, messages, *, tools, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        if any(t["function"]["name"] == "submit_planner_decision" for t in tools):
            if SteeringInbox(db, task.id).pending():
                replanned.set()
                assert db.latest_agent_record(task.id, "event")
                return reply("submit_planner_decision", {"decision": "complete", "reason": "Keep actual effect"})
            return reply("submit_planner_decision", {"decision": "execute", "reason": "Go home",
                         "plan": {"current_stage": {"goal": "Go home"}}})
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        return reply("submit_executor_step", {"decision": "act", "summary": "home",
                     "observation_id": update["current_observation_id"], "action": {"type": "home"}})

    monkeypatch.setattr("agent.session.complete", complete)
    runner = asyncio.create_task(orch.run_task(task.id))
    await asyncio.wait_for(dispatched.wait(), 10)
    SteeringInbox(db, task.id).accept(SteeringRequest(instruction="Keep the current result", request_key="change"))
    orch.steering_controls[task.id].notify()
    await asyncio.sleep(0)
    assert not replanned.is_set()
    release.set()
    assert await asyncio.wait_for(runner, 10) == TaskStatus.SUCCEEDED
    assert len(driver.actions) == 1
    assert db.get_task(task.id).state.revisable.execution_count == 1
    assert db.list_agent_records(task.id, "dialogue")


async def test_planner_continue_keeps_stage_identity_and_context(runtime, monkeypatch):
    from shared.revisable import Plan, Stage
    orch, db, _, driver = runtime
    state = AgentState(instruction="Go home")
    state.revisable.plan = Plan(current_stage=Stage(goal="Go home"))
    state.revisable.revision = 4
    task = db.create_task(state.instruction, state)
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    SteeringInbox(db, task.id).accept(SteeringRequest(instruction="Report it in Chinese", request_key="change"))

    async def complete(model, messages, *, tools, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        if any(t["function"]["name"] == "submit_planner_decision" for t in tools):
            decision = "continue" if SteeringInbox(db, task.id).pending() else "complete"
            return reply("submit_planner_decision", {"decision": decision, "reason": "Chinese response required"})
        serialized = json.dumps(messages, ensure_ascii=False)
        assert "Report it in Chinese" in serialized and "Original instruction:\\nGo home" in serialized
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        return reply("submit_executor_step", {"decision": "finish", "summary": "done",
                     "observation_id": update["current_observation_id"]})

    monkeypatch.setattr("agent.session.complete", complete)
    assert await asyncio.wait_for(orch.run_task(task.id), 10) == TaskStatus.SUCCEEDED
    assert db.get_task(task.id).state.revisable.revision == 4
    assert not driver.actions


async def test_terminal_followup_and_budget_resume_retries(app):
    db = app.state.db
    async def idle(tid):
        return TaskStatus.QUEUED
    app.state.orchestrator.run_task = idle
    parent = db.create_task("original", device_serial="fixture")
    HumanInputBroker(db).configure(parent.id, HitlConfig(enabled=True))
    db.update_task(parent.id, status=TaskStatus.SUCCEEDED)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"instruction": "change the result", "request_key": "followup"}
        first = await client.post(f"/api/assistant/tasks/{parent.id}/steer", json=body)
        assert first.status_code == 200, first.text
        second = await client.post(f"/api/assistant/tasks/{parent.id}/steer", json=body)
        assert first.json()["followup_task_id"] == second.json()["followup_task_id"]
        assert db.get_task(parent.id).status == TaskStatus.SUCCEEDED
        child = first.json()["followup_task_id"]
        assert db.get_agent_record(child, "parent_task", "source")["payload"]["task_id"] == parent.id
        await client.post(f"/api/tasks/{child}/cancel")
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)
        # The stub above deliberately returns queued; finish orphan cancellation.
        await client.post(f"/api/tasks/{child}/cancel")
        state = AgentState(instruction="budget")
        state.revisable.limits.model_calls = 1
        state.role_invocation_count = 1
        state.revisable.checkpoint_version = 1
        state.revisable.stop_reason = "budget_exhausted"
        state.revisable.budget_kind = "model_calls"
        task = db.create_task("budget", state, device_serial="fixture")
        db.update_task(task.id, status=TaskStatus.PAUSED)
        stopped = await client.post(f"/api/tasks/{task.id}/resume")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "failed"
        extension = {"request_key": "renew", "budget": {"model_calls": 10}}
        assert (await client.post(f"/api/tasks/{task.id}/resume", json=extension)).status_code == 200
        assert (await client.post(f"/api/tasks/{task.id}/resume", json=extension)).status_code == 200
        assert db.get_task(task.id).state.revisable.limits.model_calls == 11
        assert (await client.post(f"/api/tasks/{task.id}/resume", json={**extension, "budget": {"model_calls": 20}})).status_code == 409
        assert db.get_task(task.id).state.role_invocation_count == 1


async def test_waiting_question_is_preserved_and_cancel_wins(app):
    from tests.test_hitl import begin
    db = app.state.db
    task = db.create_task("Send email", device_serial="fixture")
    broker = HumanInputBroker(db)
    broker.configure(task.id, HitlConfig(enabled=True))
    pending = begin(broker, task.id)
    async def idle(tid):
        pass
    app.state.schedule_task = idle
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(f"/api/assistant/tasks/{task.id}/steer", json={"instruction": "Do not send; keep draft", "request_key": "change"})
        assert result.status_code == 200
        assert result.json()["superseded_question"]["questions"] == pending["questions"]
        assert broker.current(task.id)["status"] == "superseded"
        await client.post(f"/api/tasks/{task.id}/cancel")
        assert SteeringInbox(db, task.id).projection()[-1]["status"] == "cancelled"
        result = await client.post(f"/api/assistant/tasks/{task.id}/steer", json={"instruction": "Another change", "request_key": "new"})
        assert result.status_code == 409


async def test_update_beats_old_completion_verdict(runtime, monkeypatch):
    orch, db, _, _ = runtime
    task = db.create_task("original")
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    waiting, release, updated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        if SteeringInbox(db, task.id).pending():
            updated.set()
            return reply("submit_planner_decision", {"decision": "complete", "reason": "new result"})
        waiting.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return reply("submit_planner_decision", {"decision": "complete", "reason": "obsolete result"})
    monkeypatch.setattr("agent.session.complete", complete)
    runner = asyncio.create_task(orch.run_task(task.id))
    await asyncio.wait_for(waiting.wait(), 10)
    SteeringInbox(db, task.id).accept(SteeringRequest(instruction="new requirement", request_key="change"))
    orch.steering_controls[task.id].notify()
    await asyncio.wait_for(updated.wait(), 5)
    assert await asyncio.wait_for(runner, 5) == TaskStatus.SUCCEEDED
    release.set()
    await asyncio.sleep(0)
    assert db.get_task(task.id).state.revisable.completion_reason == "new result"
