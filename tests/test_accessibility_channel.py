from __future__ import annotations

import asyncio
import base64
import io
import json
import struct
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from driver.accessibility import (
    AccessibilityChannelBootstrap,
    AccessibilityChannelRegistry,
    AccessibilityCollectorClient,
    AccessibilitySnapshotChannel,
    AccessibilityTransportError,
    CollectorPowerLeaseSession,
    SNAPSHOT_MAX_FRAME_BYTES,
)
from driver.adb import AdbError
from driver.android import AndroidDriver
from driver.observation_deadline import ObservationDeadline, ObservationStageError
from driver.scrcpy_observation import FrameGeometry, FrameHandle


def _snapshot_payload() -> dict:
    return {
        "schema_version": 1,
        "generation": 7,
        "captured_monotonic_ms": 123.0,
        "complete": True,
        "reasons": [],
        "windows": [],
    }


class _Writer:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_exchange_uses_bounded_length_prefix_and_request_id():
    channel = AccessibilitySnapshotChannel("S", "authority")
    reader = asyncio.StreamReader()
    writer = _Writer()
    channel._reader = reader
    channel._writer = writer  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap("socket", "secret", 1, 4)
    response = {"version": 1, "request_id": 1, "status": "ok", "ready": True}
    encoded = json.dumps(response).encode()
    reader.feed_data(struct.pack(">I", len(encoded)) + encoded)

    assert (await channel._exchange("health"))["ready"] is True
    request_size = struct.unpack(">I", writer.data[:4])[0]
    request = json.loads(writer.data[4:4 + request_size])
    assert request == {
        "version": 1,
        "request_id": 1,
        "operation": "health",
        "token": "secret",
    }


@pytest.mark.asyncio
async def test_exchange_includes_lease_fields_without_overriding_envelope():
    channel = AccessibilitySnapshotChannel("S", "authority")
    reader = asyncio.StreamReader()
    writer = _Writer()
    channel._reader = reader
    channel._writer = writer  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap("socket", "secret", 1, 4)
    response = {
        "version": 1, "request_id": 1, "status": "ok",
        "lease_state": "active", "active": True, "held": True,
    }
    encoded = json.dumps(response).encode()
    reader.feed_data(struct.pack(">I", len(encoded)) + encoded)

    await channel._exchange(
        "power_lease_acquire",
        fields={"lease_id": "task-1", "ttl_ms": 90_000, "token": "bad"},
    )
    request_size = struct.unpack(">I", writer.data[:4])[0]
    request = json.loads(writer.data[4:4 + request_size])
    assert request["lease_id"] == "task-1"
    assert request["ttl_ms"] == 90_000
    assert request["token"] == "secret"


@pytest.mark.asyncio
async def test_power_lease_session_acquires_and_releases_same_lease():
    class Client:
        def __init__(self):
            self.calls = []

        async def power_lease(self, operation, **fields):
            self.calls.append((operation, fields))
            if operation == "power_lease_release":
                return {"status": "ok", "lease_state": "released", "released": True}
            return {
                "status": "ok", "lease_state": "active", "active": True, "held": True,
            }

    client = Client()
    session = CollectorPowerLeaseSession(client, renew_interval_s=3600)  # type: ignore[arg-type]
    assert (await session.begin("task"))["status"] == "active"
    assert session.active is True
    assert (await session.end())["status"] == "released"
    assert session.active is False
    assert client.calls[0][0] == "power_lease_acquire"
    assert client.calls[-1][0] == "power_lease_release"
    assert client.calls[0][1]["lease_id"] == client.calls[-1][1]["lease_id"]


@pytest.mark.asyncio
async def test_power_lease_session_release_failure_defers_to_ttl():
    class Client:
        async def power_lease(self, operation, **_fields):
            if operation == "power_lease_release":
                raise AdbError("adb disconnected")
            return {"lease_state": "active", "active": True, "held": True}

    session = CollectorPowerLeaseSession(Client(), renew_interval_s=3600)  # type: ignore[arg-type]
    await session.begin("task")
    result = await session.end()
    assert result["status"] == "release_deferred_to_ttl"
    assert result["ttl_ms"] == 90_000
    assert session.active is False


