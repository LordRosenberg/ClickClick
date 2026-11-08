# Technical overview

[README](../README.md) · [中文](reliability-design.zh-CN.md) · [Architecture](architecture.md)

ClickClick lets multimodal models complete Android workflows through the screen. This overview separates **core capabilities, design challenges and tradeoffs, engineering support, and outcome evaluation**. Module interfaces and runtime flow belong in the [architecture guide](architecture.md).

## Why an agent harness is needed

A cross-app task involves more than consecutive taps. Transferring recipes requires remembering source fields, understanding the destination app, entering several records and judging which were saved. A changed page or a failed input can alter the next operation.

The agent harness supplies context, tools and feedback for sustained execution. **Models interpret the task and choose a path; runtime records, validates and executes their submissions.**

## Core capabilities

| Capability | Current support | Detailed design |
| --- | --- | --- |
| Long-task orchestration | Rolling refinement of the current stage, tentative future roadmap, Planner–Executor collaboration and on-demand Reviewer | [Orchestration](task-orchestration.md) |
| Context and memory | Role-specific context assembled from raw records, versioned notes, sourced summaries and retrieval | [Context design](memory-and-context.md) |
| Reliable observation and execution | Current screens, temporal sequences and native detail; target binding, input checks and action receipts | [Observation/execution](observation-execution-consistency.md) |
| Skill reuse | Skills selected by app, device, stage and role, with progressive disclosure | [Skill delivery](skill-delivery.md) |
| Skill self-improvement | Candidates extracted from task traces, independently reviewed, tested when needed and published after human confirmation | [Self-learning](skill-evolution.md) |
| Bidirectional HITL | Resume the original session after agent questions; accept user changes and replan during execution | [HITL design](hitl-design.md) |
| Pause and interruption recovery | Continue from safe checkpoints; take over eligible interrupted tasks and rebuild their context | [Pause/recovery](crash-recovery.md) |
| Observability and evaluation | Link role, model, tool and observation records, analyze latency and usage, and obtain independent scoring in benchmark environments | [Observability](observability.md) · [Evaluation](evaluation-design.md) |

The execution loop is **choose a stage → act and observe → update memory → continue or revise the plan**. HITL can update current intent, Session retains execution facts, and the learning entry point turns existing traces into skill candidates for later tasks.

## Key design challenges and tradeoffs

### Context compaction and factual fidelity

**Challenge:** History grows throughout a long task. Sending everything increases cost, while dropping content can lose cross-app fields, failed attempts or sources. Summaries can also introduce conclusions absent from the originals.

**Design:** Store raw records, versioned notes and structured summaries separately. Compact older history while retaining the two latest complete steps. Supply the current plan, notes and budgets separately in role context; bind summary items to sources so missing details can be retrieved.

**Tradeoff:** Compaction reduces recurring input, but retrieval and checking add overhead. Traceable sources do not guarantee correct interpretation; optional Jev checks also have cost and degradation boundaries, so they cannot guarantee error-free summaries. See [memory and context](memory-and-context.md).

### Asynchronous observation and action-target consistency

**Challenge:** Screenshots, accessibility trees and the actual interface represent different points in time. After navigation or content changes, an old index, coordinate or focus can refer to another object.

**Design:** Update pixels against request/action frame boundaries and use window generations with bounded recapture to check paired evidence. Observation IDs bind indexes and coordinate transforms. Supported clicks use retained Collector nodes with refresh and attribute checks; targeted input checks the field and focus before writing and readback.

**Tradeoff:** Bound recapture so dynamic pages do not wait indefinitely for stability; usable pixels can still be supplied when structure is missing. Asynchronous sources cannot form a strict atomic snapshot, and input readback alone does not prove that the app saved a record. See [observation and execution consistency](observation-execution-consistency.md).

### Concurrent intervention and in-flight execution

**Challenge:** A user change can arrive during a model request or device action. Editing plan text alone cannot prevent an obsolete reply from dispatching actions; rebuilding the whole task would discard valid context and cumulative consumption.

