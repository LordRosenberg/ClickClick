"""New decision vocabulary with the existing mechanical action validators."""

from typing import Literal

from pydantic import Field, model_validator

from agent.revisable.tools import (
    Arguments,
    ReadResult,
    WriteNote,
    register_memory_tools,
    save_note,
    tool_schema,
)
from agent.session import AgentSession, executor_action_variants_schema
from agent.tool_registry import (
    AgentRole,
    AgentToolRegistry,
    AgentToolResult,
    AgentToolSpec,
    ToolCategory,
    ToolStatus,
)
from shared.schemas import Action, ExecutorDecisionKind, ExecutorStep


class ExecutorSubmission(Arguments):
    decision: Literal["act", "advance", "replan", "review", "finish"]
    summary: str = Field(
        min_length=1,
        description="Brief actual result or intent. At handoff, include unresolved facts/conflicts and relevant source references; do not restate the whole history.",
    )
    observation_id: str = Field(min_length=1)
    completed_stage_id: str | None = Field(
        default=None,
        description="For advance, identify the stage just completed using its stage_id.",
    )
    action: Action | None = None
    notes: list[WriteNote] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_action(self):
        if (self.decision == "act") != (self.action is not None):
            raise ValueError("Only act requires an action; other decisions omit it")
        if (self.decision == "advance") != bool(self.completed_stage_id):
            raise ValueError("Only advance requires completed_stage_id")
        return self


def executor_submission_schema(*, double_tap=False):
    schema = tool_schema(ExecutorSubmission)
    schema["$defs"]["Action"] = executor_action_variants_schema(double_tap=double_tap)
    return schema


def terminal_registry(session, schema, name, description, handler):
    registry = AgentToolRegistry()
    legacy = session._build_registry({})
    for spec in legacy.specs_for_role(session.role):
        if spec.name != "load_skill" or session.role != "planner":
            continue

        async def delegate(args, ctx, tool=spec.name):
            skill_id = args.get("skill_id", "")
            if session.role == "planner" and skill_id in session.workflow_catalog_ids:
                pack = session.library.get(skill_id)
                return ReadResult(
                    data={
                        "skill_id": skill_id,
                        "content": session.skill_message(pack, label="loaded")["content"],
                    }
                )
            result = await legacy.execute(tool, args, ctx)
            if result.status == ToolStatus.SUCCEEDED:
                pack = session.library.get(skill_id)
                if pack is not None:
                    return ReadResult(data={"skill_id": skill_id,
                        "content": session.skill_message(pack, label="loaded")["content"]})
            return result

        registry.register(spec.model_copy(update={"description": "Read a listed generic skill or app workflow by exact ID to choose stage skill_ids. Runtime injects selected bodies into Executor."}), delegate)
    registry.register(
        AgentToolSpec(
            name=name,
            description=description,
            category=ToolCategory.TERMINAL,
            roles=(AgentRole(session.role),),
            parameters=tool_schema(schema),
        ),
        handler,
    )
    return registry


class ExecutionSession(AgentSession):
    def freeze_allow_dirs(self, dirs):
        result = super().freeze_allow_dirs(dirs)
        self._index = []  # Planner selects and runtime injects; no Executor discovery.
        return result

    def _build_registry(self, external_handlers):
        legacy = super()._build_registry(external_handlers)
        registry = AgentToolRegistry()
        for spec in legacy.specs_for_role(self.role):
            if spec.category == ToolCategory.TERMINAL or spec.name == "load_skill":
                continue

            async def delegate(args, ctx, name=spec.name):
                return await legacy.execute(name, args, ctx)

            registry.register(spec, delegate)

        async def submit(args, ctx):
            submission = ExecutorSubmission.model_validate(args)
            if submission.observation_id != ctx.state.get("active_observation_id"):
                return AgentToolResult(
                    status=ToolStatus.INVALID_ARGUMENTS,
                    summary="Use the current observation_id; historical screens cannot ground actions.",
                    error="stale_observation",
                )
            if submission.decision == "advance":
                runtime = self.state.revisable
                if submission.completed_stage_id in runtime.completed_stage_ids:
                    return AgentToolResult(
                        data={
                            "already_completed": submission.completed_stage_id,
                            "current_stage_id": runtime.stage_id,
                            "current_stage": runtime.stage.model_dump() if runtime.stage else None,
                        },
                        summary="This stage is already complete. No progress changed. Decide for the current stage.",
                    )
                if submission.completed_stage_id != runtime.stage_id:
                    return AgentToolResult(
                        status=ToolStatus.INVALID_ARGUMENTS,
                        summary=f"Complete the active stage {runtime.stage_id}, or report a plan conflict.",
                        error="stale_stage",
                    )
            if submission.decision in {"act", "advance"} and self.state.revisable.stage is None:
                return AgentToolResult(
                    status=ToolStatus.PRECONDITION_NOT_MET,
                    summary="No active stage. Request replan for remaining work, or finish with supporting evidence.",
                    error="no_active_stage",
                )
            if submission.decision == "act":
                runtime = self.state.revisable
                limit = runtime.limits.device_actions
                from agent.targeted_input import required_actions
                required = required_actions(ctx.state.get("active_package"), submission.action)
                if limit is not None and runtime.execution_count + required > limit:
                    return AgentToolResult(
                        status=ToolStatus.PRECONDITION_NOT_MET,
                        error="device_action_limit_exhausted",
                        summary="Too few device actions remain for this operation. Choose a smaller supported action, or report established results and unfinished work; do not claim unsupported completion.",
                    )
                result = await legacy.execute(
                    "submit_executor_step",
                    {
                        "decision": "act",
                        "summary": submission.summary,
                        "action": submission.action.model_dump(exclude_none=True),
                    },
                    ctx,
                )
            else:
                # Executor.act_once transports non-action decisions without dispatch.
                result = AgentToolResult(
                    terminal_value=ExecutorStep(
                        decision=ExecutorDecisionKind.REQUEST_REPLAN,
                        summary=submission.summary,
                        basis_observation_id=submission.observation_id,
                    )
                )
            if result.status == ToolStatus.SUCCEEDED and result.terminal_value:
                if submission.notes:
                    with self.store.db.transaction():
                        for note in submission.notes:
                            save_note(self.store, note, self.state)
                ctx.state["directive"] = submission.decision
                ctx.state["completed_stage_id"] = submission.completed_stage_id
            return result

        registry.register(
            AgentToolSpec(
                name="submit_executor_step",
                description="Act, advance, replan, review, or finish. Include notes to save before the decision; a failed save prevents the action.",
                category=ToolCategory.TERMINAL,
                roles=(AgentRole.EXECUTOR,),
                parameters=executor_submission_schema(double_tap=self.settings.double_tap),
            ),
            submit,
        )
        register_memory_tools(registry, self.role, self.store, self.state)
        return registry
