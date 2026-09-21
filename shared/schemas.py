"""Typed domain schemas shared across Agent, Driver, and Control API."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from shared.revisable import PlanRuntime


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AppResolutionStatus(str, Enum):
    """Deterministic app-resolution outcome exposed to the Harness/model."""

    RESOLVED = "resolved"
    MISS = "miss"


class AppResolutionProvenance(str, Enum):
    """Trusted local source that selected an installed package."""

    EXACT_PACKAGE = "exact_package"
    CURATED_ALIAS = "curated_alias"
    LEARNED_ALIAS = "learned_alias"


class AppResolutionResult(BaseModel):
    """Typed deterministic resolution result; never contains a ticket secret."""

    requested_name: str
    normalized_query: str
    status: AppResolutionStatus
    resolver_generation: str
    package: str | None = None
    provenance: AppResolutionProvenance | None = None


class ResolverMissTicket(BaseModel):
    """Model-visible one-use authorization issued after a resolver miss."""

    resolution_ticket: str
    expires_at_monotonic_ms: float
    resolver_generation: str


class ResolverTicketValidation(BaseModel):
    """Redacted ticket lifecycle result safe for normal traces."""

    accepted: bool
    reason: Literal[
        "accepted",
        "absent",
        "unknown",
        "expired",
        "consumed",
        "task_mismatch",
        "device_mismatch",
        "subgoal_mismatch",
    ]
    ticket_fingerprint: str = ""


class LaunchPreflightResult(BaseModel):
    """Recoverable terminal-submit preflight result used inside an Agent Call."""

    kind: Literal["resolved", "app_resolution_miss", "invalid_package"]
    requested_name: str
    normalized_query: str
    resolver_generation: str
    package: str | None = None
    provenance: AppResolutionProvenance | None = None
    ticket: ResolverMissTicket | None = None
    recoverable: bool = True


class ObservationMode(str, Enum):
    TREE_ONLY = "tree-only"
    TREE_PLUS_IMAGE = "tree+image"
    IMAGE_ONLY = "image-only"


class EffectOutcome(str, Enum):
    """Bounded observable outcome of a dispatched action."""

    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"
    TIMEOUT = "timeout"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


class EffectClass(str, Enum):
    """Small app-independent policy set for action completion."""

    TEXT_INPUT = "text_input"
    UI_TRANSITION = "ui_transition"
    GESTURE = "gesture"
    NONE = "none"


class ActionReceipt(BaseModel):
    """Transport and observable-effect facts for one action transaction."""

    transaction_id: str = ""
    effect_class: EffectClass = EffectClass.NONE
    dispatch_succeeded: bool = False
    effect_outcome: EffectOutcome = EffectOutcome.UNKNOWN
    effect_reason: str = ""
    started_monotonic_ms: float = 0.0
    dispatch_completed_monotonic_ms: float = 0.0
    completed_monotonic_ms: float = 0.0
    deadline_ms: int = 0
    observation_capture_count: int = 0
    effect_observation_id: str = ""
    observation_id: str = ""
    observation_accepted: bool = False
    # Set only when pre/post tree text and source pixels are byte-identical.
    # Absent/None means "do not tell the model the UI changed or succeeded".
    visible_change: Literal["none"] | None = None
    # Mechanical target-handling evidence. It never implies semantic success.
    interaction_ack: Literal["confirmed", "unobserved", "unavailable"] | None = None
    interaction_ack_source: Literal["accessibility_event"] | None = None
    # Device acknowledgement, separate from semantic effect confirmation.
    node_click_status: str | None = None
    native_action_performed: bool | None = None


class EvidenceSource(str, Enum):
    A11Y = "a11y"
    ADB_IME = "adb_ime"
    DRIVER = "driver"


class FocusedElementEvidence(BaseModel):
    """Collision-safe identity for the input-focused current-frame element."""

    index: int | None = None
    identity: str = ""
    bounds: list[int] = Field(default_factory=list)
    role: str = ""
    text: str = ""
    desc: str = ""
    hint: str = ""
    password: bool = False
    editability: Literal["editable", "not_editable", "unknown", "conflict"] = "unknown"
    value_available: bool | None = None

    @model_validator(mode="after")
    def _normalize_value_provenance(self) -> FocusedElementEvidence:
        if self.value_available is None:
            self.value_available = True
        return self


class InteractionStateEvidence(BaseModel):
    """Current-frame focus and keyboard evidence; ``None`` means unknown."""

    focused_element: FocusedElementEvidence | None = None
    focused_editable: FocusedElementEvidence | None = None
    keyboard_visible: bool | None = None
    confidence: float = 0.0
    sources: list[str] = Field(default_factory=list)
    age_ms: int = 0


class UIElement(BaseModel):
    """A numbered accessibility element for the current frame.

    Non-interactable skeleton nodes (containers, text labels, icons) are kept
    in `CanonicalUI.semantic_tree` to preserve hierarchy and semantic context;
    they carry `interactable=False` and `index=-1` and never participate in
    `act(index)` resolution. `children` holds indices into the same
    `semantic_tree` list (not into the interactable `elements` projection).
    """

    index: int
    role: str = ""
    text: str = ""
    desc: str = ""
    hint: str = ""
    bounds: list[int] = Field(default_factory=list)  # [x1, y1, x2, y2]
    clickable: bool = False
    states: dict[str, Any] = Field(default_factory=dict)
    children: list[int] = Field(default_factory=list)
    depth: int = 0
    interactable: bool = True
    ctx: str = ""
    # Android resource-id, shortened to the part after ":id/" (a11y-rich-element-hints).
    # Empty when the raw node carries no resource-id; never rendered when empty.
    resource_id: str = ""
    # Current Android accessibility-window provenance.
    window_id: int | None = None
    window_type: int | None = None
    window_layer: int | None = None
    display_id: int | None = None
    window_wrapper: bool = False
    # Opaque observation-scoped native reference. Never rendered in model trees.
    node_handle: str = ""


class CanonicalUI(BaseModel):
    """Normalized UI state fed to agents and stored for replay.

    `elements` is the action namespace: only interactable nodes, with unique
    per-frame indices for `act(index)`. `semantic_tree` is the pruned hierarchy
    that retains non-interactable context nodes (skeleton) for the model.
    `elements` MUST be the projection of `semantic_tree`'s interactable nodes.
    """

    platform: str = "android"
    app_id: str = ""
    activity: str = ""
    elements: list[UIElement] = Field(default_factory=list)
    semantic_tree: list[UIElement] = Field(default_factory=list)
    page_summary: str = ""
    filter_tier: str = "concise"
    estimated_tokens: int = 0
    capture_provider: str = "unknown"
    capture_complete: bool = False
    capture_generation: int = 0
    capture_reasons: list[str] = Field(default_factory=list)


class ExecutorDecisionKind(str, Enum):
    """Executor routing intent, separate from any device action."""

    ACT = "act"
    REQUEST_REVIEW = "request_review"
    REQUEST_REPLAN = "request_replan"


SUPPORTED_ANDROID_KEY_ACTIONS = (
    "back",
    "home",
    "enter",
    "menu",
    "delete",
    "search",
    "volume_up",
    "volume_down",
    "power",
    "media_pause",
)


class Action(BaseModel):
    """One device action requested by an Executor.

    Device types: `tap`/`tap_xy`/`skill_authorized_action`/
    `type`/`replace_text`/`swipe`/`long_press`/`scroll`/`drag`/`key`/
    `launch`/`back`/`home`/`sleep`.
    `scroll` carries a content-navigation `direction`: down reveals content
    below, up reveals content above, and horizontal values follow the same
    convention. `long_press` and `drag` reuse `duration_ms`; `drag` uses `x/y`
    as start and `x2/y2` as end.
    """

    type: Literal[
        "tap",
        "tap_xy",
        "skill_authorized_action",
        "type",
        "replace_text",
        "swipe",
        "long_press",
        "scroll",
        "drag",
        "key",
        "launch",
        "back",
        "home",
        "sleep",
    ]
    index: int | None = None
    surface_index: int | None = Field(default=None, ge=0, strict=True)
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    text: str | None = None
    key: str | None = None
    app: str | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    duration_ms: int | None = None
    skill_action_id: str | None = None
    # Bound by the executor after observation validation; cannot be model-authored
    # or resurrected from serialized history/resume data.
    _node_handle: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def validate_skill_authorized_target(self):
        if (
            self.type == "key"
            and self.key is not None
            and self.key not in SUPPORTED_ANDROID_KEY_ACTIONS
        ):
            raise ValueError(
                "key requires one supported Android key name; key chords are unsupported"
            )
        if self.type != "skill_authorized_action":
            return self
        if not self.skill_action_id or not self.skill_action_id.strip():
            raise ValueError("skill_authorized_action requires skill_action_id")
        has_index = self.index is not None
        has_any_coordinate = self.x is not None or self.y is not None
        has_coordinates = self.x is not None and self.y is not None
        if has_index == has_coordinates or (has_any_coordinate and not has_coordinates):
            raise ValueError(
                "skill_authorized_action requires exactly one target: index or (x,y)"
            )
        return self


class ActionResult(BaseModel):
    """Outcome of a single device action."""

    success: bool
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    receipt: ActionReceipt | None = None


ActionOrigin = Literal["model", "model_rejected"]
ActionStageName = Literal[
    "submitted",
    "basis_validated",
    "validated",
    "coordinate_transformed",
    "rescaled",
    "bounds_validated",
    "rounding_normalized",
    "index_resolved",
    "grounded",
    "rejected",
    "dispatched",
]
ActionCoordinateSpace = Literal["none", "image", "original_frame", "device"]
ActionPipelineReason = Literal[
    "model_submitted",
    "validated",
    "reverse_rescale",
    "coordinates_clamped",
    "index_resolved",
    "driver_dispatched",
    "basis_validated",
    "rounding_normalization",
    "stale_observation_basis",
    "unknown_observation_basis",
    "non_actionable_observation_basis",
    "index_unavailable_for_observation",
    "ambiguous_observation_basis",
    "mismatched_observation_basis",
    "unknown_coordinate_geometry",
    "coordinate_out_of_bounds",
    "unresolved_observation_index",
    "invalid_installed_app_selection",
    "surface_index_requires_drag",
    "surface_index_unavailable",
    "unknown_surface_index",
    "drag_outside_surface",
]


class ActionPipelineStage(BaseModel):
    """One material, bounded action transformation stage."""

    stage: ActionStageName
    action: Action
    coordinate_space: ActionCoordinateSpace = "none"
    reason: ActionPipelineReason
    observation_id: str = ""
    coordinate_space_id: str = ""
    transform_id: str = ""
    source_geometry: list[int] = Field(default_factory=list)
    target_geometry: list[int] = Field(default_factory=list)
    validation_result: dict[str, Any] = Field(default_factory=dict)


class ActionPipeline(BaseModel):
    """Compact provenance for one Executor action."""

    origin: ActionOrigin = "model"
    fallback_reason: ActionPipelineReason | None = None
    missing_required_fields: list[str] = Field(default_factory=list)
    validation_attempts: int = 1
    original_frame_size: list[int] = Field(default_factory=list)
    compressed_frame_size: list[int] = Field(default_factory=list)
    basis_observation_id: str = ""
    active_observation_id: str = ""
    coordinate_space_id: str = ""
    transform_id: str = ""
    source_geometry: list[int] = Field(default_factory=list)
    target_geometry: list[int] = Field(default_factory=list)
    index_set_id: str = ""
    validation_result: dict[str, Any] = Field(default_factory=dict)
    stages: list[ActionPipelineStage] = Field(default_factory=list)
    driver_coordinates: dict[str, float] = Field(default_factory=dict)
    driver_success: bool | None = None
    dispatch_suppressed: bool = False


class SubmittedActionSnapshot(BaseModel):
    """Exact model-submitted action payload safe for the semantic timeline."""

    model_config = ConfigDict(extra="forbid")

    type: str
    index: int | None = None
    surface_index: int | None = Field(default=None, ge=0, strict=True)
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    text: str | None = None
    text_redacted: bool = False
    key: str | None = None
    app: str | None = None
    direction: str | None = None
    duration_ms: int | None = None
    skill_action_id: str | None = None
    image_size: tuple[int, int] | None = None

    @model_validator(mode="after")
    def validate_image_size(self) -> SubmittedActionSnapshot:
        if self.image_size is not None and any(value <= 0 for value in self.image_size):
            raise ValueError("submitted action image_size values must be positive")
        return self


class ActionTargetSnapshot(BaseModel):
    """Source-typed target evidence bound to the submitted observation."""

    model_config = ConfigDict(extra="forbid")

    index: int | None = None
    role: str = ""
    raw_text: str = ""
    raw_a11y_label: str = ""
    raw_hint: str = ""
    raw_fields_redacted: bool = False
    bounds: list[int] = Field(default_factory=list)
    editability: Literal["editable", "not_editable", "unknown", "conflict"] = "unknown"
    focused: bool = False
    password: bool = False
    package: str = ""
    window_id: int | None = None
    source_class: str = ""
    resource_id: str = ""


class ExecutorStepSubmit(BaseModel):
    """LLM-facing arguments for ``submit_executor_step``.

    Excludes orchestrator/driver stamps (`result`, `subgoal_at_tick`) so the tool schema does not invite
    the model to invent those fields.
    """

    model_config = ConfigDict(extra="forbid")

    decision: ExecutorDecisionKind
    summary: str = Field(
        min_length=1,
        description=(
            "For act, one very short action intent with no reasoning or completion "
            "claim. For request_review, concise facts established by delivered "
            "evidence. For request_replan, the concise blocker."
        ),
    )
    action: Action | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> ExecutorStepSubmit:
        self.summary = self.summary.strip()
        if not self.summary:
            raise ValueError("executor summary must be non-empty")
        if self.decision == ExecutorDecisionKind.ACT and self.action is None:
            raise ValueError("act requires exactly one device action")
        if self.decision != ExecutorDecisionKind.ACT and self.action is not None:
            raise ValueError("handoffs must omit device action")
        return self

    def to_step(
        self,
        *,
        basis_observation_id: str = "",
        evidence_refs: list[str] | None = None,
    ) -> ExecutorStep:
        return ExecutorStep(
            schema_version=3,
            decision=self.decision,
            action=self.action,
            summary=self.summary,
            basis_observation_id=basis_observation_id,
            evidence_refs=list(evidence_refs or []),
        )


class ExecutorStep(BaseModel):
    """One atomic Executor decision within a loop tick.

    `result` carries the Driver ActionResult digest fed back to the Reviewer;
    `summary` is the compact decision text: operation intent for `act`, established
    facts for `request_review`, or the blocker for `request_replan`.
    `subgoal_at_tick` records the active model-authored subgoal. Exact
    observations and timestamps provide the trace boundary; the removed
    model-authored window label is not retained as a compatibility field.
    """

    schema_version: int = 1
    decision: ExecutorDecisionKind = ExecutorDecisionKind.ACT
    action: Action | None = None
    summary: str = ""
    result: str = ""
    subgoal_at_tick: str = ""
    basis_observation_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    action_pipeline: ActionPipeline | None = None
    action_receipt: ActionReceipt | None = None
    submitted_action_snapshot: SubmittedActionSnapshot | None = None
    target_snapshot: ActionTargetSnapshot | None = None
    recovery_reason: ActionPipelineReason | None = None


class AgentState(BaseModel):
    """Explicit shared state for Reviewer, Planner, and Executor projections.

    Persisted per task; independent of any SDK ``message_history``. On crash
    or session loss each focused role rebuilds its projection from this state.

    ``revisable`` stores the current plan and runtime budgets. Versioned notes
    and action evidence live in the task record store.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: str
    # Authoritative device-local date captured by the runtime. Models must not
    # substitute the host clock for relative-date instructions.
    current_device_date: str = ""
    # Adapter-owned conventions for deterministic evaluation environments.
    temporal_conventions: list[str] = Field(default_factory=list, max_length=8)
    revisable: PlanRuntime = Field(default_factory=PlanRuntime)
    current_subgoal: str = ""
    step_number: int = 0
    # Mechanical task-wide liveness count across Reviewer/Planner/Executor
    # invocations, including provider retries. It is not a semantic score.
    role_invocation_count: int = 0
    # Internal tool scope. Model-visible catalogs are not derived from the instruction.
    frozen_skill_dirs: list[str] = Field(default_factory=list)
    # Deterministic task hints and the Planner-selected Skill handoff.
    skill_app_candidates: list[str] = Field(default_factory=list)
    active_target_app: str = ""
    active_workflow_ids: list[str] = Field(default_factory=list, max_length=2)
    # Explicit opt-in: ordinary and evaluation tasks never run a hidden learner call.
    skill_learn: bool = False
    # Optional per-task model overrides (must be keys in MODELS_JSON when set).
    manager_model: str | None = None
    executor_model: str | None = None

