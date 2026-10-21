# Human-in-the-loop questions, confirmations and task changes

[README](../README.md) · [中文与宿主协议](hitl.zh-CN.md) · [Architecture](architecture.md)

Enable **Settings → Runtime and learning → Ask me when needed (HITL)** in Console. Assistant-guided first installations default it on; other installations default it off. Saved choices take precedence and MCP connection alone does not change them. Saved defaults affect new tasks, while each task retains its frozen setting and role/tool policy.

Planner resolves material uncertainty about intent before dependent planning. Executor asks when actual execution reveals a necessary unresolved choice or authorization gap. Both first use the request, accepted answers and available evidence. Explicitly authorized actions do not need the same permission again. Reviewer cannot ask. Question timing is model-controlled; this release adds no separate semantic judge that intercepts missed questions. Never collect passwords, verification codes or payment credentials through questions.

`ask_user` is an intermediate tool. The broker persists the pending question, original invocation and tool-call IDs, then yields the worker. No model calls or device actions occur while waiting. A matched reply resumes that call once, with the original budget/deadline and a fresh observation before further GUI actions. Replies can clarify intent but cannot prove a device effect occurred. Conflicting or stale replies are rejected.

Managed tasks can ask one to three related questions together, or request one explicit confirmation. Console offers choices and free text; selections are not submitted automatically. Decline, inability to answer and cancellation have distinct outcomes. The default limit is eight questions, 1,800 seconds per wait and 4,096 characters per answer, bounded by the task's earlier deadline.

Console tasks answer in Console. A foreground CLI or assistant host with a live session-specific reply bridge can answer at its origin. A disconnected or unsupported origin falls back to Console with the same question and deadline. A desktop notification opens the task page; it does not create a second answer form. Generic MCP elicitation support alone does not prove that a client can wake a background conversation. The detailed SSE binding/presentation/reply protocol is documented in the [host integration guide](hitl.zh-CN.md#助手联动与主动推送).

## Change a task while it runs

Tell your assistant, for example: “Change the previous task: skip brightness and check whether the stopwatch is running. Do not start or reset it.” The assistant uses `steer_task` to append your request to the same task, without first pausing it or waiting for a question.

- Running tasks acknowledge reception promptly; Planner keeps the current stage or revises affected work. A change can preempt model waiting, and obsolete replies cannot dispatch actions or commit completion. Accepted and applied are distinct progress states.
- Already dispatched actions finish recording their actual result. Completed sends or bookings are not automatically undone; the next step follows actual facts and asks for material choices when needed.
- Paused tasks stay paused. Changes during a question invalidate obsolete continuations while preserving unresolved question context for replanning.
- Completed tasks create linked follow-ups without rewriting history. A task created with HITL disabled returns settings guidance; enabling the switch affects new tasks only.

The original request and ordered changes remain inspectable. Earlier constraints and permissions remain effective where they do not conflict with the change. See [MCP integration](local-mcp.md).

## Screen preview and progress

An assistant with an in-app browser can open the task's local preview after delegation: recent phone pixels on the left, returned model text, reasoning summaries and completed tool-call output on the right. Tool arguments appear after the round completes. Task details offers **Screen preview**, and the preview offers **Task details** for return navigation.

Preview is read-only, reuses the active task's published frames and does not run a host-driven screenshot or input loop. It supports local Android tasks and explains missing pixels, paused/finished tasks and unsupported modes. Browser availability does not affect independent phone execution.

## Continue after budget exhaustion

Ordinary task defaults are 200 Executor decisions and 1000 model requests, configurable in Console. Exhaustion remains a failed attempt; a safe budget checkpoint can continue after an explicit cumulative extension, preserving task identity, earlier failures, actions and total consumption. A task adjustment does not reset counters or extend deadlines. Ordinary failures are not resumable through this feature, and benchmark episodes never automatically continue or replace an earlier score.

```powershell
python -m agent.main --hitl "Your task"
python -m agent.main --hitl --answers console "Your task"
```

Each role has one shared base prompt and a concentrated enabled/disabled decision section. `agent/interaction_policy.py` selects the matching tool description, schema and recovery hints. Human tools are absent when disabled; legacy task defaults remain disabled. Delegated MobileWorld evaluation uses one clarification question per call and the official user simulator, independently of the production reply surface.

The full MobileWorld User-Int. run passed **34/44 (77.27%)**, with GPT-5.6-Sol (high), Jev off, continuous recordings and 56 matched question/answer exchanges. See [methodology and limits](mobileworld-hitl-results-20261015.md) and [all 44 public trajectories](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore).
