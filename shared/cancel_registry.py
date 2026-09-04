"""In-process operator cancel flags shared by Control API and Orchestrator."""

from __future__ import annotations


class CancelRegistry:
    """Process-local set of task ids that operators have asked to stop."""

    def __init__(self) -> None:
        self._requested: set[str] = set()

    def request(self, task_id: str) -> None:
        self._requested.add(task_id)

    def is_requested(self, task_id: str) -> bool:
        return task_id in self._requested

    def clear(self, task_id: str) -> None:
        self._requested.discard(task_id)
