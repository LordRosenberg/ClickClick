"""Local configuration, secret handling and independent subscription task routing."""
import asyncio
import copy
import json
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from control_api.console_settings import CoreSettingsBody, prepare_settings, persist_settings, settings_summary
from control_api.mcp_http import local_token
from desktop.configuration import apply_saved_settings, console_config_path, model_environment, save_model
from desktop.files import write_json
from shared.config import Settings
from shared.schemas import AgentState, TaskStatus
from tests.test_task_pause import app


def api_body():
    return {"mode": "api", "default_model": "openai/model", "models": [{"id": "openai/model",
        "base_url": "https://relay.example/v1", "api_key": "secret-key", "user_agent": "ClickClick/Test",
        "stream": True, "reasoning_supported": True, "reasoning_effort": "high", "reasoning_summary": "auto",
        "extra_body": '{"thinking":{"type":"enabled"},"private":"body-secret"}', "max_tokens": 4096,
        "history_tokens": 12000, "max_input_tokens": 32000}], "default_task_model_calls": 12,
        "default_task_device_actions": 20, "default_task_seconds": 600,
        "learning_default_calls": 16, "learning_default_actions": 25, "learning_default_seconds": 300}


def subscription_body():
    return {"mode": "subscription", "default_model": "chatgpt/model", "models": [{"id": "chatgpt/model",
        "stream": True, "reasoning_supported": True, "reasoning_effort": "high"}]}


def test_jev_default_off_and_independent_secret_configuration_survive_profile_switch_and_restart(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, models_json="{}", jev_api_key="")
    assert settings_summary(settings)["jev"]["mode"] == "off"
    body = subscription_body()
    body["jev"] = {"mode": "enforce", "base_url": "https://jev.example", "model": "jev-custom",
                   "api_key": "jev-secret", "timeout_s": 5, "failure_policy": "rollback"}
    document, checked = prepare_settings(settings, CoreSettingsBody(**body))
    persist_settings(settings, document, checked)
    assert settings.jev_allow_external and settings.jev_api_key.get_secret_value() == "jev-secret"
    assert settings.jev_model == "jev-custom" and settings.jev_timeout_s == 5
    assert settings.jev_failure_policy == "rollback"
    summary = settings_summary(settings)
    assert summary["jev"]["api_key_configured"]
    assert "jev-secret" not in json.dumps(summary)
    assert "jev-secret" not in settings.models_json
    body = api_body()
    body["jev"] = {**summary["jev"], "api_key": ""}
    body["jev"].pop("api_key_configured")
    document, checked = prepare_settings(settings, CoreSettingsBody(**body))
    persist_settings(settings, document, checked)
    restarted = apply_saved_settings(Settings(_env_file=None, data_dir=tmp_path, jev_api_key=""))
    assert restarted.jev_mode == "enforce" and restarted.jev_allow_external
    assert restarted.jev_api_key.get_secret_value() == "jev-secret"
    environment = model_environment(tmp_path)
    assert environment["CLICKCLICK_JEV_API_KEY"] == "jev-secret"
    assert environment["CLICKCLICK_JEV_MODE"] == "enforce"
    body["jev"]["mode"] = "off"
    document, checked = prepare_settings(settings, CoreSettingsBody(**body))
    persist_settings(settings, document, checked)
    assert settings.jev_mode == "off" and not settings.jev_allow_external
    assert settings.jev_api_key.get_secret_value() == "jev-secret"


def test_jev_rejects_missing_credentials_and_endpoint_change_before_writing(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, jev_api_key="jev-secret")
    body = subscription_body()
    body["jev"] = {"mode": "enforce", "base_url": "https://different.example"}
    with pytest.raises(ValueError, match="API Key"):
        prepare_settings(settings, CoreSettingsBody(**body))
    assert not console_config_path(tmp_path).exists()
    assert settings.jev_base_url == "https://api.typesafe.ai"
    settings.jev_api_key = type(settings.jev_api_key)("")
    body["jev"] = {"mode": "enforce"}
    with pytest.raises(ValueError, match="API Key"):
        prepare_settings(settings, CoreSettingsBody(**body))
    for patch in ({"base_url": "http://remote.example"}, {"timeout_s": 0}, {"api_key": "bad\nkey"}):
        body["jev"] = patch
        with pytest.raises(ValueError):
            CoreSettingsBody(**body)


