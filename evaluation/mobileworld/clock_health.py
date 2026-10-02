"""Verify the real-time clock prerequisite used by official online tasks."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from evaluation.mobileworld.long_run_health import MobileWorldTarget, adb, verify_identity


# Mirrors BaseTask.apps_require_time_sync. Other tasks intentionally use a
# fixture date, which must never be replaced by the host's current date.
REAL_TIME_APPS = frozenset({"Chrome", "Maps", "MCP-arXiv"})

# This task converts a device-selected calendar date into a relative duration
# that Rails applies to its live clock. Its input files have no fixture-date
# dependency. Do not extend this to Mastodon tasks that query dated posts/events.
SERVER_RELATIVE_TIME_TASKS = frozenset({"MastodonNewFilterTask"})


def ensure_task_clock(target: MobileWorldTarget, apps: list[str], task: str = "") -> dict[str, Any]:
    report: dict[str, Any] = {
        "required": bool(REAL_TIME_APPS.intersection(apps)) or task in SERVER_RELATIVE_TIME_TASKS,
        "ready": True, "repaired": False, "samples": [],
    }
    if task in SERVER_RELATIVE_TIME_TASKS:
        report["policy"] = "server_relative_expiry"
    if not report["required"]:
        return report
    try:
        verify_identity(target)
        # Restored snapshots retain old telephony/network time suggestions.
        # They can overwrite a successful `date` repair minutes later. Own the
        # clock for this online episode, then restore the original setting.
        auto_time = str(adb(target, "shell", "settings", "get", "global", "auto_time")).strip()
        if auto_time not in {"0", "1"}:
            raise RuntimeError("automatic time setting is unreadable")
        report["auto_time_original"] = auto_time
        if auto_time == "1":
            report["auto_time_restore_required"] = True
            adb(target, "shell", "settings", "put", "global", "auto_time", "0")
        actual = str(adb(target, "shell", "settings", "get", "global", "auto_time")).strip()
        if actual != "0":
            raise RuntimeError("automatic time could not be suspended")

        def sample() -> bool:
            guest = int(str(adb(target, "shell", "date", "+%s")).strip())
            host = time.time()
            report["samples"].append({
                "guest_epoch": guest, "host_epoch": host,
                "offset_seconds": guest - host,
            })
            return abs(guest - host) <= 30

        if not sample():
            # Same date operation as the official time_sync_to_now(), but pinned
            # to the verified device and followed by an actual clock readback.
            stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%Y.%S")
            adb(target, "shell", "su", "root", "date", "-u", stamp)
            report["repaired"] = True
        first = sample()
        time.sleep(3)
        stable = sample()
        report["ready"] = first and stable
        if not report["ready"]:
            report["error"] = "device_clock_not_synchronized"
    except Exception as exc:
        report.update(ready=False, error=f"{type(exc).__name__}: {exc}")
    return report


def restore_task_clock(target: MobileWorldTarget, report: dict[str, Any]) -> None:
    """Release only the automatic-time setting acquired by this episode."""
    if not report.get("auto_time_restore_required"):
        return
    verify_identity(target)
    original = report["auto_time_original"]
    adb(target, "shell", "settings", "put", "global", "auto_time", original)
    actual = str(adb(target, "shell", "settings", "get", "global", "auto_time")).strip()
    if actual != original:
        raise RuntimeError("automatic time setting restoration failed")
    report["auto_time_restored"] = True
