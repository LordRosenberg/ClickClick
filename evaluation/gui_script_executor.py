"""Recover the current replay step through the normal Executor and Planner."""

from agent.revisable.orchestrator import PlanOrchestrator
from agent.revisable.store import TaskStore
from agent.runtime import create_orchestrator
from agent.skills.script_checkpoint import COMPLETION_KIND, ScriptDispatchBlocked, pending_script_operation
from agent.skills.script_runner import TakeoverDecision
from agent.skills.script_schema import script_fingerprint
from agent.traces import TraceWriter
from shared.recovery_runtime import InfrastructureInterrupted
from shared.revisable import Plan, Stage
from shared.schemas import TaskStatus
from shared.task_budgets import TaskBudgetExhausted


def _completed_choice(allowed):
    return next((decision for decision in (TakeoverDecision.FINISH, TakeoverDecision.CONTINUE_NEXT)
                 if decision in allowed), TakeoverDecision.STOP)


class _TakeoverStore(TaskStore):
    def __init__(self, db, artifacts, task_id, scope):
        super().__init__(db, artifacts, task_id)
        self.scope = scope

    def context(self, state, observation_id, role="executor"):
        return {
            **super().context(state, observation_id, role),
            "script_takeover": {**self.scope, "current_observation_id": observation_id},
        }

    def observe(self, package, state):
        row = self.db.get_agent_record(self.task_id, "observation", package.observation_id)
        if row and "visits" not in row["payload"]:
            # Existing replay observations predate formal takeover history.
            self.put("observation", package.observation_id, {
                **row["payload"], "visits": [], "stage_ids": [], "image_ref": None,
            })
        super().observe(package, state)

    def put(self, kind, key, payload):
        if kind == "event":
            payload = {**payload, "origin": "takeover", "script_step_id": self.scope["step"]["id"]}
        return super().put(kind, key, payload)


class _ScriptRecoveryOrchestrator(PlanOrchestrator):
    async def _wake_and_unlock(self):
        # The replay worker already owns and observes this device.
        return None

    async def _observe(self, *, will_send_image=True):
        package = await super()._observe(will_send_image=will_send_image)
        self.bind_replay_observation(package)
        return package

    def _succeed(self, task_id, state, reason, **kwargs):
        return TaskStatus.SUCCEEDED

    def _fail(self, task_id, state, reason, *, resumable=False):
        self.traces.write(task_id, kind="system", message="Formal script takeover stopped",
                          payload={"event": "script_takeover_stopped", "reason": reason})
        return TaskStatus.FAILED

    def _budget_fail(self, task_id, state, dimension, required=1):
        self.exhausted_replay_budget = (dimension, required)
        return super()._budget_fail(task_id, state, dimension, required)


