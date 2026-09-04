"""Control API + static Console (unified task management & debug)."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.executor import Executor
from agent.orchestrator import Orchestrator
from agent.planner import Planner
from agent.reviewer import Reviewer
from agent.traces import TraceWriter
from control_api.data_sources import ConsoleDataSources
from control_api.services import ObservabilityQueries, task_execution_elapsed_ms
from control_api.sse import EventBus, TooManySubscribers
from driver.fixture import FixtureDriver
from driver.pool import DriverPool
from driver.pool import LOCAL_DRIVER_ID, FIXTURE_DRIVER_ID, parse_device_key
from driver.scrcpy_mirror import (
    REGISTRY as MIRROR_REGISTRY,
)
from driver.scrcpy_mirror import (
    LocalStreamSource,
    MirrorUnavailableError,
    RemoteStreamSource,
    is_mirror_server_available,
)
from shared.artifacts import ArtifactStore
from shared.chatgpt_auth import (
    PendingDeviceLogin,
    chatgpt_status,
    poll_device_login,
    start_device_login,
)
from shared.config import get_settings
from shared.db import Database
from shared.model_catalog import build_model_catalog, catalog_model_ids
from shared.model_router import ModelRouter
from shared.schemas import AgentState, LogLevel, TaskStatus

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DIST_DIR = WEB_DIR / "dist"


class CreateTaskBody(BaseModel):
    instruction: str = Field(min_length=1)
    device_serials: list[str] = Field(min_length=1)
    skill_learn: bool = False
    manager_model: str | None = Field(
        default=None,
        description=(
            "Existing shared decision-model override used by both Planner and Reviewer"
        ),
    )
    executor_model: str | None = None


class SkillUpsertBody(BaseModel):
    id: str = Field(min_length=1)  # alias of name
    name: str = ""
    description: str = ""
    version: str = "0.1.0"
    app: str | None = None
    kind: Literal["generic", "app_core", "workflow", "candidate"] = "generic"
    capability: str = ""
    tags: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    body: str = ""


class SkillUpdateBody(BaseModel):
    app: str | None = None
    kind: Literal["generic", "app_core", "workflow", "candidate"] | None = None
    capability: str | None = None
    tags: list[str] | None = None
    triggers: list[str] | None = None
    description: str | None = None
    version: str | None = None
    body: str | None = None


class DraftRejectBody(BaseModel):
    reason: str = ""


def _sse_format(event: dict[str, Any]) -> str:
    """Serialize a trace event dict as a single SSE message block."""
    data = json.dumps(event, ensure_ascii=False, default=str)
    # SSE data lines may not contain raw newlines; split on them.
    lines = data.split("\n")
    body = "".join(f"data: {ln}\n" for ln in lines)
    return f"{body}\n"


# WebSocket close codes per RFC 6455. 1011 = internal error, 1013 = try
# again later (used here for "scrcpy unavailable" so the MirrorPanel can
# branch into the unavailable state without parsing payloads).
_WS_CLOSE_INTERNAL = 1011
_WS_CLOSE_TRY_AGAIN = 1013


logger = logging.getLogger("control_api")


def create_app(
    *,
    planner_factory: Callable[[], Planner] | None = None,
    reviewer_factory: Callable[[], Reviewer] | None = None,
    executor_factory: Callable[[], Executor] | None = None,
    include_temp_runs: bool | None = None,
    temp_runs_root: Path = Path("/private/tmp"),
) -> FastAPI:
    """Build the Control API application.

    Optional focused-role factories let tests inject agents; production
    callers omit them and get the llm-gateway-backed Reviewer, Planner, and
    Executor. Reviewer and Planner share the configured decision model.
    """
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    if include_temp_runs is None:
        default_data_dir = Path(__file__).resolve().parent.parent / "data"
        include_temp_runs = settings.data_dir.resolve() == default_data_dir.resolve()
    data_sources = ConsoleDataSources(
        db,
        artifacts,
        temp_root=temp_runs_root if include_temp_runs else None,
    )
    bus = EventBus()
    traces = TraceWriter(db, artifacts, bus=bus)
    queries = ObservabilityQueries(db, artifacts)
    pool = DriverPool(settings)
    router = ModelRouter.from_settings(settings)
    # Placeholder driver for factory construction; run_task rebinds per serial.
    _placeholder_driver = FixtureDriver()

    # Live mirror: local jar on API host, or relay to configured remote hubs.
    hub_by_id = {h["id"]: h["url"] for h in settings.driver_hubs()}

    def _mirror_source_for(device_key: str):
        driver_id, serial = parse_device_key(device_key)
        if driver_id in (LOCAL_DRIVER_ID, FIXTURE_DRIVER_ID):
            return LocalStreamSource(serial=serial)
        url = hub_by_id.get(driver_id)
        if not url:
            raise MirrorUnavailableError(f"unknown mirror hub id={driver_id!r}")
        return RemoteStreamSource(hub_url=url, serial=serial, hub_id=driver_id)

    MIRROR_REGISTRY.set_source_factory(_mirror_source_for)

    app = FastAPI(title="ClickClick Console API", version="0.1.0")
    app.state.db = db
    app.state.artifacts = artifacts
    app.state.queries = queries
    app.state.data_sources = data_sources
    app.state.settings = settings
    app.state.bus = bus
    app.state.driver_pool = pool

    if planner_factory is None:
        def planner_factory() -> Planner:
            return Planner(
                _placeholder_driver,
                artifacts,
                model=router.planner,
                settings=settings,
            )

    if reviewer_factory is None:
        def reviewer_factory() -> Reviewer:
            return Reviewer(
                _placeholder_driver,
                artifacts,
                model=router.reviewer,
                settings=settings,
            )

    if executor_factory is None:
        def executor_factory() -> Executor:
            return Executor(
                _placeholder_driver, artifacts, model=router.executor, settings=settings
            )

    orch = Orchestrator(
        db,
        traces,
        planner_factory=planner_factory,
        reviewer_factory=reviewer_factory,
        executor_factory=executor_factory,
        driver=None,
        driver_pool=pool,
        artifacts=artifacts,
        settings=settings,
    )
    app.state.orchestrator = orch

    # ChatGPT device-code login (single pending session for local Console).
    _chatgpt_pending: dict[str, PendingDeviceLogin | None] = {"login": None}

    # Retained handles so operator cancel can hard-stop a stuck run.
    _running: dict[str, asyncio.Task] = {}
    _TERMINAL = (
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )

    async def _run(task_id: str) -> None:
        try:
            await orch.run_task(task_id)
        finally:
            _running.pop(task_id, None)
            orch.cancel_registry.clear(task_id)

    async def _hard_cancel_after(task_id: str) -> None:
        await asyncio.sleep(float(settings.task_cancel_hard_timeout_s))
        latest = db.get_task(task_id)
        if latest is None or latest.status in _TERMINAL:
            return
        handle = _running.get(task_id)
        if handle is not None and not handle.done():
            handle.cancel()
            return
        # Grace elapsed, still non-terminal, but no live runner (orphan /
        # race where the coroutine died without writing a terminal status).
        state = latest.state or AgentState(instruction=latest.instruction)
        orch._cancel(task_id, state, mode="orphan")

    def _force_cancel_orphan(task_id: str, t) -> dict[str, Any]:
        """Mark a non-terminal task cancelled when no asyncio runner exists."""
        state = t.state or AgentState(instruction=t.instruction)
        orch._cancel(task_id, state, mode="orphan")
        latest = db.get_task(task_id)
        return {
            "task_id": task_id,
            "status": latest.status.value if latest else TaskStatus.CANCELLED.value,
            "cancel_requested": True,
            "already_terminal": True,
        }

    def _resolve_device_keys(
        requested: list[str], inv: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        """Map create payload entries to inventory ``key`` values.

        Accepts full keys (``lab-a/SERIAL``) or bare ADB serials when that
        serial uniquely identifies one online device.
        """
        by_key = {d["key"]: d for d in inv if d.get("key")}
        by_serial: dict[str, list[dict[str, Any]]] = {}
        for d in inv:
            by_serial.setdefault(d["serial"], []).append(d)

        resolved: list[str] = []
        unknown: list[str] = []
        for raw in requested:
            if raw in by_key:
                resolved.append(raw)
                continue
            matches = by_serial.get(raw) or []
            if len(matches) == 1:
                resolved.append(matches[0]["key"])
                continue
            unknown.append(raw)
        return resolved, unknown

    async def _devices_payload() -> list[dict[str, Any]]:
        inv = await pool.inventory()
        busy = db.busy_serials()
        out: list[dict[str, Any]] = []
        for d in inv:
            key = d.get("key") or d["serial"]
            task_id = busy.get(key)
            out.append(
                {
                    **d,
                    "key": key,
                    "busy": task_id is not None,
                    "busy_task_id": task_id,
                }
            )
        return out

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        devices = await _devices_payload()
        any_ok = bool(devices)
        hubs = pool.hubs()
        return {
            "ok": True,
            "skill_miner_enabled": False,
            "driver": {
                "ok": any_ok,
                "mode": pool.mode,
                "device_count": len(devices),
                "hub_count": len(hubs),
                "hubs": [{"id": h["id"], "url": h["url"]} for h in hubs],
            },
            "devices": devices,
            "runtime": "reviewer-planner-executor-loop",
        }

    @app.get("/api/models")
    async def list_models() -> dict[str, Any]:
        """Desensitized model catalog + resolved role defaults."""
        return build_model_catalog(settings)

    @app.get("/api/chatgpt/status")
    async def get_chatgpt_status() -> dict[str, Any]:
        return chatgpt_status(settings)

    @app.post("/api/chatgpt/login/start")
    async def chatgpt_login_start() -> dict[str, Any]:
        try:
            payload, pending = await asyncio.to_thread(start_device_login, settings)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"chatgpt login start failed: {exc}") from exc
        _chatgpt_pending["login"] = pending
        return payload

    @app.post("/api/chatgpt/login/poll")
    async def chatgpt_login_poll() -> dict[str, Any]:
        pending = _chatgpt_pending.get("login")
        if pending is None:
            raise HTTPException(
                409,
                "no pending chatgpt login; call /api/chatgpt/login/start first",
            )
        _status, body, next_pending = await asyncio.to_thread(
            poll_device_login, settings, pending,
        )
        _chatgpt_pending["login"] = next_pending
        return body

    @app.get("/api/devices")
    async def list_devices() -> list[dict[str, Any]]:
        return await _devices_payload()

    @app.post("/api/devices/{device_key:path}/initialize")
    async def initialize_device(device_key: str) -> dict[str, Any]:
        inventory = await pool.inventory()
        resolved, unknown = _resolve_device_keys([device_key], inventory)
        if unknown or not resolved:
            raise HTTPException(404, f"unknown or offline device: {device_key}")
        key = resolved[0]
        if key in db.busy_serials():
            raise HTTPException(409, f"device busy: {key}")
        async with pool.lock_for(key):
            driver = pool.get(key)
            initialize = getattr(driver, "initialize_environment", None)
            if not callable(initialize):
                raise HTTPException(501, "driver does not support environment initialization")
            return await initialize()

    @app.post("/api/tasks")
    async def create_task(body: CreateTaskBody) -> dict[str, Any]:
        # Deduplicate while preserving order. Entries are device keys
        # (``driver_id/serial`` for remote hubs) or bare serials when unique.
        seen: set[str] = set()
        requested: list[str] = []
        for s in body.device_serials:
            s = (s or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            requested.append(s)
        if not requested:
            raise HTTPException(400, "device_serials must be non-empty")

        known = catalog_model_ids(settings)
        manager_model = (body.manager_model or "").strip() or None
        executor_model = (body.executor_model or "").strip() or None
        for label, mid in (("manager_model", manager_model), ("executor_model", executor_model)):
            if mid is None:
                continue
            if not known:
                raise HTTPException(
                    400,
                    f"{label}={mid!r} rejected: CLICKCLICK_MODELS_JSON is empty",
                )
            if mid not in known:
                raise HTTPException(400, f"{label}={mid!r} not in model catalog")

        inv = await pool.inventory()
        serials, unknown = _resolve_device_keys(requested, inv)
        if unknown:
            raise HTTPException(400, f"unknown or offline device_serials: {unknown}")

        busy = db.busy_serials()
        conflicted = [s for s in serials if s in busy]
        if conflicted:
            raise HTTPException(
                409,
                f"device busy: { {s: busy[s] for s in conflicted} }",
            )

        created = []
        for serial in serials:
            record = db.create_task(
                body.instruction,
                AgentState(
                    instruction=body.instruction,
                    skill_learn=bool(body.skill_learn),
                    manager_model=manager_model,
                    executor_model=executor_model,
                ),
                device_serial=serial,
            )
            handle = asyncio.create_task(_run(record.id))
            _running[record.id] = handle
            created.append(record.model_dump())

        return {"tasks": created}

    @app.get("/api/tasks")
    async def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
        st = TaskStatus(status) if status else None
        return [
            data_sources.decorate(
                {
                    **task.model_dump(),
                    "execution_elapsed_ms": task_execution_elapsed_ms(task),
                },
                source,
            )
            for task, source in data_sources.list_tasks(st)
        ]

    @app.get("/api/tasks/failed/list")
    async def failed_list() -> list[dict[str, Any]]:
        return [
            data_sources.decorate(
                {
                    "id": task.id,
                    "instruction": task.instruction,
                    "failure_reason": task.failure_reason,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "execution_elapsed_ms": task_execution_elapsed_ms(task),
                    "current_node_id": task.current_node_id,
                    "current_subgoal": task.current_subgoal,
                    "step_number": task.step_number,
                    "device_serial": task.device_serial,
                },
                source,
            )
            for task, source in data_sources.list_tasks(TaskStatus.FAILED)
        ]

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Operator force-cancel: soft request now, hard cancel after grace.

        If the DB says queued/running but there is no live asyncio runner
        (typical after Control API restart), finalize as ``cancelled``
        immediately so the Console and busy_serials cannot stick forever.
        """
        t = db.get_task(task_id)
        if not t:
            raise HTTPException(404, "task not found")
        if t.status in _TERMINAL:
            return {
                "task_id": task_id,
                "status": t.status.value,
                "cancel_requested": False,
                "already_terminal": True,
            }
        if t.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            raise HTTPException(409, f"cannot cancel status={t.status.value}")

        handle = _running.get(task_id)
        live = handle is not None and not handle.done()

        orch.request_cancel(task_id)
        traces.write(
            task_id,
            kind="system",
            level=LogLevel.WARN,
            message="operator_cancel_requested",
        )

        if not live:
            return _force_cancel_orphan(task_id, t)

        asyncio.create_task(_hard_cancel_after(task_id))
        # Re-read in case the soft path already finished between request and now.
        latest = db.get_task(task_id)
        status = latest.status.value if latest else t.status.value
        return {
            "task_id": task_id,
            "status": status,
            "cancel_requested": True,
            "already_terminal": bool(latest and latest.status in _TERMINAL),
        }

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        task, source = located
        return data_sources.decorate(source.queries.task_detail(task), source)

    @app.get("/api/tasks/{task_id}/replay")
    async def replay(task_id: str) -> dict[str, Any]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        _task, source = located
        try:
            return data_sources.decorate(source.queries.replay(task_id), source)
        except KeyError:
            raise HTTPException(404, "task not found") from None

    @app.get("/api/tasks/{task_id}/timeline")
    async def timeline(task_id: str) -> dict[str, Any]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        _task, source = located
        try:
            return data_sources.decorate(source.queries.timeline(task_id), source)
        except KeyError:
            raise HTTPException(404, "task not found") from None

    @app.get("/api/tasks/{task_id}/steps/{node_id}/{seq}/debug")
    async def step_debug(task_id: str, node_id: str, seq: int) -> dict[str, Any]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "step not found")
        _task, source = located
        try:
            return data_sources.decorate(
                source.queries.step_debug(task_id, node_id, seq), source,
            )
        except KeyError:
            raise HTTPException(404, "step not found") from None

    @app.get("/api/tasks/{task_id}/traces")
    async def task_traces(
        task_id: str, level: str | None = Query(default=None)
    ) -> list[dict[str, Any]]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        _task, source = located
        return source.queries.traces(task_id, level)

    @app.get("/api/tasks/{task_id}/stream")
    async def task_stream(task_id: str):
        """SSE stream of trace events for a task.

        Replays already-persisted ticks from the DB first, then switches to
        live events from the in-process pub/sub. For a terminal task the
        stream closes after the replay; for a running task it tails live
        events until the task becomes terminal, then drains and closes.
        The subscriber queue is cleaned up on disconnect via ``try/finally``.
        """
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        task, source = located
        source_db = source.db
        if source.read_only:
            async def external_event_gen():
                for ev in source_db.list_traces(task_id):
                    yield _sse_format(ev.model_dump())

            return StreamingResponse(
                external_event_gen(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        try:
            queue, unsubscribe = bus.subscribe(task_id)
        except TooManySubscribers:
            raise HTTPException(429, "too many SSE subscribers for this task") from None

        async def event_gen():
            try:
                # 1) Replay already-persisted ticks.
                for ev in source_db.list_traces(task_id):
                    yield _sse_format(ev.model_dump())
                # 2) A terminal task won't produce more events; close so the
                #    client can finalize without waiting on a keepalive.
                if task.status in _TERMINAL:
                    return
                # 3) Live tail until the task becomes terminal.
                while True:
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield _sse_format(ev)
                    except asyncio.TimeoutError:
                        # Keepalive comment frame keeps idle connections open.
                        yield ": keepalive\n\n"
                        continue
                    # After delivering an event, check terminality. A terminal
                    # task won't produce more events, so drain the queue and
                    # close so the client can finalize.
                    latest = source_db.get_task(task_id)
                    if latest and latest.status in _TERMINAL:
                        while not queue.empty():
                            yield _sse_format(queue.get_nowait())
                        return
            finally:
                unsubscribe()

        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/artifacts/{ref:path}")
    async def get_artifact(ref: str) -> FileResponse:
        try:
            path = data_sources.resolve_artifact(ref)
        except ValueError as exc:
            raise HTTPException(403, "artifact ref not authorized") from exc
        if path is None:
            raise HTTPException(404, "artifact not found")
        return FileResponse(path)

    # ------------------------------------------------------------------
    # Skills filesystem API (canonical CRUD + pending-patch review)
    # ------------------------------------------------------------------
    def _skills_lib():
        from agent.skills.library import SkillLibrary

        return SkillLibrary()

    def _skill_summary(p: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": p.id,
            "name": getattr(p, "name", None) or p.id,
            "description": getattr(p, "description", None) or "",
            "version": getattr(p, "version", None) or "0.1.0",
            "app_name": p.app or "",
            "app": p.app or "",
            "kind": p.kind,
            "capability": getattr(p, "capability", ""),
            "intent_tags": list(p.tags),
            "tags": list(p.tags),
            "triggers": list(p.triggers),
            "source": getattr(p, "source", None) or "filesystem",
            "path": str(p.path),
        }
        return row

    def _skill_detail(p: Any) -> dict[str, Any]:
        row = _skill_summary(p)
        body = getattr(p, "body", "") or ""
        row["body"] = body
        row["frontmatter"] = dict(p.frontmatter or {})
        return row

    @app.get("/api/skills")
    async def list_skills(
        app: str | None = None,
        app_name: str | None = None,
        q: str = "",
    ) -> list[dict[str, Any]]:
        """List filesystem skills under ``skills/`` (runtime SoT)."""
        lib = _skills_lib()
        packs = lib.list_for_api(
            app=app or app_name,
            q=q,
        )
        return [_skill_summary(p) for p in packs]

    @app.post("/api/skills")
    async def create_skill(body: SkillUpsertBody) -> dict[str, Any]:
        from agent.skills.library import SkillConflictError, SkillPathJailError

        lib = _skills_lib()
        try:
            pack = lib.create_canonical(
                skill_id=body.id,
                name=body.name or body.id,
                description=body.description,
                version=body.version,
                app=body.app,
                kind=body.kind,
                capability=body.capability,
                tags=body.tags,
                triggers=body.triggers,
                body=body.body,
            )
        except SkillConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (SkillPathJailError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _skill_detail(pack)

    @app.get("/api/skills/pending")
    async def list_pending_skills() -> list[dict[str, Any]]:
        from agent.skills.pending import list_pending

        return [
            {
                "id": p.id,
                "gist": p.gist,
                "target": p.target_rel,
                "status": p.status,
                "source_task_id": p.meta.get("source_task_id") or "",
                "outcome": p.meta.get("outcome") or "",
                "app": p.meta.get("app") or "",
            }
            for p in list_pending(root=_skills_lib().root)
        ]

    @app.get("/api/skills/pending/{pending_id}")
    async def get_pending_skill(pending_id: str) -> dict[str, Any]:
        from agent.skills.library import SkillNotFoundError
        from agent.skills.pending import pending_diff

        try:
            return pending_diff(pending_id, root=_skills_lib().root)
        except SkillNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/skills/pending/{pending_id}/approve")
    async def approve_pending_skill(pending_id: str) -> dict[str, Any]:
        from agent.skills.library import SkillNotFoundError, SkillPathJailError
        from agent.skills.pending import approve_pending

        try:
            dest = approve_pending(pending_id, root=_skills_lib().root)
        except SkillNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (SkillPathJailError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "path": str(dest), "id": pending_id}

    @app.post("/api/skills/pending/{pending_id}/reject")
    async def reject_pending_skill(
        pending_id: str, body: DraftRejectBody | None = None,
    ) -> dict[str, Any]:
        from agent.skills.library import SkillNotFoundError
        from agent.skills.pending import reject_pending

        body = body or DraftRejectBody()
        try:
            reject_pending(pending_id, reason=body.reason, root=_skills_lib().root)
        except SkillNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "id": pending_id, "status": "rejected"}

    @app.post("/api/tasks/{task_id}/learn")
    async def learn_from_task(task_id: str) -> dict[str, Any]:
        from agent.skills.learner import run_skill_learner

        task = db.get_task(task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        result = await run_skill_learner(task, settings=settings, force=True)
        return result

    @app.get("/api/skills/{skill_id}")
    async def get_skill(skill_id: str) -> dict[str, Any]:
        lib = _skills_lib()
        pack = lib.get(skill_id, include_candidates=True)
        if pack is None:
            raise HTTPException(404, "skill not found")
        return _skill_detail(pack)

    @app.put("/api/skills/{skill_id}")
    async def update_skill(skill_id: str, body: SkillUpdateBody) -> dict[str, Any]:
        from agent.skills.library import SkillConflictError, SkillNotFoundError, SkillPathJailError

        lib = _skills_lib()
        try:
            pack = lib.update_skill(
                skill_id,
                app=body.app,
                kind=body.kind,
                capability=body.capability,
                tags=body.tags,
                triggers=body.triggers,
                description=body.description,
                version=body.version,
                body=body.body,
            )
        except SkillNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SkillConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (SkillPathJailError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _skill_detail(pack)

    @app.delete("/api/skills/{skill_id}")
    async def delete_skill(skill_id: str) -> dict[str, Any]:
        from agent.skills.library import SkillNotFoundError, SkillPathJailError

        lib = _skills_lib()
        try:
            lib.delete_canonical(skill_id)
        except SkillNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except SkillPathJailError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "id": skill_id}

    @app.get("/api/tasks/{task_id}/skill-links")
    async def task_skill_links(task_id: str) -> dict[str, Any]:
        located = data_sources.locate(task_id)
        if located is None:
            raise HTTPException(404, "task not found")
        task, source = located
        used: list[str] = []
        seen: set[str] = set()
        for ev in source.db.list_traces(task_id):
            if ev.kind not in (
                "planner_decision",
                "reviewer_decision",
                "executor_tick",
            ):
                continue
            payload = ev.payload or {}
            for sid in payload.get("skill_ids") or []:
                s = str(sid).strip()
                if s and s not in seen:
                    seen.add(s)
                    used.append(s)
            for call in payload.get("tool_calls") or []:
                if not isinstance(call, dict) or call.get("name") != "load_skill":
                    continue
                arguments = call.get("arguments") or {}
                s = str(arguments.get("skill_id") or "").strip() if isinstance(arguments, dict) else ""
                if s and s not in seen and call.get("status") == "succeeded":
                    seen.add(s)
                    used.append(s)
        from agent.skills.pending import list_pending

        pending = [
            {
                "id": item.id,
                "gist": item.gist,
                "target": item.target_rel,
                "status": item.status,
                "source_task_id": item.meta.get("source_task_id") or "",
                "outcome": item.meta.get("outcome") or "",
                "app": item.meta.get("app") or "",
            }
            for item in list_pending(root=_skills_lib().root, status="")
            if str(item.meta.get("source_task_id") or "") == task_id
        ]
        return data_sources.decorate(
            {"task_id": task_id, "skill_ids": used, "pending": pending}, source,
        )

    @app.get("/api/device/scrcpy")
    async def scrcpy_hint() -> dict[str, Any]:
        jar_ok = is_mirror_server_available()
        return {
            "tool": "scrcpy-server",
            "required": False,
            "command": "scrcpy",
            "note": (
                "Console Live uses vendored scrcpy-server (local or hub relay). "
                "Desktop scrcpy remains an optional external fallback."
            ),
            "available": jar_ok or bool(hub_by_id),
            "server_jar": jar_ok,
            "remote_hubs": list(hub_by_id.keys()),
        }

    # ------------------------------------------------------------------
    # live-screen-mirror: GET /api/device/mirror/stream
    # ------------------------------------------------------------------
    # Operator-side WebSocket: local scrcpy-server bridge or remote hub relay.
    # Off the agent path — screencap / get_frame unchanged.
    @app.websocket("/api/device/mirror/stream")
    async def mirror_stream(websocket: WebSocket) -> None:
        await websocket.accept()

        device_key: str | None = None
        try:
            try:
                hello = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
            except asyncio.TimeoutError:
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL,
                    reason=json.dumps({"reason": "hello_timeout"}),
                )
                return
            try:
                hello_payload = json.loads(hello)
            except json.JSONDecodeError:
                hello_payload = {}
            if not isinstance(hello_payload, dict):
                hello_payload = {}

            key = hello_payload.get("device_key")
            serial = hello_payload.get("serial")
            if isinstance(key, str) and key.strip():
                device_key = key.strip()
            elif isinstance(serial, str) and serial.strip():
                device_key = serial.strip()
            else:
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL,
                    reason=json.dumps({"reason": "missing_serial"}),
                )
                return

            # Local sessions need the vendored jar on this host; remote hubs
            # only need a configured hub URL (jar lives on the lab host).
            try:
                driver_id, adb_serial = parse_device_key(device_key)
            except ValueError as exc:
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL,
                    reason=json.dumps({"reason": "invalid_device_key", "detail": str(exc)}),
                )
                return

            if driver_id in (LOCAL_DRIVER_ID, FIXTURE_DRIVER_ID) and not is_mirror_server_available():
                await websocket.close(
                    code=_WS_CLOSE_TRY_AGAIN,
                    reason=json.dumps(
                        {
                            "reason": "mirror_unavailable",
                            "detail": "vendored scrcpy-server jar missing on API host",
                        }
                    ),
                )
                return

            try:
                session = await MIRROR_REGISTRY.start(device_key)
            except MirrorUnavailableError as exc:
                await websocket.close(
                    code=_WS_CLOSE_TRY_AGAIN,
                    reason=json.dumps(
                        {"reason": "mirror_unavailable", "detail": str(exc)}
                    ),
                )
                return
            except FileNotFoundError as exc:
                await websocket.close(
                    code=_WS_CLOSE_TRY_AGAIN,
                    reason=json.dumps(
                        {"reason": "mirror_unavailable", "detail": str(exc)}
                    ),
                )
                return

            await websocket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "serial": adb_serial,
                        "device_key": device_key,
                        "codec": "h264",
                        "codec_string": session.codec_string or "avc1.42E01E",
                    }
                )
            )

            async for chunk in session.frames():
                if not chunk:
                    break
                try:
                    await websocket.send_bytes(chunk)
                except (WebSocketDisconnect, RuntimeError):
                    # Client / Vite proxy already closed — stop quietly.
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("mirror stream error key=%s: %s", device_key, exc)
            try:
                await websocket.close(code=_WS_CLOSE_INTERNAL, reason=str(exc)[:120])
            except Exception:  # noqa: BLE001
                pass
        finally:
            if device_key is not None:
                try:
                    await MIRROR_REGISTRY.stop(device_key)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "mirror registry stop error key=%s: %s", device_key, exc
                    )

    # R6: register shutdown hooks so scrcpy subprocesses don't outlive the
    # FastAPI process. FastAPI's lifespan handler is the modern hook; we
    # also register an `atexit` fallback for uvicorn workers that bypass it.
    @app.on_event("shutdown")
    async def _shutdown_mirror_registry() -> None:
        data_sources.close()
        await MIRROR_REGISTRY.shutdown()

    def _atexit_shutdown() -> None:
        """Sync fallback for environments where the lifespan event doesn't fire.

        ``asyncio.run`` cannot safely be called from ``atexit`` (the loop is
        already closing), so we walk the registry and SIGTERM any still-live
        subprocesses directly. The Popen instances are the only real resource
        — the asyncio drain task gets cancelled by Python's interpreter
        shutdown sequence.
        """
        for session in list(MIRROR_REGISTRY._sessions.values()):  # noqa: SLF001
            proc = session._proc  # noqa: SLF001
            if proc is None:
                continue
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=1.0)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                pass

    atexit.register(_atexit_shutdown)

    if DIST_DIR.exists():
        app.mount("/", StaticFiles(directory=str(DIST_DIR), html=True), name="web")

    return app


def main() -> None:
    settings = get_settings()
    app = create_app()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
