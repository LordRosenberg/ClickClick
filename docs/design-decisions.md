# Design decisions

[README](../README.md) · [中文](design-decisions.zh-CN.md) · [Architecture](architecture.md) · [Technical overview](reliability-design.md)

This rationale index records why choices were made. Detailed mechanisms and configuration have one owning design linked from each section; dated experiments remain separate.

## Role policy

Two roles with on-demand independent review avoid fixed review overhead while retaining escalation for disputes. Protocol: [orchestration](task-orchestration.md). Historical matched controls: [runtime study](runtime-study.md).

## Context budget

Bounded history reduces per-round input; sources and retrieval preserve evidence beyond summaries. Larger windows can increase cumulative input. Current budgets: [memory design](memory-and-context.md). Historical comparisons: [study](runtime-study.md).

<a id="jev-summary-checking-original-assertion-spans-and-one-correction"></a>

## Optional Jev summary checks

Optional text checks can reject unfaithful summaries at additional latency and limited coverage. Current admission, degradation and caching: [memory design](memory-and-context.md#jev-text-fidelity-isolated-experiment). Environment-specific calibration/results: [historical experiments](memory-context-experiments.md).

## Prompt-cache continuity

Stable prefixes and retained messages support cache reuse. Rewriting older context may reduce text but increase uncached input. Compare cache, output and extra calls together; see [context layout](memory-and-context.md) and [measurement](observability.md).

## scrcpy, Collector and Python

Separate pixels/structure reduce duplicate device work. Bounded window checks and recapture balance consistency with dynamic-page availability. Methods: [consistency design](observation-execution-consistency.md). Installation/diagnostics: [transport](scrcpy-observation.md).

## Targeted text replacement

Combining focus, replacement and readback for supported fields reduces model round trips. Ambiguous/unsupported controls remain outside this path, and readback does not prove saving. See [targeted input](observation-execution-consistency.md#targeted-text-replacement-and-focus-consistency).

## Native node binding

Retained nodes and attribute checks tolerate small layout shifts without automatic coordinate fallback after uncertain outcomes. Scope/order: [node binding](observation-execution-consistency.md#native-node-binding-and-attribute-checks). Device evidence: [technical report](native-node-click-20260920.md).

## Role-specific skills

Catalog-first selection and role projection reduce unrelated bodies, but delivery and adoption still require separate checks. File knowledge is not execution permission. See [delivery](skill-delivery.md) and [self-learning](skill-evolution.md).

## Budgets and metrics

Cumulative limits bound execution; explicit extensions retain failed attempts. Roles, requests, submitted actions and physical subactions remain distinct. Limits: [orchestration](task-orchestration.md). Measurement/scoring: [evaluation](evaluation-design.md).

## Retired text-tree experiments

Reader-specific and other unadopted representations remain historical experiments rather than current capabilities or configuration switches. Original outcomes and rollback decisions are in the [memory experiment record](memory-context-experiments.md#reader-and-text-tree-experiments).

## HITL questions and active intervention

Models retain intent judgment while runtime handles durable waits and concurrent eligibility. Changes do not undo executed effects. See [HITL design](hitl-design.md) for reply ownership, late responses and replanning.

## Generic assistant screen preview

Browser presentation is decoupled from execution and reuses existing frames without a second capture/input path. Host presentation capabilities do not control backend execution; see [observability](observability.md).

## Recovery and execution idempotency

Transactions retain intent, receipts and complete state; Driver IDs deduplicate execution. SQLite commit is not an atomic phone-effect commit. Unknowns return to the original role with fresh observations rather than guessed replay; see [recovery](crash-recovery.md).
