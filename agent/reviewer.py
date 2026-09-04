"""Focused task-scope and semantic-boundary Reviewer role."""

from __future__ import annotations

from typing import Any

from agent.decision_context import (
    render_reviewer_packet,
    reviewer_protocol_metadata,
)
from agent.decision_role import DecisionRoleRunner, MutableText
from agent.prompts import render_reviewer_scope_system, render_reviewer_system
from agent.session import AgentSession
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings, get_settings
from shared.schemas import (
    AgentState,
    TaskContractBody,
    ReviewerDecision,
)


class Reviewer:
    """Author task scope once, then adjudicate evidence without planning."""

    def __init__(
        self,
        driver: Any = None,
        artifacts: ArtifactStore | None = None,
        *,
        model: str,
        settings: Settings | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.settings = settings or get_settings()
        self._runner = DecisionRoleRunner(
            "reviewer",
            driver,
            artifacts,
            model=model,
            settings=self.settings,
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

    async def author_task_scope(
        self,
        state: AgentState,
        *,
        task_id: str = "",
        model_call_meter: Any = None,
    ) -> tuple[TaskContractBody, dict[str, Any]]:
        """Author the UI/Skill-independent immutable task contract."""
        session = AgentSession(
            "reviewer",
            self.model,
            reviewer_protocol="scope",
            settings=self.settings,
            max_total_rounds=self._runner.session.max_total_rounds,
        )
        session.set_stable_system(render_reviewer_scope_system())

        def event_sink(kind: str, payload: dict[str, Any]) -> None:
            if self.traces is not None and task_id:
                self.traces.write(
                    task_id,
                    kind=kind,
                    step_seq=state.step_number,
                    message=kind,
                    payload={**payload, "phase": "task_scope"},
                )

        result = await session.run(
            [{
                "role": "user",
                "content": f"IMMUTABLE ORIGINAL TASK:\n{state.instruction}",
            }],
            observation_message_names=["task_instruction"],
            include_skill_context=False,
            context_state={
                "phase": "task_scope",
                "task_instruction": state.instruction,
            },
            event_sink=event_sink,
            model_call_meter=model_call_meter,
            artifacts=self.artifacts,
        )
        contract = result.decision
        if not isinstance(contract, TaskContractBody):
            raise TypeError("Reviewer scope tool returned an invalid contract")
        first_round = result.llm_rounds[0] if result.llm_rounds else None
        last_round = result.llm_rounds[-1] if result.llm_rounds else None
        return contract, {
            "llm_input_ref": first_round.request_ref if first_round else None,
            "llm_output_ref": last_round.response_ref if last_round else None,
            "tool_calls": [record.model_dump() for record in result.tool_calls],
            "agent_rounds": [record.model_dump() for record in result.llm_rounds],
            "phase": "task_scope",
        }

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
        """Adjudicate one exact boundary packet without authoring future work."""
        packet_digest = MutableText()
        evidence_handles: list[str] = []
        initial_evidence_handle = str(package.evidence_ref or "")
        initial_observation_id = str(package.observation_id or "")
        current_evidence_handles: list[str] = []
        temporal_evidence_handles: list[str] = []

        def dynamic_projection(current_package: ObservationPackage) -> str:
            read_handle = str(current_package.evidence_ref or "")
            observation_id = str(current_package.observation_id or "")
            fresh_after_boundary = bool(
                (read_handle and read_handle != initial_evidence_handle)
                or (observation_id and observation_id != initial_observation_id)
            )
            current_evidence = (
                boundary_reason != "post_action_observation_missing"
                or fresh_after_boundary
            )
            text, digest, handles = render_reviewer_packet(
                state,
                current_package,
                executor_report=executor_report,
                boundary_reason=boundary_reason,
                terminal_review=terminal_review,
                review_requirement_ref=review_requirement_ref,
                current_evidence=current_evidence,
            )
            packet_digest.set(digest)
            evidence_handles[:] = handles
            current_evidence_handles[:] = (
                list(dict.fromkeys(["current", *([read_handle] if read_handle else [])]))
                if current_evidence else []
            )
            return text

        protocol_metadata = reviewer_protocol_metadata(
            state,
            terminal_review=terminal_review,
        )

        return await self._runner.run_decision(
            state,
            package,
            system=render_reviewer_system(),
            task_anchor="",
            dynamic_projection=dynamic_projection,
            expected_type=ReviewerDecision,
            task_id=task_id,
            context_state={
                "reviewer_packet_digest": packet_digest,
                "reviewer_evidence_handles": evidence_handles,
                "reviewer_current_evidence_handles": current_evidence_handles,
                "reviewer_temporal_evidence_handles": temporal_evidence_handles,
                **protocol_metadata,
            },
            model_call_meter=model_call_meter,
        )

    async def aclose(self) -> None:
        await self._runner.aclose()
