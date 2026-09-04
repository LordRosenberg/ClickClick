"""Executor agent: AgentSession tool loop → one ExecutorStep + driver.act.

Each tick: ``load_skill`` / ``submit_executor_step``, then one atomic device
action. Observation and subgoal messages are replaced each tick; skill bodies
are side-stored for the task and projected by foreground app.
"""

from __future__ import annotations

import json
from typing import Any

from agent.action_observation import (
    ActionObservationTransaction,
    suppressed_action_result,
)
from agent.observation_space import (
    COORDINATE_ROUNDING_EPSILON,
    ObservationRegistry,
    action_uses_coordinates,
    action_uses_index,
    make_registry_entry,
    transform_action,
    validate_action_bounds,
)
from agent.session import AgentSession
from agent.read_tools import make_observe_screen_handler, make_search_installed_apps_handler
from agent.tool_registry import (
    AgentToolResult,
    ToolExecutionContext,
    ToolStatus,
    redact_exact_values,
    redact_value,
)
from driver.observation_deadline import ObservationStageError
from agent.prompts import (
    render_executor_system,
)
from perception.image_utils import (
    compress_for_model,
    prepare_som_for_model,
    role_model_image_profile,
    validated_model_image,
    visual_evidence_metadata,
)
from perception.input_evidence import editability_evidence, focused_target_evidence
from perception.observation import ObservationBuilder, ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings, get_settings
from shared.app_resolver import ResolverTicketStore
from shared.llm_gateway import GatewayError
from shared.protocol import DeviceDriver
from shared.schemas import (
    Action,
    ActionPipeline,
    ActionPipelineStage,
    ActionResult,
    ActionTargetSnapshot,
    CanonicalUI,
    ExecutorDecisionKind,
    ExecutorStep,
    ObservationMode,
    SubmittedActionSnapshot,
    AppResolutionResult,
    AppResolutionStatus,
    LaunchPreflightResult,
)


# ---------------------------------------------------------------------------
# Required-params contract (shared with driver-side enforcement)
# ---------------------------------------------------------------------------

# Per-type REQUIRED params. `duration_ms` is intentionally absent everywhere
# (driver applies defaults). `long_press` is listed as () and handled by a
# dedicated branch (index OR x/y).
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "tap": ("index",),
    "tap_xy": ("x", "y"),
    "type": ("text",),
    "replace_text": ("text",),
    "swipe": ("x", "y", "x2", "y2"),
    "long_press": (),
    "scroll": ("direction",),
    "drag": ("x", "y", "x2", "y2"),
    "key": ("key",),
    "launch": ("app",),
    "back": (),
    "home": (),
    "sleep": (),
}


def _submitted_action_snapshot(
    action: Action,
    *,
    redact_text: bool = False,
) -> SubmittedActionSnapshot:
    """Copy exactly the model-authored action fields used by the timeline."""
    return SubmittedActionSnapshot(
        type=action.type,
        index=action.index,
        x=action.x,
        y=action.y,
        x2=action.x2,
        y2=action.y2,
        text=(None if redact_text else action.text),
        text_redacted=bool(redact_text and action.text is not None),
        key=action.key,
        app=action.app,
        direction=action.direction,
        duration_ms=action.duration_ms,
    )


def _target_snapshot(element: Any) -> ActionTargetSnapshot:
    """Bind one current source-typed accessibility target without synthesis."""
    states = dict(getattr(element, "states", None) or {})
    password = bool(getattr(element, "password", False) or states.get("password"))
    role = str(getattr(element, "role", "") or "")
    structural_editability = getattr(element, "editability", None)
    if structural_editability not in {
        "editable", "not_editable", "unknown", "conflict",
    }:
        structural_editability = editability_evidence(element)
    return ActionTargetSnapshot(
        index=getattr(element, "index", None),
        role=role,
        raw_text="" if password else str(getattr(element, "text", "") or ""),
        raw_a11y_label="" if password else str(getattr(element, "desc", "") or ""),
        raw_hint="" if password else str(getattr(element, "hint", "") or ""),
        raw_fields_redacted=password,
        bounds=list(getattr(element, "bounds", None) or []),
        editability=structural_editability,
        focused=bool(states.get("focused") or getattr(element, "editability", "")),
        password=password,
    )


def _focused_text_target(package: ObservationPackage) -> Any | None:
    """Return exact focused structural evidence without deciding editability."""
    return focused_target_evidence(package.interaction_state)


def _required_params_satisfied(action: Action) -> bool:
    """True if every REQUIRED param for `action.type` is present and non-null.

    `long_press` is special: valid when `index` is set OR both `x` and `y` are
    set. `duration_ms` is never required (driver applies defaults).
    """
    t = action.type
    if t == "long_press":
        if action.index is not None:
            return True
        return action.x is not None and action.y is not None
    required = _REQUIRED_PARAMS.get(t)
    if not required:
        return True
    for field in required:
        if getattr(action, field, None) is None:
            return False
    return True


def _missing_required_params(action: Action) -> list[str]:
    """Return the list of missing required param names (for nudge messages)."""
    t = action.type
    if t == "long_press":
        if action.index is None and (action.x is None or action.y is None):
            return ["index or (x,y)"]
        return []
    required = _REQUIRED_PARAMS.get(t, ())
    return [f for f in required if getattr(action, f, None) is None]


