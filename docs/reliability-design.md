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

## Continue reading

| Question | Guide |
| --- | --- |
| How do modules connect and roles transition? | [System architecture](architecture.md) |
| How are memory, context and skills organized? | [The architecture's memory and skills section](architecture.md#context-memory-and-skills), with links to subsystem designs |
| Why were particular approaches chosen? | [Design decisions](design-decisions.md) |
| How can an execution be inspected? | [Console guide](observability.md) |