**Design:** Persist changes in an ordered control inbox, then let Planner retain the current stage or replan. Instruction revisions and execution generations revoke obsolete invocations' eligibility to commit. Questions instead save the unfinished role session, continue the same invocation after an answer and refresh observations before GUI actions.

**Tradeoff:** Record acceptance separately from plan application. Dispatched actions record actual results without automatically undoing external effects; valid stages and Executor history can remain in use. Paused tasks stay paused, completed-task changes create linked follow-ups, and budgets and history do not reset. See [bidirectional HITL design](hitl-design.md).

### Recovery between database commits and phone side effects

**Challenge:** A database transaction cannot cover an ADB operation. A crash can occur after the phone has acted but before its receipt is saved, making it unsafe either to assume no execution or simply retry.

**Design:** Persist in stages: pre-dispatch intent → Driver execution → raw receipt → post-action observation → step and complete state. The Driver journal deduplicates operation IDs; task ownership, execution generations and device-input fencing constrain old workers. Recovery manifests retain the original role, plan, notes, dialogue and loaded skills.

**Tradeoff:** These mechanisms provide deduplication and state consistency within their controlled scope, rather than exactly-once phone business effects. Without a reliable receipt, retain the unknown result and do not replay it; let the original role decide from a fresh observation. Normal pauses continue from safe checkpoints, while crash takeover must also isolate old input and retain cumulative budgets and the original deadline. See [interruption recovery](crash-recovery.md).

### Knowledge extraction from limited traces and validation cost

**Challenge:** A single success can depend on a particular state, and a failure can come from the environment. Turning a whole trace into a rule can overgeneralize, while rerunning every candidate in full inflates learning cost.

**Design:** Learner proposes candidates with explicit conditions. An independent Skill Reviewer checks root cause, transferability, conflicts, regression risk, conciseness and marginal value, selecting exploration or ordinary execution checks according to evidence gaps. Bind candidate content, base versions and validation records; users inspect results and changes before confirming publication per file.

**Tradeoff:** Record local validity, task-level utility and transfer separately. A failed task does not erase independently supported local knowledge, and local validity does not establish task utility; select validation when needed. Learning updates skill files rather than model weights, and ordinary task completion does not automatically start paid learning. See [skill self-improvement](skill-evolution.md).

## Engineering support

| Supporting module | Implementation and responsibility |
| --- | --- |
| Assistant integration and backend task ownership | MCP / Control API submit and control tasks; an independent backend owns execution, so client disconnection does not end a backend task. Compatible hosts can bridge replies; otherwise answers use Console. |
| Model gateway | Select models by role, adapt protocols and record requests, responses and provider-reported usage. |
| Session and storage | SQLite holds task state, versioned records and indexes; files hold dialogue, images, UI trees and recovery bodies; process memory holds active sessions and rebuildable working state. |
| Console and read-only preview | Inspect actual model inputs and outputs, tools, observations, receipts and events. Preview reuses existing frames and model events without creating a second device-capture or input path. |

## Observability and outcome evaluation

Execution records reconstruct a run and help locate problems; benchmark evaluators independently check final task state. Distinguish role calls, model rounds, tool calls and device actions, as well as task duration, model latency and token/cache usage. Missing usage remains unknown rather than zero.

Runtime acceptance of a completion decision still needs business evidence or external scoring to assess the outcome. Read-only preview is independent of scheduling, and preview failures do not change execution. See the [evaluation report](androidworld-results-20260921.md) for conditions and results, and the [Console guide](observability.md) for traces and metrics.

## Continue reading

The core table links mechanisms and configuration directly. Cross-module relationships and supporting designs are indexed in [architecture](architecture.md#module-design-documents).

- Rationale and historical controls: [design-decision index](design-decisions.md).
- User workflows: [MCP setup](local-mcp.md), [HITL usage](hitl.md), [evaluation/reproduction](evaluation.md).
