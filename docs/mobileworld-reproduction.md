# Reproduce the MobileWorld GUI-only evaluation

[中文](mobileworld-reproduction.zh-CN.md) · [Results](mobileworld-results-20261002.md) · [System architecture](architecture.md)

The public entry point prepares a separate, frozen runtime and runs the complete **117 original GUI-only tasks plus nine disclosed variants**. Official MobileWorld initialization and final-state evaluation remain independent of ClickClick. The clarified result substitutes all nine variant outcomes into the original set, including failures. It never chooses the better of two attempts.

## Prepare without a device

Use Python 3.12. From this repository:

```sh
python -m venv .venv-mobileworld
# Activate .venv-mobileworld using your shell's normal command.
python -m evaluation.mobileworld.reproduce --prepare-only
```

Preparation checks the published source seal and copies inputs into `data/mobileworld-reproduction/input`. It does not contact Docker, ADB or a model. Add `--install` to install the frozen Python runtime, PyAV 14.0.1 and Requests into the active Python environment. Dependencies use the repository's published bounds; this is not a complete historical dependency lock.

## Dedicated official environment

Follow the [upstream deployment instructions at the pinned revision](https://github.com/Tongyi-MAI/MobileWorld/tree/e41d1478e252325c513003d3d191b4c164b4af2c). The scored profile uses the official **v1.4 image, Android 14 / API 34**, and backend at `http://127.0.0.1:6800`. Use a dedicated container, normally `mobile_world_env_0`, with host ADB port 5556 published. Do not point this adapter at an unrelated host emulator.

Some v1.4 containers require two official helper updates already included in the pinned upstream source: Mattermost `_extend_session_expiry` and the `reset_chrome` import in `thanksgiving_prep.py`. Inspect and update those official modules before running. The adapter refuses to proceed if they are absent. Do not change task initializers' data or evaluator predicates. The disclosed local Mattermost readiness wait is installed between episodes:

```sh
cd data/mobileworld-reproduction/input/runtime
python -m evaluation.mobileworld.patch_mattermost_readiness --container mobile_world_env_0 --archive data/readiness-patch --apply
docker restart mobile_world_env_0
docker cp evaluation/mobileworld/adb_proxy.py mobile_world_env_0:/tmp/clickclick_mobileworld_adb_proxy.py
docker exec -d mobile_world_env_0 python3 /tmp/clickclick_mobileworld_adb_proxy.py
adb connect 127.0.0.1:5556
adb -s 127.0.0.1:5556 shell getprop ro.serialno
```

Return to the repository root after these commands. Keep the returned serial for the required `--expected-device-serial` check. The proxy bridges the container's published port to its emulator ADB listener. ADB access remains explicitly pinned after reconnects and restarts.

Build Collector from the public [Android project](../android/) using the [Collector setup instructions](accessibility-collector-setup.md), and supply an [ADBKeyboard APK](https://github.com/senzhk/ADBKeyBoard). APKs are local inputs, not distributed in this repository. Historical Collector binary SHA-256 is recorded in the [frozen provenance](../evaluation/mobileworld/reproduction/provenance.json); rebuilding does not assert an identical APK. The launcher records both supplied APK hashes and rejects changes on resume.

Configure your own model access using the [deployment guide](deployment.md). Pass a local environment file if needed; it is copied only into the ignored run workspace. The runtime fixes `plan_executor`, 16,000 history tokens, high reasoning, frozen prompts and skills, and disables automatic compaction-attempt notes. Custom `--model` runs are labeled by their supplied model and cannot be described as the published GPT-5.6-SOL result.

## Run the complete paired evaluation

```sh
python -m evaluation.mobileworld.reproduce --install \
  --expected-device-serial YOUR_CONTAINER_SERIAL \
  --collector-apk data/device-apks/collector.apk \
  --ime-apk data/device-apks/ADBKeyboard.apk \
  --env-file .env
```

PowerShell uses backticks instead of shell backslashes for line continuation; the same arguments work on one line. Backend, container, target and container-internal device name have explicit override options. The default model is `chatgpt/gpt-5.6-sol` with high reasoning. Every task permits at most **50 prediction rounds**, including rejected attempted actions and terminal answers, and a **2,400-second local deadline**. Internal role/model calls and physical subactions are separate counts. The agent runs continuously within each episode, and the official evaluator is called after execution.

`--variant original` runs all 117 original goals. `--variant clarified` runs only the nine changed cases; by itself it produces no full clarified success rate. Default `paired` runs original first, then the nine cases. Task implementation hashes are checked read-only inside the container before the batch, and goals are validated against the pinned profile before delivery. The two disclosed Mastodon cases receive verified official synthetic account information as model-visible context in both arms; login remains a GUI action. The ninth variant also adds account information to its goal. No browser cookies or answers are injected.

## Resume and inspect results

Repeat the same command with `--resume`. Input, APK, model, configuration, target and variant changes are rejected. Scored failures are retained rather than rerun for a better result. Failed initialization and interrupted execution preserve diagnostics; inspect them before resuming. Source tampering stops execution. Put `STOP` or `PAUSE_AFTER_EPISODE` inside the active `original/` or `clarified/` output directory to stop at an episode boundary; remove it before resuming.

The run stores arm-specific results, recordings, traces and health evidence under `data/mobileworld-reproduction`. `public-summary.json` contains only totals and completeness. A full success percentage is emitted only once every required task has a numeric score and all nine variant results are present. Incomplete or evaluator-error runs remain incomplete; they do not acquire a fabricated official score. Preserve the first result and inspect the failure. Raw traces, databases and configuration remain local and must be reviewed before publication.

## Frozen source and limits of reproduction

The [profile](../evaluation/mobileworld/reproduction/profile.json) publishes the ordered task set, original/clarified goals, source revision and budgets. The [seal](../evaluation/mobileworld/reproduction/seal.json) checks every distributed input. Agent and device files covered by the archived run's source manifest match those historical byte hashes; scored prompts and skills are copied from the frozen batch. Perception support is taken from the reviewed evaluation-era commit. Two historical harness files (`run_full.py`, `export_submission.py`) were not preserved byte-for-byte and are published as reviewed later copies; the public launcher and narrowly scoped fixture-context delivery are disclosed adaptations. These distinctions are recorded in provenance rather than presented as an identical historical filesystem.

The entry point supplies the complete running path, not a promise that a stochastic rerun will exactly reproduce **106/117 and 112/117**. No scored rerun is performed as part of publishing this package. Device image revisions, local APK builds and dependency versions should be recorded with any new result.
