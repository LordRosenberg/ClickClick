# ClickClick Accessibility Collector

Minimal device-side APK for current multi-window accessibility snapshots and a
short-lived, privacy-minimized interaction-event metadata ring. It exposes
read-only `snapshot` and `health` ContentProvider queries. The development build
also supports authenticated, single-use original-node `ACTION_CLICK` requests
on its local socket; see [scope and validation](../../docs/native-node-click-20260920.md).
Its authenticated local socket also owns a renewable,
task-scoped screen-bright WakeLock lease; this changes power state but never
injects a key or touch event.

On Android 13+, each snapshot clears the service's accessibility cache before
reading windows. This avoids retaining bounds from earlier window animations
without disabling caching within the traversal. `cache_cleared` records the
platform result; false makes the capture incomplete. Older platforms and older
collector clients retain their existing behavior, with no cache-clear claim.

Collector 0.4.4 uses Android 13's bounded, uninterruptible depth-first prefetch
for active-window roots and child reads (at most 50 nodes per platform batch).
Active roots must match the window ID from the windows query; other windows use
their own roots. Per-snapshot cache invalidation, single-flight admission and
the 2200 ms traversal budget remain unchanged. API 26–32 retain the original
node retrieval APIs. This reduces repeated IPC waits on updating screens without
reusing a previous observation's tree or increasing the host deadline.

Version 0.4.4 excludes known-window title/accessibility-focus-only events and
pane-title-only state events from the transition fence. Mixed or unknown flags,
missing window identity, input focus, active window, pane visibility, geometry
and topology changes remain conservative. API 26/27 window events cannot supply
subtypes and keep the original invalidation behavior. The health response counts
`filtered_window_events`; filtering never suppresses ordinary content diagnostics
or per-snapshot cache clearing. This is not a same-window layout stability proof.

Event reads are additive on that authenticated socket. Records are bounded by
count and age and never retain text, descriptions, hints, editable values,
password data, or Android event objects. See `docs/interaction-grounding.md`.

The lease is not tied to ADB or AccessibilityService lifetime. The host
acquires it when a task execution starts, renews it every 30 seconds, and
releases it in task cleanup. Collector enforces a 90-second lease TTL (maximum
accepted TTL: 120 seconds), while each Android WakeLock has an independent
platform timeout. Therefore a host crash, ADB loss, or missed release cannot
leave the screen permanently awake.

Build with Android Studio or Gradle, then install and enable through the
ClickClick device-environment initialization endpoint. Manual fallback:

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell settings put secure enabled_accessibility_services \
  ai.clickclick.collector/.CollectorService
adb shell settings put secure accessibility_enabled 1
adb shell content query --uri content://ai.clickclick.collector/health
```

When other accessibility services are enabled, do not use the literal
`settings put` example above: merge the collector component into the existing
colon-separated value. ClickClick initialization performs that merge.

After setup, run the device/API smoke and latency matrix documented in
`docs/accessibility-collector-setup.md` (collector-primary is now the default;
the matrix validates each new device model).

The [observation guide](../../docs/scrcpy-observation.md) describes timing,
source selection and known limits. Keep the Gradle version and
`driver/collector_release.py` version/digest pins in sync for a release.
Local builds take precedence over downloaded Releases.
