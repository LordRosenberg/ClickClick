"""Task snapshots and presentation events for the Console workbench."""

from shared.hitl import HumanInputBroker
from shared.schemas import TaskStatus
from shared.steering import SteeringInbox
from shared.task_budgets import resumable_budget_failure
from control_api.services import task_execution_elapsed_ms


def workbench_task(task, db, *, read_only: bool, preview_supported: bool) -> dict:
    state = task.state
    runtime = state.revisable if state else None
    hitl_enabled = not read_only and HumanInputBroker(db).config(task.id)["enabled"]
    observation = db.latest_agent_record(task.id, "observation", image_only=True)
    frame = observation["payload"] if observation else {}
    image_ref = frame.get("image_ref")
    stage = runtime.stage if runtime else None
    return {
        "id": task.id,
        "instruction": task.instruction,
        "status": task.status.value,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "execution_elapsed_ms": task_execution_elapsed_ms(task),
        "device_serial": task.device_serial,
        "current_subgoal": (stage.goal if stage else None) or task.current_subgoal,
        "result": runtime.completion_reason if runtime else None,
        "failure_reason": task.failure_reason,
        "read_only": read_only,
        "can_resume": not read_only and (
            task.status == TaskStatus.PAUSED or resumable_budget_failure(task)
            or "recover" in task.recovery.get("available_actions", [])
        ),
        "hitl_enabled": hitl_enabled,
        "adjustments": SteeringInbox(db, task.id).projection() if hitl_enabled else [],
        "models": {
            "decision": state.manager_model if state else None,
            "executor": state.executor_model if state else None,
        },
        "preview_supported": preview_supported,
        "screenshot": {
            "ref": image_ref,
            "recorded_at": frame.get("recorded_at"),
        } if isinstance(image_ref, str) and image_ref else None,
    }


ROUND_EVENTS = {"agent_llm_round_started", "agent_llm_round_finished", "agent_llm_stream"}
TOOL_EVENTS = {"agent_tool_started", "agent_tool_finished", "agent_tool_failed"}
LIFECYCLE_EVENTS = {"operator_pause_requested", "task_paused", "operator_resume_requested"}


def workbench_event(event: dict) -> dict | None:
    kind = event["kind"]
    payload = event["payload"]
    event_name = payload.get("event")
    if kind in ROUND_EVENTS:
        keys = ("round_id", "role", "attempt", "sequence", "progress_text", "model", "tool_call_count")
        projected = {key: payload[key] for key in keys if key in payload}
    elif kind in TOOL_EVENTS:
        keys = ("call_id", "invocation_id", "name", "status", "elapsed_ms")
        projected = {key: payload[key] for key in keys if key in payload}
    elif kind == "executor_tick":
        action = payload.get("action")
        result = payload.get("action_result")
        projected = {
            "action_type": action.get("type") if isinstance(action, dict) else None,
            "success": result.get("success") if isinstance(result, dict) else None,
        }
        text = payload.get("summary")
        if isinstance(text, str):
            projected["progress_text"] = text[:2000]
    elif kind == "system" and event_name == "human_input_resolved":
        projected = {key: payload.get(key) for key in ("interaction_id", "outcome")}
    elif kind == "system" and isinstance(event_name, str) and event_name in LIFECYCLE_EVENTS:
        projected = {"event": event_name}
    else:
        return None
    return {**event, "message": "", "payload_ref": None, "payload": projected}
