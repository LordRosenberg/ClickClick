# Technical overview

[README](../README.md) · [中文](reliability-design.zh-CN.md) · [Architecture](architecture.md)

ClickClick lets multimodal models complete Android workflows through the screen. This overview explains **the main task challenges, core design choices and their purpose**. Module interfaces and runtime flow belong in the [architecture guide](architecture.md).

## Why an agent harness is needed

A cross-app task involves more than consecutive taps. Transferring recipes requires remembering source fields, understanding the destination app, entering several records and judging which were saved. A changed page or a failed input can alter the next operation.

The agent harness supplies context, tools and feedback for sustained execution. **Models interpret the task and choose a path; runtime records, validates and executes their submissions.**

## Four core mechanisms

| Task challenge | Design | Purpose |
| --- | --- | --- |
| Future screens cannot all be predicted | Revisable stage planning | Specify the current outcome and adapt the remaining route to new observations |
| Details can be lost across pages and apps | Independent notes, structured summaries and source retrieval | Preserve important data, bound context and retrieve missing detail |
| Apps have non-obvious, reusable operating conventions | App- and device-scoped skills | Reuse established knowledge and reduce repeated exploration |
| Dispatch does not establish an application outcome | Current-observation binding and execution feedback | Check targets and input, then bring actual results into the next decision |

Together they support a loop: **choose a stage → act and observe → update memory → continue or revise the plan**. Planner or Executor can request Reviewer when independent judgment is needed.

## How they work together across apps

| Stage | Model judgment | System support |
| --- | --- | --- |
| Read the source | Which fields will be needed later? | Notes hold data; source records support retrieval |
| Open the destination | How should this page be operated? | Current observation and applicable skills |
| Enter each record | Did the input take effect, and which records are complete? | Action receipts, input readback and task progress |
| Deliver the result | Does the outcome satisfy the original request? | Post-save observations and independent review when needed |

## Inspectable outcomes and practical limits

Console connects model inputs, decisions, actions and observations so outcomes and failures can be inspected. Benchmark evaluators independently check final task state; the [evaluation report](androidworld-results-20260921.md) describes the conditions.

Traceable sources do not guarantee correct interpretation, successful input does not establish that a record was saved, and skills cannot cover every app version. The system preserves evidence and feedback for subsequent judgment.

## Recovery after an unexpected interruption

After an unexpected backend exit, eligible new tasks can continue with the same task ID, plan, notes, history and cumulative budget. The runtime separately persists dispatch intent, Driver receipts and complete post-step state, then uses task ownership and device-input fencing to prevent an old worker from interfering. **An operation without a reliable receipt remains unknown and is never automatically replayed; the original role receives a fresh observation and decides what to do next.**

A normal pause saves a checkpoint at a safe boundary; a crash can interrupt a physical action, requiring old-input isolation and receipt handling first. Recovery does not add budget, enable HITL or reopen terminal tasks. Console shows recovery phases and reasons, while assistants reuse `resume_task`. See [architecture](architecture.md#recovery-after-an-unexpected-interruption) for the state machine, configuration and guarantee boundaries.

## Continue reading

| Question | Guide |
| --- | --- |
| How do modules connect and roles transition? | [System architecture](architecture.md) |
| How are memory, context and skills organized? | [The architecture's memory and skills section](architecture.md#context-memory-and-skills), with links to subsystem designs |
| How can failures and detours become reusable guidance? | [Skill self-improvement](skill-evolution.md) |
| Why were particular approaches chosen? | [Design decisions](design-decisions.md) |
| How can an execution be inspected? | [Console guide](observability.md) |

## HITL questions and active intervention

HITL is frozen at task creation: assistant-guided first installations default on, other installations off, and saved choices take precedence. Models use `ask_user` for necessary questions; users can independently send task changes through `steer_task`. Original intent and ordered updates remain recorded, Planner interprets current intent, and disabled tasks retain the original model input and scheduling path.

**Separate plan revision from execution eligibility.** Changes are persisted before reception is acknowledged. Undispatched obsolete invocations lose eligibility; instruction revisions and execution generations prevent late replies from committing plans, actions or completion. Already dispatched actions record actual results and are not automatically reversed. Planner can retain a valid stage and Executor history. Material choices use existing question handling, paused tasks stay paused, and completed-task changes create linked follow-ups.

**Retain facts and consumption.** Changes do not clear receipts, budgets or earlier outcomes. Exhaustion stays failed, with explicit budget extension from a safe checkpoint and immutable earlier attempts; benchmark episodes and recorded scores are not rewritten. Models own semantic choices while the runtime checks identity, invocation eligibility and dispatch order.

## Generic assistant screen preview

The task preview reuses local Android frames and existing model events instead of creating another device-capture or input path. Hosts select their own browser from returned preview/details links and reuse pages by task ID. Read-only viewing is independent of execution; disconnects, slow readers and missing frames do not change task outcomes. The UI labels frame age and retained images, while task details keeps full outputs and saved evidence. See [usage](hitl.md).
