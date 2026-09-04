"""Scripted Planner / Reviewer / Executor fakes for closed-loop tests.

The fakes mirror the focused production role APIs. Scripts are strict: an
exhausted Planner or Reviewer script raises instead of inventing a terminal
outcome, so tests cannot pass through a lucky compatibility fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
import inspect
from typing import Any

from agent.executor import Executor
from agent.planner import Planner
from agent.reviewer import Reviewer
from perception.observation import ObservationPackage
from shared.schemas import (
    Action,
    ActionResult,
    AgentState,
    CanonicalUI,
    ExecutorDecisionKind,
    ExecutorStep,
    ObservationMode,
    PlannerDecision,
    ReviewerDecision,
    TaskContractBody,
)


def _observation_refs(package: ObservationPackage) -> dict[str, Any]:
    return {
        "som_ref": package.som_ref,
        "tree_ref": package.tree_ref,
        "observation_mode": package.mode.value,
        "gap_reasons": list(package.gap_reasons or []),
        "estimated_tokens": getattr(package.ui, "estimated_tokens", None),
        "observation_id": package.observation_id,
    }


def _minimal_role_refs(
    *,
    phase: str,
    package: ObservationPackage | None = None,
) -> dict[str, Any]:
    refs: dict[str, Any] = {
        "llm_input_ref": None,
        "llm_output_ref": None,
        "tool_calls": [],
        "agent_rounds": [],
        "phase": phase,
    }
    if package is not None:
        refs["active_package"] = package
    return refs


class _FakeDecisionRole:
    """Small shared lifecycle surface used by the two cognitive-role fakes."""

    driver: Any = None
    artifacts: Any = None

    def _init_role(self) -> None:
        self._model = "fake"
        self._traces: Any | None = None

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, model: str) -> None:
        self._model = model

    @property
    def traces(self) -> Any | None:
        return self._traces

    @traces.setter
    def traces(self, value: Any | None) -> None:
        self._traces = value

    async def aclose(self) -> None:
        return None


async def _record_fake_model_call(meter: Any, role: str) -> None:
    if meter is None:
        return
    result = meter("model_call_started", {
        "role": role, "round": 1, "kind": "fake",
    })
    if inspect.isawaitable(result):
        await result


class FakePlanner(_FakeDecisionRole, Planner):
    """Return scripted PlannerDecision entries, one per rolling plan call."""

    def __init__(self, decisions: Sequence[PlannerDecision]) -> None:
        self._init_role()
        self._decisions = list(decisions)
        self._index = 0
        self.seen_states: list[AgentState] = []
        self.seen_packages: list[ObservationPackage] = []
        self.seen_deviations: list[str] = []

    async def decide(
        self,
        state: AgentState,
        package: ObservationPackage,
        *,
        deviation: str = "",
        task_id: str = "",
        model_call_meter: Any = None,
    ) -> tuple[PlannerDecision, dict[str, Any], dict[str, Any]]:
        del task_id
        await _record_fake_model_call(model_call_meter, "planner")
        if self._index >= len(self._decisions):
            raise AssertionError("FakePlanner decision script exhausted")
        decision = self._decisions[self._index]
        self._index += 1
        self.seen_states.append(state.model_copy(deep=True))
        self.seen_packages.append(package)
        self.seen_deviations.append(deviation)
        return (
            decision,
            _minimal_role_refs(phase="planning", package=package),
            _observation_refs(package),
        )


class FakeReviewer(_FakeDecisionRole, Reviewer):
    """Author one scripted scope and return scripted boundary verdicts."""

    def __init__(
        self,
        decisions: Sequence[ReviewerDecision],
        *,
        task_scope: TaskContractBody,
    ) -> None:
        self._init_role()
        self._decisions = list(decisions)
        self._index = 0
        self._task_scope = task_scope
        self.scope_calls = 0
        self.seen_scope_states: list[AgentState] = []
        self.seen_boundary_states: list[AgentState] = []
        self.seen_packages: list[ObservationPackage] = []
        self.seen_executor_reports: list[str] = []
        self.seen_boundary_reasons: list[str] = []
        self.seen_terminal_reviews: list[bool] = []
        self.seen_review_requirement_refs: list[str] = []

    async def author_task_scope(
        self,
        state: AgentState,
        *,
        task_id: str = "",
        model_call_meter: Any = None,
    ) -> tuple[TaskContractBody, dict[str, Any]]:
        del task_id
        await _record_fake_model_call(model_call_meter, "reviewer")
        self.scope_calls += 1
        self.seen_scope_states.append(state.model_copy(deep=True))
        return self._task_scope, _minimal_role_refs(phase="task_scope")

    async def decide(
        self,
        state: AgentState,
        package: ObservationPackage,
        *,
        executor_report: str = "",
        boundary_reason: str = "",
        terminal_review: bool = False,
        review_requirement_ref: str = "",
        task_id: str = "",
        model_call_meter: Any = None,
    ) -> tuple[ReviewerDecision, dict[str, Any], dict[str, Any]]:
        del task_id
        await _record_fake_model_call(model_call_meter, "reviewer")
        if self._index >= len(self._decisions):
            raise AssertionError("FakeReviewer decision script exhausted")
        decision = self._decisions[self._index]
        self._index += 1
        self.seen_boundary_states.append(state.model_copy(deep=True))
        self.seen_packages.append(package)
        self.seen_executor_reports.append(executor_report)
        self.seen_boundary_reasons.append(boundary_reason)
        self.seen_terminal_reviews.append(terminal_review)
        self.seen_review_requirement_refs.append(review_requirement_ref)
        refs = _minimal_role_refs(phase="boundary_review", package=package)
        refs["reviewer_packet_digest"] = decision.packet_digest
        return decision, refs, _observation_refs(package)


def fake_task_scope(
    text: str = "the whole requested outcome is established",
) -> TaskContractBody:
    """Return an explicit UI-independent task scope for FakeReviewer scripts."""
    return TaskContractBody(final_ui_state=[text])


class FakeExecutor(Executor):
    """Returns scripted Executor decisions in order.

    Each scripted entry may be an `Action`, an `ExecutorStep`, or the historical
    test tuple `(Action, request_review, summary)`. The tuple shorthand belongs
    only to test fixtures, not the production protocol.
    """

    def __init__(
        self,
        actions: Sequence[Any],
        *,
        results: Sequence[ActionResult] | None = None,
        ui_states: Sequence[CanonicalUI] | None = None,
    ) -> None:
        self.driver = None  # type: ignore[assignment]
        self.artifacts = None  # type: ignore[assignment]
        self.model = "fake"
        self._raw = list(actions)
        self._results = list(results) if results else []
        self._ui_states = list(ui_states) if ui_states else []
        self._idx = 0
        self.seen_logs: list[list[Any]] = []

    def _scripted_entry(self) -> ExecutorStep:
        if self._idx < len(self._raw):
            entry = self._raw[self._idx]
        else:
            entry = Action(type="sleep", duration_ms=100)
        self._idx += 1
        if isinstance(entry, tuple):
            if len(entry) == 3:
                action, boundary, summary = entry
                return ExecutorStep(
                    decision=(ExecutorDecisionKind.REQUEST_REVIEW if boundary else ExecutorDecisionKind.ACT),
                    action=None if boundary else action,
                    summary=str(summary or ("boundary established" if boundary else f"{action.type} (fake)")),
                )
            if len(entry) == 2:
                action, boundary = entry
                return ExecutorStep(
                    decision=(ExecutorDecisionKind.REQUEST_REVIEW if boundary else ExecutorDecisionKind.ACT),
                    action=None if boundary else action,
                    summary="boundary established" if boundary else f"{action.type} (fake)",
                )
        if isinstance(entry, ExecutorStep):
            return entry
        return ExecutorStep(
            decision=ExecutorDecisionKind.ACT,
            action=entry,
            summary=f"{entry.type} (fake)",
        )

    def _scripted_result(self) -> ActionResult:
        if self._idx - 1 < len(self._results):
            return self._results[self._idx - 1]
        return ActionResult(success=True, message="fake-ok")

    async def act_once(  # type: ignore[override]
        self,
        subgoal: str,
        package: "ObservationPackage | None" = None,
        prior_result: str = "",
        *,
        task_id: str = "",
        state: Any = None,
        model_call_meter: Any = None,
    ) -> tuple[ExecutorStep, ActionResult, CanonicalUI, ObservationMode, dict[str, Any]]:
        del prior_result, task_id
        await _record_fake_model_call(model_call_meter, "executor")
        active_ids = set(getattr(state, "active_timeline_lineage_ids", []) or [])
        self.seen_logs.append([
            event
            for event in getattr(getattr(state, "task_memory", None), "events", [])
            if event.kind == "attempt" and event.lineage_id in active_ids
        ])
        step = self._scripted_entry()
        result = self._scripted_result()
        if self._ui_states:
            # Cycle through provided ui states by call index.
            ui_idx = min(self._idx - 1, len(self._ui_states) - 1)
            ui = self._ui_states[ui_idx]
        else:
            ui = CanonicalUI(app_id="com.example.demo", activity=".MainActivity")
        step.result = result.message
        step.basis_observation_id = package.observation_id if package is not None else ""
        return step, result, ui, ObservationMode.TREE_ONLY, {
            "delivered_evidence_digest": "fake-delivered-evidence",
            "observation_id": package.observation_id if package is not None else "",
            "active_package": package,
        }

__all__ = ["FakePlanner", "FakeReviewer", "FakeExecutor", "fake_task_scope"]
