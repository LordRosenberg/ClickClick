from copy import deepcopy
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from driver.fixture import DEFAULT_TREE, FixtureDriver
from evaluation import gui_script_host, gui_scripts
from shared.db import Database
from shared.hitl import HumanInputBroker
from shared.recovery_context import ExecutionContext
from shared.recovery_store import RecoveryStore


class SubmissionDevice(FixtureDriver):
    def __init__(self):
        super().__init__(deepcopy(DEFAULT_TREE))
        self.serial = "emulator-5554"
        self.ledger = [{"class": "android.widget.TextView", "resource-id": "submission-id",
                        "text": "Receipt OLD", "bounds": [100, 800, 500, 870]}]
        self.tree["tree"]["children"].append({
            "class": "android.widget.LinearLayout", "resource-id": "submission-ledger",
            "bounds": [100, 750, 600, 1100], "children": self.ledger,
        })
        self.show_new_record = False

    async def read_app_build(self, app):
        return {"package": app, "version_code": "1"}

    async def act(self, action):
        result = await super().act(action)
        if self.show_new_record:
            self.reveal_submission()
        return result

    def reveal_submission(self):
        self.ledger.append({**self.ledger[0], "text": "Receipt NEW-1", "bounds": [100, 900, 500, 970]})


@pytest.fixture
def replay(tmp_path, monkeypatch):
    device = SubmissionDevice()
    app = {"type": "app_is", "app_id": "com.example.demo"}
    ordinary_confirmation = {"type": "target_present", "target": {"text": "Submission complete"}}
    path = tmp_path / "script.json"
    path.write_text(json.dumps({
        "script_id": "submit", "app_id": app["app_id"], "initial": [app], "final": [app],
        "steps": [{"id": "submit", "before": [app], "after": [ordinary_confirmation],
                   "action": {"type": "tap", "target": {"text": "Play"}}, "timeout_seconds": 0.6,
                   "confirmation": {"scope": {"resource_id": "submission-ledger"},
                                    "target": {"resource_id": "submission-id"}, "identifier_prefix": "Receipt "}}],
    }), encoding="utf-8")
    policy = tmp_path / "assist.json"
    policy.write_text(json.dumps({"max_actions": 1, "max_seconds": 0.5, "actions": [
        {"id": "dismiss", "before": [app], "after": [app],
         "action": {"type": "tap", "target": {"text": "Dismiss"}}},
    ]}), encoding="utf-8")
    snapshots = []

    async def with_driver(settings, serial, run, **kwargs):
        return await run(device)

    async def restore(adb, serial, snapshot):
        snapshots.append(snapshot)

    async def unexpected_model(*args, **kwargs):
        pytest.fail("an uncertain input should only be verified")

    monkeypatch.setattr(gui_scripts, "with_android_driver", with_driver)
    monkeypatch.setattr(gui_scripts, "restore_snapshot", restore)
    monkeypatch.setattr(gui_script_host.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(gui_script_host.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="fixture\n"))
    monkeypatch.setattr("agent.session.complete", unexpected_model)
    args = SimpleNamespace(script=path, output=tmp_path / "replay", goal="Submit the form", serial=device.serial,
                           seconds=45.0, env_file=None, parameters=None, assets=None, ocr_executable=None,
                           assist_policy=policy, verify=False, repair=False, snapshot="clean", avd="fixture", adb="adb")
    settings = gui_script_host.evaluation_settings(None, args.output / "first-runtime")
    return device, args, settings, snapshots


async def test_replay_checks_new_visible_record_without_confirmation_screen(replay):
    device, args, settings, _ = replay
    device.show_new_record = True
    result = (await gui_scripts._replay(args))["first"]
    assert result["status"] == "succeeded", result
    assert len(device.actions) == 1
    db = Database(settings.db_path)
    try:
        completion = db.get_agent_record(result["task_id"], "gui_script_step_completion", "submit")["payload"]
        assert completion["next_index"] == 1 and completion["confirmation_id"] == "NEW-1"
    finally:
        db.close()


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("crash", ["T1", "after_dispatch", "T2", None])
async def test_cli_resume_only_verifies_an_unconfirmed_submission(replay, monkeypatch, crash):
    device, args, settings, snapshots = replay
    if crash:
        with monkeypatch.context() as fault:
            if crash == "T1":
                original = ExecutionContext.prepare_action

                def prepare(context, action):
                    original(context, action)
                    raise SimulatedCrash

                fault.setattr(ExecutionContext, "prepare_action", prepare)
            elif crash == "after_dispatch":
                original = device.act

                async def act(action):
                    await original(action)
                    raise SimulatedCrash

                fault.setattr(device, "act", act)
            else:
                original = RecoveryStore.receipt

                def receipt(store, *args, **kwargs):
                    original(store, *args, **kwargs)
                    raise SimulatedCrash

                fault.setattr(RecoveryStore, "receipt", receipt)
            with pytest.raises(SimulatedCrash):
                await gui_scripts._replay(args)
        db = Database(settings.db_path)
        try:
            task_id, = (task.id for task in db.list_tasks())
        finally:
            db.close()
    else:
        first = (await gui_scripts._replay(args))["first"]
        assert first["status"] == "paused", first
        task_id = first["task_id"]

    args.resume_task = task_id
    expected_dispatches = 0 if crash == "T1" else 1
    for _ in range(2):
        resumed = (await gui_scripts._replay(args))["first"]
        assert resumed["status"] == "paused", resumed
        assert len(device.actions) == expected_dispatches
        db = Database(settings.db_path)
        try:
            assert db.get_agent_record(task_id, "gui_script_checkpoint", "current")["payload"]["cursor"] == 0
            assert HumanInputBroker(db).current(task_id)["status"] == "pending"
            assert len(RecoveryStore(db, None).operations(task_id)) == 1
        finally:
            db.close()
    assert snapshots == ["clean"]
    if expected_dispatches:
        device.reveal_submission()
        completed = (await gui_scripts._replay(args))["first"]
        assert completed["status"] == "succeeded", completed
        assert len(device.actions) == 1


async def test_console_resume_only_verifies_current_submission_even_after_dispatch_deadline(replay, monkeypatch):
    device, args, settings, _ = replay
    first = (await gui_scripts._replay(args))["first"]
    assert first["status"] == "paused", first
    task_id = first["task_id"]
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")
    from control_api.main import create_app
    from control_api.mcp_http import local_token

    app = create_app(include_temp_runs=False)
    app.state.orchestrator._resolve_driver = lambda serial: device
    db = app.state.db
    try:
        task = db.get_task(task_id)
        task.state.revisable.limits.deadline_at = time.time() - 1
        db.update_task(task_id, state=task.state)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8080") as client:
            response = await client.get("/api/hitl/pending", headers={
                "Authorization": "Bearer " + local_token(app.state.settings),
            })
            assert response.status_code == 200, response.text
            assert any(item["task_id"] == task_id for item in response.json()["interactions"])
            for expected in ("paused", "succeeded"):
                if expected == "succeeded":
                    device.reveal_submission()
                response = await client.post(f"/api/tasks/{task_id}/resume")
                assert response.status_code == 200 and response.json()["resumed"], response.text
                await asyncio.gather(*list(app.state.running_tasks.values()))
                assert db.get_task(task_id).status.value == expected
                assert len(device.actions) == 1
    finally:
        db.close()
