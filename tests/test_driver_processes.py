"""Device tools must not interrupt the desktop with Windows consoles."""

import subprocess

import pytest

from driver import adb, processes


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.parametrize("entry", ["sync", "async", "discovery"])
@pytest.mark.asyncio
async def test_device_commands_use_platform_background_options(monkeypatch, platform, entry):
    monkeypatch.setattr(processes, "platform", platform)
    flag = 0x08000000  # Windows CREATE_NO_WINDOW
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", flag, raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=b"ok", stderr=b"")

    def check_output(args, **kwargs):
        calls.append(kwargs)
        return "List of devices attached\nS1\tdevice\n"

    class Process:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def spawn(*args, **kwargs):
        calls.append(kwargs)
        return Process()

    monkeypatch.setattr(adb, "adb_bin", lambda: "adb")
    monkeypatch.setattr(adb.subprocess, "run", run)
    monkeypatch.setattr(adb.subprocess, "check_output", check_output)
    monkeypatch.setattr(adb.asyncio, "create_subprocess_exec", spawn)
    if entry == "sync":
        assert adb._run(["adb", "version"]) == b"ok"
    elif entry == "async":
        assert await adb._run_async(["adb", "version"], timeout=1) == b"ok"
    else:
        assert adb.list_device_serials() == ["S1"]
    assert len(calls) == 1
    if platform == "win32":
        assert calls[0]["creationflags"] == flag
    else:
        assert "creationflags" not in calls[0]
