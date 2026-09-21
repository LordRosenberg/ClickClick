# Evaluation and reproducibility

[README](../README.md) · [中文](evaluation.zh-CN.md) · [115/116 report](androidworld-results-20260921.md) · [AndroidWorld adapter](androidworld-benchmark.md)

There are two useful starting points: run your own task through Console, or connect ClickClick to AndroidWorld for state-based scoring. The published 115/116 result uses a separately frozen full-suite protocol described in the result report.

## Evaluate your own task

1. Complete [deployment](deployment.md) and describe a task in natural language. See [task examples](task-examples.md) for ideas.
2. Prepare known source data and record the initial destination state.
3. Submit the task and record the model, skill set and task limits; keep these conditions fixed when comparing configurations.
4. Inspect the saved app state and the Console trace. Check required fields, extra results and changes to unrelated records.
5. For comparisons, restore the same starting state and retain all attempts.

Task termination, successful device dispatch and a correct application outcome are distinct measurements. A saved field value and a complete collection-wide result may require different checks.

## Run AndroidWorld with ClickClick

Prepare AndroidWorld's Android API 33 emulator and application setup according to its [official repository](https://github.com/google-research/android_world). Install ClickClick and AndroidWorld into an environment that can import both projects, configure model access and connect both to the same emulator.

Register `agent.integrations.android_world.ClickClickAgent` in AndroidWorld's agent factory as described in the [adapter guide](androidworld-benchmark.md). A first smoke run is:

```bash
python run.py \
  --suite_family=android_world \
  --agent_name=clickclick \
  --tasks=ContactsAddContact \
  --perform_emulator_setup
```

Run this command from the AndroidWorld checkout after registration. `--perform_emulator_setup` is for the first setup; later runs use the prepared emulator. The adapter uses the same ClickClick runtime, while AndroidWorld owns initialization, outer steps, success evaluation and cleanup.

## Reproduce the reported experiment

The [September 21 report](androidworld-results-20260921.md) specifies the frozen runtime, model, Collector digest, skills, seeds, budgets, action space and episode selection. Exact reproduction requires those frozen fixtures, skill overlay and full-suite runner; they are not currently distributed with the public source mirror. The public step-oriented adapter supports new evaluations but is not the identical runner used for that experiment.

Preserve configuration alongside results: app and Android versions, model settings, runtime and skill revisions, task parameters, retry/selection policy, initialization and scoring implementation. Compound actions must retain both submitted-action and physical-subaction accounting.

## Metrics

| Dimension | Measurements |
| --- | --- |
| Task correctness | External success score, requested fields, extra results and unrelated-record preservation. |
| Execution | Submitted actions, physical subactions, model calls and completion within limits. |
| Time | Task/worker elapsed time, model latency, capture and compaction time. |
| Usage | Reported input, cached input and output; estimates separately identified. |
| Reliability | Initialization, model transport, capture and cleanup errors. |

Missing provider usage is unavailable, not zero. Infrastructure interruptions remain recorded separately from task failures. The original result and any subsequent selection should remain identifiable.

## Inspect evidence

Console exposes linked calls, observations and tool receipts. For each scored episode retain the goal, configuration, initial/final state, result and execution trace. [Observability](observability.md) describes the task inspection process; [demo provenance](demos.md#recording-provenance) illustrates how media links back to a completed run.
