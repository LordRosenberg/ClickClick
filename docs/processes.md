# Process topology

[中文](processes.zh-CN.md) · [README](../README.md) · [Architecture](architecture.md) · [Deployment](deployment.md)

The Control API hosts the agent harness: role orchestration, task sessions, device access and context management. Planner + Executor is the default, with on-demand Reviewer. Session data uses local SQLite and artifact storage; no separate database or session service is required.

| Entry point | Process relationship |
| --- | --- |
| Console with a local device | Browser → Control API with embedded orchestrator and local driver → Android device |
| HTTP MCP | Assistant → independent HTTP MCP process → Control API → device |
| stdio MCP | Assistant → lightweight stdio adapter → Control API → device |
| Remote device host | Control API → Driver RPC host → attached devices |
| Standalone task | `clickclick-agent` → configured local or remote driver → device |
| Fixture tests | Control API → fixture driver; deterministic fake-agent tests also replace model calls |

HTTP MCP has its own event loop and port. Task submission, progress queries and control requests go to the backend; model and device work execute there. Multiple MCP clients can share that backend, and disconnecting a client does not cancel its tasks.

Control API serves the built Console. A separate Vite process is needed only for frontend development. The scrcpy source belongs to task observation; Console displays saved screenshots without acquiring or resetting the source.

See [deployment](deployment.md) for startup and device access, [local MCP](local-mcp.md) for assistant connection, and [evaluation](evaluation.md) for benchmark processes.
