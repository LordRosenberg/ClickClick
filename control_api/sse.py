"""In-process SSE pub/sub for live task trace streaming.

The Orchestrator runs in the same event loop as the Control API
(``asyncio.create_task(_run(...))`` in ``control_api.main``), so an
in-process pub/sub gives SSE subscribers zero-network, same-loop-turn
delivery of trace events. Each task keeps a list of subscriber
``asyncio.Queue`` objects; ``EventBus.publish`` fans an event out to every
subscriber queue. The ``GET /api/tasks/{id}/stream`` endpoint replays
already-persisted ticks from the DB first, then ``await queue.get()`` for
live events.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Per-task cap on concurrent SSE subscribers. Prevents a single task from
# holding an unbounded number of open streams (each queue is kept alive for
# the lifetime of its HTTP connection). Over the cap → 429 Too Many Subscribers.
MAX_SUBSCRIBERS_PER_TASK = 8


class TooManySubscribers(Exception):
    """Raised when a task already has the maximum number of subscribers."""


class EventBus:
    """In-process pub/sub keyed by task id.

    Each subscriber gets its own bounded ``asyncio.Queue``. Producers
    (``TraceWriter.write``) call ``publish(task_id, event)`` which puts the
    event onto every subscriber queue for that task. Subscribers detach via
    the returned ``unsubscribe`` callback (called from the stream endpoint's
    ``finally`` block on disconnect).
    """

    def __init__(self, max_subscribers: int | None = None) -> None:
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        # Resolve at call time so the cap can be tuned by monkeypatching the
        # module global (e.g. tests), rather than being bound at def time.
        self._max = MAX_SUBSCRIBERS_PER_TASK if max_subscribers is None else max_subscribers

    def subscribe(
        self, task_id: str
    ) -> tuple[asyncio.Queue[dict[str, Any]], "callable() -> None"]:
        """Register a new subscriber for ``task_id``.

        Returns ``(queue, unsubscribe)``. Raises ``TooManySubscribers`` if
        the per-task cap is exceeded.
        """
        subs = self._subs.setdefault(task_id, [])
        if len(subs) >= self._max:
            raise TooManySubscribers(
                f"task {task_id} already has {self._max} subscribers"
            )
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        subs.append(queue)

        def unsubscribe() -> None:
            try:
                subs.remove(queue)
            except ValueError:
                pass
            if not subs:
                self._subs.pop(task_id, None)

        return queue, unsubscribe

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every subscriber queue for ``task_id``.

        A full queue (slow consumer) drops the event for that subscriber
        rather than blocking the producer — the live stream is best-effort;
        the persisted DB remains the source of truth and late subscribers
        get a full replay on connect.
        """
        for queue in list(self._subs.get(task_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop this event for them. They still have
                # the DB replay + subsequent live events.
                pass

    def subscriber_count(self, task_id: str) -> int:
        return len(self._subs.get(task_id, []))
