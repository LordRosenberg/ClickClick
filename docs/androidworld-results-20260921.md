# AndroidWorld: 115/116 (99.14%)

[README](../README.md) · [中文 README](../README.zh-CN.md) · [Evaluation methodology](evaluation.md)

ClickClick passed **115 of 116 AndroidWorld task instances (99.14%)** in the September 21, 2026 evaluation. This report describes the system configuration, scoring protocol and reproduction requirements. Success is measured by AndroidWorld's official task predicates.

## Configuration

- **Runtime:** frozen source at `c257a5c903abf3000d3ae8cf0a4bb4c70425cb36`.
- **Model:** configured as `chatgpt/gpt-5.6-sol`, reasoning effort `high`.
- **Architecture:** Planner + Executor, with on-demand Reviewer; 16,000 estimated-token Executor history threshold.
- **Environment:** Android API 33 emulator; frozen task instances and seeds; per-task initialization and cleanup. The rear camera used the emulator's synthetic camera backend.
- **Collector:** the frozen batch's APK, SHA-256 `3d988e395a174d437423bb1ed69218d56c6a299283965af1dc0b2810d580e314`. Native node binding requires the compatible Collector build; the version label alone does not identify its contents.
- **Knowledge:** app/workflow skills, device-profile guidance and the AndroidWorld evaluation skill profile. This is a skill-adapted system evaluation.
- **Limits:** `int(complexity * 10)` submitted action units, `int(complexity * 60)` model requests and 900 seconds per task.

The score describes this model, runtime, skills, environment and selected instances together. It does not isolate the contribution of one component or establish performance on unseen apps and seeds.

## Selection and scoring

The selection contains 115 original full-run episodes and one replacement of `SystemBrightnessMax`. The replacement retained the same v6 runtime, seed, skill, model and budgets. The reported metric is the success rate of this selected set, rather than first-attempt pass@1.

The success numerator comes from the unmodified official `task.is_successful` predicates. No task-specific predicate or filename override was applied. Oracle UI acquisition used an independent API 33 UiAutomation forest with bounded acquisition attempts and official forest conversion; this acquisition adapter differs from the stock runner. Capture errors are unscored, rather than assigned a successful predicate result.

The exported session-termination flag includes explicit inconclusive termination; the predicate determines benchmark success. Thus the headline measures externally scored task state, rather than whether the agent itself expressed confidence that every requirement had been verified. The one predicate failure in the selected set is `RecipeDeleteDuplicateRecipes3`.

## Action accounting

The current action space includes skill-authorized compound actions. A supported inspect-and-return or time-sensitive tap-and-key recipe consumes **one submitted action unit**, while its physical subactions, intermediate observation and final observation are recorded separately. Targeted text replacement also records its actual focus/input operations.

One submitted action is therefore not necessarily one physical tap or key press. The action space and accounting must accompany any comparison with another system. The [ten-task runtime study](runtime-study.md) uses its own recorded action semantics and is separate from this aggregate.

## Reproducibility

The local batch is identified as `androidworld-sol-full-v6-20260921`. Its protocol, per-case results, result-selection manifest, source hashes and episode evidence were retained locally. The selected 116 episodes were retrospectively converted into AndroidWorld checkpoint PKLs. The export record reports official Checkpointer write/read and summary processing, equal screenshot pixel arrays after round trip, and archive/hash validation. Conversion does not mean that the original run used the stock AndroidWorld runner.

The PKL-only delivery archive has SHA-256:

```text
2562515e78a99de2b102e5ba668a09696fc5d158ef3665c45337a7f338036854
```

Exact reproduction requires the frozen fixtures, evaluation runner, skill overlay and runtime. The public source distribution includes the agent and an optional AndroidWorld adapter; the frozen evaluation materials and delivery archive are not bundled. This is a local evaluation result, not an accepted leaderboard submission.

The [optional AndroidWorld adapter](androidworld-benchmark.md) documents a separate integration path. Its step-oriented runner semantics should not be substituted for the full-suite protocol reported here.
