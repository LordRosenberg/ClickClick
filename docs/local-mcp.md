# Use ClickClick from a PC AI assistant

[中文](local-mcp.zh-CN.md)

ClickClick exposes nine task tools over local Streamable HTTP and stdio MCP. An assistant submits a complete Android goal; an independent ClickClick backend executes it with its own configured API or subscription models, prompts, harness and task session. The host assistant's subscription, system prompt and chat context are not used for internal inference.

## Adjusting work and extending budgets

Use `steer_task(task_id, instruction, request_key)` to append a user adjustment. Reuse the same key and payload after a lost response. Acceptance means durable receipt, not application: `get_task.adjustment` exposes accepted/applied status and Planner feedback. Planner retains a valid stage or revises remaining work. Obsolete undispatched actions are fenced; dispatched transactions finish recording their real effects. Paused tasks stay paused. Completed tasks create linked follow-ups without rewriting the parent; failed/cancelled/orphan work is not implicitly restarted.

The tool remains discoverable with HITL disabled, returning `status=disabled`, `code=hitl_disabled` and Console settings guidance. Enable HITL and create a new task: each task retains its creation-time configuration. No user-adjustment inputs, model tools or preemption waiter are added to ordinary tasks.

Ordinary defaults are 200 Executor decisions and 1000 provider requests, configurable in Console. Task-wide exhaustion remains `failed` with its failure reason, `stop_reason=budget_exhausted` and `budget_kind`, preserving usage and a validated checkpoint. Ordinary failure and model `inconclusive` retain diagnostic state but cannot resume. Original shared budget prompts remain unchanged. `resume_task(task_ids, request_key, budget)` accepts positive cumulative increments such as `{"model_calls":1000}`. A key is required for increments; identical retries extend once. `seconds` explicitly adds to the original deadline; ordinary resume/adjustment never resets usage or extends time. Console asks for an increment on budget resume. This budget behavior also applies with HITL off. Failed budget tasks release the phone; continuation checks device availability and completed cleanup before reserving it again. `can_resume` describes checkpoint eligibility, not guaranteed immediate admission. `execution_attempts` exposes immutable outcome/limit/consumption summaries; continuation creates the next attempt under the same task without clearing counters or changing prior failure records. Replaying an already applied renewal key never starts another attempt, even after the next attempt ends. Explicit evaluation protocols retain their own limits and end the original episode without automatic continuation or replacement of its score.

Material choices wait through the existing HITL surface. Without a live subscription, inspect Console or query status; acceptance does not promise proactive assistant delivery. Superseding inference does not guarantee provider cancellation or zero cost. Arbitrary crashes are not replayable checkpoints.

## Desktop setup

