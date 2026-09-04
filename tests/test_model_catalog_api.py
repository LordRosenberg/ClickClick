"""Model catalog, ChatGPT login API, and per-task model overrides."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent.executor import Executor
from agent.planner import Planner
from agent.reviewer import Reviewer
from agent.prompts import render_executor_system, render_planner_system
from shared.chatgpt_auth import PendingDeviceLogin
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.model_catalog import build_model_catalog
from shared.schemas import (
    AgentState,
    ExecutorStepSubmit,
    SubgoalContractBody,
    PlannerDecision,
    PlannerMode,
    ReviewerDecision,
    ReviewerVerdict,
)
from fake_agents import FakeExecutor, FakePlanner, FakeReviewer, fake_task_scope


def _execute() -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal="verify the requested task",
        completion_contract=SubgoalContractBody(
            success_conditions=["the requested task is verified"],
        ),
        plan=["verify the requested task"],
        target_requirement_ref="final_ui_state:1",
    )


def _done() -> ReviewerDecision:
    return ReviewerDecision(
        verdict=ReviewerVerdict.DONE,
        reason="the task scope is satisfied",
        evidence_handles=["current"],
        packet_digest="model-test-packet"
    )


def test_build_model_catalog_strips_secrets(monkeypatch):
    monkeypatch.setenv(
        "CLICKCLICK_MODELS_JSON",
        json.dumps({
            "openai/gpt-4o": {
                "provider": "openai",
                "api_key": "sk-secret",
                "base_url": "https://relay/v1",
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


@pytest.mark.asyncio
async def test_role_model_override_reaches_actual_llm_round(monkeypatch):
    requested_models: list[str] = []

    async def fake_complete(model, messages, **kwargs):
        del messages
        requested_models.append(model)
        tool_names = {
            tool["function"]["name"] for tool in kwargs["tools"]
        }
        if "submit_planner_decision" in tool_names:
            payload = {
                "mode": "execute",
                "target_requirement_ref": "final_ui_state:1",
                "next_subgoal": "verify the requested task",
                "completion_contract": {
                    "success_conditions": ["the requested task is verified"],
                    "disqualifying_clauses": [],
                },
                "plan": ["verify the requested task"],
            }
            tool_name = "submit_planner_decision"
        elif "submit_reviewer_scope" in tool_names:
            payload = {
                "final_ui_state": ["the requested task is complete"],
                "disqualifying_clauses": [],
            }
            tool_name = "submit_reviewer_scope"
        else:
            payload = {
                "decision": "act",
                "summary": "Wait briefly",
                "action": {"type": "sleep", "duration_ms": 100},
            }
            tool_name = "submit_executor_step"
        return GatewayResponse(
            content="",
            model=model,
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id=f"call-{len(requested_models)}",
                name=tool_name,
                arguments=json.dumps(payload),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)

    planner = Planner(model="factory-planner")
    planner.set_model("chatgpt/decision-override")
    planner._runner.session.set_stable_system(render_planner_system())  # noqa: SLF001
    await planner._runner.session.run(  # noqa: SLF001
        [{"role": "user", "content": "current screen"}],
    )

    reviewer = Reviewer(model="factory-reviewer")
    reviewer.set_model("chatgpt/decision-override")
    await reviewer.author_task_scope(AgentState(instruction="verify the requested task"))

    executor = Executor(driver=None, model="factory-executor")  # type: ignore[arg-type]
    executor.set_model("openai/executor-override")
    executor._session.set_stable_system(render_executor_system())  # noqa: SLF001
    await executor._session.run([{"role": "user", "content": "current screen"}])  # noqa: SLF001

    assert requested_models == [
        "chatgpt/decision-override",
        "chatgpt/decision-override",
        "openai/executor-override",
    ]


@pytest.mark.asyncio
async def test_api_models_and_task_model_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv(
        "CLICKCLICK_MODELS_JSON",
        json.dumps({
            "openai/a": {"provider": "openai", "api_key": "sk-a"},
            "chatgpt/b": {"provider": "chatgpt"},
        }),
    )
    monkeypatch.setenv("CLICKCLICK_DEFAULT_MODEL", "openai/a")

    from control_api.main import create_app

    planner = FakePlanner([_execute()])
    reviewer = FakeReviewer([_done()], task_scope=fake_task_scope())
    exe = FakeExecutor([])
    exe.model = "factory-executor"
    app = create_app(
        planner_factory=lambda: planner,
        reviewer_factory=lambda: reviewer,
        executor_factory=lambda: exe,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        catalog = await client.get("/api/models")
        assert catalog.status_code == 200
        body = catalog.json()
        assert "sk-a" not in json.dumps(body)
        assert {m["id"] for m in body["models"]} == {"openai/a", "chatgpt/b"}

        bad = await client.post(
            "/api/tasks",
            json={
                "instruction": "x",
                "device_serials": ["fixture"],
                "manager_model": "missing/model",
            },
        )
        assert bad.status_code == 400

        created = await client.post(
            "/api/tasks",
            json={
                "instruction": "hi",
                "device_serials": ["fixture"],
                "manager_model": "chatgpt/b",
                "executor_model": "openai/a",
            },
        )
        assert created.status_code == 200
        tid = created.json()["tasks"][0]["id"]
        task = await client.get(f"/api/tasks/{tid}")
        state = task.json()["state"]
        assert state["manager_model"] == "chatgpt/b"
        assert state["executor_model"] == "openai/a"
        # Wait until orchestrator applies overrides onto the shared fakes.
        for _ in range(80):
            if planner.model == "chatgpt/b" and reviewer.model == "chatgpt/b" and exe.model == "openai/a":
                break
            await asyncio.sleep(0.05)
        assert planner.model == "chatgpt/b"
        assert reviewer.model == "chatgpt/b"
        assert exe.model == "openai/a"


@pytest.mark.asyncio
async def test_chatgpt_status_and_login_flow_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLICKCLICK_CHATGPT_TOKEN_DIR", str(tmp_path / "cg"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")

    from control_api.main import create_app

    app = create_app(
        planner_factory=lambda: FakePlanner([]),
        reviewer_factory=lambda: FakeReviewer([], task_scope=fake_task_scope()),
        executor_factory=lambda: FakeExecutor([]),
    )

    pending = PendingDeviceLogin(
        device_auth_id="dev",
        user_code="ABCD",
        interval=5,
        started_at=0,
    )

    def fake_start(settings):
        return (
            {
                "user_code": "ABCD",
                "verify_url": "https://auth.openai.com/codex/device",
                "interval_s": 5,
                "message": "ok",
            },
            pending,
        )

    def fake_poll(settings, p, **kwargs):
        return (
            "authenticated",
            {
                "status": "authenticated",
                "account_id": "acct-x",
                "expires_at": 999,
                "auth_file": str(tmp_path / "cg" / "auth.json"),
            },
            None,
        )

    def fake_status(settings):
        return {
            "authenticated": True,
            "account_id": "acct-x",
            "expires_at": 999,
            "auth_file": str(tmp_path / "cg" / "auth.json"),
            "token_dir": str(tmp_path / "cg"),
        }

    monkeypatch.setattr("control_api.main.start_device_login", fake_start)
    monkeypatch.setattr("control_api.main.poll_device_login", fake_poll)
    monkeypatch.setattr("control_api.main.chatgpt_status", fake_status)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        start = await client.post("/api/chatgpt/login/start")
        assert start.status_code == 200
        assert start.json()["user_code"] == "ABCD"

        poll = await client.post("/api/chatgpt/login/poll")
        assert poll.status_code == 200
        assert poll.json()["status"] == "authenticated"
        assert "access_token" not in poll.json()

        st = await client.get("/api/chatgpt/status")
        assert st.json()["authenticated"] is True
        assert "access_token" not in st.json()


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
