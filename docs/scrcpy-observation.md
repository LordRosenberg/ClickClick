# scrcpy observation provider

Console and agent consumers share a single per-device scrcpy source through
explicit leases.  The source parses Annex-B H.264 and new/slow consumers begin
only at SPS/PPS + IDR boundaries.  A source restart increments its generation;
temporal queries never silently cross generations.

Local transport retains scrcpy's frame-length headers until a complete encoded
packet has arrived. Annex-B parsing then releases its final NAL immediately,
including on static pages; arbitrary TCP fragments still use incremental
parsing. The dummy handshake byte is consumed before connecting control, so
startup does not wait for video bytes while the server waits for control.

Host decode is the primary pixel source for current, temporal, baseline-frame,
and direct screenshot requests. Install the `decode` extra
(`pip install clickclick[decode]`) to provide PyAV (LGPL/GPL obligations depend
on the wheel/build you choose); macOS and Linux wheels are published by PyAV.
If PyAV is unavailable, diagnostics report the provider unavailable, Console
encoded mirroring continues, and pixel requests use the bounded ADB path.

The pinned scrcpy server (3.3.1) supports the official `RESET_VIDEO` control
message (type 17) on the control socket, which restarts capture/encoding and
forces a fresh keyframe. Before each real pixel capture the provider issues
`RESET_VIDEO` under its decoder-consumption lock. On success it clears retained
observation pixels, resets decoder prediction state, and waits up to 800 ms
(`SCRCPY_ROUND_REFRESH_TIMEOUT_MS`) for a later monotonic `frame_id`. Queued
predictive frames cannot cross this decode barrier; publication resumes from a
subsequent decodable keyframe. A reset does not reconnect the source, so it does
not fabricate a new source generation. Temporal history frames are captured
without per-frame resets. When a relayed hub connection has no control socket,
or reset fails, existing generation/frame-id validation remains in force and
the bounded caller may fall back to ADB.

The stream-backed path completes the ending UI Tree before selecting pixels.
After an action, decoded frame identity—not selection time or pixel content—is
the causal boundary.

## Reliability and diagnostics

Provider order is fixed rather than configurable: every Android pixel request
tries an eligible decoded frame from the shared scrcpy ring, then uses bounded
ADB screencap when the dependency, provider, or requested frame is unavailable.
Driver health includes effective dependency availability, health, selected
source, session generation, source/consumer liveness, ring frame count, visual
age, action-boundary validation, and fallback reason per device.

Before an effectful action boundary, current-frame health is independent of visual age. scrcpy emits
new video when pixels change; a static screen can leave the latest decoded
frame unchanged for seconds while the source remains fully healthy. Current
selection therefore requires a live source and consumer, healthy decoder,
existing frame, and matching generation. The latest frame remains eligible
under those conditions and is labeled `live_static_reuse` once visually old.
There is no two-identical-frame gate, so a single post-transition frame that
then becomes static is selectable.

Immediately before the actual ADB command, Android snapshots the provider
generation and latest decoded `frame_id`; it commits that boundary only when
dispatch succeeds. The next selection requires a greater frame id in the same
generation and labels it `post_action_frame`. If Tree collection finishes
before such a frame exists, current capture awaits the existing decoder ring.
A healthy stream that does not advance returns bounded unknown/timeout evidence
rather than relabeling an old frame or using ADB as a synchronization step.
Failed or non-dispatched actions do not commit a boundary.

The host decoder receives every ordered H.264 access unit. Its FPS cap applies
only when publishing complete decoded images into the ring; dropping compressed
input would break reference continuity and can leave a live connection with an
old image. An isolated libavcodec packet rejection is recorded without resetting
the mid-stream codec context; the next accepted packet can recover publication.
Decoder reset is reserved for a successful `RESET_VIDEO`, which deliberately
requests the SPS/PPS+IDR bootstrap needed to recover. Ring artifacts use fast
lossless PNG. The shared device stream uses
a fixed 1440 maximum dimension and 8 Mbps video bitrate; no runtime settings are
added for these production rules.

