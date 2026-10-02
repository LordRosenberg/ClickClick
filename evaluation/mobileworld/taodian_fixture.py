"""Verify delivery of official TaoDian configuration before agent execution.

Reads only allowlisted storage keys. No storage values enter model context or
reports. A cold relaunch lets the app apply its own configuration; we never
create cart items, orders, credentials, or other task state.
"""
from __future__ import annotations

import json
import sqlite3
import time

import requests

from evaluation.mobileworld.long_run_health import MobileWorldTarget, adb

PACKAGE = "com.testmall.app"
STORAGE = f"/data/data/{PACKAGE}/databases/DCStorage"
KEYS = ("app_config", "hadLogin", "autoLoginProcessed", "loginResult")


def _decode(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, dict) and set(value) == {"type", "data"}:
        return _decode(value["data"])
    return value


def _read_storage(target):
    raw = adb(target, "exec-out", "su", "root", "cat", STORAGE, binary=True)
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(raw)
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if row[0].startswith("DC_") and row[0].endswith("_storage")]
        if len(tables) != 1:
            raise ValueError("ambiguous application storage")
        table = tables[0].replace('"', '""')
        return {key: _decode(value) for key, value in db.execute(
            f'SELECT key, value FROM "{table}" WHERE key IN (?,?,?,?)', KEYS)}
    finally:
        db.close()


def configuration_applied(config, storage):
    if storage.get("app_config") != config:
        return False
    if config.get("requireLogin", True):
        return True  # Login is intentionally part of some official tasks.
    login = storage.get("loginResult")
    return (storage.get("hadLogin") is True
            and storage.get("autoLoginProcessed") is True
            and isinstance(login, dict)
            and login.get("userId") == config.get("defaultUserId", "mashu001"))


def ensure_taodian_configuration(target: MobileWorldTarget):
    """Call after launching TaoDian and before giving the task to the agent."""
    report = {"ready": False, "restarted": False}
    try:
        response = requests.get(f"{target.backend}/config/callback", timeout=10)
        response.raise_for_status()
        config = response.json()
        if not isinstance(config, dict) or not isinstance(config.get("requireLogin"), bool):
            raise ValueError("invalid official configuration")
        if configuration_applied(config, _read_storage(target)):
            return {**report, "ready": True}
        # Snapshot processes can outlive replacement of the host-side config.
        # Invoke onLaunch once with the new config; preserve all app data.
        adb(target, "shell", "am", "force-stop", PACKAGE)
        adb(target, "shell", "monkey", "-p", PACKAGE, "1")
        report["restarted"] = True
        for _ in range(4):
            time.sleep(2)
            if configuration_applied(config, _read_storage(target)):
                return {**report, "ready": True}
        return {**report, "failure": "taodian_configuration_not_applied"}
    except Exception as exc:
        return {**report, "failure": "taodian_configuration_probe_failed",
                "error_type": type(exc).__name__}
