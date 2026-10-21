"""Authenticated answer surfaces sharing the task broker and scheduler."""

import asyncio
import json
import time

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from pydantic import Field

from shared.hitl import HumanInputBroker, InputConflict, Reply, StrictModel
from shared.schemas import TaskStatus


class BindingBody(StrictModel):
    owner: str
    binding: str | None = Field(default=None, min_length=32, max_length=128)


class PresentationBody(StrictModel):
    interaction_id: str
    owner_version: int = Field(ge=1)


def mount_human_input(app, *, db, traces, schedule_run):
    router = APIRouter(prefix="/api/hitl")
    broker = HumanInputBroker(db, settings=app.state.settings)
    app.state.human_input = broker
    app.state.human_input_router = router
    monitor = None

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        if request.url.path.startswith("/api/hitl"):
            errors = [{key: error[key] for key in ("type", "loc", "msg")} for error in exc.errors()]
            return JSONResponse({"detail": broker.sanitize(errors)}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    def check(task_id):
        if db.get_task(task_id) is None:
            raise HTTPException(404, "task not found")

    def owner(binding):
        value = db.get_local_preference("hitl_binding:" + str(binding)) if binding else None
        if binding and not value:
            raise HTTPException(403, "unknown reply binding")
        return value["owner"] if value else "console"

    @router.get("/events")
    async def events(x_hitl_binding: str = Header()):
        value = db.get_local_preference("hitl_binding:" + x_hitl_binding)
        if not value or value["expires_at"] <= time.time():
            raise HTTPException(403, "unknown or expired reply binding")
        if value.get("subscribed"):
            raise HTTPException(409, "binding already has a live subscriber")
        db.set_local_preference("hitl_binding:" + x_hitl_binding, {**value, "subscribed": True})

        async def stream():
            delivered = set()
            try:
                yield ": connected\n\n"
                while True:
                    current = db.get_local_preference("hitl_binding:" + x_hitl_binding)
                    if not current or current["expires_at"] <= time.time():
                        break
                    broker.bind(value["owner"], x_hitl_binding)
                    for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT):
                        view = broker.public(task.id, owner=value["owner"], binding=x_hitl_binding)
                        if view and view["can_answer"]:
                            identity = (view["interaction_id"], view["owner_version"])
                            if identity not in delivered:
                                delivered.add(identity)
                                yield "event: user_input\ndata: " + json.dumps(view, ensure_ascii=False) + "\n\n"
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
            finally:
                latest = db.get_local_preference("hitl_binding:" + x_hitl_binding)
                if latest:
                    db.set_local_preference("hitl_binding:" + x_hitl_binding,
                                            {**latest, "subscribed": False, "expires_at": 0})
                for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT):
                    broker.reconcile(task.id)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    def conflict(exc):
        return HTTPException(409, str(exc))

    @router.post("/bindings")
    async def bind(body: BindingBody):
        try:
            return {"binding": broker.bind(body.owner, body.binding)}
        except (ValueError, InputConflict) as exc:
            raise conflict(exc) from exc

    @router.delete("/bindings/{binding}")
    async def detach(binding: str):
        value = db.get_local_preference("hitl_binding:" + binding)
        if value:
            db.set_local_preference("hitl_binding:" + binding, {**value, "expires_at": 0})
        for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT):
            broker.reconcile(task.id)
        return {"detached": True}

    @router.get("/pending")
    async def pending(x_hitl_binding: str | None = Header(default=None)):
        return {"interactions": [view for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT)
                                 if (view := broker.public(task.id, owner=owner(x_hitl_binding), binding=x_hitl_binding))]}

    @router.get("/tasks/{task_id}")
    async def get(task_id: str, x_hitl_binding: str | None = Header(default=None)):
        check(task_id)
        return {"interaction": broker.public(task_id, owner=owner(x_hitl_binding), binding=x_hitl_binding)}

    @router.post("/tasks/{task_id}/present")
    async def present(task_id: str, body: PresentationBody, x_hitl_binding: str | None = Header(default=None)):
        check(task_id)
        try:
            broker.present(task_id, body.interaction_id, body.owner_version,
                           owner=owner(x_hitl_binding), binding=x_hitl_binding)
        except InputConflict as exc:
            raise conflict(exc) from exc
        return {"presented": True}

    @router.post("/tasks/{task_id}/takeover")
    async def takeover(task_id: str, body: PresentationBody):
        check(task_id)
        try:
            return {"interaction": broker.takeover(task_id, interaction_id=body.interaction_id,
                                                   owner_version=body.owner_version)}
        except InputConflict as exc:
            raise conflict(exc) from exc

    @router.post("/tasks/{task_id}/reply")
    async def reply(task_id: str, body: Reply, background_tasks: BackgroundTasks,
                    x_hitl_binding: str | None = Header(default=None)):
        check(task_id)
        try:
            if body.outcome != "cancel_task":
                from desktop.updates import require_admission
                require_admission(app.state.settings.data_dir)
            accepted = broker.reply(task_id, body, owner=owner(x_hitl_binding), binding=x_hitl_binding)
        except InputConflict as exc:
            raise conflict(exc) from exc
        if accepted:
            traces.write(task_id, kind="system", message="Human input received",
                         payload={"event": "human_input_resolved", "interaction_id": body.interaction_id,
                                  "outcome": body.outcome})
            if body.outcome != "cancel_task":
                background_tasks.add_task(schedule_run, task_id)
        return {"accepted": accepted, "status": db.get_task(task_id).status.value}

    async def tick():
        while True:
            for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT):
                if broker.reconcile(task.id):
                    await schedule_run(task.id)
            # Recover answers accepted before a backend restart/scheduling interruption.
            for task in db.list_tasks(TaskStatus.QUEUED):
                item = broker.current(task.id)
                from shared.steering import SteeringInbox
                if (item and item["status"] == "resolved") or SteeringInbox(db, task.id).pending():
                    await schedule_run(task.id)
            await asyncio.sleep(1)

    @app.on_event("startup")
    async def start():
        nonlocal monitor
        # Stored liveness never proves a socket survived a backend process restart.
        for key in db.local_preference_keys("hitl_binding:"):
            value = db.get_local_preference(key)
            db.set_local_preference(key, {**value, "subscribed": False, "expires_at": 0})
        for task in db.list_tasks(TaskStatus.WAITING_FOR_INPUT):
            broker.reconcile(task.id)
        monitor = asyncio.create_task(tick(), name="human-input-broker")

    @app.on_event("shutdown")
    async def stop():
        if monitor:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    app.include_router(router)
