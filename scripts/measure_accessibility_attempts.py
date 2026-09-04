"""Measure independent Android tree-provider attempts without model calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any, Awaitable, Callable

from driver import adb
from driver.accessibility import AccessibilityCollectorClient, CHANNEL_REGISTRY


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[round((len(ordered) - 1) * fraction)], 3)


async def _measure(
    repetitions: int,
    operation: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for _ in range(max(1, repetitions)):
        started = time.monotonic()
        try:
            value = await operation()
            samples.append({
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "ok": True,
                "provider": str(getattr(value, "provider", "")),
                "complete": bool(getattr(value, "complete", True)),
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001 - failures are measured output
            samples.append({
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "ok": False,
                "provider": "",
                "complete": False,
                "error": str(exc)[:240],
            })
    successful = [float(row["elapsed_ms"]) for row in samples if row["ok"]]
    return {
        "count": len(samples),
        "successes": len(successful),
        "p50_ms": round(statistics.median(successful), 3) if successful else None,
        "p95_ms": _percentile(successful, 0.95),
        "p99_ms": _percentile(successful, 0.99),
        "samples": samples,
    }


async def run(
    serial: str,
    repetitions: int,
    persistent_timeout_s: float,
    dump_timeout_s: float,
    screencap_timeout_s: float,
    only: str,
) -> dict[str, Any]:
    client = AccessibilityCollectorClient(serial)
    channel = client._channel  # noqa: SLF001
    if channel is None:
        raise RuntimeError("persistent channel is unavailable")
    try:
        operations: dict[str, Callable[[], Awaitable[Any]]] = {
            "persistent": lambda: channel.snapshot(timeout=persistent_timeout_s),
            "fresh_dump": lambda: adb.uiautomator_dump_async(
                serial, max_attempts=1, timeout=dump_timeout_s
            ),
            "screencap": lambda: adb.screencap_async(
                serial, timeout=screencap_timeout_s
            ),
            "identity": lambda: adb.current_activity_async(serial),
        }
        selected = operations if only == "all" else {only: operations[only]}
        result: dict[str, Any] = {
            "serial": serial,
            "only": only,
            "configured_timeouts_s": {
                "persistent": persistent_timeout_s,
                "fresh_dump": dump_timeout_s,
                "screencap": screencap_timeout_s,
                "identity": dump_timeout_s,
            },
        }
        for name, operation in selected.items():
            result[name] = await _measure(repetitions, operation)
        return result
    finally:
        await CHANNEL_REGISTRY.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--persistent-timeout-s", type=float, default=3.0)
    parser.add_argument("--dump-timeout-s", type=float, default=5.0)
    parser.add_argument("--screencap-timeout-s", type=float, default=5.0)
    parser.add_argument(
        "--only",
        choices=(
            "all",
            "persistent",
            "fresh_dump",
            "screencap",
            "identity",
        ),
        default="all",
    )
    args = parser.parse_args()
    result = asyncio.run(run(
        args.serial,
        args.repetitions,
        args.persistent_timeout_s,
        args.dump_timeout_s,
        args.screencap_timeout_s,
        args.only,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
