"""Generic host presentation and first-install defaults, without paid/UI work."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from desktop.configuration import initialize_installation_defaults, apply_saved_settings, model_environment
from desktop.connection import connection_info
from shared.config import Settings
from shared.hitl import HitlConfig, HumanInputBroker
from shared.schemas import TaskStatus
from shared.assistant_presentation import task_page_presentation
from tests.test_task_pause import app


def models(app):
    app.state.settings.models_json = '{"test":{"provider":"openai","api_key":"fixture-secret"}}'
    app.state.settings.default_model = "test"
    app.state.settings.manager_model = ""
    app.state.settings.executor_model = ""


async def test_first_connection_content_is_read_only_and_reports_hitl_settings(app):
    db = app.state.db
    before = db.list_tasks()
    for enabled in (False, True):
        app.state.settings.hitl_enabled = enabled
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            responses = [(await client.get("/api/assistant/status")).json() for _ in range(2)]
        for status in responses:
            guide = status["assistant_onboarding"]
            assert guide["when"] == "after_install_or_first_connection_verification"
            assert not guide["repeat_on_status_poll"] and not guide["test_task_required"]
            assert guide["hitl"]["enabled"] is enabled
            assert guide["hitl"]["settings_url"] == status["setup_url"]
            assert "保存" in guide["hitl"]["location"]
            assert "仅新任务" in guide["hitl"]["applies_to"]
            assert not guide["hitl"]["connection_changes_setting"]
            assert "assistant_ui" not in status  # polling must not request tab opening
        assert app.state.settings.hitl_enabled is enabled
    assert db.list_tasks() == before


async def test_accepted_task_page_metadata_reuses_task_and_rejections_never_open(app):
    models(app)
    gate = asyncio.Event()
    app.state.orchestrator.run_task = lambda tid: gate.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"instruction": "read-only task", "request_key": "open-page"}
        first = (await client.post("/api/assistant/tasks", json=body)).json()
        replay = (await client.post("/api/assistant/tasks", json=body)).json()
        assert first["assistant_ui"] == replay["assistant_ui"]
        ui, task = first["assistant_ui"], first["tasks"][0]
        assert ui["preferred_browser"] == "in_app" and ui["reuse_by"] == "task_id"
        assert ui["pages"] == [{"task_id": task["task_id"], "url": task["console_url"],
                                "console_url": task["console_url"]}]
        assert ui["on_unavailable"] == "share_links" and not ui["browser_failure_affects_task"]
        assert all(name not in json.dumps(ui).lower() for name in ("codex", "chrome", "sidebar"))
        rejected = await client.post("/api/assistant/tasks", json={**body, "request_key": "another"})
        assert rejected.status_code == 409 and "assistant_ui" not in rejected.json()
        assert "assistant_ui" not in (await client.get(f"/api/assistant/tasks/{task['task_id']}")).json()
    gate.set()
    await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


async def test_linked_followup_opens_new_task_page_not_parent(app, monkeypatch):
    models(app)
    monkeypatch.setattr("control_api.preview.safe_preview_projection", lambda task, **kwargs: {
        "preview_url": f"http://test/tasks/{task.id}/preview",
        "preview_support": {"status": "supported", "reason": ""},
    })
    gate = asyncio.Event()
    app.state.orchestrator.run_task = lambda tid: gate.wait()
    db = app.state.db
    parent = db.create_task("completed goal", device_serial="fixture")
    HumanInputBroker(db).configure(parent.id, HitlConfig(enabled=True))
    db.update_task(parent.id, status=TaskStatus.SUCCEEDED)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = (await client.post(f"/api/assistant/tasks/{parent.id}/steer", json={
            "instruction": "follow up", "request_key": "followup-page",
        })).json()
        assert result["assistant_ui"]["pages"] == [{
            "task_id": result["followup_task_id"],
            "url": f"http://test/tasks/{result['followup_task_id']}/preview",
            "console_url": result["followup_console_url"],
        }]
        assert result["followup_task_id"] != parent.id
    gate.set()
    await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


async def test_queued_supported_preview_is_open_target_and_retries_reuse_it(app, monkeypatch):
    models(app)
    monkeypatch.setattr("control_api.preview.safe_preview_projection", lambda task, **kwargs: {
        "preview_url": f"http://test/tasks/{task.id}/preview",
        "preview_support": {"status": "supported", "reason": ""},
    })
    gate = asyncio.Event()
    app.state.orchestrator.run_task = lambda tid: gate.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"instruction": "read-only task", "request_key": "open-preview"}
        first = (await client.post("/api/assistant/tasks", json=body)).json()
        replay = (await client.post("/api/assistant/tasks", json=body)).json()
        assert first["assistant_ui"] == replay["assistant_ui"]
        task = first["tasks"][0]
        assert first["assistant_ui"]["pages"] == [{
            "task_id": task["task_id"], "url": task["preview_url"], "console_url": task["console_url"],
        }]
        assert "assistant_ui" not in (await client.get(f"/api/assistant/tasks/{task['task_id']}")).json()
    gate.set()
    await asyncio.gather(*app.state.running_tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("preview", [
    {},
    {"preview_url": None, "preview_support": {"status": "supported"}},
    {"preview_url": "http://test/unused", "preview_support": {"status": "unsupported"}},
    {"preview_url": "http://test/unused", "preview_support": {"status": "unavailable"}},
    {"preview_url": "http://test/unused", "preview_support": None},
])
def test_missing_or_unsupported_preview_falls_back_without_dropping_details(preview):
    task = {"task_id": "example", "console_url": "http://test/tasks/example", **preview}
    assert task_page_presentation([task])["pages"] == [{
        "task_id": "example", "url": task["console_url"], "console_url": task["console_url"],
    }]


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_connection_and_verification_handoff_explain_usage_and_hitl(tmp_path, locale):
    data = connection_info(tmp_path, {"command": "python", "args": ["-m", "desktop.main", "mcp"]}, system="darwin", locale=locale)
    for field in ("assistant_prompt", "verification_prompt"):
        assert "HITL" in data[field] and "get_status" in data[field]
        assert ("内置浏览器" if locale == "zh-CN" else "in-app browser") in data[field]
        assert ("投屏预览" if locale == "zh-CN" else "screen preview") in data[field]
    assert not (tmp_path/"installation-defaults.json").exists()


@pytest.mark.parametrize("origin,enabled", [("assistant", True), ("manual", False)])
def test_installation_defaults_are_durable_and_subordinate_to_user_choice(tmp_path, origin, enabled):
    from control_api.console_settings import CoreSettingsBody, persist_settings, prepare_settings
    from tests.test_console_settings import api_body
    initialize_installation_defaults(tmp_path, origin)
    original = (tmp_path/"installation-defaults.json").read_bytes()
    settings = apply_saved_settings(Settings(_env_file=None, data_dir=tmp_path))
    assert settings.hitl_enabled is enabled
    assert json.loads(model_environment(tmp_path)["CLICKCLICK_HITL_ENABLED"]) is enabled
    body = {**api_body(), "hitl_enabled": not enabled}
    document, checked = prepare_settings(settings, CoreSettingsBody(**body))
    persist_settings(settings, document, checked)
    initialize_installation_defaults(tmp_path, "manual" if enabled else "assistant")
    assert (tmp_path/"installation-defaults.json").read_bytes() == original
    assert apply_saved_settings(Settings(_env_file=None, data_dir=tmp_path)).hitl_enabled is not enabled
    assert json.loads(model_environment(tmp_path)["CLICKCLICK_HITL_ENABLED"]) is not enabled


@pytest.mark.parametrize("origin", ["assistant", "manual"])
def test_first_install_engine_initializes_origin_once(tmp_path, monkeypatch, origin):
    from desktop import service, upgrade
    from desktop.files import read_json
    from tests.test_desktop_upgrade import lifecycle
    home = tmp_path/"first-install"
    _, payload = lifecycle(monkeypatch, home, tmp_path)
    monkeypatch.setattr(service, "backend_status", lambda _: {"running": False})
    upgrade.install(home, payload, no_start=True, origin=origin)
    defaults = read_json(home/"data/installation-defaults.json")
    assert defaults["origin"] == origin and defaults["hitl_enabled"] == (origin == "assistant")
    upgrade.install(home, payload, no_start=True, origin="manual" if origin == "assistant" else "assistant")
    assert read_json(home/"data/installation-defaults.json") == defaults


@pytest.mark.parametrize("args,expected", [([], "manual"), (["--origin", "assistant"], "assistant")])
def test_bootstrap_forwards_explicit_origin_without_inferring_from_unattended(tmp_path, monkeypatch, args, expected):
    import subprocess
    from desktop import bootstrap
    calls = []
    monkeypatch.setattr(bootstrap.sys, "argv", ["installer", "--unattended", *args])
    monkeypatch.setattr(bootstrap, "choose_home", lambda *a, **kw: tmp_path)
    monkeypatch.setattr(bootstrap, "install_payload", lambda *a, **kw: {"python": "private-python", "root": str(tmp_path)})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="{}"))
    bootstrap.main()
    assert calls[0][calls[0].index("--origin")+1] == expected
