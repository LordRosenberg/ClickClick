"""Read-only official knowledge and bounded research lifecycle tools."""
from agent.tool_registry import AgentToolSpec, AgentToolResult, AgentRole, ToolCategory

OFFICIAL_TOOL = {"type":"function", "function": {
    "name":"read_official_skill", "description":"Read a literal page from the frozen official library, including resources. Existing knowledge is not current-screen evidence.",
    "parameters":{"type":"object", "properties":{"path":{"type":"string"},
        "offset":{"type":"integer","minimum":0}, "length":{"type":"integer","minimum":1,"maximum":4000}},
        "required":["path"], "additionalProperties":False}}}


def register_research_tools(registry, backend, budget):
    async def official(args, ctx):
        if backend.snapshot is None:
            raise ValueError("official library has not been frozen")
        return AgentToolResult(summary="Frozen official-library page", data=backend.snapshot.read(
            args["path"], args.get("offset",0), args.get("length",4000)))
    registry.register(AgentToolSpec(name="read_official_skill", description=OFFICIAL_TOOL["function"]["description"],
        category=ToolCategory.KNOWLEDGE, roles=(AgentRole.EXECUTOR,), parameters=OFFICIAL_TOOL["function"]["parameters"]), official)

    async def environment(args, ctx):
        env = backend.environment
        if env is None:
            raise ValueError("research environment has not started")
        def receipt():
            data = env.receipt()
            if (env.reset_adapter is not None and data["status"] == "open"
                    and data["unresolved_effects"] and not data["owned_resource_count"]
                    and (env.stage == "cleanup" or operation in {"verify", "close"})):
                data["cleanup_handoff"] = {
                    "status": "pending_host_fixture_reset",
                    "scope": env.reset_adapter.scope,
                    "next_step": "Set stage=cleanup and finish this probe with its findings and reset pending. The host runs the declared adapter after the probe returns; verify/close cannot run it.",
                    "policy": "Unresolved effects remain unverified. A successful independent reset receipt is required before admission, validation or exit; this handoff does not prove cleanup."}
            return data
        operation = args["operation"]
        if operation == "stage":
            env.set_stage(args["stage"])
            budget.set_phase(args["stage"])
        elif operation == "record_claim":
            # Executor memory tools return task-local IDs from the current
            # probe. Diagnosis defaults to source history, but these claims
            # must resolve in the same exploration that produced the IDs.
            refs = [ref if "/" in ref else "exploration/" + ref
                for ref in args["evidence_refs"]]
            for ref in refs:
                backend.resolve_evidence(ref)
            env.record_claim(args["description"], refs)
        elif operation == "verify":
            await env.verify()
        elif operation == "close":
            if env.receipt()["unresolved_effects"]:
                from agent.tool_registry import ToolStatus
                return AgentToolResult(status=ToolStatus.PRECONDITION_NOT_MET,
                    summary="Unresolved effects: continue cleanup or preserve an unresolved final receipt",
                    data=receipt())
            env.close()
        return AgentToolResult(summary="Research lifecycle receipt; claims are not restoration proof", data=receipt())
    registry.register(AgentToolSpec(name="research_environment", category=ToolCategory.KNOWLEDGE,
        description="Manage prepare/probe/cleanup in the same research memory. Unprefixed evidence IDs refer to this exploration; prefix original records with source/. Verification uses trusted adapters; absence remains unknown. Stage changes grant no new budget. Record changes with native evidence before cleanup. For a declared disposable fixture, finish with reset pending so the host can run its adapter; verify/close do not run that reset. Other sessions require independent cleanup verification. Does not reset apps or authorize new mutations.",
        roles=(AgentRole.EXECUTOR,), parameters={"type":"object", "properties": {
            "operation":{"type":"string","enum":["stage","record_claim","verify","close","receipt"]},
            "stage":{"type":"string","enum":["prepare","probe","cleanup"]},
            "description":{"type":"string","maxLength":1600},
            "evidence_refs":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":8}},
            "required":["operation"],"additionalProperties":False}), environment)
