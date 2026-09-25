"""State for the experimental, revisable-plan runtime."""

from typing import Literal
import time

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(
        min_length=1,
        description="Current outcome, source information to remember and exact requirements, including create/update/delete intent, distinct targets and preservation constraints when relevant. Usually one or two sentences; leave routes to Executor.",
    )
    target_app: str = Field(default="", description="Prefer the user-facing app name or alias, e.g. Camera or Clock; runtime resolves the installed package. Never invent a package ID. Use a package only when explicitly supplied by the user or current evidence; runtime validates installation. Leave empty if unknown and name the app in goal.")
    skill_ids: list[str] = Field(
        default_factory=list, max_length=4,
        validation_alias=AliasChoices("skill_ids", "workflow_ids"),
        description="Exact optional generic skill or target-app workflow IDs from the catalog. Runtime injects these bodies into Executor for this stage.",
    )


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_stage: Stage
    assumption_roadmap: list[str] = Field(
        default_factory=list,
        description="Tentative future outcomes and dependencies, not instructions to execute. Revise from new facts; omit click routes and general rules.",
    )


class PlannerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["execute", "review", "complete", "inconclusive"]
    reason: str = Field(
        min_length=1,
        description="On complete, include the requested answer; if its format is strict, give only that answer without explanation. Otherwise give a brief reason for the goal or verdict.",
    )
    plan: Plan | None = Field(
        default=None,
        description="Required only for execute; supply one current stage and optional tentative roadmap.",
    )

    source_refs: list[str] = Field(default_factory=list, max_length=12,
        validation_alias=AliasChoices("source_refs", "observation_ids"),
        description="Copy current observation IDs or tool/history source IDs used in the verdict; do not invent references.")
    resolved_questions: list[str] = Field(default_factory=list, max_length=8,
        description="For complete only: note_keys of supplied unresolved questions settled by the cited evidence. Explain in reason unless a strict answer format excludes it; this does not rewrite execution notes.")

    @model_validator(mode="after")
    def check_plan(self):
        if (self.decision == "execute") != (self.plan is not None):
            raise ValueError("Only execute requires a plan; other decisions omit it")
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["complete", "execute", "replan", "inconclusive"]
    reason: str = Field(min_length=1, description="On complete, include the requested answer; if its format is strict, give only that answer without explanation. Otherwise briefly explain the review decision.")
    source_refs: list[str] = Field(default_factory=list, max_length=12,
        validation_alias=AliasChoices("source_refs", "observation_ids"),
        description="Copy current observation IDs or tool/history source IDs used in the verdict; do not invent references.")
    resolved_questions: list[str] = Field(default_factory=list, max_length=8,
        description="For complete only: note_keys of supplied unresolved questions settled by the cited evidence. Explain in reason unless a strict answer format excludes it; this does not rewrite execution notes.")


class TaskLimits(BaseModel):
    """Caller-owned task limits, independent of per-call stepping boundaries."""

    model_config = ConfigDict(extra="forbid")
    device_actions: int | None = Field(default=None, ge=0)
    prediction_rounds: int | None = Field(default=None, ge=1)
    deadline_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    def expired(self) -> bool:
        return self.deadline_at is not None and time.time() >= self.deadline_at


class PlanRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = 0
    plan: Plan | None = None
    stage_start_step: int = 0
    next_role: Literal["planner", "executor", "reviewer"] = "planner"
    handoff: Literal["initial", "advance", "replan", "review", "finish"] = "initial"
    feedback: str = ""
    plan_reason: str = ""
    completion_reason: str = ""
    dialogue_refs: list[str] = Field(default_factory=list)
    summary: str = ""
    execution_count: int = 0
    prediction_round_count: int = Field(default=0, ge=0)
    limits: TaskLimits = Field(default_factory=TaskLimits)
    last_review_signature: str = ""
    completed_stage_ids: list[str] = Field(default_factory=list)
    delivered_context: dict = Field(default_factory=dict)
    last_executor_stall: str = ""

    def complete_stage(self, stage_id: str) -> bool:
        if stage_id in self.completed_stage_ids:
            return False
        if self.stage is None or stage_id != self.stage_id:
            raise ValueError("Completion must identify the active stage")
        self.completed_stage_ids.append(stage_id)
        return True

    @property
    def stage_id(self) -> str:
        return f"plan_{self.revision}_stage_1"

    @property
    def stage(self) -> Stage | None:
        if self.plan and self.stage_id not in self.completed_stage_ids:
            return self.plan.current_stage
        return None
