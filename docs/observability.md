# Agent observability and outcome analysis

[README](../README.md) · [中文](observability.zh-CN.md) · [Deployment](deployment.md) · [Task examples](task-examples.md)

Observability is part of every ClickClick task, including custom app workflows. It connects what a model received and returned to the tools, device operations and observations that followed. A benchmark such as AndroidWorld supplies an external correctness check; it is not the source of this instrumentation.

## From execution to Console

1. The runtime and model gateway produce role-call, model-round, tool, action and observation records. Stable references associate them with a task and the relevant call or action position.
2. `TraceWriter`, the task store and shared storage retain events, versioned records and artifact references. Large request/response bodies and images can be loaded separately from compact timeline views.
3. `ObservabilityQueries` builds task summaries and call timelines. SSE delivers stored events and subsequent live events; artifact endpoints serve referenced content.
4. Console displays canonical role invocations and their model/tool rounds. Selecting a call selects its decision and observation instead of borrowing another role's content at the same device-action position.

Source: [trace writer](../agent/traces.py), [task store](../agent/revisable/store.py), [queries and metrics](../control_api/services.py), [API and SSE](../control_api/main.py), [call presentation](../web/src/components/AgentCallsBlock.tsx), [selected-call inspector](../web/src/components/StepInspector.tsx).

## Inspect a run

Open a task and select **Timeline**. Pick a role invocation and expand its input, output and tool records. Check the supplied instruction, context and returned decision before attributing a failure to the model. Only provider-returned output or reasoning summaries are available; hidden internal reasoning is not reconstructed.

Inspect **active skills** to verify delivered IDs and versions. Open the observation and select a conversation image when comparing what a particular call saw. The mirror's **Frame** mode displays selected historical visual evidence; **Live** displays the device now. They answer different questions and should not be compared as if captured simultaneously.

Use **Trace 流** for errors, cancellation and diagnostic events. Not every capture or compression field has a dedicated UI chart: deeper inspection uses the associated artifact or an offline query. A task marked **运行时完成** means the runtime accepted a completion decision. It does not independently establish that every business field is correct.

## Metrics and their meaning

| Measure | Interpretation |
| --- | --- |
| Task execution elapsed | Runtime task duration in the Console. A benchmark worker duration can include preflight/cleanup and has a different boundary. |
| Role-call elapsed | The full selected invocation, including work beyond model inference. |
| LLM elapsed | Recorded model-round latency, aggregated where available. It is not the complete task wall time. |
| Input / cached / output | Normalized provider usage. Cache information can be absent; an unknown value is not zero usage. |
| Role invocations / model rounds / tool calls / device actions | Different units. One role invocation may contain several model rounds and tools; a compound input may contain multiple equivalent device actions. |
| Compaction, images and text estimates | Useful for finding context overhead in records/offline analysis. Estimates are not necessarily the provider's billed tokens. |
| Capture source and fallback | Identify scrcpy versus fallback evidence and related failures. Persisted observations do not necessarily enumerate every internal capture. |

Provider call counts and input volume help compare resource demand when billing is unavailable. Monetary comparisons additionally require actual usage, pricing and a consistent treatment of cache and retries. Unknown fields remain unavailable rather than being invented.

## Diagnose a failure or expensive run

| Symptom | Investigation |
| --- | --- |
| Wrong field or value | Compare the source instruction/note, submitted action, focus/readback result and saved app state. Correct readback can still contain a value the model copied incorrectly. |
| Repeated tap or scroll | Compare observation IDs, targets and resulting content. The same coordinates can act on different records or reveal new rows. |
| Repeated replan/review | Inspect stage changes and evidence available to each call. Determine whether a new fact appeared or the same question was repeated. |
| Expensive history | Inspect actual request sections, retrieved history, image sends and compaction events. A shorter summary does not by itself imply fewer total calls or lower cost. |
| UI and action mismatch | Check frame/tree timing and source metadata, target binding and device receipts before interpreting it as a semantic planning failure. |

The general method is to separate model choice, tool validation, actual dispatch, observed effect and saved task result. This supports diagnosis; it does not automatically classify every error or prove causal explanations.

For Collector 0.4.4, compare `window_generation`, `window_quiet_ms`, `window_fence`,
`content_changed_during_capture` and `observation_capture_attempt_count`. Health
reports `filtered_window_events`; it counts excluded metadata events, not dropped
trees. `collector_elapsed_ms` describes the final Collector exchange, while
`provider_attempts` and `stage_timings` retain earlier attempts. Do not report only
the accepted attempt as total latency. `observation_capture_failed` and an action
receipt's `observation_failure` preserve deadline/cancellation details.

## Programmatic access

While the API is running, replace `TASK_ID` in these read-only requests:

```bash
curl http://127.0.0.1:8080/api/tasks/TASK_ID/timeline
curl -N http://127.0.0.1:8080/api/tasks/TASK_ID/stream
```

Use `curl.exe` on Windows PowerShell. Artifact references returned in the records are available through `/api/artifacts/{ref}`. Task records can contain instructions, app content and screenshots; select or redact artifacts before sharing them.

For application correctness, inspect the resulting app data or run an external validator. [AndroidWorld evaluation](evaluation.md) is one such integration. Its frozen fixtures and scoring are separate from ordinary Console tasks.
