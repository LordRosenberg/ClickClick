"""Real Control API + harness with a gated fake provider, for stdio integration."""

import asyncio
import json
import os
from pathlib import Path

import uvicorn

from shared.llm_gateway import GatewayResponse, ToolCall


async def complete(model, messages, *, tools, attempt_meter=None, **kwargs):
    if attempt_meter:
        attempt_meter("model_call_started", {})
    gate = Path(os.environ["CLICKCLICK_TEST_RELEASE_FILE"])
    while not gate.exists():
        await asyncio.sleep(.05)
    names = [t["function"]["name"] for t in tools]
    if "submit_planner_decision" in names:
        name, args = "submit_planner_decision", {"decision": "complete", "reason": "Fixture complete"}
    else:
        name, args = "submit_reviewer_decision", {"decision": "complete", "reason": "Fixture complete"}
    return GatewayResponse(content="", model="test", stop_reason="tool_calls",
                           tool_calls=[ToolCall(id="call", name=name, arguments=json.dumps(args))])


def main():
    import agent.session
    from control_api.main import create_app
    agent.session.complete = complete
    uvicorn.run(create_app(include_temp_runs=False), host="127.0.0.1",
                port=int(os.environ["CLICKCLICK_API_PORT"]), log_level="warning")


if __name__ == "__main__":
    main()
