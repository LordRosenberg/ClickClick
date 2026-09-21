# Android Accessibility Collector Setup

[中文](accessibility-collector-setup.zh-CN.md) · [README](../README.md) · [Deployment](deployment.md)

ClickClick uses a thin, read-only Android AccessibilityService collector for
current multi-window trees. Actions remain ADB commands and pixels remain
scrcpy/ADB screenshots; this is not a general device Portal.

Since Collector 0.3.0, the service additionally holds a renewable screen-bright lease only while
a task is actively executing. The host renews a 90-second TTL every 30 seconds
and releases it from task cleanup. If the host crashes or ADB disappears, both
Collector's TTL watchdog and Android's own WakeLock timeout release it. An ADB
connection that stays open for days does not by itself keep the screen awake.

Collector installation and upgrades are automatic. On Control API startup an
immediate background reconciliation checks every idle online device, then
repeats every 30 seconds for newly-online and previously-failed devices. A
successful device is not repeatedly provisioned during the same online
period; disconnecting and reconnecting makes it eligible again. Task startup
also performs a forced version check under the device lock before acquiring
the screen lease. Busy devices are never upgraded by the background loop.

With no explicit `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`, ClickClick
prefers the local Gradle `app-debug.apk` when `output-metadata.json` reports
version 0.4.4, then downloads the pinned Release into `data/device-apks` with
SHA-256 verification. Same-version local rebuilds are compared against the
installed APK digest and reinstalled when different. Private Release access requires
`GH_TOKEN` or `GITHUB_TOKEN` in the process environment. See [APK distribution](../android/prebuilt/README.md).

## Prerequisites

Normal setup uses automatic provisioning:

1. Enable **Developer options** and **USB debugging** on the phone, or start the emulator.
2. Connect via USB or `adb connect <ip:port>` and authorize the device. `adb devices` must show `device`.
3. Start Control API using the [deployment guide](deployment.md). Collector **0.4.4** is resolved from local builds or Releases; no manual APK installation is required. Android 8.0+ / API 26 is supported.
4. Keep the device unlocked and accept installation/permission prompts if shown. Enable Collector in Android Accessibility or allow restricted settings when the system requires it.

