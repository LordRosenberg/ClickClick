import asyncio

from httpx import ASGITransport, AsyncClient
import pytest

from control_api.main import create_app
from shared.schemas import TaskStatus


def learning_app(monkeypatch, tmp_path, *, configured=True):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_MODELS_JSON", "{}")
    monkeypatch.delenv("ADB_SERVER_SOCKET", raising=False)
    monkeypatch.delenv("ANDROID_ADB_SERVER_PORT", raising=False)
    monkeypatch.setenv("CLICKCLICK_SKILL_LOOP_FIXTURE_JSON", (
        '{"serial":"emulator-5584","avd":"SkillQA","snapshot":"clean","adb_port":5059}' if configured else ""
    ))
    app = create_app(include_temp_runs=False)
    task = app.state.db.create_task("Clear the test timer", device_serial="fixture")
    app.state.db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    return app, task.id


CONSENT = {"accept_model_cost": True, "allow_device_operations": True, "run_regression": True}


@pytest.mark.asyncio
async def test_recording_verification_uses_the_isolated_worker_and_registers_only_after_pass(monkeypatch, tmp_path):
    from evaluation import gui_skill_console_worker
    from evaluation import gui_scripts

    async def adb_query(adb, *args):
        responses = {("devices",): "emulator-5584\tdevice\n",
                     ("-s", "emulator-5584", "emu", "avd", "name"): "SkillQA\nOK\n",
                     ("-s", "emulator-5584", "shell", "getprop", "sys.boot_completed"): "1\n",
                     ("-s", "emulator-5584", "emu", "avd", "snapshot", "list"): "ID clean 100M\n"}
        return responses[args]

    async def replay(args):
        assert (args.serial, args.avd, args.snapshot) == ("emulator-5584", "SkillQA", "clean")
        assert args.verify and not args.repair and not args.assist_policy
        assert args.auto_enroll_registry == tmp_path / "scripts"
        return {"update": "automatically_registered", "first": {"status": "succeeded"}}

    monkeypatch.setattr(gui_skill_console_worker, "_adb_query", adb_query)
    monkeypatch.setattr(gui_scripts, "_replay", replay)
    from driver import adb

    monkeypatch.setattr(adb, "adb_bin", lambda: "test-adb")
    report = await gui_skill_console_worker._run({"operation": "verify_recorded_script",
        "fixture": {"serial": "emulator-5584", "avd": "SkillQA", "snapshot": "clean"},
        "seconds": 60, "output": str(tmp_path / "replay"), "script": str(tmp_path / "script.json"),
        "goal": "Open the test panel", "registry": str(tmp_path / "scripts")})
    assert report == {"status": "automatically_registered", "reason": None}


@pytest.mark.asyncio
async def test_detail_queues_one_closure_and_retains_completed_state(monkeypatch, tmp_path):
    app, task_id = learning_app(monkeypatch, tmp_path)
    jobs = app.state.skill_loop_jobs
    reached, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def worker(current_id, request, fixture):
        seen.append((current_id, request.max_calls, fixture.serial))
        jobs._write(current_id, "running", phase="regression")
        reached.set()
        await release.wait()
        return {"status": "awaiting_publication_review", "pending_id": "verified", "case_origin": "reused"}

    async def old_learning(*args, **kwargs):
        pytest.fail("the Console closure should not run learning on the task device")

    monkeypatch.setattr(jobs, "_worker", worker)
    monkeypatch.setattr(app.state.orchestrator, "learn_from_task", old_learning)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            root = f"/api/tasks/{task_id}"
            assert (await client.get(root + "/skill-loop")).json() == {"closure": None}
            assert (await client.post(root + "/learn", json={"run_regression": True})).status_code == 400
            initial = await client.post(root + "/learn", json={**CONSENT, "max_calls": 19})
            assert initial.json()["closure"]["status"] == "queued"
            await asyncio.wait_for(reached.wait(), 3)
            duplicate = await client.post(root + "/learn", json=CONSENT)
            assert duplicate.json()["closure"]["phase"] == "regression"
            assert seen == [(task_id, 19, "emulator-5584")]
            handle = jobs._jobs[task_id]
            release.set()
            await handle
            final = (await client.get(root + "/skill-loop")).json()["closure"]
            assert final["status"] == "awaiting_publication_review" and final["case_origin"] == "reused"
            assert app.state.db.get_task(task_id).status == TaskStatus.SUCCEEDED
    finally:
        await jobs.close()
        app.state.db.close()


@pytest.mark.asyncio
async def test_detail_keeps_legacy_api_and_blocks_unconfigured_fixture(monkeypatch, tmp_path):
    app, task_id = learning_app(monkeypatch, tmp_path, configured=False)

    async def old_learning(*args, **kwargs):
        return {"ok": True, "mode": "existing"}

    monkeypatch.setattr(app.state.orchestrator, "learn_from_task", old_learning)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            route = f"/api/tasks/{task_id}/learn"
            blocked = await client.post(route, json=CONSENT)
            assert blocked.json()["closure"]["reason"] == "fixture_not_configured"
            legacy = await client.post(route, json={**CONSENT, "run_regression": False})
            assert legacy.json() == {"ok": True, "mode": "existing"}
    finally:
        app.state.db.close()


@pytest.mark.asyncio
async def test_console_cancellation_does_not_cancel_source_task(monkeypatch, tmp_path):
    app, task_id = learning_app(monkeypatch, tmp_path)
    jobs = app.state.skill_loop_jobs
    reached = asyncio.Event()

    async def worker(*args):
        reached.set()
        await asyncio.Future()

    monkeypatch.setattr(jobs, "_worker", worker)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            root = f"/api/tasks/{task_id}"
            await client.post(root + "/learn", json=CONSENT)
            await asyncio.wait_for(reached.wait(), 3)
            handle = jobs._jobs[task_id]
            assert (await client.post(root + "/skill-loop/cancel")).json()["closure"]["phase"] == "cancelling"
            await asyncio.gather(handle, return_exceptions=True)
            assert (await client.get(root + "/skill-loop")).json()["closure"]["status"] == "cancelled"
            assert app.state.db.get_task(task_id).status == TaskStatus.SUCCEEDED
    finally:
        await jobs.close()
        app.state.db.close()
