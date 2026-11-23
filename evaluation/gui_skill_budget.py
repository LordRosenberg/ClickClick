"""Meter resources consumed by the isolated Skill regression worker."""

from __future__ import annotations

import time

from agent.skills.learning import LearningStopped, TERMINAL
from shared.revisable import TaskLimits
from shared.task_budgets import TaskBudgetExhausted


def bounded_seconds(budget, seconds):
    if budget is None:
        return seconds
    budget.check(new_request=False)
    return min(seconds, budget.remaining_seconds())


def ordinary_limits(budget, rounds, seconds):
    remaining = rounds if budget is None else budget.max_actions - budget.actions
    if remaining <= 0:
        raise LearningStopped("action_budget")
    if budget is not None:
        budget.check()
    return TaskLimits(device_actions=min(rounds, remaining), model_calls=rounds * 6,
                      deadline_at=time.time() + bounded_seconds(budget, seconds))


def bind_model_budget(orchestrator, budget):
    if budget is None:
        return
    original = orchestrator._model_call_meter

    def meter_for(task_id, state, phase):
        task_meter = original(task_id, state, phase)

        def meter(kind, payload):
            try:
                budget.check()
            except LearningStopped as exc:
                dimension = "task_time" if str(exc) == "time_budget" else "model_calls"
                raise TaskBudgetExhausted(dimension) from exc
            task_meter(kind, payload)
            budget.meter(kind, payload)

        return meter

    orchestrator._model_call_meter = meter_for


def charge_actions(budget, actions):
    budget.actions += actions
    costs = budget.stage_costs.setdefault(budget.phase, {"calls": 0, "actions": 0})
    costs["actions"] += actions


def settle_actions(budget, task, allowance, uncertain):
    if budget is None:
        return
    known = task is not None and task.status in TERMINAL and task.state is not None and not uncertain
    actions = task.state.revisable.execution_count if known else allowance
    charge_actions(budget, actions)
    if actions > allowance:
        raise LearningStopped("device_action_allowance_exceeded")


def replay_dispatch_meter(budget):
    if budget is None:
        return None

    def charge():
        budget.check(new_request=False)
        if budget.actions >= budget.max_actions:
            raise LearningStopped("action_budget")
        charge_actions(budget, 1)

    return charge
