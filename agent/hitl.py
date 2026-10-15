"""Register the optional ordinary human-input tool for a task session."""

from pydantic import ValidationError

from agent.tool_registry import AgentRole, AgentToolResult, AgentToolSpec, ToolCategory, ToolStatus
from shared.hitl import AskUser, Question, HumanInputSuspended, InputConflict
from agent.interaction_policy import session_interaction_profile


def register_human_tool(registry, session):
    bridge = getattr(session, "human_input", None)
    if bridge is None or session.role not in {"planner", "executor"}:
        return
    broker, task_id = bridge
    config = broker.config(task_id)
    profile = session_interaction_profile(session, config)
    if not profile.enabled:
        return
    delegated = profile.delegated

    def ask(args, ctx):
        try:
            request = AskUser(questions=[Question.model_validate(args)]) if delegated else AskUser.model_validate(args)
            if ctx.state.get("provider_rounds_remaining", 0) < 1:
                raise InputConflict("no_post_answer_provider_round")
            remaining = getattr(session, "human_provider_budget", lambda: None)()
            if remaining is not None and remaining < 1:
                raise InputConflict("task_provider_budget_exhausted")
            checkpoint = ctx.state["_human_checkpoint"]
            item = broker.begin(task_id, request.model_dump(exclude_none=True), checkpoint,
                                role=session.role, invocation_id=ctx.invocation_id,
                                call_id=ctx.state["_active_tool_call_id"],
                                resumed_interaction_id=ctx.state.get("_resumed_human_interaction_id"))
        except ValidationError as exc:
            return AgentToolResult(status=ToolStatus.INVALID_ARGUMENTS,
                                   summary=f"Invalid question: {exc}", error="invalid_human_arguments")
        except InputConflict as exc:
            return AgentToolResult(status=ToolStatus.PRECONDITION_NOT_MET,
                                   summary=f"Cannot ask: {exc}", error=str(exc).split(":", 1)[0])
        raise HumanInputSuspended(item["interaction_id"])

    registry.register(AgentToolSpec(
        name="ask_user", roles=(AgentRole(session.role),), category=ToolCategory.KNOWLEDGE,
        description=profile.description,
        parameters=profile.schema,
    ), ask)