1. Check the [official Releases](https://github.com/LordRosenberg/ClickClick/releases) for an installer matching the operating system and CPU architecture. If there is no matching installer, use the source setup below. GitHub's Source code ZIP is for source deployment. macOS test builds may trigger system source checks; verify the download source and follow system prompts.
2. For assistant-led first installation, pass --origin assistant when supported; preserve existing user choices. Run the installer. It bundles private Python, installed dependencies, Console, ADB, Collector and ADBKeyboard, and opens a local browser setup page. You do not need to install Python, Node, Git or pip separately.
3. In Console Settings, choose Codex / ChatGPT subscription login or configure an API. For subscription login, complete official browser authorization with the provided device code. For an API, enter the model ID, base URL and key, with optional User-Agent and thinking/reasoning parameters. Keep keys out of chat and command arguments. Save when all tasks (including paused tasks) and personal optimization have ended; new tasks immediately use the saved settings. Saving does not issue a billable model test.
4. Connect a phone or existing emulator as described below. Check device initialization and follow any Android permission instructions.
5. In Settings, copy the assistant connection prompt or use Manual setup. The assistant or user performs registration. If a restart/new session is required, send the separate verification prompt in that session and call `get_status`. Confirm actual device readiness before submitting a task; registration alone does not verify the connection.

After installation, launch ClickClick from the Windows desktop/Start Menu or macOS user Applications folder. Its tray/menu-bar icon provides Console access, backend start/stop, login startup and exit. Active tasks must be explicitly paused or cancelled before shutdown; a failed wait keeps the backend running. Closing the browser keeps it running. The backend uses the current user's Windows scheduled task or macOS LaunchAgent, listens on loopback and retains task data under `data/`. The PC/backend and phone must stay available for execution.

### Let a local assistant install it

Copy this prompt to an assistant with local command execution:

> Install and connect ClickClick on this PC (https://github.com/LordRosenberg/ClickClick). Follow the installation guide, detect Windows/macOS and CPU architecture, download the matching official Release installer and verify its published SHA-256. If unavailable, follow source deployment; do not treat a source ZIP as an installer. For assistant-led first installation, explicitly use --origin assistant when the installer supports it; for source or older installations without that option, enable and save HITL during first setup while preserving any existing user choice. Check the installation exit status and backend identity. Open local settings so I can choose subscription login or an API; guide official authorization and let me enter API keys in the page, keeping them out of chat. Ask whether I want Wi-Fi, USB or an existing emulator, handle PC-side connection and guide phone debugging and permission prompts. Obtain the generated assistant connection prompt from Settings, read the local stdio configuration at its complete path, and register clickclick through your own official MCP command or configuration entry, preserving existing settings. If exporting with the installed ClickClick CLI, explicitly specify --client generic --transport stdio --output with a complete local file path, then read that file; do not use ClickClick register instead of your own registration mechanism. If this session can load MCP, call get_status to check model configuration and device initialization, and share the Console URL. If I must restart the client or open a new session, report "registered, verification pending", give exact steps, and provide this standalone instruction for the new session: "Call ClickClick get_status, check the connection, model configuration and phone initialization, and report readiness and unfinished steps." After verification, briefly introduce ClickClick and basic usage, explain the current HITL switch and its Console configuration location with new-task-only scope, and prefer your in-app browser for future supported task screen previews, falling back to task details when unsupported. Registration and ADB connectivity alone are not successful verification. get_status makes no model requests or phone test tasks; run an actual phone test only when I explicitly request it.

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
| `resume_task` | Resume the same clean paused or failed-budget checkpoint; retain prior execution attempts |
| `steer_task` | Append requirements to a HITL-enabled task and report acceptance/application |
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


## Read-only preview of a local task

Task projections may include `preview_url` and `preview_support`. `supported` means that local Android mode supports the page, not that pixels are currently available. Queued, paused and finished tasks can retain links, but only an actively running task can provide its existing pixels. Remote Driver hubs, fixtures and other platforms return a null link and an `unsupported` reason. Preview enrichment errors return null and `unavailable/preview_metadata_error` without changing the task result.

An assistant with an in-app browser can open the returned `/tasks/<task_id>/preview` URL alongside the conversation. This is a separate webpage, requiring a loopback backend and the returned same-origin address. It is not native sidebar registration or a guarantee of automatic opening. It requests no setup/MCP token and provides no touch, keyboard, gesture or task controls.

The page has equal left/right columns: the phone screen and an append-only model-output log. UI labels default to English; EN/中文 switches presentation and remembers the preview preference without changing other Console pages or translating model output. Logs reuse the existing task event stream and saved responses: streaming snapshots append new text without duplicating earlier content, while completed responses remain identified as saved output. Scrolling up pauses automatic following. Only recent output is retained in this view (200 blocks / 262144 characters); full records remain in task details. The language controls and log scrolling do not operate the phone or task. Hiding/closing the page also closes its log subscription and cancels pending output reads.

The page checks state at most twice per second and fetches PNG only when the published frame cursor advances. This is not a video frame rate or a guarantee that every phone change appears within 500ms. Preview reads only pixels already published by the task decoder: it starts no capture, screenshot, UI-tree request or additional video subscription, and never flushes pending pixels. A final static frame can remain pending until the normal Agent path publishes it. Frame age describes the published image; an image retained after invalidation is labelled as last received, not current model input or evidence of task success. Hidden pages stop polling; closing releases browser image resources. Errors and throttling degrade only preview availability.

`get_task_snapshot` still reads saved historical screenshots. Do not poll MCP per frame or infer task failure from preview failure. Desktop 0.3.1 and later include this feature; upgrade older installations first.

## Questions and confirmations (HITL)

Enable optional HITL in Console settings for new tasks. Necessary questions pause the task and matched replies resume the original call. Standard MCP clients answer in Console; assistant hosts with a session-specific reply bridge can receive proactive questions in their own conversation, with Console fallback after disconnection. Generic elicitation support alone does not establish background delivery. See [HITL usage and integration](hitl.md).

## First-connection introduction and automatic task pages

After installation or first MCP connection verification, the assistant should briefly introduce ClickClick and basic usage in the conversation. get_status.assistant_onboarding supplies the summary, natural-language delegation and controls, actual HITL value, Console Settings > Run and Learning location, saving and new-task-only scope. Ordinary polling does not repeat onboarding, change HITL or start a phone test.

Assistant-led first installation explicitly uses --origin assistant and defaults HITL on; other installations default off, including unattended installation without that marker. Saved choices take precedence, and upgrades or MCP connections alone never overwrite them. If an older installer lacks this option, explicitly enable and save HITL during first setup instead of assuming the flag exists.

After successful start_task, assistant_ui guides the host to open each supported preview_url task screen preview in its own in-app browser, falling back to console_url task details when the preview link is missing or unsupported. Each page entry retains console_url. Reuse pages by task_id; queued tasks may wait for pixels in the preview without opening a second details tab. Do not hardcode a simulator viewer port or create extra capture/control sessions. Polling and request-key retries must not duplicate tabs. A linked follow-up selects its own supported preview or details page. Without browser capability, or if opening fails, share clickable links and keep tracking; phone execution is independent. These are generic host instructions and metadata, not a product-specific browser integration or a server-side system-browser launch.