class StepReport(BaseModel):
    """One persisted loop-tick step, carrying artifact refs for Console debug."""

    action: Action | None = None
    executor_decision: ExecutorDecisionKind = ExecutorDecisionKind.ACT
    action_pipeline: ActionPipeline | None = None
    action_receipt: ActionReceipt | None = None
    summary: str = ""
    observation_digest: str = ""
    observation_mode: ObservationMode = ObservationMode.TREE_ONLY
    subgoal_at_tick: str = ""
    success: bool = True
    reason: str = ""
    tree_ref: str | None = None
    screenshot_ref: str | None = None
    annotated_ref: str | None = None
    llm_input_ref: str | None = None
    llm_output_ref: str | None = None
    estimated_tokens: int = 0
    observation_id: str = ""
    post_observation_id: str = ""
    post_tree_ref: str | None = None
    post_annotated_ref: str | None = None
    basis_observation_id: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class TraceEvent(BaseModel):
    """A single observability event attached to a task."""

    task_id: str
    node_id: str | None = None  # now used as current_subgoal identifier slot
    step_seq: int | None = None
    kind: Literal[
        "llm",
        "ui",
        "shot",
        "dag",
        "skill",
        "system",
        "task_scope",
        "planner_decision",
        "reviewer_decision",
        "executor_tick",
        "loop_tick",
        "agent_tool_started",
        "agent_tool_finished",
        "agent_tool_failed",
        "agent_llm_round_started",
        "agent_llm_round_finished",
        "agent_llm_stream",
        "agent_input_safety_degraded",
        "agent_tool_call_recovery",
        "harness_event",
    ] = "system"
    level: LogLevel = LogLevel.INFO
    message: str = ""
    payload_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: float | None = None


class TaskRecord(BaseModel):
    """Persisted task metadata for Console list/status views.

    `current_node_id` stores the `current_subgoal` identifier slot while
    `state` carries the full `AgentState`.
    `device_serial` binds the task to one ADB device (multi-device-concurrent).
    """

    id: str
    instruction: str
    status: TaskStatus = TaskStatus.QUEUED
    current_node_id: str | None = None
    failure_reason: str | None = None
    created_at: float
    updated_at: float
    plan: list[str] = Field(default_factory=list)
    current_subgoal: str = ""
    step_number: int = 0
    state: AgentState | None = None
    device_serial: str | None = None
    skill_learn: bool = False