def test_model_preset_reuses_saved_credentials_only_on_same_endpoint(tmp_path):
    settings = Settings(data_dir=tmp_path, models_json="{}", _env_file=None)
    document, checked = prepare_settings(settings, CoreSettingsBody(**api_body()))
    persist_settings(settings, document, checked)
    body = api_body()
    model = body["models"][0]
    model.update(id="openai/gpt-6-luna", api_key="", extra_body=None, credentials_from="openai/model")
    body["default_model"] = model["id"]
    document, checked = prepare_settings(settings, CoreSettingsBody(**body))
    assert checked.provider_for(model["id"])["api_key"] == "secret-key"
    assert checked.provider_for(model["id"])["extra_body"]["private"] == "body-secret"
    assert "credentials_from" not in checked.provider_for(model["id"])
    persist_settings(settings, document, checked)
    assert "secret-key" not in json.dumps(settings_summary(settings))
    model["credentials_from"] = model["id"]
    for changes in ({"base_url": "https://other.example/v1"}, {"credentials_from": "missing"}):
        invalid = copy.deepcopy(body)
        invalid["models"][0].update(changes)
        with pytest.raises(ValueError, match="同一 API 地址"):
            prepare_settings(settings, CoreSettingsBody(**invalid))



def test_private_profiles_legacy_restart_gateway_and_secret_preservation(tmp_path, monkeypatch):
    from shared.llm_gateway import _build_completion_kwargs
    settings = Settings(data_dir=tmp_path, models_json="{}", chatgpt_token_dir="", _env_file=None)
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "test-auth"))
    save_model(tmp_path, model="openai/legacy", base_url="https://api.example/v1", api_key="legacy-key")
    apply_saved_settings(settings)
    assert settings.default_model == "openai/legacy"
    document, checked = prepare_settings(settings, CoreSettingsBody(**api_body()))
    persist_settings(settings, document, checked)
    summary = json.dumps(settings_summary(settings))
    assert "secret-key" not in summary and "body-secret" not in summary
    assert settings_summary(settings)["profiles"]["api"]["models"][0]["extra_body_configured"]
    kwargs = _build_completion_kwargs("openai/model", [], None, None, None, settings)
    assert kwargs["extra_headers"] == {"User-Agent": "ClickClick/Test"}
    assert kwargs["reasoning_effort"] == "high" and kwargs["reasoning"] == {"summary": "auto"}
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert kwargs["max_tokens"] == 4096 and kwargs["stream"] is True
    update = api_body()
    update["models"][0]["api_key"] = ""
    update["models"][0]["extra_body"] = None
    document, checked = prepare_settings(settings, CoreSettingsBody(**update))
    persist_settings(settings, document, checked)
    assert settings.provider_for("openai/model")["api_key"] == "secret-key"
    assert settings.provider_for("openai/model")["extra_body"]["private"] == "body-secret"
    document, checked = prepare_settings(settings, CoreSettingsBody(**subscription_body()))
    persist_settings(settings, document, checked)
    assert set(settings_summary(settings)["profiles"]) == {"api", "subscription"}
    restarted = apply_saved_settings(Settings(data_dir=tmp_path, _env_file=None))
    assert restarted.default_model == "chatgpt/model"
    assert model_environment(tmp_path)["CLICKCLICK_DEFAULT_MODEL"] == "chatgpt/model"
    assert restarted.chatgpt_token_dir == str(tmp_path.resolve() / "chatgpt-auth")
    # Installed launchers pass these strings through the environment. Optional
    # numbers cannot be encoded as 'null' in a BaseSettings scalar env field.
    for key, value in model_environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    installed_restart = apply_saved_settings(Settings(data_dir=tmp_path, _env_file=None))
    assert installed_restart.default_task_model_calls is None
    assert installed_restart.default_task_seconds is None
    assert installed_restart.default_model == "chatgpt/model"
    kwargs = _build_completion_kwargs("chatgpt/model", [], None, None, None, restarted)
    assert "extra_headers" not in kwargs and "api_key" not in kwargs and "max_tokens" not in kwargs
    assert kwargs["reasoning_effort"] == "high"
    # Old CLI remains authoritative if called after switching in Console.
    save_model(tmp_path, model="openai/cli", base_url="https://api.example", api_key="cli-key")
    assert model_environment(tmp_path)["CLICKCLICK_DEFAULT_MODEL"] == "openai/cli"


@pytest.mark.parametrize("mutate", [
    lambda b: b["models"][0].update(user_agent="bad\r\nHeader: secret"),
    lambda b: b["models"][0].update(extra_body="secret-invalid-json"),
    lambda b: b["models"][0].update(extra_body="[]"),
    lambda b: b["models"][0].update(base_url="https://user:secret@api.example"),
    lambda b: b.update(default_task_model_calls=201),
    lambda b: b.update(manager_model="missing"),
    lambda b: b.update(default_model=""),
    lambda b: b.update(mode="subscription"),
    lambda b: b["models"].append(copy.deepcopy(b["models"][0])),
])
def test_invalid_configs_do_not_persist(tmp_path, mutate):
    settings = Settings(data_dir=tmp_path, models_json="{}", _env_file=None)
    body = api_body()
    mutate(body)
    with pytest.raises(ValueError):
        prepare_settings(settings, CoreSettingsBody(**body))
    assert not console_config_path(tmp_path).exists()
    assert settings.models_json == "{}"