def _normalize_act(value: str) -> str:
    """Normalize whitespace without discarding already generated semantics."""
    return " ".join(str(value or "").split())


# ---------------------------------------------------------------------------
# tap(index) → tap_xy resolution (agent-side, D9 / 4.6)
# ---------------------------------------------------------------------------


def resolve_tap_index(action: Action, ui: CanonicalUI) -> Action:
    """Resolve `tap(index)` to `tap_xy(x, y)` using the current frame's UI.

    Returns the action unchanged when it's not a tap-by-index or the index
    can't be resolved (caller will surface a driver-level failure).
    """
    if action.type != "tap" or action.index is None:
        return action
    el = next((e for e in ui.elements if e.index == action.index), None)
    if el is None or len(el.bounds) != 4:
        return action  # unresolved; driver will fail with a clear message
    x1, y1, x2, y2 = el.bounds
    return Action(type="tap_xy", x=(x1 + x2) / 2, y=(y1 + y2) / 2)


def resolve_long_press_index(action: Action, ui: CanonicalUI) -> Action:
    """Resolve `long_press(index)` to `long_press(x, y, duration_ms)`."""
    if action.type != "long_press" or action.index is None:
        return action
    el = next((e for e in ui.elements if e.index == action.index), None)
    if el is None or len(el.bounds) != 4:
        return action
    x1, y1, x2, y2 = el.bounds
    return Action(
        type="long_press",
        x=(x1 + x2) / 2, y=(y1 + y2) / 2,
        duration_ms=action.duration_ms if action.duration_ms is not None else 1000,
    )


def rescale_action_xy(action: Action, sx: float, sy: float) -> Action:
    """Reverse-rescale `Action` coordinates from a compressed frame to the original frame.

    The Executor compresses the annotated screenshot to ``max_dim=1080`` long
    edge before sending it to the LLM, so any `tap_xy` / `swipe` /
    `long_press(x, y)` / `drag` coordinates the model emits are in the
    compressed-image pixel space. The driver, however, expects
    original-frame coordinates (they map to `adb input tap` directly). This
    helper multiplies the four coordinate fields by `sx = orig_w / comp_w` and
    `sy = orig_h / comp_h` (each axis independently because the resize only
    scales the long edge).

    Untouched fields: `index`, `text`, `key`, `app`, `direction`,
    `duration_ms`. When `sx == sy == 1.0` the helper is effectively a
    no-op — it still constructs a new `Action`, which is fine because
    `Action.model_copy(update=...)` is cheap.
    """
    if action.x is None and action.y is None and action.x2 is None and action.y2 is None:
        return action
    kwargs: dict[str, Any] = {
        "type": action.type,
        "index": action.index,
        "text": action.text,
        "key": action.key,
        "app": action.app,
        "direction": action.direction,
        "duration_ms": action.duration_ms,
    }
    if action.x is not None:
        kwargs["x"] = float(action.x) * sx
    if action.y is not None:
        kwargs["y"] = float(action.y) * sy
    if action.x2 is not None:
        kwargs["x2"] = float(action.x2) * sx
    if action.y2 is not None:
        kwargs["y2"] = float(action.y2) * sy
    return Action(**kwargs)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_executor_prompt(
    subgoal: str,
    package: ObservationPackage,
    *,
    state: Any | None = None,
) -> tuple[str, str, str, str]:
    """Construct (system, goal, history, observation) for one Executor tick."""
    from agent.decision_context import (
        render_executor_history_v2,
        render_executor_observation_v2,
        render_executor_task_anchor,
    )
    from shared.schemas import AgentState

    system = render_executor_system()
    if isinstance(state, AgentState):
        if subgoal and subgoal != state.current_subgoal:
            state = state.model_copy(update={"current_subgoal": subgoal})
        goal = render_executor_task_anchor(state)
        history = render_executor_history_v2(state)
    else:
        from shared.schemas import AgentState as _AgentState

        ephemeral = _AgentState(
            instruction=subgoal,
            current_subgoal=subgoal,
        )
        goal = render_executor_task_anchor(ephemeral)
        history = render_executor_history_v2(ephemeral)
    observation = render_executor_observation_v2(package)
    return system, goal, history, observation


def _build_messages(
    system: str,
    user: str,
    image_bytes: bytes | None,
) -> list[dict[str, Any]]:
    import base64

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if image_bytes is None:
        messages.append({"role": "user", "content": user})
    else:
        # The bytes' format may be JPEG (compress_for_model no-resize path),
        # PNG (resize path through render_som, or a raw-PNG fallback), or
        # theoretically something else. Sniff the magic bytes so the data
        # URL's media type always matches the bytes we're sending —
        # Anthropic's content validator rejects mismatches with HTTP 400.
        media_type = _sniff_image_media_type(image_bytes)
        data_url = f"data:{media_type};base64," + base64.b64encode(image_bytes).decode("ascii")
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        content.append({"type": "image_url", "image_url": {"url": data_url}})
        messages.append({"role": "user", "content": content})
    return messages


