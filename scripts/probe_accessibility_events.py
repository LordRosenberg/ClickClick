#!/usr/bin/env python3
"""Operator probe for safe accessibility-event coverage; never enters model context."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
import sys
import time

from driver.accessibility import AccessibilityCollectorClient


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--label", required=True, help="e.g. native-indexed-tap")
    parser.add_argument("--window-seconds", type=float, default=3.0)
    parser.add_argument("--limit", type=int, default=128)
    args = parser.parse_args()
    client = AccessibilityCollectorClient(args.serial)
    # A standalone probe starts from a cold adb forward/channel; give bootstrap
    # enough time so transport startup is not reported as missing capability.
    cursor = await client.event_cursor(timeout=3.0)
    if cursor is None:
        print(json.dumps({"label": args.label, "status": "event_channel_unavailable"}))
        return 2
    print(
        f"Perform {args.label!r} now; recording safe metadata for "
        f"{args.window_seconds:.1f}s...",
        file=sys.stderr,
        flush=True,
    )
    started = time.monotonic()
    await asyncio.sleep(max(0.1, args.window_seconds))
    batch = await client.events_after(cursor, limit=args.limit, timeout=2.0)
    elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
    if batch is None:
        print(json.dumps({"label": args.label, "status": "event_read_unavailable"}))
        return 2
    rows = [asdict(event) for event in batch.events]
    print(json.dumps({
        "label": args.label,
        "status": "ok",
        "elapsed_ms": elapsed_ms,
        "cursor": cursor,
        "oldest_sequence": batch.oldest_sequence,
        "current_sequence": batch.current_sequence,
        "complete_coverage": batch.complete_coverage,
        "event_count": len(rows),
        "events": rows,
    }, ensure_ascii=False, separators=(",", ":")))
    await client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
