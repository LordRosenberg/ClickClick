"""A cooperative operator pause, deliberately distinct from task cancellation."""

import asyncio


class TaskPauseRequested(asyncio.CancelledError):
    """Propagate through tool handlers without turning pause into a tool error."""
