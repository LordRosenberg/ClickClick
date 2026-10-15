# Human-in-the-loop questions and confirmations

[README](../README.md) · [中文与宿主协议](hitl.zh-CN.md) · [Architecture](architecture.md)

Enable **Settings → Runtime and learning → Ask me when needed (HITL)** in Console. It is off by default; saved defaults affect new tasks, while each task retains its frozen setting and role/tool policy.

Planner resolves material uncertainty about intent before dependent planning. Executor asks when actual execution reveals a necessary unresolved choice or authorization gap. Both first use the request, accepted answers and available evidence. Explicitly authorized actions do not need the same permission again. Reviewer cannot ask. Question timing is model-controlled; this release adds no separate semantic judge that intercepts missed questions. Never collect passwords, verification codes or payment credentials through questions.

`ask_user` is an intermediate tool. The broker persists the pending question, original invocation and tool-call IDs, then yields the worker. No model calls or device actions occur while waiting. A matched reply resumes that call once, with the original budget/deadline and a fresh observation before further GUI actions. Replies can clarify intent but cannot prove a device effect occurred. Conflicting or stale replies are rejected.

Managed tasks can ask one to three related questions together, or request one explicit confirmation. Console offers choices and free text; selections are not submitted automatically. Decline, inability to answer and cancellation have distinct outcomes. The default limit is eight questions, 1,800 seconds per wait and 4,096 characters per answer, bounded by the task's earlier deadline.

Console tasks answer in Console. A foreground CLI or assistant host with a live session-specific reply bridge can answer at its origin. A disconnected or unsupported origin falls back to Console with the same question and deadline. A desktop notification opens the task page; it does not create a second answer form. Generic MCP elicitation support alone does not prove that a client can wake a background conversation. The detailed SSE binding/presentation/reply protocol is documented in the [host integration guide](hitl.zh-CN.md#助手联动与主动推送).

```powershell
python -m agent.main --hitl "Your task"
python -m agent.main --hitl --answers console "Your task"
```

Each role has one shared base prompt and a concentrated enabled/disabled decision section. `agent/interaction_policy.py` selects the matching tool description, schema and recovery hints. Human tools are absent when disabled; legacy task defaults remain disabled. Delegated MobileWorld evaluation uses one clarification question per call and the official user simulator, independently of the production reply surface.

The full MobileWorld User-Int. run passed **34/44 (77.27%)**, with GPT-5.6-Sol (high), Jev off, continuous recordings and 56 matched question/answer exchanges. See [methodology and limits](mobileworld-hitl-results-20261015.md) and [all 44 public trajectories](https://lordrosenberg.github.io/ClickClick/mobileworld/?category=hitl#explore).
