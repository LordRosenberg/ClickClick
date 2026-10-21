"""Typed task exhaustion and idempotent cumulative checkpoint extensions."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


def resumable_budget_failure(task):
    from shared.schemas import TaskStatus
    return bool(task and task.status == TaskStatus.FAILED and task.state
                and task.state.revisable.stop_reason == "budget_exhausted"
                and task.state.revisable.checkpoint_version
                and task.state.revisable.checkpoint_resumable)


def record_execution_attempt(db, task_id):
    """Append once: later continuation must never rewrite a closed attempt."""
    task = db.get_task(task_id)
    if task is None or task.state is None:
        return
    key = str(task.state.revisable.execution_attempt)
    with db.transaction():
        if db.get_agent_record(task_id, "execution_attempt", key):
            return
        db.append_agent_record(task_id, "execution_attempt", key, {
            "attempt": int(key), "status": task.status.value,
            "started_at": task.state.revisable.attempt_started_at or task.created_at,
            "ended_at": task.updated_at, "failure_reason": task.failure_reason,
            "stop_reason": task.state.revisable.stop_reason,
            "budget_kind": task.state.revisable.budget_kind,
            "limits": task.state.revisable.limits.model_dump(),
            "executor_decisions": task.state.step_number,
            "model_calls": task.state.role_invocation_count,
            "device_actions": task.state.revisable.execution_count,
            "step_records": len(db.list_steps(task_id)),
            "task": task.model_dump(mode="json"),
        })


def execution_attempts(db, task_id):
    return [{key: value for key, value in row["payload"].items() if key != "task"}
            for row in db.list_agent_records(task_id, "execution_attempt")]


class TaskBudgetExhausted(BaseException):
    def __init__(self, dimension, required=1):
        self.dimension = dimension
        self.required = required
        super().__init__(dimension)


class BudgetIncrement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor_decisions: int | None = Field(default=None, ge=1, le=100000, strict=True)
    model_calls: int | None = Field(default=None, ge=1, le=100000, strict=True)
    device_actions: int | None = Field(default=None, ge=1, le=100000, strict=True)
    seconds: int | None = Field(default=None, ge=1, le=86400, strict=True)

    @model_validator(mode="after")
    def nonempty(self):
        if not self.model_dump(exclude_none=True):
            raise ValueError("Supply at least one positive budget increment")
        return self


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str | None = Field(default=None, min_length=1, max_length=160)
    budget: BudgetIncrement | None = None

    @model_validator(mode="after")
    def key_required(self):
        if self.budget is not None and not self.request_key:
            raise ValueError("Budget extension requires request_key")
        return self


def extend_budget(db, task, body, *, default_decisions, default_calls):
    """Caller checks checkpoint first; transaction includes both limits and key."""
    if body.budget is None:
        return False
    payload = body.budget.model_dump(exclude_none=True)
    with db.transaction():
        old = db.get_agent_record(task.id, "budget_extension", body.request_key)
        if old:
            if old["payload"]["increments"] != payload:
                raise ValueError("request_key_conflict")
            return False
        limits = task.state.revisable.limits
        for key, amount in payload.items():
            name = "deadline_at" if key == "seconds" else key
            value = getattr(limits, name)
            if value is None:
                value = {"executor_decisions": default_decisions, "model_calls": default_calls}.get(name)
            if value is None:
                raise ValueError(f"{key} has no finite limit to extend")
            setattr(limits, name, value + amount)
        db.append_agent_record(task.id, "budget_extension", body.request_key, {"increments": payload})
        db.update_task(task.id, state=task.state)
    return True
