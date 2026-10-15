"""CLI uses the shared authenticated namespace and explicitly answers or detaches."""

import asyncio
import json
import os

import httpx
import pytest

from agent.hitl_cli import run_shared
from shared.config import Settings


@pytest.mark.parametrize("surface,terminal,answer", [("terminal", True, "1"), ("console", True, None), ("terminal", False, None), ("terminal", True, "EOF")])
async def test_cli_shared_origin_answers_and_console_fallback(tmp_path, monkeypatch, capsys, surface, terminal, answer):
    calls = []
    state = {"subscribed": False, "status": "waiting_for_input"}
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            state["subscribed"] = True
            yield b": connected\n\n"
            while True:
                await asyncio.sleep(10)

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        assert request.headers.get("Authorization", "").startswith("Bearer ")
        path = request.url.path
        if path == "/api/hitl/bindings":
            return httpx.Response(200, json={"binding": "B" * 43})
        if path == "/api/hitl/events":
            return httpx.Response(200, stream=Events())
        if path == "/api/assistant/tasks":
            body = json.loads(request.content)
            assert body["submission_origin"] == "cli" and body["hitl_enabled"]
            if "reply_binding" in body:
                assert state["subscribed"]
            return httpx.Response(200, json={"tasks": [{"task_id": "T", "status": state["status"], "console_url": "http://127.0.0.1:8080/tasks/T"}]})
        if path == "/api/hitl/tasks/T":
            return httpx.Response(200, json={"interaction": {"interaction_id": "I", "owner_version": 1, "can_answer": True,
                    "purpose": "clarification", "questions": [{"id": "q", "question": "Which person?", "options": [{"id": "a", "label": "Alice"}, {"id": "b", "label": "Bob"}]}]}})
        if path.endswith("/reply"):
            assert json.loads(request.content)["answers"] == [{"question_id": "q", "text": "Alice", "option_id": "a"}]
            state["status"] = "succeeded"
        if path == "/api/assistant/tasks/T":
            return httpx.Response(200, json={"task_id": "T", "status": state["status"], "result": "done"})
        return httpx.Response(200, json={})

    monkeypatch.setattr("agent.hitl_cli.ensure_backend", lambda *args, **kwargs: None)
    original = httpx.AsyncClient
    monkeypatch.setattr("agent.hitl_cli.httpx.AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    monkeypatch.setattr("agent.hitl_cli.sys.stdin.isatty", lambda: terminal)
    monkeypatch.setattr("agent.hitl_cli.sys.stdout.isatty", lambda: terminal)
    if os.name != "nt":
        monkeypatch.setattr("agent.hitl_cli.sys.stdin.fileno", lambda: 0)
        monkeypatch.setattr("agent.hitl_cli.os.tcgetpgrp", lambda fd: os.getpgrp())
    def input_(prompt):
        if answer == "EOF":
            raise EOFError
        assert answer is not None
        return answer
    monkeypatch.setattr("builtins.input", input_)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert await run_shared("goal", settings, answer_surface=surface) == "T"
    paths = [path for _, path, _ in calls]
    assert "/api/assistant/tasks" in paths
    assert "http://127.0.0.1:8080/tasks/T" in capsys.readouterr().out
    if terminal and surface == "terminal":
        assert "/api/hitl/bindings/" + "B" * 43 in paths
        assert ("/api/hitl/tasks/T/reply" in paths) == (answer != "EOF")
    else:
        assert "/api/hitl/bindings" not in paths
    assert all(not path.endswith("/cancel") for path in paths)
