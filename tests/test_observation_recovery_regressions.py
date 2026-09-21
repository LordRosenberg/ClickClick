from __future__ import annotations

import asyncio
import time

import pytest

from driver import adb
from driver.accessibility import (
    AccessibilityChannelBootstrap, AccessibilitySnapshotChannel, AccessibilityTransportError,
)


class Writer:
    def __init__(self, failures=0):
        self.closed = False
        self.failures = failures

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed

    async def wait_closed(self):
        if self.failures:
            self.failures -= 1
            raise OSError("temporary close error")


@pytest.mark.asyncio
@pytest.mark.parametrize("old_forward_present", [True, False])
async def test_later_request_recovers_transient_cleanup(monkeypatch, old_forward_present):
    channel = AccessibilitySnapshotChannel("S", "authority")
    channel._writer = Writer(failures=1)
    channel._port = 41000
    channel._bootstrap = AccessibilityChannelBootstrap("owned", "token", 1, 1)
    removes = []

    async def remove(serial, port, **kwargs):
        removes.append((serial, port))
        if len(removes) == 1:
            raise adb.AdbError("transient")

    async def listed(serial, socket_name, **kwargs):
        assert (serial, socket_name) == ("S", "owned")
        return [41000, 42000] if old_forward_present else [42000]

    async def bootstrap(*, timeout):
        assert channel._port is None
        assert channel._closing_writer is None
        return AccessibilityChannelBootstrap("new", "token", 1, 2)

    async def forward(serial, name):
        return 43000

    async def connect(host, port):
        return asyncio.StreamReader(), Writer()

    async def exchange(operation):
        return {"ready": True}

    monkeypatch.setattr(adb, "remove_forward_async", remove)
    monkeypatch.setattr(adb, "list_forwards_to_localabstract_async", listed)
    monkeypatch.setattr(adb, "forward_localabstract_async", forward)
    monkeypatch.setattr(channel, "_load_bootstrap", bootstrap)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    first = await channel._close_locked(deadline=time.monotonic()+1)
    assert first["outcome"] == "cleanup_failed"
    await channel._ensure_connected(timeout=.2)
    assert channel.diagnostics()["cleanup_failed"] is False
    assert removes == [("S", 41000)] * (2 if old_forward_present else 1)
    assert channel.connection_generation == 1
    await channel.close()


@pytest.mark.asyncio
async def test_persistent_failure_has_one_retry_per_admission(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    channel._port = 41000
    channel._forward_socket_name = "owned"
    calls = 0

    async def remove(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise adb.AdbError("still broken")

    async def listed(*args, **kwargs):
        return [41000]

    monkeypatch.setattr(adb, "remove_forward_async", remove)
    monkeypatch.setattr(adb, "list_forwards_to_localabstract_async", listed)
    await channel._close_locked(deadline=time.monotonic()+.1)
    for expected in range(2, 5):
        with pytest.raises(AccessibilityTransportError):
            await channel.snapshot(timeout=.1)
        assert calls == expected
        assert channel._port == 41000
        assert channel.connection_generation == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_null_root_never_reads_old_file_when_cleanup_fails(monkeypatch, asynchronous):
    paths = []

    def run(command, **kwargs):
        if "uiautomator" in command:
            paths.append(command[-1])
            return b"ERROR: null root node"
        if "wc" in command:
            assert command[-1] != adb._DEVICE_DUMP_PATH
            raise adb.AdbError("No such file or directory")
        if "rm" in command:
            raise adb.AdbError("cleanup unavailable")
        pytest.fail("must not pull an old file")

    async def run_async(command, **kwargs):
        return run(command, **kwargs)

    monkeypatch.setattr(adb, "_run", run)
    monkeypatch.setattr(adb, "_run_async", run_async)
    monkeypatch.setattr(adb, "_DUMP_BACKOFF_S", 0)
    with pytest.raises(adb.AdbError):
        if asynchronous:
            await adb.uiautomator_dump_async("S", max_attempts=2, timeout=1)
        else:
            adb.uiautomator_dump("S", max_attempts=2)
    assert len(set(paths)) == 2


@pytest.mark.asyncio
async def test_cancelled_dump_does_not_start_cleanup_outside_budget(monkeypatch):
    commands = []

    async def run(command, **kwargs):
        commands.append(command)
        await asyncio.Future()

    monkeypatch.setattr(adb, "_run_async", run)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(adb.uiautomator_dump_async("S", timeout=1), .01)
    assert len(commands) == 1
    assert "uiautomator" in commands[0]
