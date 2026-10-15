"""Foreground terminal answering on the same backend namespace as Console."""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

from control_api.mcp_launcher import ensure_backend, local_url
from control_api.mcp_http import local_token


async def run_shared(instruction, settings, *, answer_surface="terminal"):
    host = settings.api_host if settings.api_host in {"127.0.0.1", "localhost", "::1"} else "127.0.0.1"
    host = "[::1]" if host == "::1" else host
    scheme = "https" if settings.api_ssl_certfile and settings.api_ssl_keyfile else "http"
    url = local_url(f"{scheme}://{host}:{settings.api_port}")
    verify = True
    if scheme == "https":
        import ssl
        verify = ssl.create_default_context(cafile=settings.api_ssl_certfile)
    await asyncio.to_thread(ensure_backend, url, Path.cwd(), settings.data_dir.resolve(), verify=verify)
    headers = {"Authorization": "Bearer " + local_token(settings)}
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, verify=verify, timeout=40) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        interactive = answer_surface == "terminal" and sys.stdin.isatty() and sys.stdout.isatty()
        if interactive and os.name != "nt":
            try:
                interactive = os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp()
            except (OSError, ValueError):
                interactive = False
        binding = None
        listener = None
        events = asyncio.Queue()
        connected = asyncio.Event()
        if interactive:
            binding = (await request("POST", "/api/hitl/bindings", json={"owner": "cli"}))["binding"]

            async def listen():
                try:
                    async with client.stream("GET", "/api/hitl/events", headers={"X-HITL-Binding": binding}, timeout=None) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line.startswith(": connected"):
                                connected.set()
                            elif line.startswith("data: "):
                                await events.put(json.loads(line[6:]))
                finally:
                    connected.set()

            listener = asyncio.create_task(listen())
            try:
                await asyncio.wait_for(connected.wait(), 10)
                if listener.done():
                    await listener
                    raise RuntimeError("No live terminal reply channel; use --answers console")
            except BaseException:
                try:
                    await request("DELETE", "/api/hitl/bindings/" + binding)
                finally:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
                raise
        task_id = None
        try:
            submission = {
                "instruction": instruction, "request_key": uuid.uuid4().hex,
                "submission_origin": "cli", "hitl_enabled": True,
                **({"reply_binding": binding} if binding else {}),
            }
            for attempt in range(2):
                try:
                    created = await request("POST", "/api/assistant/tasks", json=submission)
                    break
                except httpx.RequestError:
                    if attempt:
                        raise RuntimeError("Submission outcome unknown; inspect Console before resubmitting: " + url)
            task = created["tasks"][0]
            task_id = task["task_id"]
            print(task_id)
            print("回答入口 / Answer: " + task["console_url"], flush=True)
            if not interactive:
                return task_id
            while task["status"] not in {"succeeded", "failed", "cancelled", "paused"}:
                if task["status"] == "waiting_for_input":
                    view = (await request("GET", f"/api/hitl/tasks/{task_id}", headers={"X-HITL-Binding": binding}))["interaction"]
                    if not view or not view["can_answer"]:
                        print("请在控制台回答 / Answer in Console: " + task["console_url"])
                        return task_id
                    async def presented():
                        await request("POST", f"/api/hitl/tasks/{task_id}/present", headers={"X-HITL-Binding": binding},
                                      json={"interaction_id": view["interaction_id"], "owner_version": view["owner_version"]})
                    try:
                        outcome, answers = await terminal_answers(view, on_present=presented)
                    except (EOFError, KeyboardInterrupt):
                        print("终端已离开；请在控制台回答 / Answer in Console: " + task["console_url"])
                        return task_id
                    await request("POST", f"/api/hitl/tasks/{task_id}/reply", headers={"X-HITL-Binding": binding}, json={
                        "interaction_id": view["interaction_id"], "owner_version": view["owner_version"],
                        "response_id": uuid.uuid4().hex, "outcome": outcome, "answers": answers,
                    })
                task = await request("GET", f"/api/assistant/tasks/{task_id}", params={"wait_seconds": 2})
                await asyncio.sleep(.1)
            print(task.get("result") or task.get("failure_reason") or task["status"])
            return task_id
        finally:
            try:
                if binding:
                    await request("DELETE", "/api/hitl/bindings/" + binding)
            finally:
                if listener:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)


async def terminal_answers(view, *, on_present=None):
    answers = []
    for question in view["questions"]:
        print("\n" + question["question"], flush=True)
        if question.get("reason"):
            print(question["reason"])
        if view["purpose"] == "confirmation":
            if on_present:
                await on_present()
                on_present = None
            while True:
                text = (await asyncio.to_thread(input, "确认这次操作？yes / no / ? 无法回答 / cancel 取消任务: ")).strip().lower()
                if text in {"yes", "no", "?", "cancel"}:
                    return {"yes": "confirmed", "no": "declined", "?": "unable_to_answer", "cancel": "cancel_task"}[text], []
        options = question.get("options", [])
        for index, option in enumerate(options, 1):
            print(f"  {index}. {option['label']}" + (" — " + option["description"] if option.get("description") else ""))
        if on_present:
            await on_present()
            on_present = None
        while True:
            text = (await asyncio.to_thread(input, "选择编号或输入回答；? 无法回答；cancel 取消任务: ")).strip()
            if text in {"?", "cancel"}:
                return ("unable_to_answer" if text == "?" else "cancel_task"), []
            if not text or len(text) > 4096:
                continue
            answer = {"question_id": question["id"], "text": text}
            if text.isdigit() and 1 <= int(text) <= len(options):
                option = options[int(text) - 1]
                answer.update(text=option["label"], option_id=option["id"])
            answers.append(answer)
            break
    return "answered", answers
