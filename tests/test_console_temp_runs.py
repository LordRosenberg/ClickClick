"""Console aggregation of isolated evaluation runs under a temp root."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState, LogLevel, TaskStatus, TraceEvent


@pytest.mark.asyncio
async def test_summary_preserves_list_metadata_without_execution_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    from control_api.main import create_app

    app = create_app(include_temp_runs=False)
    db = app.state.data_sources.primary.db
    task = db.create_task("inspect settings", AgentState(
        instruction="inspect settings", current_subgoal="open settings", step_number=7,
    ), device_serial="device-1")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            summary = (await client.get("/api/tasks?summary=true")).json()[0]
            full = (await client.get("/api/tasks")).json()[0]
            assert "state" not in summary and "plan" not in summary
            assert full["state"] is not None
            for key in ("id", "instruction", "status", "current_subgoal", "step_number", "device_serial"):
                assert summary[key] == full[key]
            assert summary["id"] == task.id
            assert summary["step_number"] == 7
    finally:
        app.state.data_sources.close()
        db.close()


def test_discovery_is_throttled_but_task_status_is_live(tmp_path, monkeypatch):
    from control_api.data_sources import ConsoleDataSources
    import control_api.data_sources as module

    db = Database(tmp_path / "clickclick.db")
    sources = ConsoleDataSources(db, ArtifactStore(tmp_path / "artifacts"), temp_root=None)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    scans = []
    monkeypatch.setattr(sources, "_discover_paths", lambda: scans.append(True) or set())
    try:
        task = db.create_task("test", AgentState(instruction="test"))
        assert sources.list_tasks(include_state=False)[0][0].status == TaskStatus.QUEUED
        db.update_task(task.id, status=TaskStatus.SUCCEEDED)
        assert sources.list_tasks(include_state=False)[0][0].status == TaskStatus.SUCCEEDED
        assert len(scans) == 1
        clock[0] += 16
        sources.sources()
        assert len(scans) == 2
    finally:
        sources.close()
        db.close()


def test_task_source_cache_reuses_and_invalidates_external_source(tmp_path, monkeypatch):
    from control_api.data_sources import ConsoleDataSources

    primary_db = Database(tmp_path / "primary" / "clickclick.db")
    run_root = tmp_path / "runs" / "candidate"
    run_db = Database(run_root / "clickclick.db")
    task = run_db.create_task("cached external task", AgentState(instruction="cached external task"))
    sources = ConsoleDataSources(
        primary_db,
        ArtifactStore(tmp_path / "primary" / "artifacts"),
        temp_root=None,
    )
    discovered = [{run_db.path.resolve()}]
    monkeypatch.setattr(sources, "_discover_paths", lambda: set(discovered[0]))
    try:
        sources.refresh()
        located = sources.locate(task.id)
        assert located is not None and located[1].read_only is True

        # A cached task lookup remains direct even when a new discovery would be due.
        sources._next_discovery_at = 0.0
        monkeypatch.setattr(
            sources,
            "_discover_paths",
            lambda: pytest.fail("cached task lookup rediscovered data sources"),
        )
        assert sources.locate(task.id) is not None

        # Restoring discovery with the source absent closes it and evicts ownership.
        monkeypatch.setattr(sources, "_discover_paths", lambda: set())
        sources.refresh()
        assert task.id not in sources._task_sources
        assert sources.locate(task.id) is None
    finally:
        sources.close()
        run_db.close()
        primary_db.close()


@pytest.mark.asyncio
async def test_console_exposes_temp_run_read_only_and_forgets_deleted_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary_root = tmp_path / "primary"
    temp_root = tmp_path / "tmp-runs"
    run_root = temp_root / "clickclick-eval" / "candidate" / "r1-case"
    run_db = Database(run_root / "clickclick.db")
    run_artifacts = ArtifactStore(run_root / "artifacts")

    state = AgentState(instruction="isolated device evaluation", step_number=2)
    task = run_db.create_task(state.instruction, state, device_serial="device-1")
    run_db.update_task(task.id, status=TaskStatus.SUCCEEDED, state=state)
    run_db.add_step(
        task.id,
        "verify result",
        2,
        {"act": "inspect result", "success": True, "reason": "done"},
    )
    run_db.add_trace(
        TraceEvent(
            task_id=task.id,
            node_id="verify result",
            step_seq=2,
            kind="reviewer_decision",
            level=LogLevel.INFO,
            message="done",
            payload={
                "verdict": "done",
                "reason": "task evidence accepted",
                "answer": "verified",
                "packet_digest": "temp-packet",
            },
        )
    )
    artifact_ref = run_artifacts.save_json("llm", {"source": "temp-run"})

    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(primary_root))
    from control_api.main import create_app

    app = create_app(include_temp_runs=True, temp_runs_root=temp_root)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = (await client.get("/api/tasks")).json()
        external = next(item for item in listed if item["id"] == task.id)
        assert external["read_only"] is True
        assert external["data_source"] == "temp/clickclick-eval/candidate/r1-case"
        assert external["step_number"] == 2
        assert external["execution_elapsed_ms"] >= 0

        detail = (await client.get(f"/api/tasks/{task.id}")).json()
        assert detail["read_only"] is True
        assert detail["step_number"] == 2

        timeline = (await client.get(f"/api/tasks/{task.id}/timeline")).json()
        assert timeline["read_only"] is True
        assert timeline["steps"][0]["step_seq"] == 2
        assert timeline["execution_elapsed_ms"] >= 0
        assert timeline["calls"][0]["role"] == "reviewer"
        assert timeline["metrics"]["runtime_terminal_status"] == {
            "status": "succeeded",
            "failure_reason": None,
            "semantic_true_success": "not_assessed",
        }

        traces = await client.get(f"/api/tasks/{task.id}/traces")
        assert traces.status_code == 200
        assert traces.json()[0]["message"] == "done"

        artifact = await client.get(f"/api/artifacts/{artifact_ref}")
        assert artifact.status_code == 200
        assert artifact.json() == {"source": "temp-run"}
        assert (await client.get(
            f"/api/tasks/missing-task/artifacts/{artifact_ref}"
        )).status_code == 404

        # Once a task has been located, task-scoped artifact reads must not
        # trigger another recursive discovery scan or search other sources.
        sources = app.state.data_sources
        original_discover = sources._discover_paths
        sources._next_discovery_at = 0.0
        monkeypatch.setattr(
            sources,
            "_discover_paths",
            lambda: pytest.fail("task-scoped artifact lookup rediscovered data sources"),
        )
        scoped_artifact = await client.get(
            f"/api/tasks/{task.id}/artifacts/{artifact_ref}"
        )
        assert scoped_artifact.status_code == 200
        assert scoped_artifact.json() == {"source": "temp-run"}
        escaped = await client.get(
            f"/api/tasks/{task.id}/artifacts/%2E%2E%2Foutside.json"
        )
        assert escaped.status_code == 403

        monkeypatch.setattr(sources, "_discover_paths", original_discover)

        run_db.close()
        # Windows cannot unlink a database while the Console still holds a reader.
        app.state.data_sources.close()
        shutil.rmtree(run_root)
        listed_after_delete = (await client.get("/api/tasks")).json()
        assert all(item["id"] != task.id for item in listed_after_delete)


@pytest.mark.asyncio
async def test_console_exposes_nested_eval_db_under_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary_root = tmp_path / "data"
    eval_root = primary_root / "androidworld-deepseek-additional-20260908" / "BrowserMaze"
    run_db = Database(eval_root / "clickclick.db")
    run_artifacts = ArtifactStore(eval_root / "artifacts")

    state = AgentState(instruction="navigate the maze", step_number=9)
    task = run_db.create_task(state.instruction, state, device_serial="device-1")
    run_db.update_task(task.id, status=TaskStatus.FAILED, state=state)
    artifact_ref = run_artifacts.save_json("llm", {"source": "data-dir-eval"})

    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(primary_root))
    from control_api.main import create_app

    app = create_app(include_temp_runs=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = (await client.get("/api/tasks")).json()
        external = next(item for item in listed if item["id"] == task.id)
        assert external["read_only"] is True
        assert external["data_source"] == (
            "eval/androidworld-deepseek-additional-20260908/BrowserMaze"
        )

        detail = (await client.get(f"/api/tasks/{task.id}")).json()
        assert detail["read_only"] is True
        assert detail["data_source"] == external["data_source"]

        artifact = await client.get(f"/api/artifacts/{artifact_ref}")
        assert artifact.status_code == 200
        assert artifact.json() == {"source": "data-dir-eval"}

    run_db.close()


@pytest.mark.asyncio
async def test_console_exposes_deeply_nested_eval_db_under_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    primary_root = tmp_path / "data"
    eval_root = (
        primary_root
        / "androidworld-hard10-20260911"
        / "ordinary"
        / "plan_executor"
        / "SystemBrightnessMin"
    )
    run_db = Database(eval_root / "clickclick.db")
    state = AgentState(instruction="turn brightness to minimum", step_number=4)
    task = run_db.create_task(state.instruction, state, device_serial="device-1")
    run_db.update_task(task.id, status=TaskStatus.SUCCEEDED, state=state)

    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(primary_root))
    from control_api.main import create_app

    app = create_app(include_temp_runs=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = (await client.get("/api/tasks")).json()
        external = next(item for item in listed if item["id"] == task.id)
        assert external["read_only"] is True
        assert external["data_source"] == (
            "eval/androidworld-hard10-20260911/ordinary/plan_executor/SystemBrightnessMin"
        )

    run_db.close()
