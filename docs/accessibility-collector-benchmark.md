# Accessibility Collector Real-Device Benchmark

> **时效说明**：本文是 2026-08-23 的测量记录，数据本身仍有效；但其中的标定结论已被后续
> 测量更新——当前 collector 主超时为 **2500ms**（`TREE_PRIMARY_ATTEMPT_TIMEOUT_MS`），
> 观测总 deadline 为 current **12000ms** / temporal **8000ms**，均集中在
> `driver/observation_deadline.py`。文中的 2000ms 等数值为当时的决策依据，请勿直接引用为现状。

Measured on 2026-08-23 with Xiaomi `REDACTED_MODEL`, Android 15 / API 35,
serial `REDACTED_DEVICE`, collector `0.2.0`, 1200×2670 pixels. Each scenario uses
10 current collector snapshot + ADB screencap + normalization samples unless
noted otherwise. No OCR or CV path ran.

| Scenario | Complete | Windows | Semantic nodes | Total p50/p95 | Collector p50/p95 | Traversal p50/p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Simple calculator | 10/10 | 3 | 46–149 | 1729 / 1886 ms | 1138 / 1336 ms | 23 / 61 ms |
| Large developer-settings tree, calibrated | 10/10 | 3 | 102 | 1671 / 1941 ms | 1173 / 1402 ms | 26 / 157 ms |
| System volume dialog | 10/10 | 4 | 156 | 1928 / 2084 ms | 1308 / 1429 ms | 62 / 82 ms |
| Settings search + visible Baidu IME | 10/10 | 4 | 90 | 1805 / 1922 ms | 1216 / 1250 ms | 1 / 29 ms |
| Notification-shade overlay | 10/10 | 1 | 107 | 2050 / 2141 ms | 1173 / 1200 ms | 25 / 43 ms |
| Local recents custom/composited surface | 10/10 | 3 | 146 | 1919 / 2012 ms | 1157 / 1224 ms | 2 / 71 ms |

The custom-surface case uses the local recents compositor instead of starting
a network/account-backed video app. It exercises the same sparse/custom
surface boundary without exposing an account session.

## Read-tool and failure results

- Collector-primary `observe_screen`: current p50/p95 1960/2052ms and temporal
  4361/4462ms over five samples; all returned complete evidence.
- Raw single-root dump on this device succeeded in 8/10 samples with an
  approximate 2510ms median and 4735ms p95; two samples exceeded 5 seconds.
  It cannot fit the 2500ms current-observation deadline reliably.
- Before screenshot-only degradation, a 1400ms dump cap caused every current
  and temporal fallback request to time out.
- After the fix, collector-disabled/dump-timeout current returned a current
  screenshot in 10/10 samples at 2151/2278ms p50/p95. A three-sample temporal
  smoke retained usable current evidence at 1901/1939ms rather than failing
  the whole tool call.

## Calibration decisions

- Use a 2000ms collector timeout. A 1500ms trial had one false fallback and a
  13.5s legacy Frame-Gate long tail; 2000ms produced 10/10 complete large-tree
  samples while leaving enough of the 2500ms deadline for the measured
  screenshot and normalization stages.
- Keep the collector node/depth safety guards at 4000/60. The largest measured
  normalized tree had 156 nodes; no guard fired, normalization p95 stayed
  below 8ms, and traversal p95 stayed below 160ms. The measurements provide
  no reason to add per-app or pixel thresholds or to tighten guards near
  ordinary UI sizes.
- Xiaomi/HyperOS sideloaded services require the operator to choose **Allow
  restricted settings**. Merely writing `enabled_accessibility_services`
  binds the service but can leave the active application root unavailable.
  Initialization now detects the AppOp and reports `operator_action_required`.

## Representative task smoke

- Re-running device initialization returned every step `ready`, preserved
  `com.baidu.input_mi/.ImeService` as the default IME, and did not duplicate
  the collector service.
- Driver grounding resolved calculator label `1` to accessibility index 19 in
  window 4188, dispatched the indexed tap successfully, observed `1` in the
  replacement snapshot, then cleared the input successfully.
- Dialog, IME, overlay, and custom-surface samples remained tree-only because
  their aggregate snapshots were complete and usable.

Run `scripts/benchmark_accessibility_scenario.py` to repeat a labeled surface.
Run `scripts/measure_observation_latency.py` for current/temporal read-tool
coverage and the collector-disabled fallback path.

## Persistent channel result

The 0.2.0 collector replaces per-snapshot `adb shell content query` transport
with one authenticated, length-prefixed local socket forwarded once per device.
The permission-protected health endpoint is used only to bootstrap a random
socket name and token. The service traverses the current windows on demand;
it does not retain AccessibilityEvent content or stale trees.

On the same device, a 20-sample warm run measured tree-only p50/p95 at
**36.7/41.3 ms**, including transport, traversal, JSON serialization, and host
decode. Traversal p95 was 3.5 ms after the first sample and host decode stayed
below 0.15 ms. Historical compatibility samples through the removed
ContentProvider path measured 1208/1230 ms. The first process bootstrap measured 1724 ms, so
device initialization now establishes and verifies the channel before a task
starts; subsequent observations remain on the warm connection.

Current production capture performs one Tree route per complete observation:
one persistent collector request when collector mode is enabled, otherwise one
fresh `uiautomator dump`. A Tree failure returns to the outer transaction,
which may wait once and reacquire one complete Tree+pixel package. It does not
chain reconnect, ContentProvider, and dump fallbacks inside one capture.

Run `.venv/bin/python scripts/measure_accessibility_attempts.py --serial SERIAL`
to measure the current persistent, fresh-dump, screencap, and identity routes
independently.
