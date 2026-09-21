"""Real-device observation probe; reports failures as failures, no model calls."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

from agent.action_observation import ActionObservationTransaction
from driver import adb
from driver.factory import get_driver
from driver.scrcpy_mirror import REGISTRY
from perception.observation import ObservationBuilder
from shared.config import get_settings
from shared.schemas import Action


def save_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def run(args):
    driver = get_driver(get_settings(), serial=args.serial)
    transaction = ActionObservationTransaction(driver, ObservationBuilder())
    rows = []
    started = time.monotonic()
    try:
        await driver.warm_observation_provider()
        previous = await transaction.observe_current(attach_image=True) if args.alternate or args.apps else None
        for i in range(args.count):
            tick = time.monotonic()
            try:
                if args.apps and i % args.switch_every == 0:
                    app = args.apps[(i // args.switch_every) % len(args.apps)]
                    if args.cold_start:
                        await adb.shell_async(args.serial, ["am", "force-stop", app])
                    receipt, package = await transaction.act_and_observe(Action(type="launch", app=app), previous)
                elif args.alternate:
                    action = (Action(type="launch", app="com.android.settings")
                              if i % 2 == 0 else Action(type="home"))
                    receipt, package = await transaction.act_and_observe(action, previous)
                else:
                    receipt = None
                    package = await transaction.observe_current(attach_image=True, source="recovery_probe")
                previous = package
                meta = package.capture_meta
                row = {"i": i, "accepted": package.accepted, "app": package.ui.app_id,
                       "elapsed_ms": round((time.monotonic()-tick)*1000, 2),
                       "elements": len(package.ui.elements), "meta": meta}
                if receipt is not None:
                    row["action_success"] = receipt.success
            except Exception as exc:
                row = {"i": i, "accepted": False, "error": str(exc)[:500],
                       "elapsed_ms": round((time.monotonic()-tick)*1000, 2)}
            rows.append(row)
            # Preserve progress even when the probe or device is interrupted.
            save_report(args.output, {"samples": rows})
            if args.interval:
                await asyncio.sleep(args.interval)
    finally:
        await driver.close_observation_provider()
        await REGISTRY.shutdown()
    times = sorted(r["elapsed_ms"] for r in rows)
    report = {"count": len(rows), "accepted": sum(bool(r["accepted"]) for r in rows),
              "duration_s": round(time.monotonic()-started, 2),
              "p50_ms": statistics.median(times), "p95_ms": times[round((len(times)-1)*.95)],
              "samples": rows}
    save_report(args.output, report)
    print(json.dumps({k:v for k,v in report.items() if k != "samples"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--alternate", action="store_true")
    modes.add_argument("--apps", nargs="+")
    parser.add_argument("--switch-every", type=int, default=10)
    parser.add_argument("--cold-start", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
