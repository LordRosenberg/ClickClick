from __future__ import annotations

import pytest

from agent.integrations.android_world import (
    ANDROIDWORLD_TEMPORAL_CONVENTIONS,
    ClickClickAgent,
    androidworld_agent_state,
)
from shared.schemas import AgentState, TaskStatus


class FakeEnv:
    def __init__(self) -> None:
        self.resets: list[bool] = []

    def reset(self, go_home: bool = False) -> None:
        self.resets.append(go_home)


class FakeRuntime:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.cancelled: list[str] = []
        self.max_steps: list[int] = []
        self.runs = 0
        self.closed = False

    def create_task(self, goal: str) -> str:
        self.created.append(goal)
        return "task-1"

    def run_one_step(self, task_id: str, *, max_steps: int) -> TaskStatus:
        assert task_id == "task-1"
        self.max_steps.append(max_steps)
        self.runs += 1
        return TaskStatus.RUNNING if self.runs == 1 else TaskStatus.SUCCEEDED

    def task_payload(self, task_id: str, *, previous_step: int):
        assert task_id == "task-1"
        assert previous_step == self.runs - 1
        return {
            "clickclick_task_id": task_id,
            "clickclick_status": (
                TaskStatus.RUNNING.value if self.runs == 1 else TaskStatus.SUCCEEDED.value
            ),
            "clickclick_step_number": self.runs,
            "clickclick_device_action_count": self.runs,
            "clickclick_role_invocations": self.runs * 3,
            "clickclick_executor_steps": [{"seq": self.runs - 1}],
            "clickclick_executor_step": {"seq": self.runs - 1},
            "clickclick_failure_reason": None,
        }

    def cancel_if_running(self, task_id: str) -> None:
        self.cancelled.append(task_id)

    def close(self) -> None:
        self.closed = True


def test_android_world_adapter_advances_one_runtime_step_per_call() -> None:
    env = FakeEnv()
    runtime = FakeRuntime()
    agent = ClickClickAgent(env, runtime=runtime)
    agent.set_max_steps(17)

    first = agent.step("add a contact")
    second = agent.step("add a contact")

    assert first.done is False
    assert second.done is True
    assert runtime.created == ["add a contact"]
    assert runtime.max_steps == [17, 17]
    assert first.data["clickclick_executor_step"]["seq"] == 0
    assert second.data["clickclick_executor_step"]["seq"] == 1

    agent.reset(go_home=True)
    assert runtime.cancelled == ["task-1"]
    assert env.resets == [True]

    agent.close()
    assert runtime.closed is True


def test_android_world_adapter_rejects_goal_change_without_reset() -> None:
    agent = ClickClickAgent(FakeEnv(), runtime=FakeRuntime())
    agent.step("first goal")

    try:
        agent.step("different goal")
    except ValueError as exc:
        assert "without resetting" in str(exc)
    else:
        raise AssertionError("goal change should be rejected")


def test_androidworld_temporal_conventions_are_adapter_scoped() -> None:
    state = androidworld_agent_state("What happens this Sunday?")

    assert state.current_device_date == ""
    assert state.temporal_conventions == [ANDROIDWORLD_TEMPORAL_CONVENTIONS[0]]
    assert AgentState(instruction=state.instruction).temporal_conventions == []


@pytest.mark.parametrize(("goal", "selected"), [
    ("What tasks have I completed Wednesday?", []),
    ("What is due Wednesday?", []),
    ("Count activities this week and next week.", []),
    ("Merge my notes in Markor.", []),
    ("What happens THIS  Sunday?", [0]),
    ("What happens this Sundayish?", []),
    ("What happens the Monday after next?", [1]),
    ("Compare this Sunday with Monday after next.", [0, 1]),
    ("What is my first event after 9:00 AM Wednesday in Simple Calendar Pro?", [2]),
    ("What is my first event after 9AM Wednesday in Simple Calendar Pro?", [2]),
    ("What is my first event after 9:00AM Wednesday in Simple Calendar Pro?", [2]),
    ("What is my first event after 9:00 AM this Sunday in Simple Calendar Pro?", [0, 2]),
    ("What is my first event after 9:00 AM in another calendar?", []),
    ("Create an event after 9:00 AM in Simple Calendar Pro.", []),
])
def test_androidworld_temporal_conventions_match_only_original_expressions(
    goal: str, selected: list[int],
) -> None:
    state = androidworld_agent_state(goal)

    assert state.instruction == goal
    assert state.temporal_conventions == [
        ANDROIDWORLD_TEMPORAL_CONVENTIONS[index] for index in selected
    ]
