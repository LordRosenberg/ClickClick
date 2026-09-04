"""Shared non-acting decision-role runner machinery.

Planner and Reviewer reuse one transport path for image preparation,
AgentSession invocation, observation refresh, artifacts, and trace metadata.
This module intentionally exposes no combined planning-and-reviewing role.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from agent.read_tools import make_observe_screen_handler
from agent.session import AgentSession, ReviewerProtocol
from driver.observation_deadline import ObservationStageError
from perception.image_utils import (
    compress_for_model,
    role_model_image_profile,
    validated_model_image,
    visual_evidence_metadata,
)
from perception.input_evidence import interaction_envelope
from perception.observation import ObservationBuilder, ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings, get_settings
from shared.llm_gateway import GatewayError
from shared.schemas import AgentState, ObservationMode, PlannerDecision, ReviewerDecision

DecisionT = TypeVar("DecisionT", PlannerDecision, ReviewerDecision)
DecisionRole = Literal["planner", "reviewer"]
DynamicProjection = Callable[[ObservationPackage], str]


class MutableText:
    """A shared exact string binding updated after an in-round observation."""

    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class DecisionRoleRunner:
    """Common transport for focused non-acting roles."""

    def __init__(
        self,
        role: DecisionRole,
        driver: Any = None,
        artifacts: ArtifactStore | None = None,
        *,
        model: str,
        settings: Settings | None = None,
        reviewer_protocol: ReviewerProtocol = "boundary",
    ) -> None:
        self.role = role
        self.driver = driver
        self.artifacts = artifacts
        self.model = model
        self.settings = settings or get_settings()
        self.session = AgentSession(
            role,
            model,
            settings=self.settings,
            reviewer_protocol=reviewer_protocol,
        )
        self.traces: Any | None = None

    def set_model(self, model: str) -> None:
        self.model = model
        self.session.model = model

    def prepare_lifecycle(
        self,
        state: AgentState,
        package: ObservationPackage,
        *,
        task_id: str,
    ) -> None:
        lifecycle_key = f"task:{task_id}" if task_id else f"anonymous:{id(self)}"
        if self.session.reset_lifecycle(lifecycle_key):
            directories = self.session.freeze_allow_dirs(["generic"])
            state.frozen_skill_dirs = list(directories)
        elif state.frozen_skill_dirs and not self.session.frozen_allow_dirs:
            self.session.freeze_allow_dirs(state.frozen_skill_dirs)
        self.session.set_foreground_app(package.ui.app_id)

    def prepare_observation(
        self,
        package: ObservationPackage,
    ) -> tuple[bytes | None, dict[str, Any]]:
        tree_available = package.mode != ObservationMode.IMAGE_ONLY
        profile = role_model_image_profile(tree_available=tree_available)
        source_image = package.clean_png or package.image_for_llm
        image, _original_size, model_size = compress_for_model(
            source_image,
            max_dim=profile.long_edge,
            quality=profile.jpeg_quality,
        )
        image, model_size = validated_model_image(image, model_size)
        if image is not None:
            package.image_for_llm = image
            package.model_image_width, package.model_image_height = model_size
            package.mode = (
                ObservationMode.TREE_PLUS_IMAGE
                if tree_available else ObservationMode.IMAGE_ONLY
            )
        else:
            package.image_for_llm = None
            package.model_image_width = 0
            package.model_image_height = 0
            package.mode = ObservationMode.TREE_ONLY
            if not tree_available:
                package.accepted = False
                package.acceptance_reason = "role_image_unavailable"
                package.actionable = False
                package.index_actionable = False
                if "role_image_unavailable" not in package.gap_reasons:
                    package.gap_reasons.append("role_image_unavailable")
                raise ObservationStageError("role_image", "decode_failed")
        evidence = visual_evidence_metadata(
            role=self.role,
            visual_kind="clean",
            observation_id=package.observation_id,
            image_bytes=image,
            source_image_bytes=source_image,
            model_size=model_size,
            model=self.model,
            profile=profile,
        )
        return image, evidence

    async def run_decision(
        self,
        state: AgentState,
        package: ObservationPackage,
        *,
        system: str,
        task_anchor: str,
        dynamic_projection: DynamicProjection,
        expected_type: type[DecisionT],
        task_id: str = "",
        context_state: dict[str, Any] | None = None,
        model_call_meter: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> tuple[DecisionT, dict[str, Any], dict[str, Any]]:
        """Run one role invocation and return its exact active observation."""
        self.prepare_lifecycle(state, package, task_id=task_id)
        image, baseline_visual_evidence = self.prepare_observation(package)
        observation = dynamic_projection(package)
        observation_messages = build_observation_messages(observation, image)

        self.session.set_stable_system(system)
        runtime_context: dict[str, Any] = {
            "active_package": package,
            "agent_state": state,
            "model": self.model,
            "visual_evidence_metadata": baseline_visual_evidence,
            **(context_state or {}),
        }

        def render_observation_bucket(
            current_package: ObservationPackage,
        ) -> tuple[list[dict[str, Any]], list[str]]:
            self.session.set_foreground_app(current_package.ui.app_id)
            rendered = build_observation_messages(
                dynamic_projection(current_package),
                current_package.image_for_llm,
            )
            return rendered, ["observation"] * len(rendered)

        runtime_context["render_observation_bucket"] = render_observation_bucket
        handlers = {
            "observe_screen": make_observe_screen_handler(
                driver=self.driver,
                builder=ObservationBuilder(),
                artifacts=self.artifacts,
                baseline_package=package,
            ),
        }

        def event_sink(kind: str, payload: dict[str, Any]) -> None:
            if self.traces is not None and task_id:
                self.traces.write(
                    task_id,
                    kind=kind,
                    step_seq=state.step_number,
                    message=kind,
                    payload=payload,
                )

        final_messages = (
            [{"role": "user", "content": task_anchor}] if task_anchor else []
        )
        result = await self.session.run(
            observation_messages,
            final_messages=final_messages,
            observation_message_names=["observation"] * len(observation_messages),
            final_message_names=["task_anchor"] if final_messages else [],
            handlers=handlers,
            context_state=runtime_context,
            event_sink=event_sink,
            model_call_meter=model_call_meter,
            artifacts=self.artifacts,
        )
        decision = result.decision
        if not isinstance(decision, expected_type):
            raise TypeError(
                f"{self.role} terminal tool returned {type(decision).__name__}, "
                f"expected {expected_type.__name__}"
            )
        active_package = result.context_state.get("active_package")
        if not isinstance(active_package, ObservationPackage):
            raise GatewayError(
                "active observation package missing after decision invocation",
                category="malformed",
            )
        package = active_package
        invocation_refs = self._invocation_refs(result, decision)
        invocation_refs["active_package"] = package
        observation_refs = self._observation_refs(
            package,
            result.context_state,
            baseline_visual_evidence,
        )
        return decision, invocation_refs, observation_refs

    def _invocation_refs(self, result: Any, decision: DecisionT) -> dict[str, Any]:
        llm_input_ref = None
        llm_output_ref = None
        if self.artifacts is not None:
            llm_input_ref = self.artifacts.save_json("llm", result.request_snapshot)
            llm_output_ref = self.artifacts.save_json("llm", {
                "role": self.role,
                "protocol": "agent_session_tools",
                "tool_calls": [record.model_dump() for record in result.tool_calls],
                "agent_rounds": [record.model_dump() for record in result.llm_rounds],
                "decision": decision.model_dump(mode="json"),
                "active_skills": self.session.active_skill_metadata,
            })
        return {
            "llm_input_ref": llm_input_ref,
            "llm_output_ref": llm_output_ref,
            "tool_calls": [record.model_dump() for record in result.tool_calls],
            "agent_rounds": [record.model_dump() for record in result.llm_rounds],
            "active_skills": self.session.active_skill_metadata,
            "delivered_evidence_digest": str(
                result.context_state.get("delivered_evidence_digest") or ""
            ),
            "reviewer_packet_digest": str(
                result.context_state.get("reviewer_packet_digest") or ""
            ),
        }

    def _observation_refs(
        self,
        package: ObservationPackage,
        result_context: dict[str, Any],
        baseline_visual_evidence: dict[str, Any],
    ) -> dict[str, Any]:
        final_visual = result_context.get("visual_evidence_metadata")
        final_visual = (
            dict(final_visual)
            if isinstance(final_visual, dict)
            else dict(baseline_visual_evidence)
        )
        if self.artifacts is not None and package.image_for_llm is not None:
            final_visual["image_artifact_ref"] = self.artifacts.save_bytes(
                "model-images", package.image_for_llm, suffix=".bin",
            )
        return {
            "som_ref": package.som_ref,
            "tree_ref": package.tree_ref,
            "observation_mode": package.mode.value,
            "gap_reasons": list(package.gap_reasons or []),
            "estimated_tokens": getattr(package.ui, "estimated_tokens", None),
            "observation_id": package.observation_id,
            "captured_monotonic_ms": package.captured_monotonic_ms,
            "frame_geometry": [package.frame_width, package.frame_height],
            "coordinate_actionable": package.actionable,
            "index_actionable": package.index_actionable,
            "visual_evidence": final_visual,
            "capture": dict(package.capture_meta or {}),
            "interaction_state": interaction_envelope(package.interaction_state),
        }

    async def aclose(self) -> None:
        return None


def build_observation_messages(
    text: str,
    image_bytes: bytes | None,
) -> list[dict[str, Any]]:
    """Build one current clean-image bucket for a non-acting role."""
    if not image_bytes:
        return [{"role": "user", "content": text}]
    encoded = base64.b64encode(image_bytes).decode("ascii")
    media = "image/jpeg" if image_bytes[:2] == b"\xff\xd8" else "image/png"
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media};base64,{encoded}"},
            },
        ],
    }]
