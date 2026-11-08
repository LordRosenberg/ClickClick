# Historical memory and context experiments

[Current design](memory-and-context.md) · [中文](memory-context-experiments.zh-CN.md)

This page separates historical experiments from the current memory protocol. Detailed development diaries, raw traces, machine-specific commands and internal evidence remain in the development repository.

## Jev text-fidelity experiments

Historical studies used source-bound summary checks and controlled-error replay to investigate unsupported assertions, coverage gaps, correction costs and degradation behavior. Controlled injected errors and naturally occurring errors remain separate; small replay samples do not establish production recall or a benchmark success-rate gain.

Current optional Jev modes, thresholds, correction limits, source resolution and failure policies are documented in the [memory design](memory-and-context.md#optional-jev-text-fidelity-checks), rather than inferred from old experiment settings. Jev checks textual fidelity, not image pixels or business correctness.

## Reader and text-tree experiments

<a id="reader-pdf-observation-experiment"></a>

Earlier Reader-specific, page-labeling and text-tree representations were explored and retired. They are not current runtime capabilities or recommended configuration switches. Current snapshot, sequence and native-detail behavior belongs to the [observation and execution design](observation-execution-consistency.md).

## Role and context-budget comparisons

The [runtime study](runtime-study.md) retains the published matched role-policy and 16k/24k context-budget comparisons, including sample sizes, consumption, unscored attempts and limitations. These historical controls do not isolate the contribution of every later memory mechanism.

Benchmark setup and aggregate outcomes remain in the [evaluation guide](evaluation.md). Historical experimental settings do not override current defaults or replace scored results.
