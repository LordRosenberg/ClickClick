"""Inspect task-app entry screens without exposing account data or secrets.

This is an environment precondition check, not a task action. It returns only
bounded readiness categories; raw accessibility nodes and text are never
persisted. The official task initializer remains authoritative.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from driver.accessibility import AccessibilityCollectorClient, AccessibilityTransportError
from evaluation.mobileworld.long_run_health import MobileWorldTarget, adb, required_packages, verify_identity


SESSION_APPS = frozenset({"Mattermost", "Mail", "Mastodon", "Taodian"})
AUTH_MARKERS = {
    "Mattermost": (
        "let’s connect to a server", "let's connect to a server",
        "log in to your account", "server_form.connect.button", "login_form.signin.button",
    ),
    "Mail": ("sign in to your account", "add your email account"),
    "Mastodon": ("log in to mastodon", "choose a server to join"),
    "Taodian": ("请登录账号", "登录/注册"),
}
MATTERMOST_READY_MARKERS = (
    "channels", "town square", "direct messages", "customer-feedback",
)


def _visible_strings(node: Any) -> list[str]:
    if not isinstance(node, dict):
        return []
    values = [
        str(node.get(key, ""))
        for key in ("text", "content_description", "content_desc", "resource_id")
        if isinstance(node.get(key), str)
    ]
    for child in node.get("children") or []:
        values.extend(_visible_strings(child))
    return values


def classify_entry_screen(app: str, values: list[str]) -> str:
    """Mattermost requires positive signed-in evidence, not just no login text."""
    haystack = "\n".join(values).casefold()
    if any(
        marker in haystack for marker in AUTH_MARKERS.get(app, ())
    ):
        return "authentication_required"
    if app == "Mattermost":
        return "authenticated" if any(
            marker in haystack for marker in MATTERMOST_READY_MARKERS
        ) else "unverified_session"
    return "no_known_blocker"


async def _snapshot_strings(target: MobileWorldTarget, *, app: str = "") -> list[str]:
    client = AccessibilityCollectorClient(target.adb_target)
    try:
        for attempt in range(8):
            try:
                snapshot = await client.fetch_primary(timeout=10)
            except AccessibilityTransportError as exc:
                # Initialization can briefly interrupt the collector transport.
                # Retry only reads within the existing eight-attempt allowance;
                # protocol failures and exhausted retries remain failed probes.
                if exc.failure_class not in {
                    "timeout", "deadline_exhausted", "connection_reset", "header_eof",
                    "payload_eof", "not_connected", "transport_io", "transport_unavailable",
                } or attempt == 7:
                    raise
                await asyncio.sleep(1.5)
                continue
            if snapshot.complete:
                values = [value for value in _visible_strings(snapshot.to_raw_tree()) if value.strip()]
                if len(values) >= 3:
                    # A complete splash-screen tree is not a ready session.
                    # Keep the same read-attempt allowance and fail closed if
                    # positive signed-in evidence never appears.
                    pending = app == "Mattermost" and classify_entry_screen(app, values) == "unverified_session"
                    if not pending or attempt == 7:
                        return values
            if attempt < 7:
                await asyncio.sleep(1.5)
        raise RuntimeError("empty or incomplete app-session accessibility snapshot")
    finally:
        await client.close()


def inspect_task_sessions(target: MobileWorldTarget, apps: list[str]) -> dict[str, Any]:
    """Launch only declared session apps, inspect, then return to Home.

    The result deliberately excludes all text, account identifiers and input
    field values. It must be called after the official task initializer.
    """
    verify_identity(target)
    packages = required_packages(apps)
    checks: dict[str, dict[str, Any]] = {}
    for app in apps:
        if app not in SESSION_APPS:
            continue
        package = packages[app]
        try:
            adb(target, "shell", "monkey", "-p", package, "1", timeout=20)
            time.sleep(3)
            fixture_check = None
            if app == "Taodian":
                from evaluation.mobileworld.taodian_fixture import ensure_taodian_configuration
                fixture_check = ensure_taodian_configuration(target)
                if not fixture_check["ready"]:
                    checks[app] = {"package": package, "status": "invalid_configuration",
                                   "configuration": fixture_check}
                    continue
            strings = asyncio.run(_snapshot_strings(target, app=app))
            checks[app] = {"package": package, "status": classify_entry_screen(app, strings)}
            if fixture_check is not None:
                checks[app]["configuration"] = fixture_check
        except Exception as exc:
            checks[app] = {"package": package, "status": "probe_failed", "error_type": type(exc).__name__}
            if isinstance(exc, AccessibilityTransportError):
                checks[app].update(failure_class=exc.failure_class, stage=exc.stage)
        finally:
            adb(target, "shell", "input", "keyevent", "KEYCODE_HOME", timeout=15)
    verify_identity(target)
    blocked = [app for app, result in checks.items()
               if result["status"] not in {"authenticated", "no_known_blocker"}]
    return {"ready": not blocked, "checks": checks, "blocked_apps": blocked}
