"""Typed domain schemas shared across Agent, Driver, and Control API."""

from __future__ import annotations

from enum import Enum
import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class PlannerMode(str, Enum):
    """The only two outcomes available to the planning role."""

    EXECUTE = "execute"
    REVIEW = "review"


class ExecutorDecisionKind(str, Enum):
    """Executor routing intent, separate from any device action."""

    ACT = "act"
    REQUEST_REVIEW = "request_review"
    REQUEST_REPLAN = "request_replan"


class ReviewerVerdict(str, Enum):
    """Reviewer-owned semantic boundary outcomes."""

    ACCEPT = "accept"
    RETRY = "retry"
    REPLAN = "replan"
    DONE = "done"
    BLOCKED = "blocked"


class TaskContractBody(BaseModel):
    """Immutable model-authored task requirements; refs are projected by Harness."""

    model_config = ConfigDict(extra="forbid")

    must_happen: list[str] = Field(default_factory=list)
    final_ui_state: list[str] = Field(default_factory=list)
    answer: list[str] = Field(default_factory=list)
    disqualifying_clauses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_projection(self) -> TaskContractBody:
        groups = (
            self.must_happen,
            self.final_ui_state,
            self.answer,
            self.disqualifying_clauses,
        )
        if not any(groups[:3]):
            raise ValueError("task contract requires at least one requirement")
        for values in groups:
            normalized = [value.strip() for value in values]
            if any(not value for value in normalized):
                raise ValueError("task contract text must be non-empty")
            values[:] = normalized
        return self


class SubgoalContractBody(BaseModel):
    """Planner-authored observable boundary conditions."""

    model_config = ConfigDict(extra="forbid")

    success_conditions: list[str] = Field(min_length=1)
    disqualifying_clauses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_projection(self) -> SubgoalContractBody:
        success = [value.strip() for value in self.success_conditions]
        clauses = [value.strip() for value in self.disqualifying_clauses]
        if any(not value for value in [*success, *clauses]):
            raise ValueError("subgoal contract text must be non-empty")
        self.success_conditions = success
        self.disqualifying_clauses = clauses
        return self


class ActiveCompletionContract(BaseModel):
    """Runtime binding for one Planner-authored subgoal contract body."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str = ""
    lineage_id: str = ""
    boundary_generation: int = 0
    created_step: int = 0
    target_requirement_ref: str = ""
    body: SubgoalContractBody | None = None


class ActiveTaskCompletionContract(BaseModel):
    """Runtime binding for the persistent whole-task completion contract."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str = ""
    revision: int = 0
    created_step: int = 0
    body: TaskContractBody | None = None


