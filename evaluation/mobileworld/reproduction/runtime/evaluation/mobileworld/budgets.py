"""MobileWorld prediction rounds, with separate local resource guards."""

from __future__ import annotations

import math


def episode_budgets(
    max_steps: int, max_model_calls: int | None = None, max_seconds: float = 2400,
) -> dict[str, int | float | None]:
    if max_steps <= 0 or (max_model_calls is not None and max_model_calls <= 0):
        raise ValueError("round and explicit model-call budgets must be positive")
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("local time guard must be positive and finite")
    return {
        "rounds": max_steps,
        "executor_decisions": None,
        "model_calls": max_model_calls,
        "seconds": max_seconds,
    }
