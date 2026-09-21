# ClickClick: an agent harness for complex mobile tasks

[README](../README.md) · [中文](reliability-design.zh-CN.md) · [Architecture](architecture.md) · [Design decisions](design-decisions.md)

## Abstract

ClickClick connects general-purpose multimodal models to Android through revisable stage planning, persistent task memory, application skills and device feedback. Models make semantic decisions; the harness manages context, skill scope, action binding and execution records. The system passes 115 of 116 AndroidWorld task instances, a success rate of 99.14%. The [evaluation report](androidworld-results-20260921.md) specifies the experimental conditions.

## 1. Problem and design objectives

Mobile workflows must preserve goals, data and target identity across changing interfaces. Transferring recipes can involve reading a long note, switching apps, filling several forms and inspecting saved records. Correct individual taps are insufficient if source fields disappear from context, observations become stale or records are processed twice.

ClickClick addresses four connected requirements: revise plans from new evidence, preserve retrievable information across stages, apply relevant app knowledge, and feed device results into subsequent decisions. Observability connects each result to the model input and device operation that produced it.

## 2. Revisable stage planning

Planner specifies one current stage as an outcome and maintains a tentative roadmap. Executor chooses the local interaction path. It can request replanning when observations contradict the stage, or return to Planner when the stage is complete.

This division keeps future plans provisional while allowing local adaptation. The original instruction remains authoritative for create/update intent, source fields, counts, ordering and preservation requirements. Intermediate plans do not replace it.

The default loop uses Planner and Executor. Either can request Reviewer for independent judgment; mandatory final review is also configurable. The runtime enforces transitions and limits while models interpret task meaning. See the [role protocol](../shared/revisable.py) and [orchestrator](../agent/revisable/orchestrator.py).

## 3. Persistent memory and task continuity

Session stores observations, action events, stages, dialogue and versioned notes outside the active model context. The harness selects relevant material and provides source-reading tools. Specific missing facts can be retrieved from text or historical screenshots without revisiting the app.

Compaction operates on complete execution steps and retains recent steps. Original evidence and unresolved questions remain separately available. Summaries reduce active context; source records provide a path back to detail. Historical images support fact checking, while the current observation supplies action targets.

Long-list work also requires progress state: processed records, pending rows and remaining scope. Shared traversal guidance adjusts scrolling to visible anchors. Ambiguous or identical-looking rows use a cursor and smaller forward reveals; clearly anchored lists permit larger moves. Stopping conditions distinguish target lookup, group processing and full collection coverage. This is a task-execution strategy within the memory and skill layers.

Implementation: [records](../agent/revisable/store.py), [retrieval](../agent/revisable/recall.py), [compaction](../agent/revisable/dialogue.py), [list traversal](../skills/generic/adaptive-list-traversal/SKILL.md).

## 4. Application skills and bounded compound actions

App cores contain stable conventions, workflows describe specific procedures, and generic skills provide methods shared across apps. Planner selects stage guidance. Device profiles, target and foreground app identity, and role projection determine delivery. Skill IDs, versions and content hashes make that delivery inspectable.

Skills may also declare supported compound actions. An inspect-and-return operation, for example, must retain the detail screen as evidence and re-establish the returned screen as the next action basis. The harness executes a closed recipe, records each subaction and observation, and exposes the latest actionable state. Stage selection, active skill and foreground identity constrain availability.

This connects operating knowledge to execution without exposing arbitrary scripts or model-authored action sequences. One submitted action can contain multiple physical inputs; both levels are recorded. See the [skills guide](../skills/README.md) for authoring, role sections and reviewed candidate updates.

## 5. Observation-bound device execution

Observations combine scrcpy pixels with Accessibility Collector structure and events; ADB supplies fallback pixels. Acquisition uses request boundaries, window-state checks and bounded resampling. Role handoffs refresh the observation before execution resumes.

Executor actions reference the current observation ID. For supported local native clicks, the harness retains the observed node, refreshes and checks its identity, and calls Android `ACTION_CLICK` once. Invalid or uncertain native actions return feedback without silently replaying old coordinates. Coordinate actions remain available for visual controls and unsupported paths.

Supported text entry checks a structurally identifiable field, focus, replacement and readback, reporting the operations that actually occurred. Successful input and a saved application result are separate judgments. See [action/observation transactions](../agent/action_observation.py), [input control](../agent/targeted_input.py) and [native node storage](../android/accessibility-collector/app/src/main/java/ai/clickclick/collector/NodeClickStore.kt).

## 6. Observability and evaluation

Console connects tasks, role calls, model inputs and outputs, skills, tools, receipts and observations. Developers can inspect live and historical screens, diagnose plan or input failures, and analyze latency and usage. The [observability guide](observability.md) explains the interface.

Evaluation is separate from the agent harness. AndroidWorld initializes tasks and checks their final state; the runtime records execution and resource use. The 115/116 result measures the complete system, rather than the isolated gain of an individual mechanism. The [evaluation report](androidworld-results-20260921.md) defines its protocol and action space. [Demo recordings](demos.md) illustrate individual workflows.

## 7. Scope

ClickClick targets Android workflows requiring information transfer, multi-field entry, record management and inspectable execution. Source retrieval does not guarantee lossless summaries; native binding checks mechanical identity rather than all business meaning; traversal guidance is not a formal coverage proof. Application outcomes still require observation or external evaluation.

The contribution is the working combination of these mechanisms.
