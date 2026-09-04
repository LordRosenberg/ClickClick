# Android Accessibility Collector Setup

ClickClick uses a thin, read-only Android AccessibilityService collector for
current multi-window trees. Actions remain ADB commands and pixels remain
scrcpy/ADB screenshots; this is not a general device Portal.

## Prerequisites

1. Enable **Developer options** and **USB debugging** on the phone
2. Connect via USB or `adb connect <ip:port>`
3. Confirm: `adb devices` shows one or more authorized devices (`device` state)
4. Build `android/accessibility-collector/` with Android Studio or Gradle
5. Configure `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH`
6. Call `POST /api/devices/{serial}/initialize`

The collector is **enabled by default** (`CLICKCLICK_ACCESSIBILITY_COLLECTOR_ENABLED=true`
in `shared/config.py`); no extra switch is needed after initialization.

## Environment

```bash
export CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH=android/accessibility-collector/app/build/outputs/apk/debug/app-debug.apk
export CLICKCLICK_DRIVER_URL=http://127.0.0.1:8765
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

## Start Driver

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
