"""Passive local task preview. Never owns a device or observation lease."""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
import json
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from driver.pool import FIXTURE_DRIVER_ID, LOCAL_DRIVER_ID, parse_device_key
from shared.schemas import TaskStatus

FRAME_HEADER = "X-ClickClick-Preview-Frame"
NO_STORE = {"Cache-Control": "no-store"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
WAITING = {"no_published_frame", "post_action_frame_pending"}


def preview_projection(task, *, settings, pool, enabled: bool, base_url: str) -> dict:
    """Static support only. Do not inspect provider, inventory or current pixels."""
    reason = ""
    if not enabled:
        reason = "preview_unavailable"
    elif settings.api_host not in LOOPBACK:
        reason = "unsupported_origin"
    elif pool.mode == "remote":
        reason = "unsupported_remote"
    elif pool.mode == "fixture":
        reason = "unsupported_fixture"
    elif pool.mode != "android":
        reason = "unsupported_platform"
    elif not task.device_serial:
        reason = "unsupported_device"
    else:
        owner, _ = parse_device_key(task.device_serial or "")
        if owner != LOCAL_DRIVER_ID:
            reason = "unsupported_fixture" if owner == FIXTURE_DRIVER_ID else "unsupported_remote"
    return {
        "preview_url": None if reason else f"{base_url}/tasks/{quote(task.id, safe='')}/preview",
        "preview_support": {"status": (
            "unavailable" if reason == "preview_unavailable" else
            "unsupported" if reason else "supported"
        ), "reason": reason},
    }


def safe_preview_projection(task, *, settings, pool, enabled, base_url) -> dict:
    try:
        projection = preview_projection(
            task, settings=settings, pool=pool, enabled=enabled, base_url=base_url,
        )
        # Validate enrichment separately, before handing the original payload to
        # the framework's serializer. This exception boundary owns no business I/O.
        json.dumps(projection, ensure_ascii=True, allow_nan=False)
        return projection
    except Exception:
        return {"preview_url": None, "preview_support": {
            "status": "unavailable", "reason": "preview_metadata_error",
        }}


@dataclass
class _Budget:
    state: deque = field(default_factory=deque)
    frame: deque = field(default_factory=deque)
    inflight: int = 0
    touched: float = 0.0


class PreviewBudget:
    """Request-driven bounded admission; no tasks, frame history or wait queues."""
    def __init__(self, *, clock=time.monotonic, capacity=128, idle_seconds=60.0):
        self.clock, self.capacity, self.idle_seconds = clock, capacity, idle_seconds
        self.entries: OrderedDict[str, _Budget] = OrderedDict()

    def admit(self, task_id: str, kind: str) -> _Budget | None:
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if not entry.inflight and now - entry.touched >= self.idle_seconds:
                del self.entries[key]
        entry = self.entries.get(task_id)
        if entry is None:
            if len(self.entries) >= self.capacity:
                victim = next((key for key, value in self.entries.items() if not value.inflight), None)
                if victim is None:
                    return None
                del self.entries[victim]
            entry = self.entries[task_id] = _Budget(touched=now)
        entry.touched = now
        self.entries.move_to_end(task_id)
        history = getattr(entry, kind)
        while history and now - history[0] >= 1.0:
            history.popleft()
        if len(history) >= (8 if kind == "state" else 4):
            return None
        if kind == "frame" and entry.inflight >= 2:
            return None
        history.append(now)
        if kind == "frame":
            entry.inflight += 1
        return entry

    def finished(self, task_id: str) -> None:
        self.entries.pop(task_id, None)

    def clear(self) -> None:
        self.entries.clear()


class PreviewImageResponse(Response):
    """Bound network sending independently of the task/provider lifetime."""
    send_timeout = 2.0

    def __init__(self, image: bytes, descriptor: str, budget: _Budget):
        super().__init__(image, media_type="image/png", headers={
            **NO_STORE, FRAME_HEADER: descriptor, "X-Content-Type-Options": "nosniff",
        })
        self.budget = budget

    async def __call__(self, scope, receive, send):
        async def disconnect():
            while (await receive())["type"] != "http.disconnect":
                pass

        sender = asyncio.create_task(super().__call__(scope, receive, send))
        listener = asyncio.create_task(disconnect())
        try:
            done, _ = await asyncio.wait(
                (sender, listener), timeout=self.send_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if sender in done:
                await sender
        except OSError:
            pass  # A disconnected viewer has no task cancellation authority.
        finally:
            for handle in (sender, listener):
                if not handle.done():
                    handle.cancel()
            try:
                await asyncio.gather(sender, listener, return_exceptions=True)
            finally:
                self.budget.inflight -= 1
                self.body = b""


class PreviewBoundary:
    def __init__(self, app, *, host: str, port: int, scheme: str):
        self.app = app
        self.enabled = host in LOOPBACK
        canonical_host = f"[{host}]" if ":" in host else host
        self.hosts = {f"{canonical_host}:{port}"}
        if port == (443 if scheme == "https" else 80):
            self.hosts.add(canonical_host)
        self.scheme = scheme

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        api = path.startswith("/api/tasks/") and "/preview/" in path
        page = path.startswith("/tasks/") and path.endswith("/preview")
        if scope["type"] != "http" or not (api or page):
            return await self.app(scope, receive, send)
        raw = scope["headers"]
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in raw}
        duplicate = any(sum(k.lower() == name for k, _ in raw) > 1 for name in (
            b"host", b"origin", b"sec-fetch-site", b"authorization",
        ))
        forbidden = (
            not self.enabled or duplicate or headers.get("host", "").lower() not in self.hosts
            or ("origin" in headers and headers["origin"] != f"{self.scheme}://{headers.get('host', '')}")
            or (api and headers.get("sec-fetch-site") != "same-origin")
        )
        if forbidden:
            return await JSONResponse(
                {"detail": "Local same-origin preview required"}, status_code=403, headers={
                    **NO_STORE, **({"Connection": "close"} if api and
                                  scope.get("http_version") in {"1.0", "1.1"} else {}),
                },
            )(scope, receive, send)

        async def guarded_send(message):
            if api and message["type"] == "http.response.start":
                message = {**message, "headers": [
                    (key, value) for key, value in message["headers"]
                    if key.lower() != b"cache-control" and not (
                        scope.get("http_version") in {"1.0", "1.1"} and key.lower() == b"connection"
                    )
                ] + [(b"cache-control", b"no-store")] + (
                    # Bound these optional HTTP/1 responses to one connection.
                    # Windows Proactor shutdown can strand reset keep-alive
                    # transports. Do not alter normal business connections or
                    # the Agent loop/subprocess policy to handle preview peers.
                    [(b"connection", b"close")] if scope.get("http_version") in {"1.0", "1.1"} else []
                )}
            if page and message["type"] == "http.response.start":
                message = {**message, "headers": list(message["headers"]) + [
                    (b"x-frame-options", b"DENY"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; "
                     b"style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
                     b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ]}
            await send(message)

        return await self.app(scope, receive, guarded_send)


class TaskPreview:
    def __init__(self, app, db, pool):
        self.app, self.db, self.pool = app, db, pool
        self.budget = PreviewBudget()

    def snapshot(self, task_id: str):
        # Polling needs status/device metadata, never Agent state/history parsing.
        task = self.db.task_preview_identity(task_id)
        if task is None:
            return None, None
        device_key = task["device_serial"]
        state = {"task_id": task_id, "device_key": device_key,
                 "task_status": task["status"], "preview_state": "unavailable",
                 "reason": "task_not_running", "frame": None}
        mode = self.pool.mode
        reason = (
            "unsupported_remote" if mode == "remote" else
            "unsupported_fixture" if mode == "fixture" else
            "unsupported_platform" if mode != "android" else ""
        )
        if reason:
            return {**state, "reason": reason}, None
        if not device_key:
            return {**state, "reason": "owner_unavailable"}, None
        owner, serial = parse_device_key(device_key or "")
        if owner != LOCAL_DRIVER_ID:
            return {**state, "reason": "unsupported_fixture" if owner == FIXTURE_DRIVER_ID
                    else "unsupported_remote"}, None
        if task["status"] != TaskStatus.RUNNING.value:
            return state, None
        worker = self.app.state.running_tasks.get(task_id)
        if worker is None or worker.done():
            return {**state, "reason": "owner_unavailable"}, None
        driver = self.pool.peek(device_key)
        if driver is None:
            return {**state, "reason": "owner_unavailable"}, None
        shot = driver.peek_task_preview(task_id, device_serial=serial)
        frame = shot["frame"]
        if frame is None:
            reason = shot["reason"]
            return {**state, "reason": reason,
                    "preview_state": "waiting" if reason in WAITING else "unavailable"}, None
        now = time.monotonic()
        descriptor = {
            "task_id": task_id, "device_key": device_key,
            "run_epoch": shot["run_epoch"], "provider_generation": frame.generation,
            "frame_id": frame.frame_id, "width": frame.geometry.stream_width,
            "height": frame.geometry.stream_height, "geometry": asdict(frame.geometry),
            "frame_timestamp": frame.timestamp, "frame_age_ms": max(0.0, (now-frame.timestamp)*1000),
            "observed_at": now,
        }
        # Validate metadata even for state so broken descriptors never escape to
        # FastAPI's serializer or get paired with unrelated PNG bytes.
        self.encode_descriptor(descriptor)
        return {**state, "preview_state": "available", "reason": "", "frame": descriptor}, frame

    @staticmethod
    def encode_descriptor(descriptor):
        encoded = json.dumps(descriptor, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 4096:
            raise ValueError("preview descriptor too large")
        return encoded


def mount_preview_api(app, *, db, pool, settings):
    service = TaskPreview(app, db, pool)
    router = APIRouter(prefix="/api/tasks")

    def error(code, reason):
        return JSONResponse({"reason": reason}, status_code=code, headers={
            **NO_STORE, **({"Retry-After": "1"} if code == 429 else {}),
        })

    @router.get("/{task_id}/preview/state")
    async def state(task_id: str):
        if service.budget.admit(task_id, "state") is None:
            return error(429, "preview_busy")
        try:
            payload, _ = service.snapshot(task_id)
            if payload is None:
                return error(404, "task_not_found")
            return JSONResponse(payload, headers=NO_STORE)
        except Exception:
            return JSONResponse({"task_id": task_id, "preview_state": "unavailable",
                                 "reason": "preview_internal_error", "frame": None}, headers=NO_STORE)

    @router.get("/{task_id}/preview/frame")
    async def image(task_id: str, request: Request):
        try:
            query = request.query_params
            epoch = query["run_epoch"]
            generation = int(query["generation"])
            after = int(query.get("after_frame_id", "-1"))
            if not epoch or len(epoch) > 128 or generation < 0 or after < -1:
                raise ValueError("invalid cursor")
        except (KeyError, ValueError):
            return error(400, "invalid_cursor")
        budget = service.budget.admit(task_id, "frame")
        if budget is None:
            return error(429, "preview_busy")
        handed_off = False
        try:
            payload, frame = service.snapshot(task_id)
            if payload is None:
                return error(404, "task_not_found")
            descriptor = payload["frame"]
            if frame is None:
                return error(409, payload["reason"])
            if descriptor["run_epoch"] != epoch or descriptor["provider_generation"] != generation:
                return error(409, "execution_changed")
            if after > frame.frame_id:
                return error(409, "invalid_cursor")
            encoded = service.encode_descriptor(descriptor)
            if after == frame.frame_id:
                return Response(status_code=204, headers={**NO_STORE, FRAME_HEADER: encoded})
            response = PreviewImageResponse(frame.data, encoded, budget)
            handed_off = True
            return response
        except Exception:
            return error(503, "preview_internal_error")
        finally:
            if not handed_off:
                budget.inflight -= 1

    app.include_router(router)
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    # Starlette normally constructs middleware lazily on the first request.
    # Initialize optional preview configuration here, inside create_app's rollback
    # boundary, so its constructor cannot break an unrelated first API request.
    boundary = PreviewBoundary(None, host=settings.api_host, port=settings.api_port, scheme=scheme)
    def attach_boundary(inner):
        boundary.app = inner
        return boundary
    app.add_middleware(attach_boundary)
    return service
