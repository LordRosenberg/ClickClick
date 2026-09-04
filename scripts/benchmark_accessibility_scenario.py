"""Measure one real-device accessibility observation scenario as JSON."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any

from driver import adb
from driver.android import AndroidDriver
from perception.observation import ObservationBuilder
from shared.config import Settings


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[round((len(ordered) - 1) * fraction)], 3)


def _summary(values: list[float]) -> dict[str, float | None]:
    return {
        "p50_ms": round(statistics.median(values), 3) if values else None,
        "p95_ms": _percentile(values, 0.95),
    }


async def benchmark(
    serial: str,
    label: str,
    repetitions: int,
    *,
    before_keyevent: int | None = None,
    after_keyevent: int | None = None,
) -> dict[str, Any]:
    settings = Settings(accessibility_collector_enabled=True)
    driver = AndroidDriver(
        serial=serial,
        collector_enabled=True,
        collector_timeout_ms=2000,
    )
    builder = ObservationBuilder()
    samples: list[dict[str, Any]] = []
    for _ in range(max(1, repetitions)):
        if before_keyevent is not None:
            await adb.input_keyevent_async(serial, before_keyevent)
            await asyncio.sleep(0.1)
        started = time.monotonic()
        tree, shot = await driver.get_frame()
        package = builder.build((tree, shot))
        total_ms = (time.monotonic() - started) * 1000.0
        if after_keyevent is not None:
            await adb.input_keyevent_async(serial, after_keyevent)
        capture = package.capture_meta
        frame_ms = float(capture.get("frame_capture_elapsed_ms") or 0.0)
        collector_ms = float(capture.get("collector_elapsed_ms") or 0.0)
        traversal = [float(value) for value in capture.get("window_traversal_ms") or []]
        samples.append({
            "total_ms": round(total_ms, 3),
            "collector_ms": collector_ms,
            "pixel_estimate_ms": round(max(0.0, frame_ms - collector_ms), 3),
            "normalization_ms": float(capture.get("normalization_elapsed_ms") or 0.0),
            "traversal_total_ms": round(sum(traversal), 3),
            "window_count": int(capture.get("window_count") or 0),
            "semantic_nodes": int(capture.get("semantic_node_count") or 0),
            "actionable_nodes": int(capture.get("actionable_node_count") or 0),
            "complete": bool(capture.get("complete", True)),
            "reasons": list(capture.get("reasons") or []),
            "mode": package.mode.value,
        })
    metrics = {}
    for key in (
        "total_ms", "collector_ms", "pixel_estimate_ms",
        "normalization_ms", "traversal_total_ms",
    ):
        metrics[key] = _summary([float(sample[key]) for sample in samples])
    return {
        "serial": serial,
        "scenario": label,
        "repetitions": len(samples),
        "metrics": metrics,
        "complete_samples": sum(bool(sample["complete"]) for sample in samples),
        "window_counts": sorted({int(sample["window_count"]) for sample in samples}),
        "semantic_node_range": [
            min(int(sample["semantic_nodes"]) for sample in samples),
            max(int(sample["semantic_nodes"]) for sample in samples),
        ],
        "actionable_node_range": [
            min(int(sample["actionable_nodes"]) for sample in samples),
            max(int(sample["actionable_nodes"]) for sample in samples),
        ],
        "samples": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--before-keyevent", type=int)
    parser.add_argument("--after-keyevent", type=int)
    args = parser.parse_args()
    result = asyncio.run(benchmark(
        args.serial,
        args.label,
        args.repetitions,
        before_keyevent=args.before_keyevent,
        after_keyevent=args.after_keyevent,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
