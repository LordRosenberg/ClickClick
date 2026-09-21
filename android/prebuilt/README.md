# Collector APK distribution

APK binaries are no longer committed here. Distribution uses GitHub Releases;
local development still uses freshly built APKs.

Resolution order:

1. `CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH` (explicit local file).
2. `android/accessibility-collector/app/build/outputs/apk/debug/app-debug.apk`
   when Gradle metadata matches `driver/collector_release.py` version.
3. The exact Release tag and asset pinned in `driver/collector_release.py`,
   verified by SHA-256 and cached under ignored `data/device-apks`.

For local/explicit APKs, initialization compares the installed APK digest even
when `versionName` is unchanged. A rebuilt APK is installed when different;
a Release cannot override a matching local build. Stale-version automatic build
outputs are skipped; bump the host version when bumping Gradle version.

The default Release repository is `LordRosenberg/ClickClick`; override it with
`CLICKCLICK_COLLECTOR_RELEASE_REPO`. Private repositories require `GH_TOKEN` or
`GITHUB_TOKEN` in the process environment. `CLICKCLICK_DEVICE_APK_CACHE` overrides
the cache directory. Authentication/download/hash failures are reported; there
is no fallback to an older release.

## Install or build

Download the pinned Collector APK from [GitHub Releases](https://github.com/LordRosenberg/ClickClick/releases), or build `android/accessibility-collector` with Gradle. APKs are distributed as Release assets rather than committed to the source tree.

Bootstrap uses the same version and digest checks:

```bash
bash scripts/bootstrap-device.sh YOUR_ADB_SERIAL
```

ADBKeyboard is a separate third-party input method; its source and license are linked in the Release notes. Source commits and APK assets are versioned separately.
