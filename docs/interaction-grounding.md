# Interaction and visual grounding

## Evidence boundaries

The Android collector keeps at most 256 safe interaction-event records and
evicts records older than 15 seconds. Each record contains only monotonic
sequence/time, event type, package/window identity, source class/resource id,
bounds, and boolean structural flags. It never retains Android event/node
objects, text, content descriptions, hints, editable values, value-derived
lengths, or password data. The host decoder rejects unknown event fields so a
future collector cannot silently add content to this channel.

`interaction_ack` is mechanical evidence only:

- `confirmed`: one supported post-boundary event matched the captured target.
- `unobserved`: retained coverage was complete but contained no match. This is
  not an action-failure verdict.
- `unavailable`: the action family/target/channel was unsupported, the cursor
  was evicted, or transport degraded. This carries no verdict.

Dispatch, acknowledgement, observation equality, and task semantics remain
independent. In particular, `confirmed` may coexist with
`visible_change=none`.

## Coverage probe

Build/install the collector, then run one operator-labelled window per surface:

```bash
python scripts/probe_accessibility_events.py \
  --serial SERIAL --label native-indexed-tap --window-seconds 3
```

Repeat for indexed tap, long press, scroll, text focus, native controls, Chrome
WebView buttons, and custom drawing surfaces. The JSON output is diagnostic
only and is not placed in model input. Record event coverage, unrelated and
duplicate counts, stable/changed bounds, and handled interactions that emit no
usable event. Do not enable model-facing acknowledgement until the reference
device matrix and regression gates pass.

### Reference-device evidence (2026-09-11)

Collector 0.4.0 was installed on reference device `20c89e2f` (Android API 35)
and probed with ordinary adb input, not accessibility `performAction`:

| Surface/action | Safe events observed | Usable action ACK | Notes |
|---|---|---|---|
| Android Settings row tap | click plus transition scroll | yes | click source bounds matched the row |
| Android Settings repeated scroll (12 bursts) | scroll in 12/12 bursts | yes for scroll | no retained content fields |
| Chrome URL-bar tap/text focus | click, selected, focused, suggestion-list scroll | click may match; focus is not click evidence | one action produced four records; animated bounds moved by up to 31 px |
| Chrome URL-bar long press | long-click plus popup focus | yes | popup focus is unrelated noise |
| Chrome native menu/refresh | scroll, then focused/selected | no click ACK in these samples | handled interaction can lack `TYPE_VIEW_CLICKED` |
| Chrome WebView palette buttons | click appeared in end-to-end receipts, but one direct sample emitted none | intermittent | absence remains neutral |
| Chrome custom drawing surface drag | WebView focus only | no | drawing succeeded visually, with no usable drag event |

The highest observed noise ratio was three unrelated/non-action-family records
beside one useful URL-bar click. No duplicate target/action-family event was
observed in these samples. Native row bounds were stable; Chrome toolbar bounds
moved during focus animation. This coverage is useful but not universal, so
`unobserved` must remain non-failure and model-facing ACK remains disabled by
default.

## Performance gate

Use bursts up to the retained count limit and compare warm snapshot latency
before/after event collection. The callback performs one bounded metadata copy
and constant-time append/eviction under a small private lock; it performs no
Tree traversal or serialization. The release gate requires the existing warm
snapshot latency target to remain satisfied. Current provisional bounds are
256 events, 15 seconds of age, 128 events per read, and one post-dispatch read.

On the same device, 20 warm samples measured snapshot median/p95/max at
18.81/21.17/21.52 ms. A cursor plus empty bounded read measured
2.93/3.86/6.38 ms. Twelve alternating Settings scroll bursts were all visible;
post-adb event availability was 2.98 ms median, 4.60 ms near-p95, with one
73.15 ms maximum. The callback performs no snapshot traversal, and these
figures keep the existing warm-snapshot latency target intact.

Selective bounds add zero text to ordinary non-coordinate Executor turns. A
drawing turn adds bounds only to the selected semantic surface (one canvas row
in BrowserDraw); generic Chrome `ImageView` nodes, toolbar hairlines and icons
are excluded. This avoids activating any CV small-region proposal path.

### End-to-end evidence

- BrowserMultiply seed 20260908 completed in 10 steps with ordered values
  `5, 10, 2, 9, 10`, product `9000`, and final `Success!`. Equal values remained
  separate note occurrences; each advancing click was dispatched once.
- BrowserDraw completed in 8 steps. Every stroke stayed inside the current
  canvas `image_bounds=[11,215,476,614]`, clean-pixel region inspection was used,
  and submission returned `Success!`. The workflow now forbids choosing an
  unmeasured exact-match swatch after a candidate batch misses.

## Independent rollback switches

- `CLICKCLICK_INTERACTION_EVENT_READS_ENABLED=0` disables host cursor/read and
  correlation while leaving snapshot capture intact.
- `CLICKCLICK_INTERACTION_ACK_MODEL_VISIBLE=0` (the default until coverage gates
  pass) retains canonical/trace facts but removes acknowledgement from model
  history.
- `CLICKCLICK_SURFACE_BOUNDS_ENABLED=0` removes selective current
  `image_bounds` projection.
- `CLICKCLICK_IMAGE_REGION_INSPECTION_ENABLED=0` makes the clean-pixel read tool
  unavailable without affecting device actions or observations.

To disable collection itself, deploy the prior collector APK or remove the new
event types from its service configuration; the host remains compatible with
that legacy collector.
