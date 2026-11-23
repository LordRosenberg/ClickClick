"""Control API: submit/status/failed/replay/trace/debug/skills."""

import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def recording_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")
    monkeypatch.setenv("CLICKCLICK_MODELS_JSON", "{}")
    from control_api.main import create_app

    app = create_app(include_temp_runs=False)
    yield app
    app.state.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in,skill_learn", ((None, False), (False, True), (True, False), (True, True)))
async def test_task_recording_opt_in_persists_and_retries(recording_app, opt_in, skill_learn):
    from shared.db import Database

    starts = []

    async def run(task_id):
        task = recording_app.state.db.get_task(task_id)
        starts.append((task_id, task.state.generate_test_script))

    recording_app.state.orchestrator.run_task = run
    body = {"instruction": "Observe the test screen", "device_serials": ["fixture"], "request_key": "record",
            "skill_learn": skill_learn}
    if opt_in is not None:
        body["generate_test_script"] = opt_in
    async with AsyncClient(transport=ASGITransport(app=recording_app), base_url="http://test") as client:
        assert (await client.post("/api/tasks", json={**body, "generate_test_script": "true"})).status_code == 422
        response = await client.post("/api/tasks", json=body)
        assert response.status_code == 200, response.text
        record = response.json()["tasks"][0]
        task_id = record["id"]
        expected = bool(opt_in)
        assert record["generate_test_script"] is expected
        assert record["state"]["generate_test_script"] is expected
        assert record["state"]["skill_learn"] is skill_learn
        retry = await client.post("/api/tasks", json={**body, "generate_test_script": expected})
        assert retry.json()["replayed"] and retry.json()["tasks"][0]["id"] == task_id
        conflict = await client.post("/api/tasks", json={**body, "generate_test_script": not expected})
        assert conflict.status_code == 409
        detail = await client.get(f"/api/tasks/{task_id}")
        assert detail.json()["generate_test_script"] is expected
        summary = await client.get("/api/tasks", params={"summary": True})
        assert summary.json()[0]["generate_test_script"] is expected
        await asyncio.sleep(0)
        assert starts == [(task_id, expected)]

    reopened = Database(recording_app.state.db.path, read_only=True)
    try:
        assert reopened.get_task(task_id).state.generate_test_script is expected
        assert reopened.get_task_summary(task_id).generate_test_script is expected
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_legacy_submission_and_task_without_recording_option(recording_app):
    db = recording_app.state.db
    task = db.create_task("Legacy task", device_serial="fixture")
    db._conn.execute("UPDATE tasks SET state_json=? WHERE id=?", (json.dumps({"instruction": "Legacy task"}), task.id))
    canonical = {"instruction": task.instruction, "device_serials": ["fixture"], "skill_learn": False,
                 "manager_model": None, "executor_model": None, "recovery_policy": None}
    legacy_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    db.save_submission("legacy", legacy_hash, [task.id])
    async with AsyncClient(transport=ASGITransport(app=recording_app), base_url="http://test") as client:
        response = await client.post("/api/tasks", json={**canonical, "request_key": "legacy", "generate_test_script": False})
        assert response.status_code == 200, response.text
        assert response.json()["replayed"]
        record = response.json()["tasks"][0]
        assert record["id"] == task.id
        assert record["generate_test_script"] is False
        assert record["state"]["generate_test_script"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,succeeded", [(False, True), (True, False), (True, True)])
async def test_only_successful_recording_tasks_publish_a_draft(recording_app, monkeypatch, enabled, succeeded):
    from agent.skills import script_generation_jobs
    from shared.schemas import TaskStatus

    jobs = recording_app.state.script_generation_jobs
    db = recording_app.state.db
    completed = asyncio.Event()
    invoked = asyncio.Event()
    release = asyncio.Event()
    before = {"type": "target_present", "target": {"text": "Open"}}
    after = {"type": "target_present", "target": {"text": "Done"}}
    script = {
        "script_id": "api-draft", "app_id": "com.example.demo",
        "initial": [before], "final": [after],
        "steps": [{"id": "one", "before": [before], "after": [after],
                   "action": {"type": "tap", "target": {"text": "Open"}}}],
    }

    async def run(task_id):
        status = TaskStatus.SUCCEEDED if succeeded else TaskStatus.FAILED
        db.update_task(task_id, status=status)
        completed.set()
        return status

    async def author(task, *, settings, store, destination, budget):
        assert db.get_task(task.id).status == TaskStatus.SUCCEEDED
        invoked.set()
        await release.wait()
        destination.mkdir()
        (destination / "script.json").write_text(json.dumps(script), encoding="utf-8")
        return {"status": "candidate"}

    monkeypatch.setattr(recording_app.state.orchestrator, "run_task", run)
    monkeypatch.setattr(script_generation_jobs, "_author", author)
    try:
        async with AsyncClient(transport=ASGITransport(app=recording_app), base_url="http://test") as client:
            created = await client.post("/api/tasks", json={
                "instruction": "Open", "device_serials": ["fixture"], "generate_test_script": enabled,
            })
            task_id = created.json()["tasks"][0]["id"]
            route = f"/api/tasks/{task_id}/script-generation"
            await asyncio.wait_for(completed.wait(), timeout=10)
            if not enabled or not succeeded:
                result = (await client.get(route)).json()
                assert result is None if not enabled else result["status"] == "blocked"
                assert (await client.get(f"{route}/download")).status_code == 404
                assert not invoked.is_set()
                return
            await asyncio.wait_for(invoked.wait(), timeout=10)
            assert (await client.get(route)).json()["status"] == "running"
            assert (await client.get(f"{route}/download")).status_code == 404
            release.set()
            for _ in range(100):
                result = (await client.get(route)).json()
                if result["status"] == "draft":
                    break
                await asyncio.sleep(0.05)
            assert result["status"] == "draft" and result["replay_required"], result
            archive = await client.get(f"{route}/download")
            assert archive.status_code == 200
            with ZipFile(BytesIO(archive.content)) as bundle:
                assert json.loads(bundle.read("script.json")) == script
    finally:
        release.set()
        await jobs.close()


@pytest.mark.asyncio
async def test_console_spa_deep_link_serves_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """React Router paths must fall back to index.html, not JSON 404."""
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>console</title><div id='root'></div>\n",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("window.__console = 1;\n", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"ico")

    monkeypatch.setattr("control_api.main.DIST_DIR", dist)

    from control_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert "console" in root.text

        deep = await client.get(
            "/tasks/ea3a3a8c-5e53-4a9a-8301-3866970588c0"
        )
        assert deep.status_code == 200
        assert "console" in deep.text
        assert "text/html" in deep.headers["content-type"]

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "window.__console" in asset.text

        favicon = await client.get("/favicon.ico")
        assert favicon.status_code == 200
        assert favicon.content == b"ico"

        missing_api = await client.get("/api/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.json()["detail"] == "Not Found"
