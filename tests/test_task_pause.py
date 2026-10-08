"""Operator pause boundaries, durable identity and submission races."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import AgentState, TaskStatus


def reply(name, args):
    return GatewayResponse(content="", model="test", stop_reason="tool_calls",
                           tool_calls=[ToolCall(id="call", name=name, arguments=json.dumps(args))])


def test_checkpoint_reservation_and_submission_survive_reopen(tmp_path):
    path = tmp_path / "test.db"
    db = Database(path)
    task = db.create_task("goal", device_serial="S")
    for status in (TaskStatus.PAUSING, TaskStatus.PAUSED):
        db.update_task(task.id, status=status)
        assert db.busy_serials() == {"S": task.id}
    with db.transaction():
        db.save_submission("key", "hash", [task.id])
    db.close()
    db = Database(path)
    assert db.get_submission("key") == {"request_hash": "hash", "task_ids": [task.id]}
    db.update_task(task.id, status=TaskStatus.CANCELLED)
    assert not db.busy_serials()
    db.close()


@pytest.mark.parametrize("pause_at", ["model", "action", "model_round"])
async def test_runtime_pause_releases_resources_and_resumes_with_history(tmp_path, monkeypatch, pause_at):
    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(buffer, "PNG")
    monkeypatch.setattr("driver.fixture._blank_png", lambda: buffer.getvalue())
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="test")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  artifacts=artifacts, driver=driver)
    state = AgentState(instruction="Go home")
    state.revisable.limits.device_actions = 5
    state.revisable.limits.deadline_at = time.time() + 300
    task = db.create_task(state.instruction, state, device_serial="fixture")
    original_act = driver.act
    date_calls = []
    async def date():
        date_calls.append(True)
        return "2026-10-05"
    driver.current_device_date = date
    async def act(action):
        result = await original_act(action)
        if pause_at == "action" and len(driver.actions) == 1:
            runtime.request_pause(task.id)
        return result
    driver.act = act
    executor_calls = 0
    resumed = False
    old_observation = None
    async def complete(model, messages, *, tools, **kwargs):
        nonlocal executor_calls, old_observation
        kwargs["attempt_meter"]("model_call_started", {"round": 1})
        names = [tool["function"]["name"] for tool in tools]
        if "submit_planner_decision" in names:
            if resumed and driver.actions:
                return reply("submit_planner_decision", {"decision": "complete", "reason": "Home visible"})
            return reply("submit_planner_decision", {"decision": "execute", "reason": "Go home",
                         "plan": {"current_stage": {"goal": "Go home"}}})
        executor_calls += 1
        update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                      if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        observation = update["current_observation_id"]
        if resumed:
            assert observation != old_observation
            assert "Original instruction" in messages[0]["content"]
            if pause_at == "action":
                assert "action_result" in json.dumps(messages)
                assert any(m.get("role") == "assistant" for m in messages)
        elif executor_calls == 1:
            old_observation = observation
            if pause_at in {"model", "model_round"}:
                runtime.request_pause(task.id)
        if pause_at == "model_round" and not resumed:
            return reply("read_history", {"query": "nothing"})
        directive = "finish" if driver.actions else "act"
        args = {"decision": directive, "summary": directive, "observation_id": observation}
        if directive == "act":
            args["action"] = {"type": "home"}
        return reply("submit_executor_step", args)
    monkeypatch.setattr("agent.session.complete", complete)
    assert await runtime.run_task(task.id) == TaskStatus.PAUSED
    checkpoint = db.get_task(task.id)
    assert checkpoint.status == TaskStatus.PAUSED
    assert len(driver.actions) == (pause_at == "action")
    assert driver.task_sessions[-1] == ("end", task.id)
    assert checkpoint.state.revisable.checkpoint_version == 1
    assert checkpoint.state.role_invocation_count == 2
    saved_deadline = checkpoint.state.revisable.limits.deadline_at
    if pause_at == "action":
        assert checkpoint.state.revisable.dialogue_refs
        assert checkpoint.state.step_number == 1
        assert db.latest_agent_record(task.id, "event")
    resumed = True
    assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
    final = db.get_task(task.id)
    assert len(driver.actions) == 1
    assert final.state.revisable.execution_count == 1
    assert final.state.revisable.limits.deadline_at == saved_deadline
    assert final.state.role_invocation_count > checkpoint.state.role_invocation_count
    assert len(date_calls) == 2
    db.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")
    monkeypatch.setenv("CLICKCLICK_MODELS_JSON", "{}")
    from control_api.main import create_app
    application = create_app(include_temp_runs=False)
    yield application
    application.state.db.close()


async def test_api_submit_replay_overlap_and_independent_pause_resume(app):
    gate = asyncio.Event()
    starts = []
    async def run(tid):
        starts.append(tid)
        await gate.wait()
    app.state.orchestrator.run_task = run
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"instruction": "goal", "device_serials": ["fixture"], "request_key": "request"}
        responses = await asyncio.gather(client.post("/api/tasks", json=body), client.post("/api/tasks", json=body))
        assert [r.status_code for r in responses] == [200, 200]
        tid = responses[0].json()["tasks"][0]["id"]
        assert responses[1].json()["tasks"][0]["id"] == tid
        await asyncio.sleep(0)
        assert starts == [tid]
        assert (await client.post("/api/tasks", json={**body, "instruction": "changed"})).status_code == 409
        assert (await client.post("/api/tasks", json={**body, "request_key": "different"})).status_code == 409
        app.state.running_tasks[tid].cancel()
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)
        # Use a queued task whose worker has not started to test a clean checkpoint.
        task = app.state.db.create_task("paused goal", device_serial="fixture")
        assert (await client.post(f"/api/tasks/{task.id}/pause")).json()["status"] == "paused"
        assert (await client.post(f"/api/tasks/{task.id}/pause")).json()["status"] == "paused"
        resumed = await asyncio.gather(*(client.post(f"/api/tasks/{task.id}/resume") for _ in range(2)))
        assert sum(r.json()["resumed"] for r in resumed) == 1
        await asyncio.sleep(0)
        assert starts.count(task.id) == 1
        gate.set()
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


async def test_paused_cancel_deadline_and_orphan_resume(app):
    db = app.state.db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        task = db.create_task("goal", device_serial="fixture")
        await client.post(f"/api/tasks/{task.id}/pause")
        assert db.busy_serials()["fixture"] == task.id
        assert (await client.post(f"/api/tasks/{task.id}/cancel")).json()["status"] == "cancelled"
        assert not db.busy_serials()
        assert (await client.post(f"/api/tasks/{task.id}/resume")).json()["resumed"] is False
        deadline_task = db.create_task("deadline", device_serial="fixture")
        await client.post(f"/api/tasks/{deadline_task.id}/pause")
        state = db.get_task(deadline_task.id).state
        state.revisable.limits.deadline_at = time.time() - 1
        db.update_task(deadline_task.id, state=state)
        response = await client.post(f"/api/tasks/{deadline_task.id}/resume")
        assert response.json()["status"] == "cancelled"
        assert db.get_task(deadline_task.id).failure_reason == "task_deadline_exhausted"
        orphan = db.create_task("orphan", device_serial="fixture")
        db.update_task(orphan.id, status=TaskStatus.RUNNING)
        assert (await client.post(f"/api/tasks/{orphan.id}/resume")).status_code == 409
        assert (await client.post(f"/api/tasks/{orphan.id}/pause")).status_code == 409


async def test_cancel_during_pause_cleanup_wins_over_checkpoint(tmp_path, monkeypatch):
    from PIL import Image
    import io
    buffer = io.BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(buffer, "PNG")
    monkeypatch.setattr("driver.fixture._blank_png", lambda: buffer.getvalue())
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="test")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    driver = FixtureDriver()
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                  artifacts=artifacts, driver=driver)
    task = db.create_task("goal", device_serial="fixture")
    cleanup_started, cleanup_continue = asyncio.Event(), asyncio.Event()
    async def complete(model, messages, *, tools, attempt_meter=None, **kwargs):
        attempt_meter("model_call_started", {})
        runtime.request_pause(task.id)
        return reply("submit_planner_decision", {"decision": "execute", "reason": "plan",
                     "plan": {"current_stage": {"goal": "goal"}}})
    monkeypatch.setattr("agent.session.complete", complete)
    original_end = driver.end_task_session
    async def end(tid):
        cleanup_started.set()
        await cleanup_continue.wait()
        return await original_end(tid)
    driver.end_task_session = end
    runner = asyncio.create_task(runtime.run_task(task.id))
    await asyncio.wait_for(cleanup_started.wait(), timeout=10)
    assert db.get_task(task.id).status == TaskStatus.PAUSING
    runtime.request_cancel(task.id)
    cleanup_continue.set()
    assert await runner == TaskStatus.CANCELLED
    assert db.get_task(task.id).status == TaskStatus.CANCELLED
    assert not driver.actions and not db.busy_serials()
    db.close()


async def test_missing_checkpoint_artifact_is_not_reported_paused(app):
    db = app.state.db
    state = AgentState(instruction="goal")
    state.revisable.dialogue_refs = ["missing"]
    task = db.create_task("goal", state, device_serial="fixture")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/tasks/{task.id}/pause")
        assert response.json()["status"] == "failed"
        assert db.get_task(task.id).failure_reason.startswith("pause_checkpoint_invalid")
        assert not db.busy_serials()


async def test_clean_paused_task_remains_resumable_after_backend_recreation(app):
    from control_api.main import create_app
    db = app.state.db
    task = db.create_task("goal", device_serial="fixture")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(f"/api/tasks/{task.id}/pause")
    replacement = create_app(include_temp_runs=False)
    started = []
    async def run(tid):
        started.append(tid)
    replacement.state.orchestrator.run_task = run
    async with AsyncClient(transport=ASGITransport(app=replacement), base_url="http://test") as client:
        response = await client.post(f"/api/tasks/{task.id}/resume")
        assert response.json()["resumed"] is True
        await asyncio.sleep(0)
        assert started == [task.id]
        await asyncio.gather(*replacement.state.running_tasks.values(), return_exceptions=True)
    replacement.state.db.close()


def test_checkpoint_excludes_undispatched_dialogue_but_retains_closed_steps(app):
    from agent.revisable.store import TaskStore
    db = app.state.db
    task = db.create_task("goal", device_serial="fixture")
    state = task.state
    store = TaskStore(db, app.state.artifacts, task.id)
    store.save_dialogue([{"role": "user", "content": "closed result"}], state)
    closed = state.revisable.dialogue_refs[:]
    state.step_number = 1
    store.save_dialogue([{"role": "assistant", "content": "undispatched action"}], state)
    assert app.state.orchestrator._pause(task.id, state) == TaskStatus.PAUSED
    assert db.get_task(task.id).state.revisable.dialogue_refs == closed


async def test_submit_alias_dedup_concurrent_overlap_and_atomic_rollback(app, monkeypatch):
    async def inventory():
        await asyncio.sleep(.01)
        return [{"key": "lab/S1", "serial": "S1"}, {"key": "lab/S2", "serial": "S2"}]
    app.state.driver_pool.inventory = inventory
    async def run(tid):
        return None
    app.state.orchestrator.run_task = run
    db = app.state.db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.post("/api/tasks", json={
            "instruction": "goal", "device_serials": ["lab/S1", "S1"], "request_key": f"key-{i}"
        }) for i in range(2)))
        assert sorted(r.status_code for r in responses) == [200, 409]
        created = next(r for r in responses if r.status_code == 200).json()["tasks"]
        assert len(created) == 1
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)
        db.update_task(created[0]["id"], status=TaskStatus.CANCELLED)
    original_create = db.create_task
    count = 0
    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated database failure")
        return original_create(*args, **kwargs)
    monkeypatch.setattr(db, "create_task", fail_second)
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        response = await client.post("/api/tasks", json={"instruction": "fanout",
            "device_serials": ["lab/S1", "lab/S2"], "request_key": "rollback"})
        assert response.status_code == 500
        assert len(db.list_tasks()) == 1
        assert db.get_submission("rollback") is None
        assert not db.busy_serials()