def _sniff_image_media_type(image_bytes: bytes) -> str:
    """Return the IANA media type implied by the leading bytes of `image_bytes`.

    Recognized signatures:
    - JPEG: `\xff\xd8\xff` SOI marker → `image/jpeg`
    - PNG: `\x89PNG\r\n\x1a\n` signature → `image/png`
    - GIF: `GIF87a` / `GIF89a` → `image/gif`
    - WEBP: `RIFF....WEBP` → `image/webp`

    Any other leading bytes default to `image/jpeg` (the legacy tag) so a
    future format we don't recognize here still gets tagged — Anthropic's
    validator will reject if needed, surfacing the gap rather than silently
    passing. The sniff keeps the data-URL tag and the bytes in lockstep,
    which is the contract this helper exists to enforce.
    """
    if len(image_bytes) >= 3 and image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(image_bytes) >= 6 and image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """Executor via AgentSession (subgoal-scoped Chat Completions tool loop)."""

    def __init__(
        self,
        driver: DeviceDriver,
        artifacts: ArtifactStore | None = None,
        *,
        model: str,
        settings: Settings | None = None,
    ) -> None:
        self.driver = driver
        self.artifacts = artifacts
        self.model = model
        self.settings = settings or get_settings()
        self._session = AgentSession(
            "executor", model, settings=self.settings,
        )
        self.traces: Any | None = None
        self._frozen_skill_dirs: list[str] = []
        self._ticket_store = ResolverTicketStore()
        self._ticket_task_id = ""
        self._app_resolution_generations: dict[str, str] = {}

    def set_model(self, model: str) -> None:
        """Use ``model`` for both role metadata and subsequent LLM requests."""
        self.model = model
        session = getattr(self, "_session", None)
        if session is not None:
            session.model = model

    def configure_skills(
        self,
        *,
        frozen_skill_dirs: list[str] | None = None,
    ) -> None:
        """Apply the task's frozen generic skill discovery scope."""
        if frozen_skill_dirs is not None:
            self._frozen_skill_dirs = list(frozen_skill_dirs)

    def _device_id(self) -> str:
        value = getattr(self.driver, "serial", None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001
                value = None
        return str(value or getattr(self.driver, "_serial", None) or "default")

    async def _resolve_installed_app(self, app: str) -> AppResolutionResult:
        method = getattr(self.driver, "resolve_installed_app", None)
        if not callable(method):
            raise RuntimeError("device driver must implement resolve_installed_app")
        raw = await method(app)
        return AppResolutionResult.model_validate(raw)

    def _issue_resolution_ticket(
        self,
        resolution: AppResolutionResult,
        *,
        task_id: str,
        subgoal_id: str,
    ) -> dict[str, Any]:
        ticket = self._ticket_store.issue(
            task_id=task_id,
            device_id=self._device_id(),
            subgoal_id=subgoal_id,
            query=resolution.requested_name,
            resolver_generation=resolution.resolver_generation,
        )
        self._app_resolution_generations[
            resolution.normalized_query
        ] = resolution.resolver_generation
        return ticket.model_dump(mode="json")

    async def _launch_preflight(
        self, step: ExecutorStep, context: ToolExecutionContext,
    ) -> AgentToolResult | None:
        action = step.action
        if action is None or action.type != "launch":
            return None
        requested = str(action.app or "").strip()
        resolution = await self._resolve_installed_app(requested)
        normalized = resolution.normalized_query
        generations = context.state.setdefault("app_resolution_generations", {})
        if isinstance(generations, dict):
            generations[normalized] = resolution.resolver_generation
        context.state.setdefault("app_resolution_events", []).append(
            resolution.model_dump(mode="json")
        )
        if resolution.status == AppResolutionStatus.RESOLVED and resolution.package:
            step.action = action.model_copy(update={"app": resolution.package})
            context.state["launch_preflight"] = LaunchPreflightResult(
                kind="resolved",
                requested_name=requested,
                normalized_query=normalized,
                resolver_generation=resolution.resolver_generation,
                package=resolution.package,
                provenance=resolution.provenance,
            ).model_dump(mode="json")
            return None
        if "." in requested and len(requested.split(".")) >= 2:
            result = LaunchPreflightResult(
                kind="invalid_package",
                requested_name=requested,
                normalized_query=normalized,
                resolver_generation=resolution.resolver_generation,
            )
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS,
                summary="invalid_package: explicit launch package is not currently installed",
                data=result.model_dump(mode="json"),
                error="invalid_package",
            )
        issued = context.state.setdefault("issued_app_resolution_tickets", {})
        ticket_payload = issued.get(normalized) if isinstance(issued, dict) else None
        if not isinstance(ticket_payload, dict):
            ticket_payload = self._issue_resolution_ticket(
                resolution,
                task_id=str(context.state.get("task_id") or ""),
                subgoal_id=str(context.state.get("subgoal_id") or ""),
            )
            if isinstance(issued, dict):
                issued[normalized] = ticket_payload
        result = LaunchPreflightResult(
            kind="app_resolution_miss",
            requested_name=requested,
            normalized_query=normalized,
            resolver_generation=resolution.resolver_generation,
            ticket=ticket_payload,
        )
        context.state["launch_preflight"] = result.model_dump(mode="json")
        return AgentToolResult(
            status=ToolStatus.PRECONDITION_NOT_MET,
            summary=(
                "app_resolution_miss: no device action was dispatched; use the "
                "matching ticket to search, then resubmit an explicit package"
            ),
            data=result.model_dump(mode="json"),
            error="app_resolution_miss",
        )

    async def _terminal_preflight(
        self, step: ExecutorStep, context: ToolExecutionContext,
    ) -> AgentToolResult | None:
        """Reject a deterministically invalid launch before device dispatch."""
        if step.decision != ExecutorDecisionKind.ACT:
            return None
        return await self._launch_preflight(step, context)

    async def act_once(
        self,
        subgoal: str,
        package: ObservationPackage,
        prior_result: str = "",
        *,
        task_id: str = "",
        state: Any | None = None,
        model_call_meter: Any = None,
    ) -> tuple[ExecutorStep, ActionResult, CanonicalUI, ObservationMode, dict[str, str | None]]:
        """Perform one atomic action and return (step, action_result, ui, mode, refs).

        The UI and mode come from the ObservationPackage built by the
        Orchestrator. Active-subgoal history is projected from the canonical
        task-memory event stream in ``state``.
        """
        ui = package.ui
        mode = package.mode
        # Production path: Agent Session tool loop (task-scoped lifecycle).
        life_key = f"task:{task_id}" if task_id else "task:anon"
        if self._session.reset_lifecycle(life_key):
            if self._ticket_task_id:
                self._ticket_store.clear_task(self._ticket_task_id)
            self._ticket_task_id = task_id
            base_dirs = self._frozen_skill_dirs or ["generic"]
            self._session.freeze_allow_dirs(base_dirs)
        self._ticket_store.clear_other_subgoals(task_id, subgoal)
        self._session.set_foreground_app(package.ui.app_id)
        # Every new Executor device decision receives one current SoM image
        # when pixels exist. Derive it from the clean pixels that share this
        # package's tree/coordinate identity; gap reasons are capture-quality
        # facts, not image-delivery policy.
        source_image = package.clean_png or package.image_for_llm
        tree_available = package.mode != ObservationMode.IMAGE_ONLY
        image_profile = role_model_image_profile(
            tree_available=tree_available
        )
        if package.index_actionable:
            llm_image, orig_size, comp_size = prepare_som_for_model(
                source_image,
                package.ui.elements,
                frame_geometry=(package.frame_width, package.frame_height),
                max_dim=image_profile.long_edge,
                quality=image_profile.jpeg_quality,
                source_already_annotated=package.clean_png is None,
            )
        else:
            llm_image, orig_size, comp_size = compress_for_model(
                source_image,
                max_dim=image_profile.long_edge,
                quality=image_profile.jpeg_quality,
            )
        llm_image, comp_size = validated_model_image(llm_image, comp_size)
        size_for_messages: tuple[int, int] | None = None
        if llm_image is not None:
            size_for_messages = comp_size
        frame_geometry = (
            (package.frame_width, package.frame_height)
            if package.frame_width > 0 and package.frame_height > 0
            else (orig_size if orig_size != (0, 0) else None)
        )
        if frame_geometry is not None:
            package.frame_width, package.frame_height = frame_geometry
        registry_model_size = size_for_messages
        if size_for_messages is not None:
            package.model_image_width, package.model_image_height = size_for_messages
            # Keep the exact model-facing pixels on the active package.  A
            # later observe_screen(current) may reuse this baseline, and must
            # not rebind the same observation id to the larger persisted SoM.
            package.image_for_llm = llm_image
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
        package.actionable = bool(
            package.actionable
            and (
                package.index_actionable
                or (size_for_messages is not None and frame_geometry is not None)
            )
        )
        baseline_visual_evidence = visual_evidence_metadata(
            role="executor",
            visual_kind="som" if package.index_actionable else "clean",
            observation_id=package.observation_id,
            image_bytes=llm_image,
            source_image_bytes=source_image,
            model_size=(
                package.model_image_width,
                package.model_image_height,
            ),
            model=self.model,
            profile=image_profile,
        )
        observation_registry = ObservationRegistry()
        initial_entry = make_registry_entry(
            observation_id=package.observation_id,
            coordinate_space_id=package.coordinate_space_id,
            ui=package.ui,
            actionable=package.actionable,
            index_actionable=package.index_actionable,
            captured_monotonic_ms=package.captured_monotonic_ms,
            model_image_size=registry_model_size,
            frame_geometry=frame_geometry,
            rotation_degrees=package.rotation_degrees,
            crop_box=package.crop_box,
            transform_id=package.transform_id,
        )
        observation_registry.register(initial_entry, make_active=package.actionable)

        # Packaging above finalizes actionability and exact attachment
        # geometry, so render the observation bucket from those final values.
        system, goal, history, observation = build_executor_prompt(
            subgoal, package,
            state=state,
        )

        self._session.set_stable_system(system)
        history_messages: list[dict[str, Any]] = []
        history_message_names: list[str] = []
        if history:
            history_messages.append({"role": "user", "content": history})
            history_message_names.append("history")
        final_messages = [{"role": "user", "content": goal}]
        # Observation bucket only (system lives in session stable prefix).
        full_msgs = _build_messages(
            system,
            observation,
            llm_image,
        )
        obs_messages = [m for m in full_msgs if m.get("role") != "system"]

        def render_observation_bucket(
            current_package: ObservationPackage,
        ) -> tuple[list[dict[str, Any]], list[str]]:
            self._session.set_foreground_app(current_package.ui.app_id)
            _system, _goal, _history, current = build_executor_prompt(
                subgoal,
                current_package,
                state=state,
            )
            rendered = [
                message
                for message in _build_messages(
                    "",
                    current,
                    current_package.image_for_llm,
                )
                if message.get("role") != "system"
            ]
            return rendered, ["observation"] * len(rendered)

        llm_input_ref: str | None = None
        llm_output_ref: str | None = None

        handlers = {
            "observe_screen": make_observe_screen_handler(
                driver=self.driver, builder=ObservationBuilder(), artifacts=self.artifacts,
                baseline_package=package,
            ),
            "search_installed_apps": make_search_installed_apps_handler(
                driver=self.driver, ticket_store=self._ticket_store,
            ),
        }

        def event_sink(event_kind: str, payload: dict[str, Any]) -> None:
            if self.traces is not None and task_id:
                self.traces.write(
                    task_id, kind=event_kind,
                    step_seq=getattr(state, "step_number", None),
                    message=event_kind, payload=payload,
                )

        step, invocation = await self._call_with_session(
            obs_messages,
            history_messages,
            observation_message_names=["observation"] * len(obs_messages),
            history_message_names=history_message_names,
            final_messages=final_messages,
            final_message_names=["task_anchor"],
            handlers=handlers,
            context_state={
                "active_package": package,
                "active_observation_id": observation_registry.active_observation_id,
                "observation_registry": observation_registry,
                "task_id": task_id,
                "device_id": self._device_id(),
                "subgoal_id": subgoal,
                "agent_state": state,
                "model": self.model,
                "visual_evidence_metadata": baseline_visual_evidence,
                "app_resolution_generations": dict(self._app_resolution_generations),
                "terminal_preflight": self._terminal_preflight,
                "render_observation_bucket": render_observation_bucket,
            },
            event_sink=event_sink,
            model_call_meter=model_call_meter,
        )
        if self.artifacts is not None:
            llm_input_ref = self.artifacts.save_json(
                "llm", invocation.request_snapshot,
            )
        active_package = invocation.context_state.get("active_package")
        if not isinstance(active_package, ObservationPackage):
            raise GatewayError(
                "active observation package missing after Executor invocation",
                category="malformed",
            )
        package = active_package
        ui = package.ui
        mode = package.mode
        final_visual_evidence = invocation.context_state.get("visual_evidence_metadata")
        if not isinstance(final_visual_evidence, dict):
            final_visual_evidence = baseline_visual_evidence
        else:
            final_visual_evidence = dict(final_visual_evidence)
        if self.artifacts is not None and package.image_for_llm is not None:
            final_visual_evidence["image_artifact_ref"] = self.artifacts.save_bytes(
                "model-images", package.image_for_llm, suffix=".bin",
            )
        if step.decision != ExecutorDecisionKind.ACT:
            result = ActionResult(
                success=True,
                message=step.decision.value,
                detail={"device_dispatch": "not_applicable"},
            )
            step.result = result.message
            refs: dict[str, Any] = {
                "llm_input_ref": llm_input_ref,
                "tool_calls": [record.model_dump() for record in invocation.tool_calls],
                "agent_rounds": [record.model_dump() for record in invocation.llm_rounds],
                "active_skills": self._session.active_skill_metadata,
                "delivered_evidence_digest": str(
                    invocation.context_state.get("delivered_evidence_digest") or ""
                ),
                "delivered_history_digest": str(
                    invocation.context_state.get("delivered_history_digest") or ""
                ),
                "basis_observation_id": step.basis_observation_id,
                "observation_id": package.observation_id,
                "observation_registry": [
                    entry.persistence_projection() for entry in observation_registry.values()
                ],
                "evidence_refs": list(step.evidence_refs),
                "visual_evidence": final_visual_evidence,
                "active_package": package,
                "post_action_package": None,
            }
            if self.artifacts is not None:
                refs["llm_output_ref"] = self.artifacts.save_json("llm", {
                    "role": "executor",
                    "protocol": "agent_session_tools",
                    "decision": step.decision.value,
                    "summary": step.summary,
                    "tool_calls": refs["tool_calls"],
                    "agent_rounds": refs["agent_rounds"],
                })
            return step, result, ui, mode, refs
        if step.action is None:
            raise GatewayError("act decision returned no action", category="malformed")
        action = step.action
        focused_text_target = _focused_text_target(package)
        text_target_password = bool(
            action.type in {"type", "replace_text"}
            and focused_text_target is not None
            and focused_text_target.password
        )
        step.submitted_action_snapshot = _submitted_action_snapshot(
            action,
            redact_text=text_target_password,
        )
        if action.type in {"type", "replace_text"} and focused_text_target is not None:
            step.target_snapshot = _target_snapshot(focused_text_target)
        pipeline = step.action_pipeline or ActionPipeline()
        registry = invocation.context_state.get("observation_registry")
        if not isinstance(registry, ObservationRegistry):
            registry = observation_registry
        active_observation_id = str(
            invocation.context_state.get("active_observation_id")
            or registry.active_observation_id
        )
        pipeline.basis_observation_id = step.basis_observation_id
        pipeline.active_observation_id = active_observation_id
        pipeline.stages.append(ActionPipelineStage(
            stage="validated",
            action=action,
            coordinate_space="image" if any(getattr(action, k) is not None for k in ("x", "y", "x2", "y2")) else "none",
            reason="validated",
        ))
        rejection_reason = ""
        rejection_detail: dict[str, Any] = {}
        basis_entry = None
        visual_basis_required = action_uses_coordinates(action) or action_uses_index(action)
        if visual_basis_required and not rejection_reason:
            if not step.basis_observation_id:
                rejection_reason = "ambiguous_observation_basis"
            else:
                basis_entry = registry.get(step.basis_observation_id)
                if basis_entry is None:
                    rejection_reason = "unknown_observation_basis"
                elif not basis_entry.actionable:
                    rejection_reason = "non_actionable_observation_basis"
                elif step.basis_observation_id != active_observation_id:
                    rejection_reason = "stale_observation_basis"
                elif action_uses_index(action) and not basis_entry.index_actionable:
                    rejection_reason = "index_unavailable_for_observation"
            if basis_entry is not None:
                if (
                    step.submitted_action_snapshot is not None
                    and action_uses_coordinates(action)
                    and basis_entry.model_image_size is not None
                ):
                    step.submitted_action_snapshot = (
                        step.submitted_action_snapshot.model_copy(update={
                            "image_size": tuple(basis_entry.model_image_size),
                        })
                    )
                pipeline.coordinate_space_id = basis_entry.coordinate_space_id
                pipeline.transform_id = (
                    basis_entry.transform.transform_id if basis_entry.transform else ""
                )
                pipeline.source_geometry = list(basis_entry.model_image_size or ())
                pipeline.target_geometry = list(basis_entry.frame_geometry or ())
                pipeline.original_frame_size = list(basis_entry.frame_geometry or ())
                pipeline.compressed_frame_size = list(basis_entry.model_image_size or ())
                pipeline.index_set_id = basis_entry.element_set_id
                for existing_stage in pipeline.stages:
                    if not existing_stage.observation_id:
                        existing_stage.observation_id = basis_entry.observation_id
                    if not existing_stage.coordinate_space_id:
                        existing_stage.coordinate_space_id = basis_entry.coordinate_space_id
                    if not existing_stage.transform_id:
                        existing_stage.transform_id = pipeline.transform_id
                    if not existing_stage.source_geometry:
                        existing_stage.source_geometry = list(
                            basis_entry.model_image_size or ()
                        )
                    if not existing_stage.target_geometry:
                        existing_stage.target_geometry = list(
                            basis_entry.frame_geometry or ()
                        )
            if not rejection_reason and basis_entry is not None:
                pipeline.stages.append(ActionPipelineStage(
                    stage="basis_validated",
                    action=action,
                    coordinate_space="image" if action_uses_coordinates(action) else "none",
                    reason="basis_validated",
                    observation_id=basis_entry.observation_id,
                    coordinate_space_id=basis_entry.coordinate_space_id,
                    transform_id=pipeline.transform_id,
                    source_geometry=list(basis_entry.model_image_size or ()),
                    target_geometry=list(basis_entry.frame_geometry or ()),
                    validation_result={"valid": True},
                ))

        epsilon = COORDINATE_ROUNDING_EPSILON
        if not rejection_reason and basis_entry is not None and action_uses_coordinates(action):
            if basis_entry.model_image_size is None or basis_entry.transform is None:
                rejection_reason = "unknown_coordinate_geometry"
            else:
                image_validated, image_normalized, image_evidence = validate_action_bounds(
                    action,
                    width=basis_entry.model_image_size[0],
                    height=basis_entry.model_image_size[1],
                    epsilon=epsilon,
                )
                if image_validated is None:
                    rejection_reason = str(image_evidence.get("reason") or "coordinate_out_of_bounds")
                    rejection_detail = {"image_space": image_evidence}
                else:
                    action = image_validated
                    if image_normalized:
                        pipeline.stages.append(ActionPipelineStage(
                            stage="rounding_normalized", action=action,
                            coordinate_space="image", reason="rounding_normalization",
                            observation_id=basis_entry.observation_id,
                            coordinate_space_id=basis_entry.coordinate_space_id,
                            transform_id=basis_entry.transform.transform_id,
                            source_geometry=list(basis_entry.model_image_size),
                            target_geometry=list(basis_entry.frame_geometry or ()),
                            validation_result=image_evidence,
                        ))
                    action = transform_action(action, basis_entry.transform)
                    pipeline.stages.append(ActionPipelineStage(
                        stage="coordinate_transformed", action=action,
                        coordinate_space="original_frame", reason="reverse_rescale",
                        observation_id=basis_entry.observation_id,
                        coordinate_space_id=basis_entry.coordinate_space_id,
                        transform_id=basis_entry.transform.transform_id,
                        source_geometry=list(basis_entry.model_image_size),
                        target_geometry=list(basis_entry.frame_geometry or ()),
                    ))
                    if basis_entry.frame_geometry is None:
                        rejection_reason = "unknown_coordinate_geometry"
                    else:
                        bounded, normalized, bounds_evidence = validate_action_bounds(
                            action,
                            width=basis_entry.frame_geometry[0],
                            height=basis_entry.frame_geometry[1],
                            epsilon=epsilon,
                        )
                        if bounded is None:
                            rejection_reason = str(
                                bounds_evidence.get("reason") or "coordinate_out_of_bounds"
                            )
                            rejection_detail = {"driver_space": bounds_evidence}
                        else:
                            action = bounded
                            pipeline.stages.append(ActionPipelineStage(
                                stage=("rounding_normalized" if normalized else "bounds_validated"),
                                action=action, coordinate_space="original_frame",
                                reason=("rounding_normalization" if normalized else "validated"),
                                observation_id=basis_entry.observation_id,
                                coordinate_space_id=basis_entry.coordinate_space_id,
                                transform_id=basis_entry.transform.transform_id,
                                source_geometry=list(basis_entry.model_image_size),
                                target_geometry=list(basis_entry.frame_geometry),
                                validation_result=bounds_evidence,
                            ))

        if not rejection_reason and basis_entry is not None and action_uses_index(action):
            basis_ui = basis_entry.ui()
            before_index_resolution = action
            target = next(
                (
                    element for element in basis_ui.elements
                    if element.index == before_index_resolution.index
                ),
                None,
            )
            if target is not None:
                step.target_snapshot = _target_snapshot(target)
            action = resolve_tap_index(action, basis_ui)
            action = resolve_long_press_index(action, basis_ui)
            if action == before_index_resolution:
                rejection_reason = "mismatched_observation_basis"
                rejection_detail = {
                    "index_set_id": basis_entry.element_set_id,
                    "submitted_index": before_index_resolution.index,
                }
            else:
                pipeline.stages.append(ActionPipelineStage(
                    stage="index_resolved", action=action,
                    coordinate_space="device", reason="index_resolved",
                    observation_id=basis_entry.observation_id,
                    coordinate_space_id=basis_entry.coordinate_space_id,
                    transform_id=pipeline.transform_id,
                    source_geometry=list(basis_entry.model_image_size or ()),
                    target_geometry=list(basis_entry.frame_geometry or ()),
                    validation_result={"valid": True, "index_set_id": basis_entry.element_set_id},
                ))

        post_action_package: ObservationPackage | None = None
        if rejection_reason:
            pipeline.dispatch_suppressed = True
            pipeline.fallback_reason = rejection_reason  # type: ignore[assignment]
            pipeline.validation_result = {
                "valid": False,
                "reason": rejection_reason,
                "submitted_basis_observation_id": step.basis_observation_id,
                "active_observation_id": active_observation_id,
                **rejection_detail,
            }
            pipeline.stages.append(ActionPipelineStage(
                stage="rejected",
                action=action,
                coordinate_space="image" if action_uses_coordinates(action) else "none",
                reason=rejection_reason,  # type: ignore[arg-type]
                observation_id=step.basis_observation_id,
                coordinate_space_id=pipeline.coordinate_space_id,
                transform_id=pipeline.transform_id,
                source_geometry=list(pipeline.source_geometry),
                target_geometry=list(pipeline.target_geometry),
                validation_result=dict(pipeline.validation_result),
            ))
            result = suppressed_action_result(action, rejection_reason)
            result.detail.update(pipeline.validation_result)
            post_action_package = package
        else:
            discovered_candidates = {
                str(value) for value in invocation.context_state.get("installed_app_candidates", [])
                if value
            }
            discovery_query = str(invocation.context_state.get("installed_app_query") or "")
            if (
                action.type == "launch" and discovered_candidates
                and str(action.app or "") not in discovered_candidates
            ):
                pipeline.dispatch_suppressed = True
                pipeline.origin = "model_rejected"
                pipeline.fallback_reason = "invalid_installed_app_selection"
                pipeline.stages.append(ActionPipelineStage(
                    stage="rejected", action=action, coordinate_space="device",
                    reason="invalid_installed_app_selection",
                ))
                result = suppressed_action_result(
                    action, "invalid_installed_app_selection",
                )
                post_action_package = package
            else:
                transaction = ActionObservationTransaction(
                    self.driver, ObservationBuilder(),
                )
                result, post_action_package = await transaction.act_and_observe(
                    action, package,
                )
                if (
                    result.success and action.type == "launch" and discovery_query
                    and str(action.app or "") in discovered_candidates
                ):
                    remember = getattr(self.driver, "record_installed_app_selection", None)
                    if callable(remember):
                        await remember(discovery_query, str(action.app))
                pipeline.stages.append(ActionPipelineStage(
                    stage="dispatched", action=action, coordinate_space="device",
                    reason="driver_dispatched",
                ))
        dispatched_to_driver = any(
            stage.stage == "dispatched" and stage.reason == "driver_dispatched"
            for stage in pipeline.stages
        )
        pipeline.driver_coordinates = {
            key: float(value) for key in ("x", "y", "x2", "y2")
            if dispatched_to_driver and (value := getattr(action, key)) is not None
        }
        if dispatched_to_driver and pipeline.driver_coordinates:
            result.detail.setdefault(
                "dispatched_coordinates", dict(pipeline.driver_coordinates),
            )
        pipeline.driver_success = result.success if dispatched_to_driver else None
        if text_target_password:
            password_value = str(action.text or "")
            action = action.model_copy(update={
                "text": None,
                "text_redacted": True,
            })
            for stage in pipeline.stages:
                if stage.action.type in {"type", "replace_text"}:
                    stage.action = stage.action.model_copy(update={
                        "text": None,
                        "text_redacted": True,
                    })
            result.message = str(redact_exact_values(result.message, [password_value]))
            result.detail = redact_exact_values(result.detail, [password_value])
            if result.receipt is not None:
                result.receipt = result.receipt.model_copy(update={
                    "effect_reason": redact_exact_values(
                        result.receipt.effect_reason,
                        [password_value],
                    ),
                })
        step.action = action
        step.action_pipeline = pipeline
        step.action_receipt = result.receipt
        step.result = result.message

        if self.artifacts is not None:
            action_artifact_payload = action.model_dump()
            pipeline_artifact_payload = pipeline.model_dump(mode="json")
            password_target = text_target_password
            if password_target:
                action_artifact_payload["text"] = None
                action_artifact_payload["text_redacted"] = True
                for stage_payload in pipeline_artifact_payload.get("stages", []):
                    staged_action = stage_payload.get("action")
                    if (
                        isinstance(staged_action, dict)
                        and staged_action.get("type") in {"type", "replace_text"}
                    ):
                        staged_action["text"] = None
                        staged_action["text_redacted"] = True
            llm_output_ref = self.artifacts.save_json("llm", {
                "role": "executor",
                "protocol": "agent_session_tools",
                "action": action_artifact_payload,
                "action_pipeline": pipeline_artifact_payload,
                "summary": step.summary or "",
                "decision": step.decision.value,
                "basis_observation_id": step.basis_observation_id,
                "observation_registry": [
                    entry.persistence_projection() for entry in registry.values()
                ],
                "evidence_refs": list(step.evidence_refs),
                "tool_calls": [record.model_dump() for record in invocation.tool_calls],
                "agent_rounds": [record.model_dump() for record in invocation.llm_rounds],
            })

        refs: dict[str, Any] = {
            "llm_input_ref": llm_input_ref,
            "llm_output_ref": llm_output_ref,
            "tool_calls": [record.model_dump() for record in invocation.tool_calls],
            "agent_rounds": [record.model_dump() for record in invocation.llm_rounds],
            "active_skills": self._session.active_skill_metadata,
            "app_resolution": redact_value(
                list(invocation.context_state.get("app_resolution_events") or [])
            ),
            "delivered_evidence_digest": str(
                invocation.context_state.get("delivered_evidence_digest") or ""
            ),
            "delivered_history_digest": str(
                invocation.context_state.get("delivered_history_digest") or ""
            ),
            "ticket_lifecycle": list(invocation.context_state.get("ticket_lifecycle") or []),
            "basis_observation_id": step.basis_observation_id,
            "observation_id": package.observation_id,
            "observation_registry": [
                entry.persistence_projection() for entry in registry.values()
            ],
            "evidence_refs": list(step.evidence_refs),
            "visual_evidence": final_visual_evidence,
            "active_package": package,
            "post_action_package": post_action_package,
        }
        from perception.input_evidence import interaction_envelope

        refs["interaction_state"] = interaction_envelope(
            getattr(package, "interaction_state", None),
        )
        return step, result, ui, mode, refs

    async def _call_with_session(
        self,
        obs_messages: list[dict[str, Any]],
        history_messages: list[dict[str, Any]] | None = None,
        *,
        final_messages: list[dict[str, Any]] | None = None,
        observation_message_names: list[str] | None = None,
        history_message_names: list[str] | None = None,
        final_message_names: list[str] | None = None,
        handlers: dict[str, Any] | None = None,
        context_state: dict[str, Any] | None = None,
        event_sink: Any = None,
        model_call_meter: Any = None,
    ) -> tuple[ExecutorStep, Any]:
        """Run the AgentSession role loop and bind its validated submission."""
        result = await self._session.run(
            obs_messages,
            history_messages=history_messages,
            final_messages=final_messages,
            observation_message_names=observation_message_names,
            history_message_names=history_message_names,
            final_message_names=final_message_names,
            handlers=handlers, context_state=context_state, event_sink=event_sink,
            model_call_meter=model_call_meter,
            artifacts=self.artifacts,
        )
        step = result.decision
        assert isinstance(step, ExecutorStep)
        step.summary = _normalize_act(step.summary)
        terminal_attempts = sum(
            1 for record in result.tool_calls
            if record.name == "submit_executor_step"
        )
        pipeline = _model_submission_pipeline(
            step,
            validation_attempts=max(1, terminal_attempts),
        )
        step.action_pipeline = pipeline
        if not _is_valid_step(step) or not step.summary:
            raise GatewayError(
                "AgentSession returned an invalid ExecutorStep",
                category="malformed",
            )
        return step, result


def _model_submission_pipeline(
    step: ExecutorStep,
    *,
    validation_attempts: int,
) -> ActionPipeline | None:
    """Bind provenance to the exact submission adopted for this returned step."""
    if step.decision != ExecutorDecisionKind.ACT or step.action is None:
        return None
    submitted = step.action
    missing = _missing_required_params(step.action)
    coordinate_space = (
        "image"
        if any(
            getattr(submitted, key) is not None
            for key in ("x", "y", "x2", "y2")
        )
        else "none"
    )
    return ActionPipeline(
        origin="model",
        basis_observation_id=step.basis_observation_id,
        missing_required_fields=missing,
        validation_attempts=max(1, int(validation_attempts)),
        stages=[ActionPipelineStage(
            stage="submitted",
            action=submitted,
            coordinate_space=coordinate_space,
            reason="model_submitted",
            observation_id=step.basis_observation_id,
        )],
    )


def _is_valid_step(step: ExecutorStep) -> bool:
    """Validate the mechanical shape of an Executor decision."""
    if step.decision != ExecutorDecisionKind.ACT:
        return step.action is None and bool(step.summary.strip())
    if step.action is None:
        return False
    if not step.action.type:
        return False
    if not _required_params_satisfied(step.action):
        return False
    return True
