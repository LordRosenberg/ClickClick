# Deployment and operation

[README](../README.md) · [中文](deployment.zh-CN.md) · [Architecture](architecture.md) · [Evaluation setup](evaluation.md)

## Requirements

- Python 3.12 is the validated choice for the pinned video decoder. Project metadata declares Python 3.11+; decoder wheel compatibility is a separate constraint.
- Node.js/npm builds the Vite Console. Use `npm ci` with the committed lockfile.
- Android 8.0+ is the Collector minimum. Authorize ADB on the target device. The AndroidWorld runner has stricter API 33/emulator requirements.
- Model access is required for actual agent tasks. Fixture mode replaces device I/O, not the language model.

Create and activate `.venv` using the platform-specific commands in the [README](../README.md#deployment), then install `python -m pip install -e ".[decode]"`. Add `dev` to the extras when running tests. Preserve an existing `.env` instead of overwriting it with the example.

## Model routing

`CLICKCLICK_DEFAULT_MODEL` must match a key in `CLICKCLICK_MODELS_JSON`. The key carries the routing prefix; its value describes the provider. Fill in real credentials locally, not in tracked files. The example file is a template, not a working model service.

For an OpenAI-compatible endpoint, replace every placeholder:

```dotenv
CLICKCLICK_DEFAULT_MODEL=openai/YOUR_MODEL_ID
CLICKCLICK_MODELS_JSON={"openai/YOUR_MODEL_ID":{"provider":"openai","base_url":"https://YOUR_ENDPOINT/v1","api_key":"YOUR_API_KEY","max_tokens":4096,"reasoning_supported":false,"stream":false}}
```

Optional `CLICKCLICK_MANAGER_MODEL` selects the shared Planner/Reviewer model; `CLICKCLICK_EXECUTOR_MODEL` selects Executor. Both must name configured entries. Empty role overrides use the default. Reasoning support and parameters are explicit per model/provider, not assumed from its name.

Each model entry accepts top-level `"tool_choice":"auto"` or `"tool_choice":"required"`, alongside `max_tokens`, outside `extra_body`. Omitting it preserves `required`; changing one model does not change other models, including GPT. With `auto`, the model may return text first, but Agent still requires a valid decision submission tool call to finish the invocation. This setting alone neither enables thinking nor implements the `reasoning_content` round-trip required by some thinking models.

For compatible relay aliases missing from LiteLLM's model registry, a model entry can explicitly set `"allowed_openai_params":["reasoning_effort"]` alongside `"reasoning":{"effort":"high"}`. This preserves the configured effort through LiteLLM's parameter validation; the upstream endpoint must still support it. The override is per model and does not enable global parameter dropping.

The locally validated study used this subscription route:

```dotenv
CLICKCLICK_DEFAULT_MODEL=chatgpt/gpt-5.6-sol
CLICKCLICK_MODELS_JSON={"chatgpt/gpt-5.6-sol":{"provider":"chatgpt","reasoning_supported":true,"reasoning":{"effort":"high","summary":"concise"},"stream":true}}
```

Authenticate using the project's login helper before starting tasks:

```bash
python -m shared.chatgpt_login
```

This route uses login tokens rather than the API-key/base-URL fields above. `CLICKCLICK_CHATGPT_TOKEN_DIR` optionally selects token storage. Provider/account availability can vary; the recorded experiment is not a promise that every account has that model. See [.env.example](../.env.example) and [model routing](../shared/model_router.py) for the project-side configuration.

## Local device deployment

The agent harness defaults to `CLICKCLICK_AGENT_ARCHITECTURE=plan_executor`: two roles with explicit on-demand review. `plan_reviewer` requires final review. The old `contract` mode has been removed. Keep the default unless you are deliberately comparing policies. Context tuning is part of the harness and is documented separately in [design decisions](design-decisions.md#context-budget).

For a device attached to the API host:

```dotenv
CLICKCLICK_DRIVER_URL=
CLICKCLICK_USE_FIXTURE_DRIVER=false
```

Remove remote-hub entries from `CLICKCLICK_DRIVER_URLS_JSON` if you previously used them. The copied example has a remote Driver URL, so explicitly clear it for the single-process setup.

```bash
adb devices
npm --prefix web ci
npm --prefix web run build
clickclick-api
```

Open `http://127.0.0.1:8080`. The platform automatically reconciles connected idle devices, installing/upgrading the Collector, enabling accessibility, checking its channel and provisioning the configured input method. Normal startup does not require manual APK installation or an initialization API call.

Keep the device unlocked. Accept installation, USB-debugging or permission prompts if the device presents them; an authorized ADB installation does not require an on-screen confirmation on every device. If Android restricts the Collector, enable it in Accessibility settings and allow restricted settings in its app details when that option is available. Use the [initialization diagnostics](#initialization-results) for unresolved failures.

The Collector resolver prioritizes `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`, then a matching local Gradle build, then the pinned GitHub Release with SHA-256 verification. Same-version local rebuilds are detected by installed APK digest. Release assets are cached under `data/device-apks` (override with `CLICKCLICK_DEVICE_APK_CACHE`). Private releases require `GH_TOKEN` or `GITHUB_TOKEN` in the process environment; `CLICKCLICK_COLLECTOR_RELEASE_REPO` selects the repository. `CLICKCLICK_IME_APK_PATH` must point to an existing ADBKeyboard APK if it is not already installed. The API does not download this APK automatically. Relative paths are resolved on the process host; remote devices need the files and configuration on their Driver host.

Initialization enables ADBKeyboard but does not select it as the current input method. Per-task readiness selects it before actions and verifies the selected IME. Check `test_ime.status`, not only Collector readiness.

### Input method APK

Skip this step when ADBKeyboard is already installed or an existing APK path is configured. For a fresh device, supply the source file once; the platform handles installation. Obtain the APK from [ADBKeyBoard](https://github.com/senzhk/ADBKeyBoard). The following cross-platform command downloads the same file used by the project's bootstrap helper, without installing it on a device:

```bash
python -c "from pathlib import Path; import urllib.request; p=Path('data/device-apks/ADBKeyboard.apk'); p.parent.mkdir(parents=True, exist_ok=True); urllib.request.urlretrieve('https://raw.githubusercontent.com/senzhk/ADBKeyBoard/master/ADBKeyboard.apk', p)"
```

Set its location in `.env` before starting the service, or restart the service after changing the setting:

```dotenv
CLICKCLICK_IME_APK_PATH=data/device-apks/ADBKeyboard.apk
```

If the download is unavailable, obtain the file from the upstream project and use its local path. Missing input-method files are reported as an initialization failure/degradation, not silently downloaded. Missing Collector files can be built using the [Collector setup guide](accessibility-collector-setup.md).

### Optional bootstrap helper

With Bash, an alternative is the bootstrap helper. Its default Collector release reference is older than the current runtime; pass the current APK path explicitly:

```bash
bash scripts/bootstrap-device.sh YOUR_ADB_SERIAL
```

This helper downloads the input-method APK when needed. It can replace helper apps on signing mismatch. The API initializer also replaces only the stateless Collector package when its signing key prevents an upgrade. It does not clear target-app data. Manual/build instructions are in [Collector setup](accessibility-collector-setup.md).

### Initialization results

Use this endpoint to diagnose provisioning or retry after fixing a reported problem. It is optional during normal startup. Keep the service running and replace `emulator-5554` with the target serial:

```bash
# Linux / macOS
curl -X POST http://127.0.0.1:8080/api/devices/emulator-5554/initialize
```

```powershell
# Windows PowerShell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/api/devices/emulator-5554/initialize' | ConvertTo-Json -Depth 8
```

The explicit initialization response has top-level `status` and a `steps` object. Check these steps individually:

| Step | Meaning when ready | Recovery when not ready |
| --- | --- | --- |
| `adb` | Target device is online and authorized. | Check the serial, cable/emulator, authorization prompt and ADB host. |
| `collector_apk` | Required Collector is installed. | Supply the matching APK; inspect installation/version errors. |
| `accessibility_service` | Collector is enabled and required settings accepted. | Follow `guidance`: Android Settings → Accessibility → ClickClick Accessibility Collector; allow restricted settings in its app details when available. |
| `collector_health` / `collector_channel` | Service responds and its channel can be warmed. | Recheck accessibility, unlock the device, then initialize again. |
| `test_ime` | Configured input method is installed and enabled. | Supply the [input-method APK](#input-method-apk) and correct `CLICKCLICK_IME_APK_PATH`; restart the service after changing `.env`, then let automatic initialization retry or use the endpoint above. |

`ready` is the desired aggregate status. `degraded` is a partial environment; `operator_action_required` needs the indicated device-side step; `failed` requires correcting the reported failure. HTTP 409 means the device is busy: finish or cancel its task before provisioning. These statuses verify device components, not model credentials or a successful scrcpy frame; the first task also checks those paths.

To verify registration manually, replace the serial:

```bash
adb -s emulator-5554 shell ime list -s
adb -s emulator-5554 shell settings get secure enabled_accessibility_services
```

Expect `com.android.adbkeyboard/.AdbIME` in the IME list and `ai.clickclick.collector/.CollectorService` among enabled accessibility services. Do not replace the whole accessibility-service setting, which may contain other services. Restore your preferred keyboard in Android settings after testing if needed.

The Console and agent use the vendored scrcpy server on demand. There is no separate desktop scrcpy process to start. ADB pixel fallback is recorded, while missing video decoding prevents the strict evaluation preflight from passing.

## Windows ADB and debugging

The resolver checks `PATH`, `CLICKCLICK_ADB_PATH`, Android SDK locations and the project cache. On Windows it can provision a checksum-verified Google Platform-Tools copy when discovery fails. `CLICKCLICK_ADB_AUTO_DOWNLOAD=0` disables that fallback. An explicit path avoids ambiguity when several SDKs are installed.

[VS Code launch configurations](../.vscode/launch.json) use `.venv/bin/python` by default and `.venv/Scripts/python.exe` on Windows. Run the fixture API configuration for device-free UI debugging. The selected model still needs access for a real agent task.

## Fixture and remote modes

For a local fixture:

```dotenv
CLICKCLICK_USE_FIXTURE_DRIVER=true
CLICKCLICK_DRIVER_URL=
```

Start `clickclick-api` normally. To run deterministic tests without provider calls, use the project's fake-agent tests rather than submitting a real model task.

For a remote device host, install compatible code and decoding support there, authorize its attached devices and start:

```bash
clickclick-driver --host 0.0.0.0 --port 8765
```

Configure the API host:

```dotenv
CLICKCLICK_USE_FIXTURE_DRIVER=false
CLICKCLICK_DRIVER_URL=http://DEVICE_HOST:8765
```

Several hubs can be configured with `CLICKCLICK_DRIVER_URLS_JSON`, for example `[{"id":"lab-a","url":"http://DEVICE_HOST:8765"}]`. Device keys then include their hub. Keep platform and hub code/protocol versions compatible, including the vendored scrcpy server. Use a trusted network for the device-control endpoint.

## HTTPS and frontend development

The built Console is served by Control API. A separate Vite development server is optional:

```bash
npm --prefix web run dev
```

Vite uses port 5173 and proxies `/api` to the Control API. Driver RPC defaults to 8765 and Control API to 8080. `clickclick-agent "YOUR_TASK"` is the standalone task entry point when a Console is unnecessary.

For Live video viewed through a LAN address, enable HTTPS: browser WebCodecs needs a secure context. Configure both certificate paths:

```dotenv
CLICKCLICK_API_HOST=0.0.0.0
CLICKCLICK_API_SSL_CERTFILE=./data/certs/cert.pem
CLICKCLICK_API_SSL_KEYFILE=./data/certs/key.pem
```

The API generates a local development certificate when needed and redirects plain HTTP on that public port to HTTPS. This is a development certificate workflow, not production certificate management. Localhost can use ordinary HTTP.

## Operational checks

| Symptom | Check |
| --- | --- |
| No device in Console | ADB authorization, intended local/remote URL, hub reachability and device initialization. |
| Model fails before acting | Model ID/catalog match, route-specific login or credentials, and the recorded gateway failure. |
| Page loads but Live is blank | Secure context, matching server version, device stream diagnostics and decoder installation. |
| Frontend is absent | Run the build; the API can start without `web/dist`, but that does not build the Console. |
| AndroidWorld cannot find NEXT | Use the required accessibility observer for onboarding and verify app readiness before snapshotting. |

`data/` contains local databases, screenshots, traces and potentially login material. It is not part of the distributable documentation. Source-controlled configuration and experiment artifacts should identify versions and settings without including credentials.
