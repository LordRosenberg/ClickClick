# Use ClickClick from a PC AI assistant

[中文](local-mcp.zh-CN.md)

ClickClick exposes eight task tools over local Streamable HTTP and stdio MCP. An assistant submits a complete Android goal; an independent ClickClick backend executes it with its own configured API or subscription models, prompts, harness and task session. The host assistant's subscription, system prompt and chat context are not used for internal inference.

## Desktop setup

1. Check the [official Releases](https://github.com/LordRosenberg/ClickClick/releases) for an installer matching the operating system and CPU architecture. If there is no matching installer, use the source setup below. GitHub's Source code ZIP is for source deployment. macOS test builds may trigger system source checks; verify the download source and follow system prompts.
2. Run the installer. It bundles private Python, installed dependencies, Console, ADB, Collector and ADBKeyboard, and opens a local browser setup page. You do not need to install Python, Node, Git or pip separately.
3. In Console Settings, choose Codex / ChatGPT subscription login or configure an API. For subscription login, complete official browser authorization with the provided device code. For an API, enter the model ID, base URL and key, with optional User-Agent and thinking/reasoning parameters. Keep keys out of chat and command arguments. Save when all tasks (including paused tasks) and personal optimization have ended; new tasks immediately use the saved settings. Saving does not issue a billable model test.
4. Connect a phone or existing emulator as described below. Check device initialization and follow any Android permission instructions.
5. In Settings, copy the assistant connection prompt or use Manual setup. The assistant or user performs registration. If a restart/new session is required, send the separate verification prompt in that session and call `get_status`. Confirm actual device readiness before submitting a task; registration alone does not verify the connection.

After installation, launch ClickClick from the Windows desktop/Start Menu or macOS user Applications folder. Its tray/menu-bar icon provides Console access, backend start/stop, login startup and exit. Active tasks must be explicitly paused or cancelled before shutdown; a failed wait keeps the backend running. Closing the browser keeps it running. The backend uses the current user's Windows scheduled task or macOS LaunchAgent, listens on loopback and retains task data under `data/`. The PC/backend and phone must stay available for execution.

### Let a local assistant install it

Copy this prompt to an assistant with local command execution:

> Install and connect ClickClick on this PC (https://github.com/LordRosenberg/ClickClick). Follow the installation guide, detect Windows/macOS and CPU architecture, download the matching official Release installer and verify its published SHA-256. If unavailable, follow source deployment; do not treat a source ZIP as an installer. Check the installation exit status and backend identity. Open local settings so I can choose subscription login or an API; guide official authorization and let me enter API keys in the page, keeping them out of chat. Ask whether I want Wi-Fi, USB or an existing emulator, handle PC-side connection and guide phone debugging and permission prompts. Obtain the generated assistant connection prompt from Settings, read the local stdio configuration at its complete path, and register clickclick through your own official MCP command or configuration entry, preserving existing settings. If exporting with the installed ClickClick CLI, explicitly specify --client generic --transport stdio --output with a complete local file path, then read that file; do not use ClickClick register instead of your own registration mechanism. If this session can load MCP, call get_status to check model configuration and device initialization, and share the Console URL. If I must restart the client or open a new session, report "registered, verification pending", give exact steps, and provide this standalone instruction for the new session: "Call ClickClick get_status, check the connection, model configuration and phone initialization, and report readiness and unfinished steps." Registration and ADB connectivity alone are not successful verification. get_status makes no model requests or phone test tasks; run an actual phone test only when I explicitly request it.

The assistant remains subject to its client's own execution permissions. `--unattended` suppresses automatic browser opening; `--no-start` installs files without starting the backend.

## Connect Android

- **Android 11+ Wi-Fi:** connect PC and phone to the same reachable network. On the phone, enable developer options and Wireless debugging. Use the pairing-code screen's IP/port and code in ClickClick setup, then connect using the IP/port on the main Wireless debugging screen. Pairing and connection ports can differ; do not assume port 5555. Enablement and Android permissions require phone confirmation.
- **USB:** enable USB debugging, connect a data cable, unlock the phone and approve this computer. Resolve `unauthorized` or `offline` before proceeding. Some Windows phones require a manufacturer's USB driver.
- **Existing emulator:** start and unlock an Android 8+/API 26+ instance. If ADB does not discover it, connect its actual displayed ADB endpoint. ClickClick does not download large emulator images or recreate existing instances.

The backend initializes Collector/input-method assets on idle devices. Use `get_status` to distinguish ADB connectivity from complete initialization and follow its returned guidance. A successful pairing alone is not task readiness. Android 10 and older commonly require initial USB authorization for traditional Wi-Fi ADB; see the [full desktop guide (中文)](desktop-setup.zh-CN.md).

## Register MCP

Settings offers two paths:

- **Let your assistant add it (recommended):** the page creates `data/mcp-connection-stdio.json` and copies a prompt containing its complete local path. An assistant with local command/file access reads it and registers `clickclick` through its own official MCP mechanism. The page does not change assistant settings.
- **Manual setup:** choose your assistant to see fully populated commands for Codex/Claude Code, or merge instructions and configuration for Claude Desktop/Kimi Code/other clients. Commands are quoted for PowerShell on Windows or the terminal on macOS. Preserve existing settings; inspect same-name conflicts before replacement.

The default handoff uses stdio and contains no HTTP bearer credential. It starts a lightweight adapter to the task backend. HTTP MCP runs in a separate process, usually at `http://127.0.0.1:18081/mcp/`; first installation chooses another free port if needed and preserves it. Console stays on 18080. Export the actual URL and local Bearer credential with `export --client generic --transport http --output <complete-local-file-path>` and keep the file local. Desktop startup, shutdown and upgrades manage both services together.

After registration, reload the assistant's MCP configuration if supported in the current session. If the client requires restart or a new session, report **registered, verification pending**, follow its reload instructions, and send this in the new session:

> Call ClickClick get_status. Check the MCP connection, model configuration and phone initialization, and report readiness and unfinished steps. Do not issue model requests or phone test tasks.

`get_status` checks configuration and initialization without a billable model test. Run a real phone task only when the user requests it. Multiple assistants can share the backend; disconnecting one does not cancel tasks. A localhost URL is usable only by clients with access to this PC.

## Source setup

First configure a model and Android device using [deployment](deployment.md), then run from the repository root in the ClickClick Python environment:

```bash
python -m pip install -e ".[decode,mcp]"
python -m control_api.mcp --print-config
```

Copy the generated `command` and `args` into the client's local stdio MCP settings, reload and call `get_status`. Generated paths are absolute. The launcher validates an existing backend or starts one independently; logs are stored in the data directory's `mcp-backend.log`.

For source HTTP, set `CLICKCLICK_MCP_HTTP_ENABLED=true`, keep the API host on loopback, and run `python -m control_api.main` and `python -m control_api.mcp_http` separately with the same workspace and data configuration. The backend defaults to 8080; HTTP MCP defaults to 8081 (`CLICKCLICK_MCP_HTTP_PORT`), which must differ from the backend port. The local token is generated under `data/mcp-token`. `clickclick setup` opens the desktop setup page; same-origin browser access obtains its settings credential automatically and cross-site requests are rejected.

If a Windows client prevents the stdio launcher from detaching the backend, start the API in an independent terminal and use `python -m control_api.mcp --no-start` in the MCP configuration. See the [detailed source guide (中文)](local-mcp.zh-CN.md#源码安装和-stdio-连接) for workspace, data-directory and HTTPS settings.

## Task tools and workflow

| Tool | Use |
| --- | --- |
| `get_status` | Check backend identity, configuration, devices, initialization guidance and Console URLs |
| `start_task` | Submit a goal with a required `request_key` and optional `device_ids` |
| `get_task` | Query one or more tasks; `wait_seconds` supports bounded waits of 0–30 seconds |
| `list_tasks` | Browse/filter tasks with cursor pagination |
| `get_task_snapshot` | Read one task's latest saved screenshot and recorded time/step |
| `pause_task` | Request a pause at a safe execution boundary |
| `resume_task` | Resume the same cleanly paused task/checkpoint |
| `cancel_task` | Cancel one or more tasks without undoing prior phone actions |

1. Check readiness, clarify the goal/app/objects when needed, and submit the complete objective. ClickClick controls individual phone steps; this MCP does not expose arbitrary ADB or per-step clicking.
2. Reuse the same `request_key` and parameters when retrying an uncertain submission; a new execution needs a new key. If several devices are available, specify returned device IDs. Multi-device submission runs independent copies of the goal rather than one coordinated workflow.
3. Query progress/results after receiving task IDs. A returned ID confirms submission, not completion. Share `console_url` for saved screenshots, Timeline model inputs/outputs and Trace logs. Runtime success is not an independent business-outcome audit.
4. For pause, wait until `paused`; `pausing` means dispatched work and recording are still settling. A paused task retains its device reservation. Resume preserves task identity, plan, conversation and budgets while observing the phone again. Absolute deadlines continue during the pause.

Cleanly paused tasks retain their progress and can be explicitly resumed after a backend restart. Unexpected running-task crashes do not imply a safe checkpoint and are not automatically replayed. Polling timeouts and client disconnects do not cancel tasks; reconnect and use `list_tasks` to find them.

No companion skill is required: global MCP instructions, tool descriptions and schemas carry usage guidance. This service operates connected Android apps, not iOS or desktop applications. There is no built-in scheduler, proactive completion notification or cross-task habit learning; schedule/notification promises depend on the host assistant's actual features. Task control and storage stay local, while model APIs receive task/interface content needed for inference. Saved screenshots do not prove current phone state.

## Updates

Desktop setup and Console show available stable-release updates. Metadata checks run on startup and after the 12-hour interval; users can disable checks or check manually. Installation requires confirmation, and active work must finish, pause or cancel before upgrade. API configuration, MCP credentials, task history and user skills are retained. A source commit is not an update release.

See [desktop setup and upgrade details (中文)](desktop-setup.zh-CN.md#版本检查与升级).

### Model access and core settings

Console Settings includes role models, optional HTTP User-Agent, reasoning effort/summary, streaming, context/output limits and provider-specific thinking JSON. Blank existing keys or advanced bodies retain saved values; `{}` clears the body. Subscription login opens official authorization and provides a copyable device code; browser account login and code confirmation remain required. ClickClick automatically waits for approval and saves credentials locally.

The same page contains task and Skill learning budgets, local task statistics and report export settings. Saved settings apply to new Console and MCP tasks and survive restart. Workflow optimization requires separate confirmation of model costs and device operations. You can configure everything in the page without editing files.