Each decoded frame derives its stream dimensions from the image and device
logical dimensions from the connected display. That geometry is carried into
ObservationBuilder: Accessibility bounds are scaled into SoM image pixels, and
attached-image coordinate actions are transformed back into device pixels.

Agent and Console acquire separate consumer leases on the same device-keyed
session/ring. The last release starts the bounded idle timer rather than
immediately killing the server. A failed/restarted source advances generation,
so temporal packages and invocation-local retry suppression cannot silently
cross a recovery boundary.

Current requests use a fixed 12000 ms end-to-end deadline; temporal requests
use 8000 ms (`CURRENT_DEADLINE_MS` / `TEMPORAL_DEADLINE_MS` in
`driver/observation_deadline.py`). The collector primary attempt has its own
2500 ms cap (`TREE_PRIMARY_ATTEMPT_TIMEOUT_MS`), and the post-`RESET_VIDEO`
ring refresh waits at most 800 ms (`SCRCPY_ROUND_REFRESH_TIMEOUT_MS`). UI dump,
screencap, provider wait, decode/alignment admission, and ADB process cleanup
share the enclosing monotonic deadline. The headroom over the measured
collector p95 (≈1.1–1.34 s) absorbs slow first-capture and bounded-resample
paths without turning transient slowness into task-level failure.

With collector 0.2.0 and persistent tree transport, the old visual-age rule
selected scrcpy in only 1/10 static samples and produced 1772/1913ms p50/p95
current latency because nine samples invoked ADB screencap. The subsequent
static-reuse experiment selected scrcpy in 10/10 samples and measured
127/138ms p50/p95, but later replay found it could return a 26.7-second
pre-action image after navigation. Those numbers are historical performance
evidence, not a correctness result; post-repair benchmarks must also prove the
selected frame follows the action boundary and matches the Tree.

The repaired path on device `REDACTED_DEVICE` measured 105.95/114.80ms static-current
p50/p95 (10/10 scrcpy), 140.44/178.08ms under a 30-sample shared Console+Agent
post-action stress run, and 202.12/297.63ms while alternating Settings and Home
across real navigation boundaries. All navigation samples were post-boundary,
had the expected alternating Accessibility app identity and visual class, and
reported no decode error or ADB fallback. Six temporal samples completed at
134.07/138.20ms with no timeout. A controlled scrcpy failure used ADB 5/5 times
at 1881.99/1954.52ms p50/p95, within the current deadline.

Run the read-only device latency probe from the repository root:

```bash
.venv/bin/python -m scripts.measure_observation_latency \
  --serial <adb-serial> --mode current --repetitions 20 \
  > /tmp/observation-latency.json
```

When measurements justify recalibration, update the centralized constants above
the measured p95 plus operational headroom and verify the parent safety guard.
Add `--before-action home` or `--before-action back` to probe action boundaries.
Use `--before-action alternate --shared-consumer` to alternate Settings/Home
while a second Console-like subscriber drains the same source. Use
`--force-scrcpy-failure` only as a diagnostic fault injection for the bounded
ADB path.
The output records provider, generation, stage timings, validation class,
visual age, scrcpy selection count, ADB fallback count, and fallback edges so
stream and ADB samples are not mixed accidentally.

## Runtime fallback

Monitor per-device dependency availability, source/consumer health, generation,
visual age, validation, latency, and fallback edges. Current and direct
screenshot requests select the newest valid ring frame; temporal requests
sample only genuinely captured ordered frames and never clone static reuse.
Missing initial frames, source/consumer death, decoder errors, or generation
mismatch cross an explicit fallback edge to the calibrated bounded ADB path.
The absence of a later post-action frame while scrcpy remains healthy stays on
the bounded scrcpy wait and never falls back solely to force alignment.
Changing provider order requires a code change rather than another persistent
policy switch.
