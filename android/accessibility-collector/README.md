# ClickClick Accessibility Collector

Minimal device-side APK for current multi-window accessibility snapshots.
It exposes read-only `snapshot` and `health` ContentProvider queries and no
device-action API.

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
