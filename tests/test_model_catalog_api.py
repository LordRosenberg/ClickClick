"""Model catalog, ChatGPT login API, and per-task model overrides."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent.executor import Executor
from agent.prompts import render_executor_system, render_planner_system
from shared.chatgpt_auth import PendingDeviceLogin
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.model_catalog import build_model_catalog
from shared.schemas import AgentState, ExecutorStepSubmit
from tests.fake_agents import FakeExecutor


def test_build_model_catalog_strips_secrets(monkeypatch):
    monkeypatch.setenv(
        "CLICKCLICK_MODELS_JSON",
        json.dumps({
            "openai/gpt-4o": {
                "provider": "openai",
                "api_key": "sk-secret",
                "base_url": "https://relay/v1",
                "stream": True,
            },
            "chatgpt/gpt-5.4": {"provider": "chatgpt"},
        }),
    )
    monkeypatch.setenv("CLICKCLICK_DEFAULT_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("CLICKCLICK_MANAGER_MODEL", "chatgpt/gpt-5.4")
    monkeypatch.setenv("CLICKCLICK_EXECUTOR_MODEL", "")
    catalog = build_model_catalog(Settings())
    blob = json.dumps(catalog)
    assert "sk-secret" not in blob
    assert "api_key" not in blob
    ids = {m["id"] for m in catalog["models"]}
    assert ids == {"openai/gpt-4o", "chatgpt/gpt-5.4"}
    chatgpt = next(m for m in catalog["models"] if m["id"].startswith("chatgpt/"))
    assert chatgpt["is_chatgpt"] is True
    relay = next(m for m in catalog["models"] if m["id"].startswith("openai/"))
    assert relay["stream"] is True
    assert chatgpt["stream"] is False
    assert catalog["roles"]["planner"] == "chatgpt/gpt-5.4"
    assert catalog["roles"]["reviewer"] == "chatgpt/gpt-5.4"
    assert catalog["roles"]["executor"] == "openai/gpt-4o"


def test_agent_state_model_override_assignment():
    state = AgentState(
        instruction="x",
        manager_model="chatgpt/gpt-5.4",
        executor_model="openai/gpt-4o",
    )
    manager_model = "default-m"
    executor_model = "default-e"
    if state.manager_model:
        manager_model = state.manager_model
    if state.executor_model:
        executor_model = state.executor_model
    assert manager_model == "chatgpt/gpt-5.4"
    assert executor_model == "openai/gpt-4o"


def test_poll_treats_403_as_pending(monkeypatch, tmp_path):
    """OpenAI deviceauth returns 403 while waiting for the user to enter the code."""
    from shared.chatgpt_auth import PendingDeviceLogin, poll_device_login
    from shared.config import Settings

    class _Resp:
        status_code = 403

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr("shared.chatgpt_auth.httpx.Client", _Client)
    monkeypatch.setattr(
        "shared.chatgpt_auth._authenticator",
        lambda: type("A", (), {"auth_file": str(tmp_path / "a.json")})(),
    )
    pending = PendingDeviceLogin(
        device_auth_id="dev",
        user_code="WXYZ",
        interval=5,
        started_at=__import__("time").time(),
    )
    status, body, next_pending = poll_device_login(
        Settings(chatgpt_token_dir=str(tmp_path)), pending,
    )
    assert status == "pending"
    assert body["status"] == "pending"
    assert next_pending is pending
