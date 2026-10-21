"""Isolated HTTP pressure client; never imports ClickClick or accesses a device.

Windows uses a Selector loop in this HTTP-only child, leaving the backend's
Proactor loop and asyncio subprocess support untouched. Parent closes a batch
by creating --stop-file; missing normal exit is a failed diagnostic.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

import httpx


async def run(args):
    counts = Counter()
    stopping = asyncio.Event()
    # Construct TLS/HTTP contexts before announcing readiness. Even clear-text
    # clients prepare SSL defaults; that setup is not preview request work.
    clients = [httpx.AsyncClient(base_url=args.url, trust_env=False, timeout=5,
                                headers={"Sec-Fetch-Site": "same-origin"})
               for _ in range(args.viewers)]

    async def viewer(index):
        client = clients[index]
        while not stopping.is_set():
            try:
                # Response Connection: close performs actual socket reconnects;
                # rebuilding an HTTPX/SSL context for each refresh is unnecessary.
                if not client.is_closed:
                    cursor = -1
                    for _ in range(3 if args.slow else 1000000):
                        if stopping.is_set():
                            break
                        params = None
                        path = args.path
                        if args.task_id:
                            state = await client.get(f"/api/tasks/{args.task_id}/preview/state")
                            counts[f"state_{state.status_code}"] += 1
                            frame = state.json().get("frame") if state.status_code == 200 else None
                            if not frame:
                                await asyncio.sleep(args.interval)
                                continue
                            path = f"/api/tasks/{args.task_id}/preview/frame"
                            params = {"run_epoch": frame["run_epoch"],
                                      "generation": frame["provider_generation"], "after_frame_id": cursor}
                        async with client.stream("GET", path, params=params) as response:
                            counts[f"frame_{response.status_code}"] += 1
                            if args.slow and index % 2:
                                await asyncio.sleep(args.read_delay)
                            else:
                                await response.aread()
                                if args.task_id and response.status_code == 200:
                                    cursor = json.loads(response.headers["X-ClickClick-Preview-Frame"])["frame_id"]
                        await asyncio.sleep(args.interval)
            except (httpx.HTTPError, ValueError, KeyError):
                counts["viewer_error"] += 1
                await asyncio.sleep(1)

    handles = [asyncio.create_task(viewer(i)) for i in range(args.viewers)]
    print(json.dumps({"kind": "client_ready", "pid": os.getpid(),
                      "loop": type(asyncio.get_running_loop()).__name__, "viewers": args.viewers}), flush=True)
    began = time.monotonic()
    try:
        while time.monotonic() - began < args.duration and not Path(args.stop_file).exists():
            await asyncio.sleep(.05)
    finally:
        # Finish an in-progress HTTPX connect/context exit before stopping.
        # Cancelling inside AnyIO connect races can leave its child tasks alive.
        stopping.set()
        done, pending = await asyncio.wait(handles, timeout=8)
        for handle in done:
            if not handle.cancelled() and handle.exception():
                raise handle.exception()
        print(json.dumps({"kind": "client_cleanup", "complete": not pending,
                          "http_counts": dict(counts), "elapsed_seconds": time.monotonic()-began,
                          "cpu_seconds": time.process_time()}), flush=True)
        if pending:
            # Do not let Runner's cancellation hide an incomplete client batch.
            sys.stdout.flush()
            os._exit(2)
        await asyncio.gather(*(client.aclose() for client in clients))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--task-id")
    parser.add_argument("--path", default="/image")
    parser.add_argument("--viewers", type=int, default=10)
    parser.add_argument("--slow", action="store_true")
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--duration", type=float, default=3600)
    parser.add_argument("--interval", type=float, default=.5)
    parser.add_argument("--read-delay", type=float, default=.7)
    args = parser.parse_args()
    # This choice is confined to an HTTP-only child process, never the Agent.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None) as runner:
        runner.run(run(args))


if __name__ == "__main__":
    main()
