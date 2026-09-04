"""Bounded, zero-model real-device checks for the collector socket lifecycle."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from driver import adb
from driver.accessibility import (
    AccessibilityChannelBootstrap,
    AccessibilitySnapshotChannel,
)


COLLECTOR_SERVICE = "ai.clickclick.collector/.CollectorService"


def _exception_chain(error: BaseException) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({
            "module": type(current).__module__,
            "type": type(current).__name__,
            "message": str(current),
        })
        current = current.__cause__ or current.__context__
    return chain


def _is_transport_closed(error: BaseException | None) -> bool:
    accepted = (
        asyncio.IncompleteReadError,
        ConnectionResetError,
        BrokenPipeError,
        ConnectionAbortedError,
    )
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, accepted):
            return True
        if isinstance(current, (asyncio.TimeoutError, TimeoutError)):
            return False
        current = current.__cause__ or current.__context__
    return False


class _PreloadedBootstrapChannel(AccessibilitySnapshotChannel):
    """Diagnostic channel that isolates socket admission from ADB bootstrap."""

    def __init__(
        self,
        serial: str,
        authority: str,
        bootstrap: AccessibilityChannelBootstrap,
    ) -> None:
        super().__init__(serial, authority)
        self._preloaded_bootstrap = bootstrap

    async def _load_bootstrap(self, *, timeout: float) -> AccessibilityChannelBootstrap:
        del timeout
        return self._preloaded_bootstrap


def _snapshot_summary(channel: AccessibilitySnapshotChannel, snapshot: Any) -> dict[str, Any]:
    return {
        "complete": snapshot.complete,
        "provider": snapshot.provider,
        "window_count": len(snapshot.windows),
        "connection_generation": channel.connection_generation,
        "transport_elapsed_ms": round(snapshot.transport_elapsed_ms, 3),
    }


async def _enabled_services(serial: str) -> str:
    raw = await adb.shell_async(
        serial,
        ["settings", "get", "secure", "enabled_accessibility_services"],
        timeout=2.5,
    )
    value = raw.decode("utf-8", "replace").strip()
    return "" if value == "null" else value


async def _set_enabled_services(serial: str, value: str) -> None:
    command = (
        ["settings", "put", "secure", "enabled_accessibility_services", value]
        if value
        else ["settings", "delete", "secure", "enabled_accessibility_services"]
    )
    await adb.shell_async(serial, command, timeout=2.5)


async def _wait_service_ready(serial: str, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "collector service not ready"
    while time.monotonic() < deadline:
        channel = AccessibilitySnapshotChannel(serial, "ai.clickclick.collector")
        try:
            snapshot = await channel.snapshot(timeout=2.5)
            if snapshot.complete:
                return
            last_error = f"collector incomplete: {snapshot.reasons}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        finally:
            await channel.close()
        await asyncio.sleep(0.25)
    raise RuntimeError(last_error)


async def _apk_identity(serial: str, local_apk: Path) -> dict[str, Any]:
    local_sha256 = hashlib.sha256(local_apk.read_bytes()).hexdigest()
    raw_path = await adb.shell_async(
        serial, ["pm", "path", "ai.clickclick.collector"], timeout=2.5
    )
    package_line = raw_path.decode("utf-8", "replace").strip()
    if not package_line.startswith("package:"):
        raise RuntimeError("collector installed APK path is unavailable")
    device_path = package_line.removeprefix("package:")
    raw_sha = await adb.shell_async(
        serial, ["sha256sum", device_path], timeout=2.5
    )
    device_sha256 = raw_sha.decode("ascii", "replace").split(maxsplit=1)[0]
    return {
        "passed": local_sha256 == device_sha256,
        "local_sha256": local_sha256,
        "device_sha256": device_sha256,
        "device_path": device_path,
    }


async def verify(
    serial: str,
    *,
    max_clients: int,
    idle_seconds: float,
    local_apk: Path,
) -> dict[str, Any]:
    channels: list[AccessibilitySnapshotChannel] = []
    report: dict[str, Any] = {
        "serial": serial,
        "model_calls": 0,
        "max_clients": max_clients,
        "idle_seconds": idle_seconds,
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "apk_identity": await _apk_identity(serial, local_apk),
    }
    enabled_before = await _enabled_services(serial)
    if COLLECTOR_SERVICE not in enabled_before.split(":"):
        raise RuntimeError("collector accessibility service is not enabled")

    try:
        first = AccessibilitySnapshotChannel(serial, "ai.clickclick.collector")
        second = AccessibilitySnapshotChannel(serial, "ai.clickclick.collector")
        channels.extend([first, second])
        first_snapshot = await first.snapshot(timeout=2.5)
        second_snapshot = await second.snapshot(timeout=2.5)
        report["idle_first_second_snapshot"] = {
            "passed": first_snapshot.complete and second_snapshot.complete,
            "first": _snapshot_summary(first, first_snapshot),
            "second": _snapshot_summary(second, second_snapshot),
        }
        bootstrap = first._bootstrap
        if bootstrap is None:
            raise RuntimeError("first channel did not retain authenticated bootstrap")
        excess = _PreloadedBootstrapChannel(
            serial, "ai.clickclick.collector", bootstrap
        )
        recovery = _PreloadedBootstrapChannel(
            serial, "ai.clickclick.collector", bootstrap
        )

        while len(channels) < max_clients:
            channel = AccessibilitySnapshotChannel(serial, "ai.clickclick.collector")
            channels.append(channel)
            snapshot = await channel.snapshot(timeout=2.5)
            if not snapshot.complete:
                raise RuntimeError(f"owned channel incomplete: {snapshot.reasons}")

        excess_error = ""
        try:
            await excess.snapshot(timeout=2.5)
        except Exception as exc:  # noqa: BLE001
            excess_error = str(exc)
        finally:
            await excess.close()
        excess_reached_socket = excess.connection_generation > 0
        incumbent_checks: list[dict[str, Any]] = []
        for channel in channels:
            generation_before = channel.connection_generation
            snapshot = await channel.snapshot(timeout=2.5)
            incumbent_checks.append({
                "passed": (
                    snapshot.complete
                    and channel.connection_generation == generation_before
                ),
                "connection_generation_before": generation_before,
                "after": _snapshot_summary(channel, snapshot),
            })

        released = channels.pop(0)
        await released.close()
        await asyncio.sleep(0.25)
        channels.append(recovery)
        recovery_snapshot = await recovery.snapshot(timeout=2.5)
        report["bounded_saturation_recovery"] = {
            "passed": (
                bool(excess_error)
                and excess_reached_socket
                and all(item["passed"] for item in incumbent_checks)
                and recovery_snapshot.complete
            ),
            "excess_error": excess_error,
            "excess_reached_socket": excess_reached_socket,
            "excess_connection_generation": excess.connection_generation,
            "incumbents_after_excess": incumbent_checks,
            "recovery": _snapshot_summary(recovery, recovery_snapshot),
        }

        idle_channel = second
        generation_before = idle_channel.connection_generation
        await asyncio.sleep(idle_seconds)
        after_idle = await idle_channel.snapshot(timeout=2.5)
        report["idle_reuse"] = {
            "passed": (
                after_idle.complete
                and idle_channel.connection_generation == generation_before
            ),
            "connection_generation_before": generation_before,
            "after": _snapshot_summary(idle_channel, after_idle),
        }

        without_collector = ":".join(
            item for item in enabled_before.split(":")
            if item and item != COLLECTOR_SERVICE
        )
        live_before_stop: list[dict[str, Any]] = []
        for channel in channels:
            generation_before = channel.connection_generation
            snapshot = await channel.snapshot(timeout=2.5)
            live_before_stop.append({
                "passed": (
                    snapshot.complete
                    and channel.connection_generation == generation_before
                ),
                "connection_generation_before": generation_before,
                "after": _snapshot_summary(channel, snapshot),
            })

        await _set_enabled_services(serial, without_collector)
        await asyncio.sleep(0.5)
        stop_results: list[dict[str, Any]] = []
        for index, channel in enumerate(channels):
            error: BaseException | None = None
            try:
                await channel.snapshot(timeout=1.0)
            except Exception as exc:  # noqa: BLE001
                error = exc
            stop_results.append({
                "channel": index,
                "transport_closed": _is_transport_closed(error),
                "exception_chain": _exception_chain(error) if error else [],
            })
        report["service_stop_closes_channels"] = {
            "passed": (
                all(item["passed"] for item in live_before_stop)
                and all(item["transport_closed"] for item in stop_results)
            ),
            "live_before_stop": live_before_stop,
            "stop_results": stop_results,
            "closed_channels": sum(
                item["transport_closed"] for item in stop_results
            ),
            "held_channels": len(channels),
        }
    finally:
        await _set_enabled_services(serial, enabled_before)
        await asyncio.gather(
            *(channel.close() for channel in channels),
            return_exceptions=True,
        )
        await _wait_service_ready(serial)

    enabled_after = await _enabled_services(serial)
    report["settings_restored"] = {
        "passed": enabled_after == enabled_before,
        "enabled_before": enabled_before,
        "enabled_after": enabled_after,
    }

    report["passed"] = all(
        bool(report.get(key, {}).get("passed"))
        for key in (
            "idle_first_second_snapshot",
            "bounded_saturation_recovery",
            "idle_reuse",
            "service_stop_closes_channels",
            "settings_restored",
            "apk_identity",
        )
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--max-clients", type=int, default=8)
    parser.add_argument("--idle-seconds", type=float, default=35.0)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.max_clients < 2 or not 30.0 < args.idle_seconds < 60.0:
        parser.error("require max-clients >=2 and 30 < idle-seconds < 60")
    result = asyncio.run(verify(
        args.serial,
        max_clients=args.max_clients,
        idle_seconds=args.idle_seconds,
        local_apk=args.apk,
    ))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
