# Design decisions

[README](../README.md) · [中文](design-decisions.zh-CN.md) · [Architecture](architecture.md) · [Technical overview](reliability-design.md)

ClickClick assigns semantic choices to models and mechanical checks to the harness. The following decisions explain how that division affects reliability, latency and extensibility.

## Role policy

**Default: Planner + Executor, with Reviewer on demand.** Planner owns stage goals; Executor owns local interaction. Either can request independent review for a concrete conflict. Mandatory final review remains available through `plan_reviewer`.

Routine execution already produces new observations and feedback. Reviewing every stage adds calls without necessarily adding useful evidence. A [paired task study](runtime-study.md) found no consistent accuracy advantage for mandatory review, supporting the simpler default. On-demand review depends on model judgment; repeated requests with unchanged state are rejected to limit unproductive loops.

## Context budget

**Default Executor history threshold: 16,000 estimated tokens.** This is a soft history-compaction trigger, separate from the model's context window and the total request size.

Compaction groups complete execution steps and retains recent steps. Original artifacts and note versions remain in Session. Current state and unresolved questions are restored independently of the summary. Exact duplicate observation text can be reused without merging the identity of distinct observations.

In four matched valid task pairs, increasing the threshold to 24k reduced compression calls from 19 to 12 and worker time by 8.1%, while total model calls stayed at 146 and estimated cumulative text input increased by 17.3%. This trade-off supports a conservative default; the [study](runtime-study.md) provides conditions and limitations.

A model-specific override can be added to its existing configuration:

```json
"context": {
  "executor": {"history_tokens": 24000}
}
```

`context.max_input_tokens` optionally bounds the estimated complete request. Image estimates can differ from provider accounting.

## Prompt-cache continuity

Historical messages and image blocks remain stable within the active budget. Repeatedly rewriting earlier context may reduce nominal input while losing reusable cached prefixes. History compaction remains bounded; optimization considers uncached input, cached input, output and additional calls together.

## scrcpy, Collector and Python

**scrcpy supplies pixels, Collector supplies structure, and ADB supplies fallback pixels.** Sharing scrcpy between agent capture and Console avoids maintaining separate video sources. Accessibility data adds indexed controls, windows and interaction evidence.

Acquisition checks request freshness and window state, with a bounded quiet interval and resampling. Content updates remain diagnostic rather than automatically forcing another tree traversal. This allows animated pages to remain observable. A missing tree can still yield usable image evidence when window checks pass.

`snapshot` observes current state; `sequence` observes change over time. `sleep` waits for a required duration, such as recording length, and the next decision receives a fresh observation. The validated decoding stack uses Python 3.12 and PyAV 14.0.1. See [Collector setup](accessibility-collector-setup.md) for provisioning and diagnostics.

## Targeted text replacement

**Focus and replacement are combined only for a supported, identifiable field.** The model supplies a target and text; the harness establishes focus, verifies field identity, clears/types and checks readback. Ambiguous, password and unsupported controls do not receive the same path.

This reduces model round trips while retaining input feedback. Partial execution reports the operations already performed. Readback establishes the field value; an app save still requires its own evidence.

## Native node binding

Supported local indexed taps preserve a reference to the observed Android node. A bounded refresh checks identity and clickability before one native action. Changed bounds can be accepted without selecting a different object at an old coordinate. An invalid or uncertain native result does not automatically retry or fall back to coordinates.

This path applies to compatible Collector nodes and the in-process driver. Coordinate actions, compound helper taps and unsupported transports retain their existing paths. The [native-node technical report](native-node-click-20260920.md) specifies the identity checks and device validation.

## Role-specific skills

**App core, workflow and generic knowledge are maintained separately.** Device profiles and stage selection determine applicability; explicit role sections determine delivery. ID, version and content checks make the selected guidance auditable.

Supported app skills may authorize closed compound recipes. An inspect-and-return operation keeps intermediate evidence, while the returned screen becomes the action basis. This reduces repeated model handoffs but expands the action space: submitted units and physical subactions must be counted separately. The interface deliberately excludes arbitrary scripts and model-generated sequences. See [skill authoring](../skills/README.md).

## Budgets and metrics

Ordinary task limits are caller inputs. The reported AndroidWorld experiment uses complexity-derived action and model-call limits plus a 900-second deadline. Compression counts toward model calls; non-action stage handoffs do not count as device actions.

Report elapsed time, provider calls, submitted actions, physical subactions and token/cache usage separately. The [evaluation report](androidworld-results-20260921.md) defines the benchmark action space. Missing provider usage remains unavailable; estimated input volume is not a monetary bill.
