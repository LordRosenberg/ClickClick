"""Authorized emulator probe: inject host faults, use real sockets/ADB files."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import time
from unittest.mock import patch

from driver import adb
from driver.accessibility import AccessibilitySnapshotChannel, DEFAULT_AUTHORITY


async def run(args):
    report = {"transport": [], "dump": []}
    real_remove = adb.remove_forward_async
    channel = AccessibilitySnapshotChannel(args.serial, DEFAULT_AUTHORITY)
    try:
        first = await channel.snapshot()
        report["initial_complete"] = first.complete
        for i in range(5):
            old_port = channel._port
            old_generation = channel.connection_generation
            calls = 0

            async def fail_once(serial, port, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise adb.AdbError("probe: transient forward removal failure")
                return await real_remove(serial, port, **kwargs)

            channel._writer.transport.abort()
            await asyncio.sleep(0)
            with patch.object(adb, "remove_forward_async", fail_once):
                failed = ""
                try:
                    await channel.snapshot()
                except Exception as exc:
                    failed = str(exc)[:200]
                tick = time.monotonic()
                recovered = await channel.snapshot()
                recovery_ms = round((time.monotonic()-tick)*1000, 2)
            ports = await adb.list_forwards_to_localabstract_async(args.serial, channel._forward_socket_name)
            report["transport"].append({"iteration": i, "injected_failure": failed,
                "recovered_complete": recovered.complete, "recovery_ms": recovery_ms,
                "generation_before": old_generation, "generation_after": channel.connection_generation,
                "old_port_removed": old_port not in ports, "remove_calls": calls,
                "cleanup_failed": channel.diagnostics()["cleanup_failed"]})
    finally:
        await channel.close()

    for mode in ("async", "sync"):
        tick = time.monotonic()
        xml = (await adb.uiautomator_dump_async(args.serial, max_attempts=1, timeout=8)
               if mode == "async" else await asyncio.to_thread(adb.uiautomator_dump, args.serial, max_attempts=1))
        report["dump"].append({"mode": mode, "chars": len(xml), "elapsed_ms": round((time.monotonic()-tick)*1000, 2)})

    prefix = f"/sdcard/clickclick_probe_{secrets.token_hex(8)}.xml"
    fixture = args.output.parent / "stale-fixture.xml"
    fixture.write_text('<hierarchy><node text="STALE_SENTINEL" padding="' + 'x'*200 + '"/></hierarchy>', encoding="utf-8")
    await adb.push_file_async(args.serial, fixture, prefix)
    real_run = adb._run_async
    attempted_paths = []

    async def null_root(command, **kwargs):
        if "uiautomator" in command:
            attempted_paths.append(command[-1])
            return b"ERROR: null root node returned by UiTestAutomationBridge."
        if "rm" in command:
            raise adb.AdbError("probe: cleanup failed")
        return await real_run(command, **kwargs)

    try:
        with patch.object(adb, "_DEVICE_DUMP_PATH", prefix), patch.object(adb, "_run_async", null_root):
            try:
                await adb.uiautomator_dump_async(args.serial, max_attempts=2, timeout=5)
                report["stale_xml"] = {"rejected": False}
            except adb.AdbError as exc:
                report["stale_xml"] = {"rejected": True, "reason": str(exc)[:300],
                    "distinct_paths": len(set(attempted_paths)) == 2,
                    "old_path_never_read": prefix not in attempted_paths}
    finally:
        for owned in [prefix, *attempted_paths]:
            await adb.shell_async(args.serial, ["rm", "-f", owned], timeout=2)
    report["forwards_after"] = (await adb._run_async([adb.adb_bin(), "-s", args.serial, "forward", "--list"], timeout=2)).decode()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
