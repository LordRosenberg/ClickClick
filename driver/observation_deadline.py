"""Monotonic end-to-end deadlines and typed observation-stage failures."""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable


CURRENT_DEADLINE_MS = 12_000
TEMPORAL_DEADLINE_MS = 8_000
TREE_PRIMARY_ATTEMPT_TIMEOUT_MS = 2_500
SCREENCAP_MAX_BUDGET_MS = 3_500
SCRCPY_START_ATTEMPT_TIMEOUT_MS = 4_000
SCRCPY_ROUND_REFRESH_TIMEOUT_MS = 800
CURRENT_BASELINE_MAX_AGE_MS = 5_000
TEMPORAL_BASELINE_MAX_AGE_MS = 10_000


class ObservationStageError(RuntimeError):
    """A deadline-aware observation stage failed or was not admitted."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        elapsed_ms: float = 0.0,
        budget_ms: float = 0.0,
        timed_out: bool = False,
        fallback_edges: list[dict[str, str]] | None = None,
        provider_attempts: list[dict[str, object]] | None = None,
        cancelled_tasks: list[str] | None = None,
        capture_attempt_count: int = 0,
    ) -> None:
        super().__init__(f"{stage}: {reason}")
        self.stage = stage
        self.reason = reason
        self.elapsed_ms = max(0.0, float(elapsed_ms))
        self.budget_ms = max(0.0, float(budget_ms))
        self.timed_out = bool(timed_out)
        self.fallback_edges = list(fallback_edges or [])
        self.provider_attempts = list(provider_attempts or [])
        self.cancelled_tasks = list(cancelled_tasks or [])
        self.capture_attempt_count = max(0, int(capture_attempt_count))


@dataclass
class ObservationDeadline:
    """One propagated monotonic deadline with per-stage admission/timing."""

    mode: str
    budget_ms: float
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    ends_at: float = field(init=False)
    stage_timings: list[dict[str, float | str | bool]] = field(default_factory=list)
    provider_attempts: list[dict[str, object]] = field(default_factory=list)
    fallback_edges: list[dict[str, str]] = field(default_factory=list)
    cancelled_tasks: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.budget_ms = max(1.0, float(self.budget_ms))
        self.started_at = self.clock()
        self.ends_at = self.started_at + self.budget_ms / 1000.0

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, (self.clock() - self.started_at) * 1000.0)

    @property
    def remaining_ms(self) -> float:
        return max(0.0, (self.ends_at - self.clock()) * 1000.0)

    def remaining_seconds(self, stage: str) -> float:
        """Return the outer-fuse remainder; reject only exhausted work."""
        remaining = self.remaining_ms
        if remaining <= 0:
            self.stage_timings.append({
                "stage": stage,
                "elapsed_ms": 0.0,
                "budget_ms": 0.0,
                "outcome": "not_admitted",
                "timed_out": True,
            })
            raise ObservationStageError(
                stage,
                "outer_deadline_exhausted",
                budget_ms=0.0,
                timed_out=True,
            )
        return remaining / 1000.0

    def record(
        self,
        stage: str,
        started_at: float,
        *,
        budget_s: float,
        outcome: str = "ok",
        timed_out: bool = False,
    ) -> None:
        self.stage_timings.append({
            "stage": stage,
            "elapsed_ms": max(0.0, (self.clock() - started_at) * 1000.0),
            "budget_ms": max(0.0, budget_s * 1000.0),
            "outcome": outcome,
            "timed_out": bool(timed_out),
        })

    def record_attempt(self, attempt: dict[str, object]) -> None:
        self.provider_attempts.append(dict(attempt))

    def telemetry(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "deadline_ms": self.budget_ms,
            "elapsed_ms": self.elapsed_ms,
            "remaining_ms": self.remaining_ms,
            "stage_timings": list(self.stage_timings),
            "provider_attempts": list(self.provider_attempts),
            "fallback_edges": list(self.fallback_edges),
            "cancelled_tasks": list(self.cancelled_tasks),
        }
