"""AndroidWorld-only adapter for running ClickClick as a benchmark agent.

AndroidWorld owns task initialization, its external step budget, success
evaluation, teardown, and checkpoints.  ClickClick owns agent reasoning and
device interaction. One call to :meth:`ClickClickAgent.step` dispatches at most
one ClickClick Executor ``act`` decision. Non-action review/replan decisions do
not consume that action quota.

This module deliberately uses an optional import: the normal ClickClick runtime
does not depend on AndroidWorld.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import re
import threading
from typing import Any, Protocol

try:
    from android_world.agents.base_agent import (
        AgentInteractionResult,
        EnvironmentInteractingAgent,
    )

    _ANDROID_WORLD_AVAILABLE = True
except ImportError:
    _ANDROID_WORLD_AVAILABLE = False

    @dataclass
    class AgentInteractionResult:  # type: ignore[no-redef]
        """Import-safe stand-in used only by adapter unit tests."""

        done: bool
        data: dict[str, Any]

    class EnvironmentInteractingAgent:  # type: ignore[no-redef]
        """Minimal stand-in that keeps the optional module importable."""

        def __init__(
            self,
            env: Any,
            name: str = "",
            transition_pause: float | None = 1.0,
        ) -> None:
            self.env = env
            self.name = name
            self.transition_pause = transition_pause
            self._max_steps: int | None = None

        def reset(self, go_home: bool = False) -> None:
            self.env.reset(go_home=go_home)

        def set_max_steps(self, max_steps: int) -> None:
            self._max_steps = max_steps


from agent.orchestrator import DEFAULT_MAX_STEPS
from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.factory import get_driver
from shared.artifacts import ArtifactStore
from shared.config import Settings, get_settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus


ANDROIDWORLD_TEMPORAL_CONVENTIONS = (
    "AndroidWorld: 'this <weekday>' means the next strictly future occurrence; "
    "if today has that weekday, use the date seven days later.",
    "AndroidWorld: 'the <weekday> after next' denotes that weekday 8-14 days "
    "after the current device date.",
    "AndroidWorld calendar retrieval: 'first event after <time>' includes an "
    "event starting exactly at the stated time.",
)

_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"


def _temporal_conventions_for_goal(goal: str) -> list[str]:
    """Select benchmark exceptions from original wording, never evaluator data.

    Bare weekdays can refer to past or future dates in AndroidWorld. They must
    not inherit the strictly-future meaning of the explicit 'this' template.
    """
    conventions: list[str] = []
    if re.search(rf"\bthis\s+{_WEEKDAY}\b", goal, re.IGNORECASE):
        conventions.append(ANDROIDWORLD_TEMPORAL_CONVENTIONS[0])
    if re.search(rf"\b{_WEEKDAY}\s+after\s+next\b", goal, re.IGNORECASE):
        conventions.append(ANDROIDWORLD_TEMPORAL_CONVENTIONS[1])
    if re.search(r"\bSimple\s+Calendar\s+Pro\b", goal, re.IGNORECASE) and re.search(
        r"\bfirst\s+event\s+after\s+\d{1,2}(?::\d{2})?(?:\s*[ap]m)?\b",
        goal,
        re.IGNORECASE,
    ):
        conventions.append(ANDROIDWORLD_TEMPORAL_CONVENTIONS[2])
    return conventions


def androidworld_agent_state(goal: str) -> AgentState:
    """Build state with benchmark-only temporal semantics kept out of core prompts."""

    return AgentState(
        instruction=goal,
        temporal_conventions=_temporal_conventions_for_goal(goal),
    )


class _Runtime(Protocol):
    """Narrow seam used by the AndroidWorld adapter and its tests."""

    def create_task(self, goal: str) -> str: ...

    def run_one_step(self, task_id: str, *, max_steps: int) -> TaskStatus: ...

    def task_payload(self, task_id: str, *, previous_step: int) -> dict[str, Any]: ...

    def cancel_if_running(self, task_id: str) -> None: ...

    def close(self) -> None: ...


class _ClickClickRuntime:
    """Persistent ClickClick state behind the synchronous AndroidWorld API."""

    def __init__(self, settings: Settings, device_serial: str) -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.device_serial = device_serial
        self.db = Database(settings.db_path)
        artifacts = ArtifactStore(settings.artifacts_dir)
        traces = TraceWriter(self.db, artifacts)
        driver = get_driver(settings, serial=device_serial)

        self.orchestrator = create_orchestrator(
            self.db,
            traces,
            driver=driver,
            artifacts=artifacts,
            settings=settings,
        )
        self._event_loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="clickclick-androidworld",
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait()

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._event_loop)
        self._loop_ready.set()
        self._event_loop.run_forever()

    def create_task(self, goal: str) -> str:
        record = self.db.create_task(
            goal,
            androidworld_agent_state(goal),
            device_serial=self.device_serial,
        )
        return record.id

    def run_one_step(self, task_id: str, *, max_steps: int) -> TaskStatus:
        task = self.db.get_task(task_id)
        if task is None or task.state is None:
            raise KeyError(task_id)
        task.state.revisable.limits.device_actions = max(1, int(max_steps))
        self.db.update_task(task_id, state=task.state)
        future = asyncio.run_coroutine_threadsafe(
            self.orchestrator.run_task(task_id, max_device_actions=1),
            self._event_loop,
        )
        return future.result()

    def task_payload(self, task_id: str, *, previous_step: int) -> dict[str, Any]:
        task = self.db.get_task(task_id)
        if task is None or task.state is None:
            raise KeyError(task_id)
        steps = self.db.list_steps(task_id)
        new_steps = [step for step in steps if int(step["seq"]) >= previous_step]
        device_action_count = task.state.revisable.execution_count
        return {
            "clickclick_task_id": task_id,
            "clickclick_status": task.status.value,
            "clickclick_step_number": task.step_number,
            "clickclick_device_action_count": device_action_count,
            "clickclick_role_invocations": task.state.role_invocation_count,
            "clickclick_executor_steps": new_steps,
            "clickclick_executor_step": new_steps[-1] if new_steps else None,
            "clickclick_failure_reason": task.failure_reason,
            "clickclick_completion_reason": task.state.revisable.completion_reason,
        }

    def cancel_if_running(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task is None or task.state is None:
            return
        if task.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
            self.db.update_task(task_id, status=TaskStatus.CANCELLED, state=task.state)

    def close(self) -> None:
        self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        self._loop_thread.join(timeout=5)
        self._event_loop.close()
        self.db.close()


class ClickClickAgent(EnvironmentInteractingAgent):
    """Expose ClickClick through AndroidWorld's step-oriented agent contract."""

    def __init__(
        self,
        env: Any,
        *,
        device_serial: str | None = None,
        settings: Settings | None = None,
        runtime: _Runtime | None = None,
    ) -> None:
        if runtime is None and not _ANDROID_WORLD_AVAILABLE:
            raise RuntimeError(
                "AndroidWorld is not installed. Install google-research/android_world "
                "before constructing ClickClickAgent."
            )
        super().__init__(env, name="clickclick", transition_pause=None)
        serial = (
            device_serial
            or os.environ.get("CLICKCLICK_ANDROIDWORLD_DEVICE_SERIAL", "").strip()
            or "emulator-5554"
        )
        self._runtime = runtime or _ClickClickRuntime(settings or get_settings(), serial)
        self._task_id: str | None = None
        self._goal: str | None = None
        self._previous_step = 0

    def reset(self, go_home: bool = False) -> None:
        if self._task_id is not None:
            self._runtime.cancel_if_running(self._task_id)
        self._task_id = None
        self._goal = None
        self._previous_step = 0
        super().reset(go_home=go_home)

    def step(self, goal: str) -> AgentInteractionResult:
        if self._task_id is None:
            self._task_id = self._runtime.create_task(goal)
            self._goal = goal
        elif goal != self._goal:
            raise ValueError("AndroidWorld changed the goal without resetting the agent")

        max_steps = self._max_steps or DEFAULT_MAX_STEPS
        status = self._runtime.run_one_step(self._task_id, max_steps=max_steps)
        payload = self._runtime.task_payload(
            self._task_id,
            previous_step=self._previous_step,
        )
        self._previous_step = int(payload["clickclick_step_number"])
        done = status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        return AgentInteractionResult(done=done, data=payload)

    def close(self) -> None:
        """Release the adapter's local SQLite connection."""
        self._runtime.close()


__all__ = [
    "ANDROIDWORLD_TEMPORAL_CONVENTIONS",
    "ClickClickAgent",
    "androidworld_agent_state",
]
