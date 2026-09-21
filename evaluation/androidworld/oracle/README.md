# Independent API 33 scoring read

`OracleDump.java` runs under `app_process` using the device's UiAutomation and
native AccessibilityNodeInfoDumper. It omits the stock command's
`waitForIdle(1000, 10000)` requirement. That requirement can fail on a running
stopwatch even when the official Pause/Lap predicate is satisfied.

The command is evaluation-only, uses no Collector state, preserves other
accessibility services, clears its own connection cache, checks active root
identity/geometry and rotation before and after traversal, and emits the supplied
nonce only after successful serialization and validation. The host bounds the
process lifetime, validates XML, and removes unique temporary XML/dex files.
This is not an atomic tree snapshot; same-window content may change during reads.
The framework reflection is intentionally restricted to the tested API 33 image.

Build with `evaluation/androidworld/build_oracle.py` and `JAVA_HOME`/`ANDROID_HOME`.
Full batch preparation compiles and hashes the executable inside the new frozen
runner. No jar belongs in version control or the production Collector APK release.

Policy v4 exports interactive windows and native node JSON, converted by the host
to the official forest protobuf. Required fields used by the official converter
are validated before protobuf defaults can hide omissions. The controller builds
both State views normally; no task-specific facade or audio score override is
used. Up to three acquisition attempts tolerate brief transitions; the official
predicate itself runs once. This preserves predicate logic, but uses an adapted
transport and does not establish official benchmark acceptance.
