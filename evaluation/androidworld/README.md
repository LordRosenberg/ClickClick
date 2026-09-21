# Reproduce the AndroidWorld evaluation

[中文](../../docs/evaluation.zh-CN.md) · [Evaluation report](../../docs/androidworld-results-20260921.md)

Run the 116 published task instances with the full-episode ClickClick harness. The command downloads a pinned AndroidWorld revision, generates protobuf bindings, restores typed task parameters, freezes the agent and evaluation Skills, compiles the independent scoring observer, prepares app baselines, checks model access, and runs the suite.

## Before the first run

- Install ClickClick in a Python 3.12 environment with the `decode` extra, following the [deployment guide](../../docs/deployment.md). Configure your model in the local `.env`; the reference profile uses `chatgpt/gpt-5.6-sol` with reasoning effort `high`.
- Install JDK 17 and Android SDK platform 33/build-tools; set `JAVA_HOME` and `ANDROID_HOME` (or `ANDROID_SDK_ROOT`). Put `adb` and `emulator` on PATH, or supply `--adb`.
- Prepare the API 33 AndroidWorld AVD following the [pinned upstream installation guide](https://github.com/google-research/android_world/blob/e3fea3ccc69787570e282c99573298f1c3019a34/README.md). Start it on the default ports with `emulator -avd AndroidWorldAvd -no-snapshot -grpc 8554 -camera-back emulated`. The runner uses `emulator-5554`, ADB port 5555 and gRPC port 8554.
- Install/enable the ClickClick Collector and ADBKeyboard using the [device setup guide](../../docs/accessibility-collector-setup.md). Use this dedicated benchmark emulator: evaluation initializes application state and restores task baselines.

## One command

From the ClickClick repository, with its Python environment activated:

```bash
python -m evaluation.androidworld.reproduce --install
```

`--install` creates a separate evaluation environment under `data/androidworld-eval-venv`; the model worker uses the Python environment that launched the command. The batch is stored under `data/androidworld-run`. Omit `--install` and pass `--eval-python` to reuse an existing evaluation environment. Use `--output data/my-run` for another batch; existing batches are never overwritten.

To validate downloads, fixtures and the oracle build without touching the emulator or calling a model:

```bash
python -m evaluation.androidworld.reproduce --install --prepare-only
```

Then start that prepared batch:

```bash
python -m evaluation.androidworld.reproduce --install --resume
```

The same `--resume` command skips completed attempts, including scored failures. It never silently replaces an interrupted attempt. For a batch explicitly paused because of model quota, use `--resume-after-quota`; the interrupted evidence is archived before retrying that case. Create a `STOP` file in the batch directory for cooperative cancellation, or `PAUSE_AFTER_EPISODE` to pause at the next clean episode boundary. Remove the marker before resuming. Other incomplete/infrastructure-failed attempts require inspection and a fresh output directory.

Options include `--env-file`, `--agent-python`, `--eval-python`, `--adb`, and `--model`. A different model is a new experiment; resume must retain the original model. `--upstream` accepts a clean local checkout at the pinned revision for offline preparation. SDK/AVD installation is a prerequisite, not a hidden part of the command.

## Inputs and scoring

- AndroidWorld is pinned to `e3fea3ccc69787570e282c99573298f1c3019a34`.
- [Published instances](fixtures/instances.json) contain all 116 synthetic task parameter sets, seeds, budgets and two synthetic receipt images. JSON uses an explicit type allowlist; no executable pickle is downloaded. A typed pickle is generated locally for the runner.
- Task limits are `int(complexity * 10)` submitted action units, `int(complexity * 60)` model requests and 900 seconds. Executor history is 16,000 estimated tokens. The app Skills and three evaluation-only Skills are frozen with the runtime.
- Each episode runs one persistent agent session. Official initialization, `task.is_successful` and teardown determine task state and score. Independent API 33 UI acquisition is described in the [oracle guide](oracle/README.md).
- Preparation applies the same scoped SQLite replacement repair as the reference environment: update only the requested database and its sidecars, preserving unrelated databases. This changes environment setup, not task predicates.
- `source-hashes.json` freezes relative paths and hashes. Modified inputs stop execution before model/device work. The runner keeps every completed failure and records infrastructure interruptions separately.

The published agent implementation matches the reference runtime's agent/driver logic except for the newer Collector release pin. Collector 0.4.5 uses the same Collector source with an updated version label. This entry point reruns the published instances with the checked-out source; stochastic model results and later model-service changes can differ from the historical result. It does not automatically select replacement episodes.

## Results and sharing

Raw logs, screenshots, model messages, local paths, databases and generated runtime snapshots remain under ignored `data/`. They are useful locally and are not a public results package.

After a completed command, `summary.public.json` contains only task names, numeric metrics and boolean outcomes. You can export the current results at any time:

```bash
python -m evaluation.androidworld.reproduce --export-summary summary.public.json
```

The export excludes exception messages, model transcripts, endpoints, device identifiers and host paths by an explicit field allowlist. Review the summary before sharing. It distinguishes official predicate success from the stricter budgeted completion field.

## Licensing

AndroidWorld source and its synthetic fixture conventions are attributed to Google Research's AndroidWorld project, licensed Apache-2.0. The official source and license are downloaded at the pinned revision. ClickClick's environment repair, runner and fixture codec are distributed under this repository's license. APK distribution and third-party licenses are described in [Third-party components](../../THIRD_PARTY_NOTICES.md).
