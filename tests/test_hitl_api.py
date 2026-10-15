"""Actual API authentication, shared task origins and proactive delivery."""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from control_api.mcp_http import local_token
from shared.hitl import HitlConfig, Reply
from shared.schemas import TaskStatus
from tests.test_hitl import answer, begin, question
from tests.test_task_pause import app


def auth(app):
    return {"Authorization": "Bearer " + local_token(app.state.settings)}


async def test_answer_authentication_and_single_resume(app):
    starts = []
    async def run(task_id):
        starts.append(task_id)
    app.state.orchestrator.run_task = run
    value = app.state.human_input
    task = app.state.db.create_task("goal", device_serial="fixture")
    value.configure(task.id, HitlConfig(enabled=True))
    item = begin(value, task.id)
    headers = auth(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
        path = f"/api/hitl/tasks/{task.id}/reply"
        assert (await client.post(path, json=answer(item).model_dump())).status_code == 401
        assert (await client.post(path, headers={**headers, "Origin": "https://evil.example"}, json=answer(item).model_dump())).status_code == 403
        malformed = answer(item).model_dump(exclude_none=True)
        malformed["answers"][0]["question_id"] = "other"
        assert (await client.post(path, headers=headers, json=malformed)).status_code == 409
        body = answer(item).model_dump(exclude_none=True)
        responses = await asyncio.gather(*(client.post(path, headers=headers, json=body) for _ in range(2)))
        assert [r.status_code for r in responses] == [200, 200]
        assert sum(r.json()["accepted"] for r in responses) == 1
        await asyncio.sleep(0)
        assert starts == [task.id]
        assert (await client.post(path, headers=headers, json={**body, "response_id": "different"})).status_code == 409


async def test_origin_flag_frozen_and_unsupported_assistant_fallback(app):
    starts = []
    async def run(task_id):
        starts.append(task_id)
    app.state.orchestrator.run_task = run
    settings = app.state.settings
    settings.default_model = "openai/mock"
    settings.models_json = json.dumps({"openai/mock": {}})
    settings.hitl_enabled = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/assistant/tasks", json={"instruction": "goal", "request_key": "origin-key"})
        assert created.status_code == 200, created.text
        task = created.json()["tasks"][0]
        value = app.state.human_input
        assert value.config(task["task_id"])["origin"] == "assistant"
        assert value.config(task["task_id"])["enabled"]
        settings.hitl_enabled = False
        replay = await client.post("/api/assistant/tasks", json={"instruction": "goal", "request_key": "origin-key"})
        assert replay.status_code == 200
        assert replay.json()["tasks"][0]["task_id"] == task["task_id"]
        item = begin(value, task["task_id"])
        assert item["owner"] == "console" and item["fallback_reason"] == "origin_unavailable"
        fetched = (await client.get(f"/api/assistant/tasks/{task['task_id']}")).json()
        assert fetched["status"] == "waiting_for_input"
        assert fetched["human_input"]["can_answer"]
        assert "binding" not in json.dumps(fetched["human_input"])


async def test_subscribed_origin_receives_proactive_event_and_falls_back_on_close(app):
    value = app.state.human_input
    binding = value.bind("assistant")
    unrelated = value.bind("assistant")
    endpoint = next(route.endpoint for route in app.state.human_input_router.routes if getattr(route, "path", "") == "/api/hitl/events")
    response = await endpoint(x_hitl_binding=binding)
    other_response = await endpoint(x_hitl_binding=unrelated)
    stream = response.body_iterator
    other_stream = other_response.body_iterator
    try:
        assert await anext(stream) == ": connected\n\n"
        assert await anext(other_stream) == ": connected\n\n"
        task = app.state.db.create_task("goal")
        value.configure(task.id, HitlConfig(enabled=True), origin="assistant", binding=binding)
        item = begin(value, task.id)
        assert item["owner"] == "assistant"
        event = await anext(stream)
        assert event.startswith("event: user_input")
        assert item["interaction_id"] in event
        assert binding not in event
        assert await anext(other_stream) == ": heartbeat\n\n"
        value.present(task.id, item["interaction_id"], 1, owner="assistant", binding=binding)
        item["presented"] = True
        item["delivery_deadline_at"] = time.time() - 60
        value._save(item)
        assert not value.reconcile(task.id)
        assert value.current(task.id)["owner"] == "assistant"  # thinking time is not closure
    finally:
        await stream.aclose()
        await other_stream.aclose()
    restored = value.current(task.id)
    assert restored["owner"] == "console" and restored["owner_version"] == 2
    assert restored["interaction_id"] == item["interaction_id"]
    assert restored["deadline_at"] == item["deadline_at"]


async def test_subscriber_presentation_timeout_and_cancelled_task(app):
    value = app.state.human_input
    binding = value.bind("cli")
    app.state.db.set_local_preference("hitl_binding:" + binding, {"owner": "cli", "expires_at": time.time() + 60, "subscribed": True})
    task = app.state.db.create_task("goal")
    value.configure(task.id, HitlConfig(enabled=True), origin="cli", binding=binding)
    item = begin(value, task.id)
    assert item["owner"] == "cli"
    item["delivery_deadline_at"] = time.time() - 1
    value._save(item)
    value.reconcile(task.id)
    assert value.current(task.id)["owner"] == "console"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(f"/api/tasks/{task.id}/cancel")).json()["status"] == "cancelled"
    assert app.state.db.get_task(task.id).status == TaskStatus.CANCELLED


async def test_cancel_waiting_task_while_previous_worker_releases_resources(app):
    value = app.state.human_input
    task = app.state.db.create_task("goal")
    value.configure(task.id, HitlConfig(enabled=True))
    item = begin(value, task.id)
    cleanup = asyncio.create_task(asyncio.Event().wait())
    app.state.running_tasks[task.id] = cleanup
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            cancelled = await client.post(f"/api/tasks/{task.id}/cancel")
            assert cancelled.json()["status"] == "cancelled"
            stale = await client.post(f"/api/hitl/tasks/{task.id}/reply", headers=auth(app),
                                     json=answer(item).model_dump(exclude_none=True))
            assert stale.status_code == 409
        assert app.state.db.get_task(task.id).status == TaskStatus.CANCELLED
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        app.state.running_tasks.pop(task.id, None)


async def test_answer_does_not_resume_during_desktop_shutdown(app):
    from desktop import shutdown
    value = app.state.human_input
    task = app.state.db.create_task("goal")
    value.configure(task.id, HitlConfig(enabled=True))
    item = begin(value, task.id)
    shutdown.prepare(app.state.settings.data_dir, "a" * 32)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = await client.post(f"/api/hitl/tasks/{task.id}/reply", headers=auth(app),
                                         json=answer(item).model_dump(exclude_none=True))
            assert response.status_code == 409
        assert app.state.db.get_task(task.id).status == TaskStatus.WAITING_FOR_INPUT
        assert value.current(task.id)["status"] == "pending"
    finally:
        shutdown.release(app.state.settings.data_dir, "a" * 32)