async def recover_step(context, *, settings, execution, transaction):
    """Run official roles only until the replay runner can verify this step."""
    state = execution.state
    runtime = state.revisable
    task_id, db, artifacts = execution.owner.task_id, execution.store.db, execution.store.artifacts
    fingerprint = script_fingerprint(context.script)
    checkpoint = db.get_agent_record(task_id, "gui_script_checkpoint", "current")
    progress = checkpoint["payload"] if checkpoint else None
    completed_ids = [receipt["step_id"] for receipt in progress["receipts"] if receipt["succeeded"]] if progress else []
    recent_completions = []
    for completed_id in completed_ids[-8:]:
        record = db.get_agent_record(task_id, COMPLETION_KIND, completed_id)
        if record is not None:
            recent_completions.append({key: record["payload"].get(key) for key in (
                "step_id", "observation_id", "operation_id", "confirmation_id",
            )})
    previous = db.get_agent_record(task_id, "gui_script_takeover", "current")
    prior_scope = previous["payload"] if previous else {}
    same_step = (prior_scope.get("script_hash") == fingerprint
                 and prior_scope.get("step", {}).get("id") == context.step.id)
    scope = {
        "instruction": (
            "Recover only the failed script step and achieve its postconditions. Start with the active stage "
            "if its premise still applies. If the path or stage premise is invalid, use Executor replan to "
            "request a revised plan for this same step. Preserve prior completed steps and any pending action. "
            "The script runner independently verifies the result and decides whether to resume. "
            "Leave subsequent script steps and whole-task completion to the runner."
        ),
        "script_id": context.script.script_id,
        "script_revision": context.script.revision,
        "script_hash": fingerprint,
        "stage_at_handoff": runtime.stage.model_dump(mode="json") if runtime.stage else None,
        "stage_id_at_handoff": runtime.stage_id if runtime.stage else None,
        "step": context.step.model_dump(mode="json", exclude_none=True),
        "failure": context.failure.model_dump(mode="json"),
        "observation_at_handoff": context.observation.observation_id if context.observation else None,
        "checkpoint": {
            "version": checkpoint["version"],
            "cursor": progress["cursor"],
            "completed_step_ids": completed_ids,
            "recent_completions": recent_completions,
        } if progress is not None else None,
        "pending_operation": pending_script_operation(execution.store, task_id),
    }
    store = _TakeoverStore(db, artifacts, task_id, scope)
    if scope["pending_operation"] is not None:
        # The runner can poll again or pause if the observed result is inconclusive.
        store.put("gui_script_takeover", "verification", scope)
        return _completed_choice(await context.resume_status())

    if not same_step:
        if previous or runtime.stage is None:
            runtime.plan = Plan(current_stage=Stage(
                goal=f"Satisfy the postconditions of the failed GUI-script step {context.step.id}.",
                target_app=context.script.app_id,
            ))
            runtime.revision += 1
            runtime.stage_start_step = state.step_number
        runtime.next_role = "executor"
        runtime.handoff = "initial"
        runtime.feedback = context.failure.reason
        runtime.delivered_context = {}
    with db.transaction():
        store.put("gui_script_takeover", "current", scope)
        db.update_task(task_id, state=state)

    actions_at_handoff = runtime.execution_count
    decision = TakeoverDecision.STOP

    async def check_return():
        nonlocal decision
        allowed = await context.resume_status()
        decision = _completed_choice(allowed)
        if decision is not TakeoverDecision.STOP:
            return True
        if pending_script_operation(execution.store, task_id) is not None:
            return True
        if (state.revisable.execution_count > actions_at_handoff
                and TakeoverDecision.RETRY_CURRENT in allowed):
            decision = TakeoverDecision.RETRY_CURRENT
            return True
        return False

    traces = TraceWriter(db, artifacts)
    orchestrator = create_orchestrator(
        db, traces, driver=execution.driver, artifacts=artifacts,
        settings=settings, orchestrator_type=_ScriptRecoveryOrchestrator,
    )
    orchestrator.observation_builder = transaction.builder

    def bind_replay_observation(package):
        execution.basis = package.observation_id
        if context.bind_observation is not None:
            context.bind_observation(package)

    orchestrator.bind_replay_observation = bind_replay_observation
    planner, reviewer, executor = (orchestrator.planner_factory(), orchestrator.reviewer_factory(),
                                   orchestrator.executor_factory())
    for role in (planner, reviewer, executor):
        role.traces = traces
    if state.manager_model:
        planner.set_model(state.manager_model)
        reviewer.set_model(state.manager_model)
    if state.executor_model:
        executor.set_model(state.executor_model)
    try:
        status = await orchestrator._run_loop(
            task_id, state, planner, reviewer, executor, task_store=store, stop_before_turn=check_return,
        )
    except (ScriptDispatchBlocked, InfrastructureInterrupted) as exc:
        blocked = exc if isinstance(exc, ScriptDispatchBlocked) else exc.__cause__
        if (not isinstance(blocked, ScriptDispatchBlocked)
                or str(blocked) != "script_step_requires_result_verification"
                or pending_script_operation(execution.store, task_id) is None):
            raise
        # A compound submission reached the verification gate between primitive inputs.
        await check_return()
        return decision
    if status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED} and decision is TakeoverDecision.STOP:
        await check_return()
    if decision is TakeoverDecision.STOP and getattr(orchestrator, "exhausted_replay_budget", None):
        raise TaskBudgetExhausted(*orchestrator.exhausted_replay_budget)
    return decision
