# Runtime study: role configuration and context budget

[Architecture](architecture.md) · [Design decisions](design-decisions.md) · [AndroidWorld result](androidworld-results-20260921.md)

## Scope

This September 2026 study compares Planner + Executor with mandatory final review, and a 16k versus 24k estimated-token history threshold. It is a small controlled runtime study, separate from the later 115/116 AndroidWorld evaluation. It does not isolate the causal contribution of every harness component.

The paired tasks cover expense entry/deletion, recipe entry/deletion and calendar creation. Both role configurations use the same task instances, model configuration and action budgets within each comparison. Official task predicates check final application state; separate audits inspect required fields, extra records and preservation of existing records. Agent confirmation within budget is reported separately.

Worker time includes preflight and internal cleanup through `run_task` return. It excludes fixture initialization, official scoring and final evidence export. Text input counts are estimates rather than provider billing usage. The historical action accounting differs from the current compound-action space.

## Ten-task role comparison

The frozen full matrix contains ten tasks per role configuration. These results precede the final frame-selection fix; later successful reruns are not substituted into this table.

| Metric | Planner + Executor | Mandatory final review |
| --- | ---: | ---: |
| Official task success | 10/10 | 10/10 |
| Agent confirmation within budget | 9/10 | 9/10 |
| Worker time, seconds | 4,300.798 | 4,047.845 |
| Model calls | 360 | 345 |
| Compaction calls | 52 | 50 |
| Estimated text input | 3,842,259 | 3,603,404 |
| Image submissions | 1,330 | 1,234 |
| Historical equivalent actions | 298 | 299 |

All fourteen creation episodes passed field and preservation checks; the six deletion episodes removed the requested records while preserving other records. Both configurations nevertheless had one episode without agent confirmation within budget. There is no consistent accuracy advantage for mandatory review in this sample, and individual tasks can run faster under either configuration.

## Context-budget comparison

Four matched episodes—expense transfer and recipe creation under both role configurations—compare 16k and 24k after the frame-selection fix. All four valid episodes in each arm passed within budget and preserved the required fields and existing records. An additional 24k attempt ended in an unscored observation failure and is not counted as a task success.

| Metric | 16k | 24k | Change |
| --- | ---: | ---: | ---: |
| Completion within budget | 4/4 | 4/4 | — |
| Worker time, seconds | 1,586.187 | 1,457.828 | −8.1% |
| Model calls | 146 | 146 | 0% |
| Compaction calls | 19 | 12 | −36.8% |
| Compaction wait, seconds | 225.704 | 145.361 | −35.6% |
| Estimated text input | 1,527,247 | 1,791,262 | +17.3% |
| Image submissions | 532 | 695 | +30.6% |

A larger history reduces compaction calls but carries more text and images. Expense-task time increased by about 3.6%; the aggregate speedup came from recipe tasks. These results support keeping 16k as a conservative default, with a per-model override, rather than a universal cost or speed claim.

## Additional validation and limitations

After the frame-selection fix, six valid targeted episodes passed, as did four valid episodes with new task instances. These are separate cohorts, not additions to the full-matrix numerator. Initialization, network and observation interruptions were retained in the original experiment records; their time and resource costs are not represented by valid-episode success rates.

The sample is small, runs are not repeated enough to estimate a robust failure distribution, and environment recovery can change latency. Provider billing usage was unavailable, so input estimates cannot be converted to monetary savings. Remaining observed limitations included conditional-deletion coverage within budget and intermittent graphics-path failures.

This report publishes aggregate findings and experimental boundaries. Raw device databases, screenshots, model conversations, credentials and machine-specific logs are excluded from the source distribution. The [evaluation guide](evaluation.md) explains how to perform a new controlled comparison.