def test_existing_mixed_catalog_is_presented_as_two_configurable_profiles(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="openai/model",
        manager_model="chatgpt/model", models_json=json.dumps({"openai/model": {"api_key": "secret"},
        "chatgpt/model": {"provider": "chatgpt"}}))
    summary = settings_summary(settings)
    assert [m["id"] for m in summary["profiles"]["api"]["models"]] == ["openai/model"]
    assert [m["id"] for m in summary["profiles"]["subscription"]["models"]] == ["chatgpt/model"]
    assert summary["profiles"]["subscription"]["manager_model"] == "chatgpt/model"
    assert settings.manager_model == "chatgpt/model"  # Reading does not alter routing.
    document, checked = prepare_settings(settings, CoreSettingsBody(**api_body()))
    persist_settings(settings, document, checked)
    assert "subscription" in settings_summary(settings)["profiles"]
    assert json.loads(document["profiles"]["subscription"]["models_json"])["chatgpt/model"]["provider"] == "chatgpt"


async def test_local_browser_bootstrap_and_validation_without_mcp_sdk(app):
    settings = app.state.settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://127.0.0.1:{settings.api_port}") as client:
        assert (await client.get("/api/setup/settings")).status_code == 401
        assert (await client.get("/api/setup/access")).status_code == 401
        assert (await client.get("/api/setup/access", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 401
        assert (await client.get("/api/setup/access", headers={"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"})).status_code == 403
        response = await client.get("/api/setup/access", headers={"Sec-Fetch-Site": "same-origin"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        client.headers["Authorization"] = "Bearer " + response.json()["token"]
        assert (await client.get("/api/setup/settings")).status_code == 200
        payload = api_body()
        payload["models"][0]["extra_body"] = "secret-invalid-json"
        invalid = await client.post("/api/setup/settings", json=payload)
        assert invalid.status_code == 422 and "secret" not in invalid.text
        assert not console_config_path(settings.data_dir).exists()
        assert (await client.post("/api/setup/register", json={"client": "codex", "transport": "http"})).status_code in {404, 405}
        handoff = await client.post("/api/setup/mcp-connection", json={})
        assert handoff.status_code == 200
        assert handoff.json()["transport"] == "stdio"
        assert "Bearer" not in handoff.text
        config = json.loads(Path(handoff.json()["config_path"]).read_text(encoding="utf-8"))
        assert config["mcpServers"]["clickclick"]["command"]
        client.headers.pop("Authorization")
        assert (await client.post("/api/setup/mcp-connection", json={})).status_code == 401


async def test_live_settings_and_paused_or_learning_jobs_block_changes(app):
    settings = app.state.settings
    settings.chatgpt_token_dir = ""
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://127.0.0.1:{settings.api_port}",
        headers={"Authorization": "Bearer " + local_token(settings)}) as client:
        saved = await client.post("/api/setup/settings", json=api_body())
        assert saved.status_code == 200 and saved.json()["restart_required"] is False
        assert settings.default_model == "openai/model"
        assert app.state.orchestrator.executor_factory().model == "openai/model"
        assert (await client.get("/api/models")).json()["roles"]["executor"] == "openai/model"
        assert "secret-key" not in (await client.get("/api/setup/settings")).text
        before = console_config_path(settings.data_dir).read_bytes()
        task = app.state.db.create_task("paused")
        app.state.db.update_task(task.id, status=TaskStatus.PAUSED)
        assert (await client.post("/api/setup/settings", json=subscription_body())).status_code == 409
        assert console_config_path(settings.data_dir).read_bytes() == before
        app.state.db.update_task(task.id, status=TaskStatus.CANCELLED)
        app.state.orchestrator._learning_jobs.add("learning")
        assert (await client.post("/api/setup/settings", json=subscription_body())).status_code == 409
        app.state.orchestrator._learning_jobs.clear()
        assert (await client.post("/api/setup/settings", json=subscription_body())).status_code == 200


async def test_subscription_authorization_mcp_submission_budget_and_replay(app, monkeypatch):
    from shared import chatgpt_auth
    settings = app.state.settings
    settings.chatgpt_token_dir = str(settings.data_dir / "test-auth")
    monkeypatch.delenv("CHATGPT_AUTH_FILE", raising=False)
    gate = asyncio.Event()
    async def run(_):
        await gate.wait()
    app.state.orchestrator.run_task = run
    def start(_):
        return {"user_code": "CODE", "verify_url": "https://auth.openai.com/codex/device", "interval_s": 5}, object()
    def poll(settings, pending):
        write_json(settings.data_dir / "test-auth" / "auth.json", {"access_token": "private-access", "refresh_token": "private-refresh"})
        return "authenticated", {"status": "authenticated"}, None
    monkeypatch.setattr(chatgpt_auth, "start_device_login", start)
    monkeypatch.setattr(chatgpt_auth, "poll_device_login", poll)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://127.0.0.1:{settings.api_port}",
        headers={"Authorization": "Bearer " + local_token(settings)}) as client:
        body = {**subscription_body(), "default_task_model_calls": 8, "default_task_device_actions": 12, "default_task_seconds": 600}
        assert (await client.post("/api/setup/settings", json=body)).status_code == 200
        goal = {"instruction": "phone goal", "request_key": "subscription"}
        assert (await client.post("/api/assistant/tasks", json=goal)).status_code == 409
        readiness = (await client.get("/api/assistant/status")).json()
        assert readiness["models_configured"] and readiness["model_authentication"]["subscription_authenticated"] is False
        assert (await client.post("/api/setup/chatgpt/login/start", json={})).status_code == 200
        assert (await client.post("/api/setup/settings", json=body)).status_code == 409
        assert (await client.post("/api/setup/chatgpt/login/poll", json={})).json()["status"] == "authenticated"
        assert "private-access" not in (await client.get("/api/setup/chatgpt/status")).text
        response = await client.post("/api/assistant/tasks", json=goal)
        assert response.status_code == 200
        tid = response.json()["tasks"][0]["task_id"]
        state = app.state.db.get_task(tid).state
        assert state.revisable.limits.model_calls == 8
        assert state.revisable.limits.device_actions == 12
        assert 590 < state.revisable.limits.deadline_at - time.time() <= 600
        (settings.data_dir / "test-auth" / "auth.json").unlink()
        replay = await client.post("/api/assistant/tasks", json=goal)
        assert replay.status_code == 200 and replay.json()["replayed"]
        assert replay.json()["tasks"][0]["task_id"] == tid
        assert app.state.db.get_task(tid).state.revisable.limits == state.revisable.limits
        gate.set()
        await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


async def test_mixed_role_subscription_alias_requires_clickclick_auth(app):
    settings = app.state.settings
    settings.default_model = "openai/api"
    settings.manager_model = "openai/api"
    settings.executor_model = "subscription-alias"
    settings.models_json = json.dumps({"openai/api": {"provider": "openai"},
                                      "subscription-alias": {"provider": "chatgpt"}})
    settings.chatgpt_token_dir = str(settings.data_dir / "missing-auth")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        value = (await client.get("/api/assistant/status")).json()
        assert value["models_configured"] and value["model_authentication"] == {
            "subscription_required": True, "subscription_authenticated": False}
        assert (await client.post("/api/assistant/tasks", json={"instruction": "goal", "request_key": "alias"})).status_code == 409


async def test_learning_defaults_apply_without_granting_consent(app):
    settings = app.state.settings
    settings.learning_default_calls = 16
    settings.learning_default_actions = 25
    settings.learning_default_seconds = 300
    task = app.state.db.create_task("finished")
    app.state.db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    captured = []
    async def learn(task_id, *, request):
        captured.append(request)
        return {"ok": True, "skipped": True}
    app.state.orchestrator.learn_from_task = learn
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/learning/defaults")).json() == {"max_calls": 16, "max_actions": 25, "max_seconds": 300}
        assert (await client.post(f"/api/tasks/{task.id}/learn", json={})).status_code == 400
        assert not captured
        response = await client.post(f"/api/tasks/{task.id}/learn", json={"accept_model_cost": True, "allow_device_operations": True})
        assert response.status_code == 200
        assert captured[-1].max_calls == 16 and captured[-1].max_actions == 25 and captured[-1].max_seconds == 300
        response = await client.post(f"/api/tasks/{task.id}/learn", json={"accept_model_cost": True, "allow_device_operations": True, "max_calls": 24})
        assert response.status_code == 200 and captured[-1].max_calls == 24


def test_model_budget_counts_requests_and_retains_runtime_guard(app):
    from agent.orchestrator import RoleInvocationLimitExceeded
    orch = app.state.orchestrator
    state = AgentState(instruction="goal", revisable={"limits": {"model_calls": 2}})
    task = app.state.db.create_task("goal", state)
    meter = orch._model_call_meter(task.id, state, "planner")
    meter("model", {}); meter("retry", {})
    with pytest.raises(RoleInvocationLimitExceeded):
        meter("retry", {})
    assert app.state.db.get_task(task.id).state.role_invocation_count == 2
    state.revisable.limits.model_calls = 300
    assert orch._model_call_limit(state) == 200
