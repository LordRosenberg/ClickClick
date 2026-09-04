"""Focused rolling Planner role."""

from __future__ import annotations

from typing import Any

from agent.decision_context import (
    render_planner_observation_v2,
    render_planner_task_anchor,
    task_requirement_categories,
)
from agent.decision_role import DecisionRoleRunner
from agent.prompts import render_planner_system
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.schemas import AgentState, PlannerDecision


class Planner:
    """Plan one next semantic transition; never adjudicate or terminate."""

    def __init__(
        self,
        driver: Any = None,
        artifacts: ArtifactStore | None = None,
        *,
        model: str,
        settings: Settings | None = None,
    ) -> None:
        self._runner = DecisionRoleRunner(
            "planner",
            driver,
            artifacts,
            model=model,
            settings=settings,
        )

    @property
    def traces(self) -> Any | None:
        return self._runner.traces

    @traces.setter
    def traces(self, value: Any | None) -> None:
        self._runner.traces = value

    @property
    def model(self) -> str:
        return self._runner.model

    @property
    def driver(self) -> Any:
        return self._runner.driver

    @driver.setter
    def driver(self, value: Any) -> None:
        self._runner.driver = value

    def set_model(self, model: str) -> None:
        self._runner.set_model(model)

    async def decide(
        self,
        state: AgentState,
        package: ObservationPackage,
        *,
        deviation: str = "",
        task_id: str = "",
        model_call_meter: Any = None,
    ) -> tuple[PlannerDecision, dict[str, Any], dict[str, Any]]:
        """Return one execute/review decision from a compact projection."""
        return await self._runner.run_decision(
            state,
            package,
            system=render_planner_system(),
            task_anchor=render_planner_task_anchor(
                state,
                deviation=deviation,
            ),
            dynamic_projection=render_planner_observation_v2,
            expected_type=PlannerDecision,
            task_id=task_id,
            context_state={
                "planner_requirement_categories": task_requirement_categories(state),
                "planner_accepted_requirement_refs": [
                    entry.requirement_ref
                    for entry in state.task_memory.progress
                    if entry.effective and entry.requirement_ref
                ],
            },
            model_call_meter=model_call_meter,
        )

    async def aclose(self) -> None:
        await self._runner.aclose()