For an absent input method, provide the [ADBKeyboard APK path](deployment.md#input-method-apk); the API installs the configured file but does not download it.

An optional Bash bootstrap helper downloads ADBKeyboard and provisions the device. It shares the local-build-first Release resolver:

```bash
bash scripts/bootstrap-device.sh YOUR_ADB_SERIAL
```

For local development, build `android/accessibility-collector/` with Android Studio or Gradle and configure `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`. Automatic initialization uses that file. `POST /api/devices/{serial}/initialize` is an optional diagnostic/retry endpoint; see [initialization results](deployment.md#initialization-results).

The collector is **enabled by default** (`CLICKCLICK_ACCESSIBILITY_COLLECTOR_ENABLED=true`
in `shared/config.py`); no extra switch is needed after initialization.

## Environment

```bash
export CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH=android/accessibility-collector/app/build/outputs/apk/debug/app-debug.apk
export CLICKCLICK_IME_APK_PATH=data/device-apks/ADBKeyboard.apk
export CLICKCLICK_DRIVER_URL=
```

Initialization is idempotent. It installs/upgrades the APK, merge-enables the
collector without disabling other accessibility services, prepares the test
IME without selecting it permanently, and optionally configures plugged-in
stay-awake. Each step reports `ready`, `degraded`,
`operator_action_required`, or `failed`.

ADB authorization, Android/OEM restricted-setting confirmation, and secure
keyguards remain operator boundaries. If the collector is unavailable, the
driver uses a fresh `uiautomator dump`, marks it incomplete, and automatically
attaches a screenshot. Set `CLICKCLICK_ACCESSIBILITY_COLLECTOR_ENABLED=false`
to roll back to dump-only capture.

## Acceptance benchmark

Run the repository benchmark after initialization:

```bash
python scripts/measure_observation_latency.py --serial SERIAL --repetitions 20
```

Record p50/p95 for a simple page, large tree, dialog, visible IME, overlay,
video/custom surface, disabled collector, and fresh-dump fallback. Capture
metadata includes collector fetch, per-window traversal, normalization,
screenshot/provider stages, node counts, completeness, and fallback edges.
The current collector primary-attempt timeout is 2500ms
(`TREE_PRIMARY_ATTEMPT_TIMEOUT_MS` in `driver/observation_deadline.py`), calibrated
from API-35 Xiaomi measurements (ADB ContentProvider round-trip p95 ≈ 1.1–1.34s,
window traversal below 62ms, ADB screencap below 0.36s p95; a 1500ms trial
produced a false fallback long tail).
The `4000` node and `60` depth guards are conservative safety caps; adjust them
only from these measurements, never per app or from pixel thresholds.

## Dynamic content and window transitions (0.4.4)

The collector traverses once per request. A non-blocking admission lock rejects
overlapping snapshots as `snapshot_busy`; it does not queue another traversal or
clear the cache underneath an active capture. A cooperative 2200 ms traversal
budget leaves room within the host's 2500 ms tree budget. A blocked Android
Binder call cannot be interrupted by this device-side guard; the host timeout
remains necessary.

On API 33+, version 0.4.4 fetches active-window roots and children using bounded
depth-first prefetch with ancestors/siblings and `FLAG_PREFETCH_UNINTERRUPTIBLE`.
Android limits each batch to 50 nodes. The active root is accepted only when its
window ID matches the enumerated window. The service still clears its cache once
per snapshot; this optimization does not retain trees across observations or
raise the capture deadlines. Older Android versions retain their existing APIs.

Content events remain diagnostic. Version 0.4.4 excludes only known-window events
whose change mask contains window title/accessibility focus alone, or pane title
alone. Mixed flags, zero/unknown masks, missing window IDs, pane appearance/removal,
input focus, active window, geometry, and topology changes still advance the
transition generation. API 26/27 window events keep conservative handling because
their change mask is unavailable. Health reports `filtered_window_events` for
diagnostics. The host checks window generation around acquisition and requires
at least 300 ms since the last invalidating window event.
A detected transition uses the transaction's existing single delayed resample,
within the same outer deadline, rather than delivering its provisional pixels
as coordinate-actionable evidence. If the second capture is still unsafe,
acquisition fails explicitly. Missing trees can still degrade to image-only
when the available window fence passes.

This is a mechanical window check, not a claim that HTML loading or animations
have finished. Same-window content semantics remain the model's responsibility;
`observe_screen` requests new evidence. Diagnostics record window quiet time,
snapshot attempts, window-root time, child-fetch time, and content changes.

## Start Driver

Local devices use the Driver embedded in Control API. Start a separate Driver only for a remote device host, then set the API host's `CLICKCLICK_DRIVER_URL` to that host's RPC URL:

```bash
clickclick-driver
# or: python -m driver.main
```

## Live mirror (operators)

Console Live uses the vendored standalone **scrcpy-server** jar
(`driver/vendor/scrcpy-server-v3.3.1.jar`, version pin in
`driver/scrcpy_mirror.py`) with `raw_stream=true`:

- **Local devices**: Control API pushes/forwards the jar on the API host and
  pipes H.264 to `/api/device/mirror/stream`.
- **Remote hubs**: `clickclick-driver` exposes `/mirror/stream`; the API relays
  bytes to the browser. Lab hosts must ship the same jar.
- **Browser**: Chromium WebCodecs (`VideoDecoder`). Vite dev proxy needs
  `ws: true` (already set).

Agent pixels still use scrcpy/ADB and do **not** require Console Live. Semantic
trees use the collector first and fresh `uiautomator dump` only as fallback.
Upgrade steps: see `driver/vendor/README.md`.
