import json

from httpx import ASGITransport, AsyncClient

from shared.hitl import HitlConfig, HumanInputBroker
from shared.schemas import TaskStatus, TraceEvent
from shared.steering import SteeringInbox
from tests.test_task_pause import app


async def test_workbench_recovers_all_steering_receipts_and_keeps_diagnostics_in_trace(app):
    db = app.state.db
    task = db.create_task("Export the weekly report", device_serial="fixture")
    db.update_task(task.id, status=TaskStatus.PAUSED)
    HumanInputBroker(db).configure(task.id, HitlConfig(enabled=True))
    base = f"/api/tasks/{task.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/workbench/tasks")).json()[0]["id"] == task.id
        for i, instruction in enumerate(("Yesterday only", "Save to Downloads"), 1):
            response = await client.post(f"/api/assistant/tasks/{task.id}/steer",
                                         json={"instruction": instruction, "request_key": str(i)})
            assert response.status_code == 200
            assert response.json()["revision"] == i

        SteeringInbox(db, task.id).applied(2, "Applied the new date and path")
        db.update_task(task.id, status=TaskStatus.SUCCEEDED)
        result = (await client.get(f"{base}/workbench")).json()
        assert result["instruction"] == task.instruction
        assert result["status"] == "succeeded"
        assert result["hitl_enabled"] is True
        assert result["preview_supported"] is False
        assert [change["status"] for change in result["adjustments"]] == ["applied", "applied"]
        assert [change["instruction"] for change in result["adjustments"]] == ["Yesterday only", "Save to Downloads"]

        db.add_trace(TraceEvent(task_id=task.id, kind="agent_llm_round_finished",
                                payload={"round_id": "round", "progress_text": "Checked dates",
                                         "reasoning_summary": "diagnostic-only", "request_ref": "debug"}))
        db.add_trace(TraceEvent(task_id=task.id, kind="agent_tool_finished",
                                payload={"call_id": "call", "invocation_id": "inv", "name": "ask_user",
                                         "status": "succeeded", "arguments": {"text": "diagnostic-only"}}))
        for event in ("operator_pause_requested", "task_paused", "operator_resume_requested"):
            db.add_trace(TraceEvent(task_id=task.id, kind="system", payload={"event": event}))
        body = (await client.get(f"{base}/stream?view=workbench")).text
        projected = [json.loads(item.removeprefix("data: ")) for item in body.splitlines() if item.startswith("data: ")]
        assert [event["payload"] for event in projected] == [
            {"round_id": "round", "progress_text": "Checked dates"},
            {"call_id": "call", "invocation_id": "inv", "name": "ask_user", "status": "succeeded"},
            {"event": "operator_pause_requested"},
            {"event": "task_paused"},
            {"event": "operator_resume_requested"},
        ]
        assert "diagnostic-only" in (await client.get(f"{base}/stream")).text
