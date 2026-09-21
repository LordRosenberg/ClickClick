# Vendored scrcpy-server

Pinned standalone server jar used by Console Live mirror
(`driver/scrcpy_mirror.py`) on the Control API host and on
`clickclick-driver` lab hubs.

| Constant | Value |
| --- | --- |
| Version | `3.3.1` |
| File | `scrcpy-server-v3.3.1.jar` |
| Upstream | [Genymobile/scrcpy v3.3.1](https://github.com/Genymobile/scrcpy/releases/tag/v3.3.1) |

## Upgrade steps

1. Download the matching server asset from a new scrcpy release  
   (`scrcpy-server-vX.Y.Z` → save as `driver/vendor/scrcpy-server-vX.Y.Z.jar`).
2. Update `SCRCPY_SERVER_VERSION` / `SCRCPY_SERVER_JAR_NAME` in
   `driver/scrcpy_mirror.py` to the new version string (must match the
   first argv passed to `com.genymobile.scrcpy.Server`).
3. Remove the old jar from this directory.
4. Smoke-test Live on one local device and one remote-hub device.

Do **not** mix client desktop `scrcpy` versions with this jar for the
Console path — Live uses the standalone server only. Local transport enables
`send_frame_meta` and consumes the dummy handshake byte; the host removes the
12-byte packet headers before fan-out to H.264 consumers.
