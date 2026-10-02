"""Provision ClickClick components on one explicitly identified MobileWorld device."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from pathlib import Path

from driver.adb import adb_bin
from driver.factory import get_driver
from shared.config import get_settings


def _device_serial(target: str) -> str:
    result = subprocess.run(
        [adb_bin(), "-s", target, "shell", "getprop", "ro.serialno"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout.strip()


async def _prepare(args: argparse.Namespace) -> dict:
    actual_serial = _device_serial(args.target)
    if actual_serial != args.expected_device_serial:
        raise RuntimeError(
            f"refusing device {args.target}: expected {args.expected_device_serial}, "
            f"got {actual_serial or '<empty>'}"
        )

    settings = get_settings().model_copy(
        update={
            "accessibility_collector_enabled": True,
            "accessibility_collector_apk_path": str(Path(args.collector_apk).resolve()),
            "ime_auto_setup": True,
            "ime_apk_path": str(Path(args.ime_apk).resolve()),
        }
    )
    driver = get_driver(settings, serial=args.target)
    try:
        reconciliation = await driver.reconcile_environment()
        readiness = await driver.readiness()
        return {
            "target": args.target,
            "device_serial": actual_serial,
            "reconciliation": reconciliation,
            "readiness": readiness,
        }
    finally:
        # This short-lived provisioning process cannot lend its warmed socket
        # to the task process. Release its owned transport before loop shutdown.
        await driver.close_observation_provider()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--expected-device-serial", required=True)
    parser.add_argument("--collector-apk", required=True)
    parser.add_argument("--ime-apk", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_prepare(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
