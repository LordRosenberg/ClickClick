"""Scripted ContractAuthor / Planner / Reviewer / Executor test doubles.

The fakes mirror the focused production role APIs. Scripts are strict: an
exhausted Planner or Reviewer script raises instead of inventing a terminal
outcome, so tests cannot pass through a lucky compatibility fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
import inspect
from typing import Any

from agent.executor import Executor
from perception.observation import ObservationPackage
from shared.schemas import Action, ActionResult, AgentState, CanonicalUI, ExecutorDecisionKind, ExecutorStep, ObservationMode


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


# Backward-compatible fixture name for downstream test modules.


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
                    review_reason=("boundary_ready" if boundary else None),
                    action=None if boundary else action,
                    summary=str(summary or ("boundary established" if boundary else f"{action.type} (fake)")),
                )
            if len(entry) == 2:
                action, boundary = entry
                return ExecutorStep(
                    decision=(ExecutorDecisionKind.REQUEST_REVIEW if boundary else ExecutorDecisionKind.ACT),
                    review_reason=("boundary_ready" if boundary else None),
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

__all__ = [
    "FakeContractAuthor",
    "FakePlanner",
    "FakeReviewer",
    "FakeExecutor",
    "fake_task_contract",
    "fake_task_scope",
]
