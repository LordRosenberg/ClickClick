# Process topology

[中文](processes.zh-CN.md)

[README](../README.md) · [Current architecture](architecture.md) · [Deployment guide](deployment.md)

The Control API embeds the selected agent harness, including role orchestration, tool sessions and context/memory management. The default is Planner + Executor with on-demand Reviewer. The ContractAuthor-first runtime has been removed. The model, harness, Session and device-tool boundaries in the [architecture diagram](architecture.md) are logical responsibilities, with Console and evaluation around them. Session currently uses local SQLite/artifact storage; it is not a separately deployed session service.

| Mode | Processes |
| --- | --- |
| Local device | Browser → Control API with embedded orchestrator and local driver → Android device. |
| Remote device host | Browser → Control API with orchestrator → Driver RPC host → attached devices. |
| Fixture | Control API uses a fixture driver; deterministic fake-agent tests also replace model calls. |
| Standalone task | `clickclick-agent` runs a task using configured local or remote device access. |

The built Console is served by Control API; a separate Vite process is only needed for frontend development. The vendored scrcpy server starts on demand and shares its stream between observation and Live view.

See [deployment](deployment.md) for installation, ports, remote hubs, HTTPS and startup checks. See [architecture](architecture.md) for role transitions, session tools, observations and trace persistence. See [evaluation](evaluation.md) for the separate AndroidWorld processes and fixture requirements.

Earlier descriptions of a mandatory three-role contract loop and a mandatory standalone Driver process have been superseded by these guides. Dated experiment reports remain historical evidence.
