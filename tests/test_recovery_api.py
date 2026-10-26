"""Persisted scheduling, explicit recovery API and source MCP projections."""

import asyncio
import json

from httpx import ASGITransport,AsyncClient

from shared.recovery_store import RecoveryStore
from shared.schemas import TaskStatus
from tests.test_task_pause import app


async def test_queued_manual_task_is_scheduled_after_lost_wakeup(app):
    db=app.state.db
    task=db.create_task("accepted before process exit",device_serial="fixture")
    RecoveryStore(db,app.state.artifacts).enable(task.id,"manual")
    started=[]
    async def run(task_id):
        started.append(task_id)
        db.update_task(task_id,status=TaskStatus.SUCCEEDED)
    app.state.orchestrator.run_task=run
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if started:
                break
            await asyncio.sleep(.01)
        assert started==[task.id]
        assert db.get_task(task.id).status==TaskStatus.SUCCEEDED


async def test_shutdown_retires_running_task_instead_of_cancelling_user_work(app):
    db=app.state.db
    task=db.create_task("keep going after backend restart",device_serial="fixture")
    RecoveryStore(db,app.state.artifacts).enable(task.id,"safe_auto")
    started=asyncio.Event()
    async def run(task_id):
        db.update_task(task_id,status=TaskStatus.RUNNING)
        started.set()
        await asyncio.Event().wait()
    app.state.orchestrator.run_task=run
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(),3)
    assert db.get_task(task.id).status==TaskStatus.RECOVERING
    assert RecoveryStore(db).metadata(task.id)["desired_state"]=="run"


async def test_recover_request_retry_returns_original_response_after_terminal_state(app):
    db=app.state.db
    task=db.create_task("explicit recover",device_serial="fixture")
    store=RecoveryStore(db,app.state.artifacts)
    store.enable(task.id,"manual")
    db.update_task(task.id,status=TaskStatus.RECOVERY_BLOCKED)
    recoveries=[]
    async def recover(task_id,**kwargs):
        recoveries.append(task_id)
        db.update_task(task_id,status=TaskStatus.CANCELLED)
        return {"task_id":task_id,"status":"cancelled","accepted":True}
    app.state.recovery.recover=recover
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        first=await client.post(f"/api/tasks/{task.id}/recover",json={"request_key":"once"})
        repeated=await client.post(f"/api/tasks/{task.id}/recover",json={"request_key":"once"})
        assert first.status_code==repeated.status_code==200
        assert first.json()==repeated.json()
        assert recoveries==[task.id]


async def test_recovery_projection_is_bounded_read_only_and_not_an_unknown_unlock_queue(app):
    db=app.state.db
    task=db.create_task("diagnosis",device_serial="fixture")
    store=RecoveryStore(db,app.state.artifacts)
    store.enable(task.id,"manual")
    db.update_task(task.id,status=TaskStatus.RECOVERY_BLOCKED)
    store.event(task.id,"blocked",reason="old_input_chain_live")
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        payload=(await client.get(f"/api/assistant/tasks/{task.id}")).json()
        assert payload["status"]=="recovery_blocked"
        assert payload["recovery"]["available_actions"]==["cancel","recover"]
        assert not payload["recovery"]["requires_human"]
        assert "state" not in payload
        assert (await client.post(f"/api/tasks/{task.id}/recover",json={"request_key":"r","force_result":"success"})).status_code==422


async def test_manual_recover_accepted_before_lost_wakeup_is_discovered_on_restart(app):
    db=app.state.db
    task=db.create_task("persist accepted recovery",device_serial="fixture")
    store=RecoveryStore(db,app.state.artifacts)
    store.enable(task.id,"manual")
    db.update_task(task.id,status=TaskStatus.RECOVERY_BLOCKED)
    store.request(task.id,"accepted",{"request_key":"accepted"},{"accepted":True})
    recovered=[]
    async def recover(task_id,*,explicit=False):
        assert explicit
        recovered.append(task_id)
        db.update_task(task_id,status=TaskStatus.CANCELLED)
        return {"status":"cancelled"}
    app.state.recovery.recover=recover
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if recovered:
                break
            await asyncio.sleep(.01)
        assert recovered==[task.id]
        assert not store.pending_requests(task.id)
