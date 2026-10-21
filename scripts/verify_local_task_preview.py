"""Local Android observation/preview acceptance, without models or device inputs.

Run from the repository root with its Python environment. Uses an isolated
database and loopback backend, never production configuration or task fixtures.
Output contains timing/resource metadata, not screenshots or UI tree contents.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import ctypes
from ctypes import wintypes
import gc
import inspect
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn

from driver import adb
from control_api.preview import PreviewImageResponse
from driver.scrcpy_observation import FrameHandle
from driver.scrcpy_observation import PyAVDecoder
from driver.scrcpy_stream import LocalStreamSource, REGISTRY, ScrcpySession
from shared.config import Settings
from shared.schemas import TaskStatus


def resources():
    """Current-process working set on Windows, without an extra dependency."""
    class Memory(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged",
                "pagefile", "peak_pagefile", "private",
            )]
    memory = Memory()
    memory.cb = ctypes.sizeof(memory)
    ok = False
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        ok = query(kernel.GetCurrentProcess(), ctypes.byref(memory), memory.cb)
    return {"cpu_seconds": time.process_time(), "rss_bytes": memory.rss if ok else None,
            "asyncio_tasks": len(asyncio.all_tasks())}


def percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values)*percent/100)-1)] if values else None


async def run(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    log = (output / "measurements.jsonl").open("w", encoding="utf-8", buffering=1)
    def record(kind, **values):
        log.write(json.dumps({"kind": kind, "time": time.time(), **values}, allow_nan=False)+"\n")

    settings = Settings(
        _env_file=None, data_dir=output / "backend", api_host="127.0.0.1", api_port=args.port,
        api_ssl_certfile="", api_ssl_keyfile="", driver_url="", driver_urls_json="",
        use_fixture_driver=False, platform="android", ime_auto_setup=False,
        models_json="{}", mcp_http_enabled=False,
    )
    import control_api.main as api
    api.get_settings = lambda: settings
    app = api.create_app(include_temp_runs=False)
    counts = Counter()
    # Transparent counters; no altered sampling, timeout, quality or result.
    def count_method(obj, name, label):
        counts.setdefault(label, 0)
        original = getattr(obj, name)
        if inspect.iscoroutinefunction(original):
            async def wrapped(*a, **kw):
                counts[label] += 1
                return await original(*a, **kw)
        else:
            def wrapped(*a, **kw):
                counts[label] += 1
                return original(*a, **kw)
        setattr(obj, name, wrapped)
    for obj, name, label in (
        (PyAVDecoder, "__init__", "decoder_instances"),
        (LocalStreamSource, "__init__", "source_instances"),
        (LocalStreamSource, "start", "source_start"),
        (LocalStreamSource, "stop", "source_stop"),
        (LocalStreamSource, "reset_video", "source_reset_video"),
        (ScrcpySession, "frames", "encoded_subscriptions"),
    ):
        count_method(obj, name, label)
    driver = app.state.driver_pool.get(args.serial)
    provider = driver._stream_provider
    for obj, name, label in (
        (REGISTRY, "acquire", "lease_acquire"), (REGISTRY, "release", "lease_release"),
        (provider, "_publish_pending", "forced_publication"),
        (driver._collector, "fetch_primary", "tree_capture"),
        (adb, "screencap_async", "adb_screenshot"),
        (adb, "uiautomator_dump_async", "adb_tree"),
    ):
        if hasattr(obj, name):
            count_method(obj, name, label)
    audit_seen = set()
    def audit_middleware(inner):
        async def audit(scope, receive, send):
            path = scope.get("path", "")
            async def audit_send(message):
                if scope["type"] == "http" and "/preview/" in path and message["type"] == "http.response.start":
                    headers = dict(scope["headers"])
                    browser = headers.get(b"user-agent", b"").startswith(b"Mozilla/")
                    key = (browser, path.rsplit("/", 1)[-1], message["status"])
                    if key not in audit_seen and len(audit_seen) < 24:
                        audit_seen.add(key)
                        record("http_boundary", browser=browser, endpoint=key[1], status=key[2],
                               fetch_site=headers.get(b"sec-fetch-site", b"").decode("ascii", errors="replace"),
                               canonical_host=headers.get(b"host") == f"127.0.0.1:{args.port}".encode(),
                               origin_present=b"origin" in headers)
                await send(message)
            await inner(scope, receive, audit_send)
        return audit
    app.add_middleware(audit_middleware)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                          lifespan="off", log_level="error",
                                          timeout_graceful_shutdown=5))
    server_task = asyncio.create_task(server.serve())
    active_worker = asyncio.create_task(asyncio.Event().wait())
    current = None
    samples = []
    preview_counts = Counter()
    client_batches = 0
    client_failures = 0
    active_clients = {}
    record("protocol", python=platform.python_version(), serial=args.serial,
           backend_pid=os.getpid(), backend_loop=type(asyncio.get_running_loop()).__name__,
           client_isolation="HTTP-only child process, cooperative stop",
           pairs=5, observations_per_arm=30, warmup=5, loads=[1, 2, 10],
           budget="max(20ms, baseline p95 * 0.05)", soak_seconds=args.soak_seconds,
           observation="AndroidDriver.get_frame; no LLM, no input, existing foreground screen",
           scope="real driver/HTTP acceptance; deterministic runtime is tested separately",
           versions={"httpx": httpx.__version__, "uvicorn": uvicorn.__version__},
           source_hashes={name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in (
               "control_api/preview.py", "control_api/main.py", "driver/android.py",
               "driver/scrcpy_observation.py", "driver/pool.py", "shared/db.py",
               "web/src/lib/taskPreview.ts", "web/src/views/TaskPreviewView.tsx",
               "scripts/verify_local_task_preview.py", "scripts/preview_http_client.py")})

    async def observation(kind, **labels):
        before = time.perf_counter()
        try:
            tree, image = await driver.get_frame()
            meta = tree.get("_capture", {})
            value = {"ms": (time.perf_counter()-before)*1000,
                     "pixel_provider": meta.get("pixel_provider"),
                     "fallback": bool(meta.get("fallback_edges")) or meta.get("pixel_provider") != "scrcpy",
                     "timeout": any(s.get("timed_out") for s in meta.get("stage_timings", [])),
                     "png_bytes": len(image), "geometry": meta.get("image_geometry")}
        except Exception as exc:
            value = {"ms": (time.perf_counter()-before)*1000, "error": type(exc).__name__,
                     "timeout": isinstance(exc, TimeoutError), "fallback": True}
        record(kind, **labels, **value)
        return value

    async def start_viewers(load, slow=False):
        nonlocal client_batches
        client_batches += 1
        stop = output / f"client-{client_batches}.stop"
        command = [sys.executable, str(Path(__file__).with_name("preview_http_client.py")),
                   "--url", f"http://127.0.0.1:{args.port}", "--task-id", current.id,
                   "--viewers", str(load), "--stop-file", str(stop)]
        if slow:
            command.append("--slow")
        proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        active_clients[proc.pid] = (proc, stop)
        ready = json.loads(await asyncio.wait_for(proc.stdout.readline(), 10))
        record("client_started", batch=client_batches, client=ready)
        return proc, stop

    async def stop_viewers(handle):
        nonlocal client_failures
        if handle:
            proc, stop = handle
            stop.touch()
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 10)
            except TimeoutError:
                client_failures += 1
                record("client_shutdown_failure", pid=proc.pid)
                proc.kill()
                await proc.communicate()
                raise RuntimeError("isolated HTTP client failed to exit") from None
            finally:
                active_clients.pop(proc.pid, None)
            rows = [json.loads(line) for line in stdout.decode().splitlines()]
            cleanup = next((r for r in rows if r["kind"] == "client_cleanup"), {})
            record("client_stopped", pid=proc.pid, exit_code=proc.returncode,
                   cleanup=cleanup, stderr=stderr.decode(errors="replace")[:4096])
            if proc.returncode or not cleanup.get("complete") or stderr:
                client_failures += 1
                raise RuntimeError("isolated HTTP client cleanup failed")
            preview_counts.update(cleanup["http_counts"])
        await asyncio.sleep(2.1)  # Let bounded HTTP sends finish before inspecting references.

    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("backend did not start")
            await asyncio.sleep(.05)
        current = app.state.db.create_task("Local preview observation acceptance (no model/input)",
                                           device_serial=args.serial)
        app.state.db.update_task(current.id, status=TaskStatus.RUNNING)
        app.state.running_tasks[current.id] = active_worker
        await driver.begin_task_session(current.id)
        try:
            await driver.warm_observation_provider()
        except TimeoutError:
            record("cold_start_timeout")  # Keep the original startup budget.
        for index in range(5):
            await observation("warmup", index=index)
        session = provider._session
        if not session or not provider.peek_published_frame().frame:
            raise RuntimeError("normal observation did not publish a usable frame")
        record("ready", task_id=current.id,
               preview_url=f"http://127.0.0.1:{args.port}/tasks/{current.id}/preview",
               source=session.metrics(), resources=resources())
        print(f"PREVIEW http://127.0.0.1:{args.port}/tasks/{current.id}/preview", flush=True)
        for load in (1, 2, 10):
            for pair in range(5):
                # Alternate the first arm to reduce one-direction time drift.
                for enabled in ((False, True) if pair % 2 == 0 else (True, False)):
                    handles = await start_viewers(load, slow=load == 10) if enabled else None
                    before = dict(counts)
                    http_before = dict(preview_counts)
                    resource_before = resources()
                    arm = [await observation("observation", load=load, pair=pair, enabled=enabled,
                                             index=index) for index in range(30)]
                    await stop_viewers(handles)
                    summary = {"load": load, "pair": pair, "enabled": enabled,
                               "p50_ms": percentile([v["ms"] for v in arm], 50),
                               "p95_ms": percentile([v["ms"] for v in arm], 95),
                               "timeouts": sum(v["timeout"] for v in arm),
                               "fallbacks": sum(v["fallback"] for v in arm),
                               "counts": {k: counts[k]-before.get(k, 0) for k in counts},
                               "http_counts": {k: preview_counts[k]-http_before.get(k, 0) for k in preview_counts},
                               "source": session.metrics(), "resources_before": resource_before,
                               "resources_after": resources()}
                    samples.append(summary)
                    record("arm", **summary)
                print(f"PAIR load={load} pair={pair+1}/5", flush=True)
        comparisons = []
        for load in (1, 2, 10):
            off = [r["p95_ms"] for r in samples if r["load"] == load and not r["enabled"]]
            on = [r["p95_ms"] for r in samples if r["load"] == load and r["enabled"]]
            for pair, (baseline, preview) in enumerate(zip(off, on)):
                comparisons.append({"load": load, "pair": pair, "baseline_p95_ms": baseline,
                                    "preview_p95_ms": preview, "increment_ms": preview-baseline,
                                    "budget_ms": max(20, baseline*.05),
                                    "within_budget": preview-baseline <= max(20, baseline*.05)})
        checks = dict(
               all_within_budget=all(v["within_budget"] for v in comparisons),
               all_observations_healthy=all(not r["timeouts"] and not r["fallbacks"] for r in samples),
               every_preview_arm_delivered_png=all(r["http_counts"].get("frame_200", 0) > 0
                                                   for r in samples if r["enabled"]),
               acquisition_counts_unchanged=all(not r["counts"].get(key, 0) for r in samples for key in (
                   "decoder_instances", "source_instances", "source_start", "source_stop",
                   "lease_acquire", "lease_release", "encoded_subscriptions")))
        record("paired_result", comparisons=comparisons, **checks)
        soak_seconds = args.soak_seconds
        if args.stop_on_performance_failure and not all(checks.values()):
            record("soak_skipped", reason="paired_acceptance_failed", checks=checks)
            soak_seconds = 0
        began = time.monotonic()
        while time.monotonic()-began < soak_seconds:
            handles = await start_viewers(10, slow=True)
            await observation("soak_observation")
            await asyncio.sleep(min(8, max(0, soak_seconds-(time.monotonic()-began))))
            await stop_viewers(handles)
            service = app.state.task_preview
            record("soak", elapsed_seconds=time.monotonic()-began, resources=resources(),
                   source=session.metrics(), ring_frames=len(provider.ring._frames),
                   ring_bytes=provider.ring._bytes, budgets=len(service.budget.entries),
                   inflight=sum(b.inflight for b in service.budget.entries.values()),
                   http_counts=dict(preview_counts), counts=dict(counts))
        print("UI_REVIEW: paired/soak traffic stopped; browser may open now", flush=True)
        await asyncio.sleep(args.ui_review_seconds)
        # Keep the actual browser task lifecycle independently observable.
        app.state.db.update_task(current.id, status=TaskStatus.SUCCEEDED)
        await driver.end_task_session(current.id)
        record("task_stopped", task_id=current.id)
        await asyncio.sleep(3)
        previous = current
        current = app.state.db.create_task("New local preview ownership acceptance", device_serial=args.serial)
        app.state.db.update_task(current.id, status=TaskStatus.RUNNING)
        app.state.running_tasks[current.id] = active_worker
        app.state.running_tasks.pop(previous.id, None)
        await driver.begin_task_session(current.id)
        await observation("new_task_observation")
        record("new_task", task_id=current.id, previous_task_id=previous.id,
               preview_url=f"http://127.0.0.1:{args.port}/tasks/{current.id}/preview")
        # Bounded review window; no background automation is installed.
        await asyncio.sleep(args.review_seconds)
    finally:
        for proc, stop in list(active_clients.values()):
            try:
                await stop_viewers((proc, stop))
            except Exception as exc:
                record("client_cleanup_error", error=type(exc).__name__)
        active_worker.cancel()
        await asyncio.gather(active_worker, return_exceptions=True)
        if current is not None:
            identity = app.state.db.task_preview_identity(current.id)
            if identity and identity["status"] == TaskStatus.RUNNING.value:
                app.state.db.update_task(current.id, status=TaskStatus.CANCELLED,
                                         failure_reason="observation_only_acceptance_cleanup")
        await driver.close_observation_provider()
        await REGISTRY.shutdown()
        app.state.task_preview.budget.clear()
        app.state.running_tasks.clear()
        server.should_exit = True
        server_closed = True
        try:
            # Bound the diagnostic itself. Some Windows Proactor close failures
            # can strand asyncio.Server.wait_closed before Uvicorn's own fuse.
            async with asyncio.timeout(8):
                await server_task
        except TimeoutError:
            server_closed = False
            record("shutdown_failure", reason="isolated_server_wait_closed_timeout")
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        gc.collect()
        live_responses = [o for o in gc.get_objects() if isinstance(o, PreviewImageResponse)]
        record("cleanup", resources=resources(), sessions=len(REGISTRY._sessions),
               server_active_connections=[getattr(s, "_active_count", None) for s in server.servers],
               client_processes=len(active_clients),
               client_failures=client_failures,
               provider_task=provider._task is not None, lease=provider._lease is not None,
               preview_budgets=len(app.state.task_preview.budget.entries),
               running_tasks=len(app.state.running_tasks), counts=dict(counts),
               live_preview_responses=len(live_responses),
               live_preview_response_bytes=sum(len(o.body) for o in live_responses),
               frame_handles_live=sum(isinstance(o, FrameHandle) for o in gc.get_objects()),
               provider_ring_frames=len(provider.ring._frames))
        record("acceptance_cleanup_result", server_closed=server_closed,
               cleanup_complete=server_closed and not live_responses and not active_clients and not client_failures
               and not any(getattr(s, "_active_count", 0) for s in server.servers)
               and not REGISTRY._sessions and provider._task is None and provider._lease is None
               and not app.state.task_preview.budget.entries and not app.state.running_tasks)
        app.state.db.close()
        log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", required=True, help="New isolated output directory")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--soak-seconds", type=int, default=1800)
    parser.add_argument("--review-seconds", type=int, default=120)
    parser.add_argument("--ui-review-seconds", type=int, default=0)
    parser.add_argument("--stop-on-performance-failure", action="store_true",
                        help="Keep acceptance failed and skip soak if any paired criterion fails")
    asyncio.run(run(parser.parse_args()))