@pytest.mark.asyncio
async def test_power_lease_session_does_not_hide_device_acquire_failure():
    class Client:
        async def power_lease(self, _operation, **_fields):
            return {
                "status": "ok",
                "lease_state": "error",
                "active": False,
                "held": False,
                "error_detail": "permission_denied",
            }

    session = CollectorPowerLeaseSession(Client())  # type: ignore[arg-type]
    result = await session.begin("task")
    assert result["status"] == "failed"
    assert result["reason"] == "permission_denied"


@pytest.mark.asyncio
async def test_exchange_rejects_oversized_and_mismatched_responses():
    channel = AccessibilitySnapshotChannel("S", "authority")
    writer = _Writer()
    channel._writer = writer  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap("socket", "secret", 1, 4)

    oversized = asyncio.StreamReader()
    oversized.feed_data(struct.pack(">I", SNAPSHOT_MAX_FRAME_BYTES + 1))
    channel._reader = oversized
    with pytest.raises(AdbError, match="invalid frame size"):
        await channel._exchange("health")

    mismatched = asyncio.StreamReader()
    encoded = json.dumps({"version": 1, "request_id": 99, "status": "ok"}).encode()
    mismatched.feed_data(struct.pack(">I", len(encoded)) + encoded)
    channel._reader = mismatched
    with pytest.raises(AdbError, match="request_id mismatch"):
        await channel._exchange("health")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_bytes", "failure_class", "stage"),
    [
        (b"", "header_eof", "response_header"),
        (struct.pack(">I", 8) + b"{}", "payload_eof", "response_payload"),
        (struct.pack(">I", 1) + b"{", "malformed_json", "response_decode"),
    ],
)
async def test_exchange_classifies_eof_and_malformed_frames(
    response_bytes, failure_class, stage
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    reader = asyncio.StreamReader()
    reader.feed_data(response_bytes)
    reader.feed_eof()
    channel._reader = reader
    channel._writer = _Writer()  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap("socket", "secret", 1, 4)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel._exchange("health")

    assert raised.value.failure_class == failure_class
    assert raised.value.stage == stage


@pytest.mark.asyncio
async def test_exchange_classifies_connection_reset():
    class ResetReader:
        async def readexactly(self, _size):
            raise ConnectionResetError("service restarted")

    channel = AccessibilitySnapshotChannel("S", "authority")
    channel._reader = ResetReader()  # type: ignore[assignment]
    channel._writer = _Writer()  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap("socket", "secret", 1, 4)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel._exchange("health")

    assert raised.value.failure_class == "connection_reset"
    assert raised.value.stage == "response_header"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "failure_class"),
    [
        ({"version": 2, "request_id": 1, "status": "ok"}, "protocol_mismatch"),
        ({"version": 1, "request_id": 1, "status": "error"}, "remote_error"),
    ],
)
async def test_exchange_classifies_protocol_and_remote_failures(
    response, failure_class,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    reader = asyncio.StreamReader()
    encoded = json.dumps(response).encode()
    reader.feed_data(struct.pack(">I", len(encoded)) + encoded)
    channel._reader = reader
    channel._writer = _Writer()  # type: ignore[assignment]
    channel._bootstrap = AccessibilityChannelBootstrap(
        "socket", "secret", 1, 4,
    )

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel._exchange("health")

    assert raised.value.failure_class == failure_class
    assert raised.value.stage == "response_validation"


@pytest.mark.asyncio
async def test_snapshot_failure_cleans_once_and_returns_typed_failure(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    attempts = 0
    connect_timeouts = []
    removed = []

    async def connected(*, timeout):
        connect_timeouts.append(timeout)
        port = 41000 + len(connect_timeouts)
        channel._reader = asyncio.StreamReader()
        channel._writer = _Writer()  # type: ignore[assignment]
        channel._port = port
        channel._bootstrap = AccessibilityChannelBootstrap(
            f"socket-{port}", "secret", 1, 100 + len(connect_timeouts)
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        nonlocal attempts
        attempts += 1
        raise ConnectionResetError("service restarted")

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.2)

    assert attempts == 1
    assert len(connect_timeouts) == 1
    assert 0 < connect_timeouts[0] <= 0.2
    assert removed == [41001]
    assert raised.value.failure_class == "connection_reset"
    assert raised.value.stage == "snapshot_exchange"
    assert len(raised.value.exchanges) == 1
    exchange = raised.value.exchanges[0]
    assert exchange["exchange_ordinal"] == 1
    assert exchange["status"] == "failed"
    assert exchange["failure_class"] == "connection_reset"
    assert exchange["close_outcome"] == "closed"
    assert exchange["socket_close_outcome"] == "closed"
    assert exchange["forward_remove_outcome"] == "removed"
    assert exchange["final_route"] == "persistent_failed"
    assert exchange["original_budget_ms"] == 200.0
    assert channel._writer is None
    assert channel._port is None


@pytest.mark.asyncio
async def test_warm_transport_break_closes_existing_transport_once(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    channel._reader = asyncio.StreamReader()
    channel._writer = _Writer()  # type: ignore[assignment]
    channel._port = 42000
    channel._bootstrap = AccessibilityChannelBootstrap(
        "socket-42000", "secret", 1, 100,
    )
    channel.connection_generation = 1
    attempts = 0
    removed = []

    async def connected(*, timeout):
        assert timeout > 0
        assert channel._writer is not None
        assert not channel._writer.is_closing()

    async def exchange(_operation):
        nonlocal attempts
        attempts += 1
        raise ConnectionResetError("warm channel broke")

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.2)

    assert attempts == 1
    assert len(raised.value.exchanges) == 1
    exchange = raised.value.exchanges[0]
    assert exchange["close_outcome"] == "closed"
    assert exchange["socket_close_outcome"] == "closed"
    assert exchange["forward_remove_outcome"] == "removed"
    assert exchange["final_route"] == "persistent_failed"
    assert removed == [42000]
    assert channel._writer is None
    assert channel._port is None


@pytest.mark.asyncio
async def test_channel_lock_wait_consumes_the_same_snapshot_deadline(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connect_timeouts = []

    async def connected(*, timeout):
        connect_timeouts.append(timeout)
        channel._reader = asyncio.StreamReader()
        channel._writer = _Writer()  # type: ignore[assignment]
        channel._port = 41005
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 9,
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        return _snapshot_payload()

    async def remove(_serial, _port):
        return None

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)
    await channel._lock.acquire()
    attempt = asyncio.create_task(channel.snapshot(timeout=0.2))
    await asyncio.sleep(0.02)
    channel._lock.release()

    snapshot = await attempt

    assert snapshot.complete is True
    assert len(connect_timeouts) == 1
    assert 0 < connect_timeouts[0] < 0.19
    await channel.close()


@pytest.mark.asyncio
async def test_channel_lock_timeout_never_touches_connection_or_forward(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0

    async def connected(*, timeout):
        nonlocal connects
        del timeout
        connects += 1

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    await channel._lock.acquire()
    started = time.monotonic()
    attempt = asyncio.create_task(channel.snapshot(timeout=0.02))
    await asyncio.sleep(0.04)
    channel._lock.release()

    with pytest.raises(AccessibilityTransportError) as raised:
        await attempt

    assert time.monotonic() - started < 0.08
    assert raised.value.failure_class == "deadline_exhausted"
    assert raised.value.stage == "channel_lock_admission"
    assert connects == 0
    assert channel._port is None


@pytest.mark.asyncio
async def test_bootstrap_generation_drift_does_not_reject_successful_exchange(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0
    exchanges = 0

    async def connected(*, timeout):
        nonlocal connects
        assert timeout > 0
        connects += 1
        channel._reader = asyncio.StreamReader()
        channel._writer = _Writer()  # type: ignore[assignment]
        channel._port = 41011
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 99,
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        nonlocal exchanges
        exchanges += 1
        return _snapshot_payload()  # snapshot generation is 7, bootstrap was 99

    async def remove(_serial, _port):
        return None

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    snapshot = await channel.snapshot(timeout=0.2)

    assert connects == 1
    assert exchanges == 1
    assert snapshot.generation == 7
    assert len(snapshot.collector_exchanges) == 1
    exchange = snapshot.collector_exchanges[0]
    assert exchange["exchange_ordinal"] == 1
    assert exchange["status"] == "succeeded"
    assert exchange["final_route"] == "primary"
    assert exchange[
        "bootstrap_snapshot_generation"
    ] == 99
    await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["bootstrap", "socket_open"])
async def test_initial_connect_failure_reports_and_cleans_partial_resources(
    monkeypatch, failure_point,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0
    exchanges = 0
    removed = []

    async def connected(*, timeout):
        nonlocal connects
        assert timeout > 0
        connects += 1
        if failure_point == "socket_open":
            channel._bootstrap = AccessibilityChannelBootstrap(
                "socket-initial", "secret", 1, 1,
            )
            channel._port = 42202
        raise AdbError(f"{failure_point} failed")

    async def exchange(_operation):
        nonlocal exchanges
        exchanges += 1
        raise AssertionError("connect failure must not start an exchange")

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.5)

    assert connects == 1
    assert exchanges == 0
    assert len(raised.value.exchanges) == 1
    attempt = raised.value.exchanges[0]
    assert attempt["failure_class"] == "transport_unavailable"
    assert attempt["failure_stage"] == "connect"
    assert attempt["final_route"] == "persistent_failed"
    assert attempt["connection_generation"] == 0
    assert removed == ([42202] if failure_point == "socket_open" else [])
    assert channel._writer is None
    assert channel._port is None

    await channel.close()


@pytest.mark.asyncio
async def test_snapshot_timeout_cleans_once_without_a_second_exchange(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0
    removed = []

    async def connected(*, timeout):
        nonlocal connects
        connects += 1
        channel._reader = asyncio.StreamReader()
        channel._writer = _Writer()  # type: ignore[assignment]
        channel._port = 43001
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 1
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        await asyncio.sleep(1.0)

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.02)

    assert connects == 1
    assert removed == [43001]
    assert len(raised.value.exchanges) == 1
    assert raised.value.exchanges[0]["failure_class"] == "timeout"
    assert raised.value.exchanges[0]["final_route"] == "persistent_failed"

    await channel.close()


@pytest.mark.asyncio
async def test_snapshot_deadline_does_not_wait_for_stalled_owned_cleanup(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    release = asyncio.Event()
    connects = 0

    class StalledWriter(_Writer):
        async def wait_closed(self) -> None:
            await release.wait()

    async def connected(*, timeout):
        nonlocal connects
        assert timeout > 0
        connects += 1
        channel._reader = asyncio.StreamReader()
        channel._writer = StalledWriter()  # type: ignore[assignment]
        channel._port = 43011
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 1,
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        await asyncio.Future()

    async def stalled_remove(_serial, _port):
        await release.wait()

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr(
        "driver.accessibility.adb.remove_forward_async", stalled_remove,
    )

    started = time.monotonic()
    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.02)
    elapsed = time.monotonic() - started

    assert elapsed < 0.08
    assert connects == 1
    assert raised.value.failure_class == "cleanup_pending"
    assert raised.value.exchanges[0]["close_outcome"] == "cleanup_pending"
    assert raised.value.exchanges[0]["socket_close_outcome"] == "close_pending"
    assert raised.value.exchanges[0]["forward_remove_outcome"] == "remove_pending"
    assert raised.value.exchanges[0]["final_route"] == "persistent_failed"

    release.set()
    await channel.close()
    assert channel._socket_cleanup_task is None
    assert channel._forward_cleanup_task is None
    assert channel._port is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_resource", ["socket", "forward"])
async def test_cleanup_failure_is_terminal_after_one_exchange(
    monkeypatch, failed_resource,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0

    class FailingWriter(_Writer):
        async def wait_closed(self) -> None:
            if failed_resource == "socket":
                raise OSError("close failed")

    async def connected(*, timeout):
        nonlocal connects
        assert timeout > 0
        connects += 1
        channel._reader = asyncio.StreamReader()
        channel._writer = FailingWriter()  # type: ignore[assignment]
        channel._port = 43021
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 1,
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        raise ConnectionResetError("service restarted")

    async def remove(_serial, _port):
        if failed_resource == "forward":
            raise OSError("remove failed")

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.2)

    assert connects == 1
    assert raised.value.failure_class == "cleanup_failed"
    assert raised.value.stage == "cleanup"
    assert raised.value.exchanges[0]["close_outcome"] == "cleanup_failed"
    assert raised.value.exchanges[0]["final_route"] == "persistent_failed"


@pytest.mark.asyncio
async def test_cleanup_required_fails_without_connect_retry(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    connects = 0

    async def unsettled(*, timeout):
        nonlocal connects
        assert timeout > 0
        connects += 1
        raise AccessibilityTransportError(
            "previous cleanup unsettled",
            failure_class="cleanup_required",
            stage="connect_admission",
        )

    monkeypatch.setattr(channel, "_ensure_connected", unsettled)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.snapshot(timeout=0.2)

    assert connects == 1
    assert raised.value.failure_class == "cleanup_required"
    assert len(raised.value.exchanges) == 1
    exchange = raised.value.exchanges[0]
    assert exchange["failure_family"] == "cleanup"
    assert exchange["final_route"] == "persistent_failed"


@pytest.mark.asyncio
async def test_pending_socket_cleanup_blocks_new_connection_after_forward_removed(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    release = asyncio.Event()
    bootstrap_calls = 0

    async def pending_close():
        await release.wait()

    async def bootstrap(*, timeout):
        nonlocal bootstrap_calls
        del timeout
        bootstrap_calls += 1

    channel._socket_cleanup_task = asyncio.create_task(pending_close())
    channel._port = None
    monkeypatch.setattr(channel, "_load_bootstrap", bootstrap)

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel._ensure_connected(timeout=0.1)

    assert raised.value.failure_class == "cleanup_required"
    assert bootstrap_calls == 0
    release.set()
    await channel.close()
    assert channel._socket_cleanup_task is None


@pytest.mark.asyncio
async def test_explicit_close_cancels_pending_cleanup_without_losing_ownership(
    monkeypatch,
):
    channel = AccessibilitySnapshotChannel("S", "authority")
    never = asyncio.Event()

    async def pending():
        await never.wait()

    monkeypatch.setattr("driver.accessibility.SNAPSHOT_REQUEST_TIMEOUT_S", 0.01)
    channel._socket_cleanup_task = asyncio.create_task(pending())
    channel._forward_cleanup_task = asyncio.create_task(pending())
    channel._port = 43031

    with pytest.raises(AccessibilityTransportError) as raised:
        await channel.close()

    assert raised.value.stage == "final_cleanup"
    assert raised.value.failure_class == "cleanup_failed"
    assert channel._socket_cleanup_task is None
    assert channel._forward_cleanup_task is None
    assert channel._port == 43031
    assert channel.diagnostics()["cleanup_failed"] is True


@pytest.mark.asyncio
async def test_snapshot_cancellation_closes_exact_owned_forward(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    entered = asyncio.Event()
    removed = []

    async def connected(*, timeout):
        del timeout
        channel._reader = asyncio.StreamReader()
        channel._writer = _Writer()  # type: ignore[assignment]
        channel._port = 44001
        channel._bootstrap = AccessibilityChannelBootstrap(
            "socket", "secret", 1, 1
        )
        channel.connection_generation += 1

    async def exchange(_operation):
        entered.set()
        await asyncio.Future()

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_ensure_connected", connected)
    monkeypatch.setattr(channel, "_exchange", exchange)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)
    attempt = asyncio.create_task(channel.snapshot(timeout=1.0))
    await entered.wait()
    attempt.cancel()

    with pytest.raises(asyncio.CancelledError):
        await attempt

    assert removed == [44001]
    assert channel._writer is None
    cancellation = channel.diagnostics()["collector_exchanges"][0]
    assert cancellation["status"] == "cancelled"
    assert cancellation["failure_family"] == "lifecycle"
    assert cancellation["close_outcome"] == "closed"
    assert cancellation["final_route"] == "cancelled"


@pytest.mark.asyncio
async def test_bootstrap_uses_the_complete_primary_attempt_timeout(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    encoded = base64.b64encode(json.dumps({
        "socket_name": "collector",
        "token": "secret",
        "protocol_version": 1,
        "generation": 9,
    }).encode()).decode()

    async def query(_serial, uri, *, timeout):
        assert uri.endswith("/health")
        assert timeout == 2.5
        return f"Row: 0 health={encoded}".encode()

    monkeypatch.setattr("driver.accessibility.adb.content_query_async", query)

    bootstrap = await channel._load_bootstrap(timeout=2.5)

    assert bootstrap.socket_name == "collector"
    assert bootstrap.bootstrap_snapshot_generation == 9


@pytest.mark.asyncio
async def test_client_close_releases_its_persistent_channel(monkeypatch):
    client = AccessibilityCollectorClient("S", authority="authority")
    channel = client._channel
    closes = 0

    async def close_channel():
        nonlocal closes
        closes += 1

    monkeypatch.setattr(channel, "close", close_channel)

    await client.close()
    await client.close()

    assert closes == 1
    assert client._channel is None


@pytest.mark.asyncio
async def test_fetch_primary_timeout_preserves_channel_exchange_diagnostics():
    exchanges = [{
        "exchange_ordinal": 1,
        "status": "cancelled",
        "failure_class": "cancelled",
    }]
    class Channel:
        async def snapshot(self, *, timeout):
            del timeout
            raise AccessibilityTransportError(
                "deadline exhausted",
                failure_class="timeout",
                stage="snapshot_exchange",
                exchanges=exchanges,
            )

    client = AccessibilityCollectorClient("S", authority="authority")
    client._channel = Channel()

    with pytest.raises(AccessibilityTransportError) as raised:
        await client.fetch_primary(timeout=0.02)

    assert raised.value.failure_class == "timeout"
    assert raised.value.stage == "snapshot_exchange"
    assert raised.value.exchanges == exchanges


@pytest.mark.asyncio
async def test_registry_shares_channel_and_shutdown_closes_it(monkeypatch):
    registry = AccessibilityChannelRegistry()
    first = registry.get("S", "authority")
    assert first is registry.get("S", "authority")
    closed = 0

    async def close():
        nonlocal closed
        closed += 1

    monkeypatch.setattr(first, "close", close)
    await registry.shutdown()
    assert closed == 1
    assert registry.get("S", "authority") is not first


@pytest.mark.asyncio
async def test_registry_retains_channel_when_final_cleanup_fails(monkeypatch):
    registry = AccessibilityChannelRegistry()
    first = registry.get("S", "authority")

    async def fail_close():
        raise AccessibilityTransportError(
            "cleanup pending",
            failure_class="cleanup_pending",
            stage="final_cleanup",
        )

    monkeypatch.setattr(first, "close", fail_close)

    with pytest.raises(AccessibilityTransportError) as raised:
        await registry.shutdown()

    assert raised.value.stage == "registry_shutdown"
    assert raised.value.failure_class == "cleanup_failed"
    assert registry.get("S", "authority") is first


@pytest.mark.asyncio
async def test_registry_shutdown_cancels_latest_idle_close_task():
    registry = AccessibilityChannelRegistry()
    channel = registry.get("S", "authority")
    channel._schedule_idle_close()
    channel._schedule_idle_close()
    channel._schedule_idle_close()
    idle_task = channel._idle_task
    assert idle_task is not None

    await registry.shutdown()

    assert idle_task.cancelled() or idle_task.done()
    assert channel._idle_task is None
    assert registry.get("S", "authority") is not channel


@pytest.mark.asyncio
async def test_channel_close_is_idempotent_for_socket_and_forward(monkeypatch):
    channel = AccessibilitySnapshotChannel("S", "authority")
    writer = _Writer()
    channel._reader = asyncio.StreamReader()
    channel._writer = writer  # type: ignore[assignment]
    channel._port = 45001
    removed = []

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)

    await channel.close()
    await channel.close()

    assert writer.closed is True
    assert removed == [45001]
    assert channel._reader is None
    assert channel._writer is None
    assert channel._port is None


@pytest.mark.asyncio
async def test_healthy_scrcpy_pixels_survive_tree_failure_without_screencap(monkeypatch):
    encoded_frame = io.BytesIO()
    Image.new("RGB", (100, 200), "white").save(encoded_frame, format="PNG")
    stream_frame = encoded_frame.getvalue()
    frame = FrameHandle(
        stream_frame,
        timestamp=time.monotonic() + 1.0,
        generation=3,
        geometry=FrameGeometry(100, 200, 100, 200),
    )

    class Provider:
        async def start(self):
            return True

        def current(self):
            return SimpleNamespace(status="healthy", frame=frame)

    driver = AndroidDriver(serial="S", stream_provider=Provider())

    async def fail_tree(_deadline):
        raise ObservationStageError("ui_dump", "tree timed out", timed_out=True)

    async def forbidden_screencap(_deadline):
        raise AssertionError("healthy scrcpy pixels must be preserved")

    monkeypatch.setattr(driver, "_deadline_tree", fail_tree)
    monkeypatch.setattr(driver, "_deadline_screencap", forbidden_screencap)

    tree, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 2000)
    )

    assert pixels == stream_frame
    assert tree["_capture"]["complete"] is False
    assert metadata["pixel_provider"] == "scrcpy"
    assert metadata["coordinate_compatible"] is False
    assert metadata["coherence_status"] == "component_degraded"