class Action(BaseModel):
    """One device action requested by an Executor.

    Device types: `tap`/`tap_xy`/`type`/`replace_text`/`swipe`/
    `long_press`/`scroll`/`drag`/`key`/`launch`/`back`/`home`/`sleep`.
    `scroll` carries a content-navigation `direction`: down reveals content
    below, up reveals content above, and horizontal values follow the same
    convention. `long_press` and `drag` reuse `duration_ms`; `drag` uses `x/y`
    as start and `x2/y2` as end.
    """

    type: Literal[
        "tap",
        "tap_xy",
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
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    text: str | None = None
    key: str | None = None
    app: str | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    duration_ms: int | None = None



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


class FactEntry(BaseModel):
    """One short working fact in TaskMemory."""

    value: str = ""
    source: Literal["reviewer"] = "reviewer"
    step: int = 0
    evidence_handles: list[str] = Field(default_factory=list)
    packet_digest: str = ""


class _BoundaryReviewBase(BaseModel):
    """Shared structure for one mechanically sourced boundary fact."""

    model_config = ConfigDict(extra="forbid")

    detail: str = ""


class DriverDispatchFailed(_BoundaryReviewBase):
    type: Literal["driver_dispatch_failed"] = "driver_dispatch_failed"
    source: Literal["runtime"] = "runtime"


class VisualGroundingRejected(_BoundaryReviewBase):
    type: Literal["visual_grounding_rejected"] = "visual_grounding_rejected"
    source: Literal["runtime"] = "runtime"


class PostActionObservationMissing(_BoundaryReviewBase):
    type: Literal["post_action_observation_missing"] = "post_action_observation_missing"
    source: Literal["runtime"] = "runtime"


class ActionResultUnsuccessful(_BoundaryReviewBase):
    type: Literal["action_result_unsuccessful"] = "action_result_unsuccessful"
    source: Literal["runtime"] = "runtime"


class ExecutorReplanRequested(_BoundaryReviewBase):
    type: Literal["executor_replan_requested"] = "executor_replan_requested"
    source: Literal["executor"] = "executor"


BoundaryReviewFact = Annotated[
    DriverDispatchFailed
    | VisualGroundingRejected
    | PostActionObservationMissing
    | ActionResultUnsuccessful
    | ExecutorReplanRequested,
    Field(discriminator="type"),
]


class RecoveryState(BaseModel):
    """Runtime-owned retry continuity state for the active subgoal."""

    lineage_id: str = ""
    boundary_generation: int = 0
    awaiting_review: bool = False
    boundary_cause: str = ""
    boundary_review: BoundaryReviewFact | None = None
    review_requirement_ref: str = ""
    last_reviewer_verdict: ReviewerVerdict | None = None
    last_reviewer_reason: str = ""


class RuntimeBudgetSnapshot(BaseModel):
    """Task-wide safety capacity; not a semantic completion signal."""

    remaining_steps: int = 0


class ProgressEntry(BaseModel):
    """One Reviewer-accepted semantic statement and its append-only provenance."""

    model_config = ConfigDict(extra="forbid")

    progress_id: str = Field(min_length=1)
    requirement_ref: str = ""
    statement: str = Field(min_length=1)
    evidence_handles: list[str] = Field(min_length=1)
    packet_digest: str = Field(min_length=1)
    accepted_step: int = Field(default=0, ge=0)
    source_subgoal: str = ""
    superseded_by_packet_digest: str = ""
    superseded_step: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_progress(self) -> ProgressEntry:
        self.progress_id = self.progress_id.strip()
        self.requirement_ref = self.requirement_ref.strip()
        self.statement = self.statement.strip()
        self.packet_digest = self.packet_digest.strip()
        self.source_subgoal = self.source_subgoal.strip()
        self.evidence_handles = [
            handle.strip() for handle in self.evidence_handles
        ]
        self.superseded_by_packet_digest = (
            self.superseded_by_packet_digest.strip()
        )
        if not self.progress_id or not self.statement or not self.packet_digest:
            raise ValueError("progress identity, statement, and packet digest are required")
        if any(not handle for handle in self.evidence_handles):
            raise ValueError("progress evidence handles must be non-empty")
        if len(self.evidence_handles) != len(set(self.evidence_handles)):
            raise ValueError("progress evidence handles must be unique")
        if bool(self.superseded_by_packet_digest) != (self.superseded_step is not None):
            raise ValueError(
                "superseded progress requires both packet digest and step"
            )
        return self

    @property
    def effective(self) -> bool:
        return not self.superseded_by_packet_digest


class SubmittedActionSnapshot(BaseModel):
    """Exact model-submitted action payload safe for the semantic timeline."""

    model_config = ConfigDict(extra="forbid")

    type: str
    index: int | None = None
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


class MemoryEvent(BaseModel):
    """Canonical compact memory event; raw trace remains authoritative."""

    kind: Literal["fact", "attempt", "boundary"]
    summary: str = ""
    model_intent: str = ""
    subgoal: str = ""
    step: int = 0
    action_type: str = ""
    action_signature: str = ""
    intent_sha256: str = ""
    outcome: str = ""
    lineage_id: str = ""
    contract_id: str = ""
    submitted_action_type: str = ""
    submitted_action: SubmittedActionSnapshot | None = None
    target: ActionTargetSnapshot | None = None
    dispatch_status: Literal["dispatched", "rejected", "failed"] | None = None
    post_dispatch_observation: Literal["accepted", "missing", "not_applicable"] = (
        "not_applicable"
    )
    basis_observation_id: str = ""
    post_observation_id: str = ""
    refs: list[str] = Field(default_factory=list)


class TaskMemory(BaseModel):
    """Canonical accepted progress plus durable facts and mechanical events."""

    model_config = ConfigDict(extra="forbid")

    facts: dict[str, FactEntry] = Field(default_factory=dict)
    progress: list[ProgressEntry] = Field(default_factory=list)
    events: list[MemoryEvent] = Field(default_factory=list)


class ExecutorStepSubmit(BaseModel):
    """LLM-facing arguments for ``submit_executor_step``.

    Excludes orchestrator/driver stamps (`result`, `subgoal_at_tick`) so the tool schema does not invite
    the model to invent those fields.
    """

    model_config = ConfigDict(extra="forbid")

    decision: ExecutorDecisionKind
    summary: str = Field(min_length=1)
    action: Action | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> ExecutorStepSubmit:
        self.summary = self.summary.strip()
        if not self.summary:
            raise ValueError("executor summary must be non-empty")
        if self.decision == ExecutorDecisionKind.ACT and self.action is None:
            raise ValueError("act requires exactly one device action")
        if self.decision != ExecutorDecisionKind.ACT and self.action is not None:
            raise ValueError("boundary requests must omit device action")
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
    `summary` is the semantic decision line (what + why) for cognitive logs.
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
class SubgoalCompletionContractSubmit(BaseModel):
    """LLM-facing subgoal contract; temporal task fields are not exposed."""

    model_config = ConfigDict(extra="forbid")
    success_conditions: list[str] = Field(min_length=1)
    disqualifying_clauses: list[str] = Field(default_factory=list)


class ReviewerScopeSubmit(BaseModel):
    """UI-independent whole-task success contract authored once by Reviewer."""

    model_config = ConfigDict(extra="forbid")

    must_happen: list[str] = Field(default_factory=list)
    final_ui_state: list[str] = Field(default_factory=list)
    answer: list[str] = Field(default_factory=list)
    disqualifying_clauses: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_projection(self) -> ReviewerScopeSubmit:
        clauses = [clause.strip() for clause in self.disqualifying_clauses]
        if any(not clause for clause in clauses):
            raise ValueError("scope disqualifying clauses must be non-empty")
        self.disqualifying_clauses = clauses
        return self

    def to_contract(self) -> TaskContractBody:
        return TaskContractBody(
            must_happen=self.must_happen,
            final_ui_state=self.final_ui_state,
            answer=self.answer,
            disqualifying_clauses=self.disqualifying_clauses,
        )


class PlannerDecision(BaseModel):
    """One rolling plan decision with no adjudication or terminal authority."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    mode: PlannerMode
    next_subgoal: str = ""
    completion_contract: SubgoalContractBody | None = None
    plan: list[str] = Field(default_factory=list)
    target_requirement_ref: str = ""
    review_requirement_ref: str = ""


class PlannerDecisionSubmit(BaseModel):
    """Strict LLM-facing Planner protocol for execute or current-state review."""

    model_config = ConfigDict(extra="forbid")

    mode: PlannerMode
    next_subgoal: str = ""
    completion_contract: SubgoalCompletionContractSubmit | None = None
    plan: list[str] = Field(default_factory=list)
    target_requirement_ref: str = ""
    review_requirement_ref: str = ""

    @model_validator(mode="after")
    def validate_mode(self) -> PlannerDecisionSubmit:
        supplied = set(self.model_fields_set)
        self.next_subgoal = self.next_subgoal.strip()
        self.target_requirement_ref = self.target_requirement_ref.strip()
        self.review_requirement_ref = self.review_requirement_ref.strip()
        plan = [step.strip() for step in self.plan]
        if any(not step for step in plan):
            raise ValueError("plan steps must be non-empty")
        self.plan = plan
        if self.mode == PlannerMode.EXECUTE:
            if "review_requirement_ref" in supplied:
                raise ValueError("execute must omit review_requirement_ref")
            if not self.target_requirement_ref:
                raise ValueError("execute requires target_requirement_ref")
            if not self.next_subgoal:
                raise ValueError("execute requires one non-empty next_subgoal")
            if self.completion_contract is None:
                raise ValueError("execute requires completion_contract")
            if "plan" not in supplied:
                raise ValueError("execute requires plan, which may be []")
        else:
            forbidden = supplied.intersection({
                "next_subgoal", "completion_contract", "plan",
                "target_requirement_ref",
            })
            if forbidden:
                raise ValueError(
                    "review must omit execute fields: "
                    + ", ".join(sorted(forbidden))
                )
            if not self.review_requirement_ref:
                raise ValueError("review requires review_requirement_ref")
        return self

    def to_decision(self) -> PlannerDecision:
        contract = None
        if self.completion_contract is not None:
            contract = SubgoalContractBody.model_validate(
                self.completion_contract.model_dump()
            )
        return PlannerDecision(
            schema_version=1,
            mode=self.mode,
            next_subgoal=self.next_subgoal,
            completion_contract=contract,
            plan=self.plan,
            target_requirement_ref=self.target_requirement_ref,
            review_requirement_ref=self.review_requirement_ref,
        )


class ReviewerAcceptedProgress(BaseModel):
    """One semantic progress statement correlated to delivered evidence."""

    model_config = ConfigDict(extra="forbid")

    requirement_ref: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_handles: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_projection(self) -> ReviewerAcceptedProgress:
        self.requirement_ref = self.requirement_ref.strip()
        self.statement = self.statement.strip()
        handles = [handle.strip() for handle in self.evidence_handles]
        if (
            not self.requirement_ref
            or not self.statement
            or any(not handle for handle in handles)
        ):
            raise ValueError(
                "requirement ref, accepted progress, and evidence handles must be non-empty"
            )
        if len(handles) != len(set(handles)):
            raise ValueError("accepted progress evidence handles must be unique")
        self.evidence_handles = handles
        return self


class ReviewerRememberedFact(BaseModel):
    """One Reviewer-accepted value retained for later work."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence_handles: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_projection(self) -> ReviewerRememberedFact:
        self.key = self.key.strip()
        self.value = self.value.strip()
        handles = [handle.strip() for handle in self.evidence_handles]
        if not self.key or not self.value or any(not handle for handle in handles):
            raise ValueError("remembered fact key, value, and evidence are required")
        if len(handles) != len(set(handles)):
            raise ValueError("remembered fact evidence handles must be unique")
        self.evidence_handles = handles
        return self


class ReviewerAnswer(BaseModel):
    """One Reviewer-authored answer bound to a contract answer ref."""

    model_config = ConfigDict(extra="forbid")

    requirement_ref: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_handles: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def normalize_projection(self) -> ReviewerAnswer:
        self.requirement_ref = self.requirement_ref.strip()
        self.text = self.text.strip()
        handles = [handle.strip() for handle in self.evidence_handles]
        if (
            not self.requirement_ref
            or not self.text
            or any(not handle for handle in handles)
        ):
            raise ValueError(
                "answer ref, text, and evidence handles must be non-empty"
            )
        if len(handles) != len(set(handles)):
            raise ValueError("answer evidence handles must be unique")
        self.evidence_handles = handles
        return self


class ReviewerDecision(BaseModel):
    """Reviewer-owned semantic verdict bound to one exact evidence packet."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    verdict: ReviewerVerdict
    accepted_progress: list[ReviewerAcceptedProgress] = Field(default_factory=list)
    remembered_facts: list[ReviewerRememberedFact] = Field(default_factory=list)
    answers: list[ReviewerAnswer] = Field(default_factory=list)
    superseded_progress_ids: list[str] = Field(default_factory=list)
    reason: str
    evidence_handles: list[str] = Field(default_factory=list)
    packet_digest: str

    def user_facing_answer(self) -> str:
        return "\n".join(item.text for item in self.answers if item.text)


class ReviewerDecisionSubmit(BaseModel):
    """Strict LLM-facing Reviewer boundary protocol; it cannot plan work."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewerVerdict
    accepted_progress: list[ReviewerAcceptedProgress] = Field(default_factory=list)
    remembered_facts: list[ReviewerRememberedFact] = Field(default_factory=list)
    answers: list[ReviewerAnswer] = Field(default_factory=list)
    superseded_progress_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    evidence_handles: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_verdict(self) -> ReviewerDecisionSubmit:
        supplied = set(self.model_fields_set)
        self.reason = self.reason.strip()
        self.superseded_progress_ids = [
            progress_id.strip() for progress_id in self.superseded_progress_ids
        ]
        self.evidence_handles = [
            handle.strip() for handle in self.evidence_handles
        ]
        if not self.reason:
            raise ValueError("review reason must be non-empty")
        if any(not value for value in self.superseded_progress_ids):
            raise ValueError("superseded progress ids must be non-empty")
        if any(not value for value in self.evidence_handles):
            raise ValueError("evidence handles must be non-empty")
        if len(self.superseded_progress_ids) != len(set(self.superseded_progress_ids)):
            raise ValueError("superseded progress ids must be unique")
        if len(self.evidence_handles) != len(set(self.evidence_handles)):
            raise ValueError("evidence handles must be unique")
        fact_keys = [fact.key for fact in self.remembered_facts]
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("remembered fact keys must be unique")
        answer_refs = [item.requirement_ref for item in self.answers]
        if len(answer_refs) != len(set(answer_refs)):
            raise ValueError("each answer ref may be bound at most once")
        cited_handles = {
            *self.evidence_handles,
            *(
                handle
                for progress in self.accepted_progress
                for handle in progress.evidence_handles
            ),
            *(
                handle
                for fact in self.remembered_facts
                for handle in fact.evidence_handles
            ),
            *(
                handle
                for item in self.answers
                for handle in item.evidence_handles
            ),
        }
        if not cited_handles:
            raise ValueError(f"{self.verdict.value} verdict requires evidence")
        if self.verdict != ReviewerVerdict.DONE and (
            "answers" in supplied and self.answers
        ):
            raise ValueError("non-done verdict must omit answers")
        return self

    def to_decision(self, *, packet_digest: str) -> ReviewerDecision:
        evidence_handles = list(dict.fromkeys([
            *self.evidence_handles,
            *(
                handle
                for progress in self.accepted_progress
                for handle in progress.evidence_handles
            ),
            *(
                handle
                for fact in self.remembered_facts
                for handle in fact.evidence_handles
            ),
            *(
                handle
                for item in self.answers
                for handle in item.evidence_handles
            ),
        ]))
        return ReviewerDecision(
            schema_version=1,
            packet_digest=packet_digest,
            **self.model_dump(exclude={"evidence_handles"}),
            evidence_handles=evidence_handles,
        )


class AgentState(BaseModel):
    """Explicit shared state for Reviewer, Planner, and Executor projections.

    Persisted per task; independent of any SDK ``message_history``. On crash
    or session loss each focused role rebuilds its projection from this state.

    ``task_memory.progress`` contains only Reviewer-accepted semantic progress.
    Canonical action events remain append-only mechanical evidence.
    """

    model_config = ConfigDict(extra="forbid")

    instruction: str
    plan: list[str] = Field(default_factory=list)
    next_role: Literal["reviewer", "planner", "executor"] = "reviewer"
    current_subgoal: str = ""
    task_memory: TaskMemory = Field(default_factory=TaskMemory)
    step_number: int = 0
    # Mechanical task-wide liveness count across Reviewer/Planner/Executor
    # invocations, including provider retries. It is not a semantic score.
    role_invocation_count: int = 0
    active_completion_contract: ActiveCompletionContract | None = None
    task_completion_contract: ActiveTaskCompletionContract | None = None
    recovery_state: RecoveryState = Field(default_factory=RecoveryState)
    # Explicit mechanical scope of the one active semantic-subgoal timeline.
    # Reviewer owns whether a boundary is accepted or residual; Harness only
    # maintains the corresponding lineage ids.
    active_timeline_lineage_ids: list[str] = Field(default_factory=list)
    runtime_budget: RuntimeBudgetSnapshot = Field(default_factory=RuntimeBudgetSnapshot)
    # Internal tool scope. Model-visible catalogs are not derived from the instruction.
    frozen_skill_dirs: list[str] = Field(default_factory=list)
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
        "agent_llm_round_finished",
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
