# Human-in-the-loop questions, confirmations and task changes

[README](../README.md) · [中文](hitl.zh-CN.md) · [Mechanisms and host protocol](hitl-design.md)

Enable **Settings → Runtime and learning → Ask me when needed (HITL)** in Console. Assistant-guided first installations default on, other installations off; saved choices take precedence. Settings affect new tasks. Existing tasks retain their creation-time setting, and MCP connection does not change it.

## Answering questions

Planner/Executor ask for necessary information, material choices or missing authorization after using the task, previous answers and readable evidence. Explicit existing authorization is not requested again. Use the app's secure flow for passwords, verification codes and payment credentials. Models choose when to ask; missed questions remain possible.

Waiting performs no inference or device actions while the original deadline remains active. Console keeps a pending-answer entry. Choose an option or enter text; selection never submits automatically. Confirmation requires an explicit allow/decline. Dismissal merely closes the dialog; inability to answer differs from cancellation. One clarification can contain 1–3 questions. A complete reply continues the original role session and refreshes observations before actions.

A valid assistant reply bridge/foreground CLI can answer at the origin; otherwise use Console. Disconnect or missing timely presentation transfers to Console. A shown question with a live connection does not transfer because the user thinks for more than 30 seconds. Console can explicitly take over. Tray notifications open the task page rather than another reply form. Defaults are 8 questions, 1800 seconds per wait and 4096 characters per answer, bounded by an earlier task deadline.

## CLI

```powershell
python -m agent.main --hitl "Your task"
python -m agent.main --hitl --answers console "Your task"
```

HITL CLI uses the same Control API, task database and reply broker. Interactive terminals accept numbered choices/free text; confirmations require explicit yes/no. `?` means unable to answer, `cancel` cancels, and empty input never agrees. Noninteractive input, EOF/exit or explicit Console mode transfers to Console with a task URL. Disabled HITL retains the original standalone CLI path.

## Change a task while it runs

Send a change in the assistant conversation; `steer_task` appends it to the original task without first pausing or waiting for a question.

- Running tasks distinguish reception from plan application. Planner retains a valid stage or revises affected work; obsolete responses cannot dispatch further actions.
- Dispatched actions first retain actual results. Sends, bookings and similar effects are not automatically reversed.
- Paused tasks stay paused; pending questions and continuation eligibility are rechecked.
- Completed tasks create linked follow-ups while retaining completion history. Enable HITL for new tasks when the existing task has it disabled.

Original requirements and ordered changes remain inspectable. Valid constraints, authorization and usage survive. See [MCP setup](local-mcp.md) and [HITL ordering](hitl-design.md#ordering-user-changes-and-in-flight-actions).

## Screen preview and progress

Open the task preview after delegation to view recent phone pixels beside returned model text/reasoning summaries. Tool arguments appear after the round completes. Preview and details link to each other. Read-only preview reuses existing frames from local Android tasks and labels paused, finished or missing-frame states without affecting execution. See [Console guide](observability.md#task-screen-preview).

## Continue after budget exhaustion

Ordinary cumulative defaults are 200 Executor decisions and 1000 model requests. Exhaustion remains failed; an eligible safe budget checkpoint can continue after an explicit extension, preserving failure history and consumption. Changes do not reset budgets or extend deadlines. Evaluation never automatically continues or overwrites scores. See [recovery paths](crash-recovery.md#clean-pause-budget-continuation-and-crash-takeover).

## Assistant bridging and push delivery

Private bindings, SSE, presentation acknowledgements and reply APIs are specified once in the [HITL host protocol](hitl-design.md#host-protocol). Generic MCP connection does not guarantee background question presentation. Clients without a bridge use Console; real-host compatibility requires separate validation.

## Prompts, schemas and validation

[HITL design](hitl-design.md) owns role policy, managed/delegated schemas, idempotent replies and call continuation. Protocol tests and appropriate model question timing are evaluated separately.

## Interaction evaluation

Historical results, dialogue traces and conditions: [MobileWorld interaction report](mobileworld-hitl-results-20261015.md). Methods: [evaluation design](evaluation-design.md).
