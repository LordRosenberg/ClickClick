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

        run_db.close()
        shutil.rmtree(run_root)
        listed_after_delete = (await client.get("/api/tasks")).json()
        assert all(item["id"] != task.id for item in listed_after_delete)
