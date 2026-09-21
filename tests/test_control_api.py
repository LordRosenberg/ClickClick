"""Control API: submit/status/failed/replay/trace/debug/skills/scrcpy."""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from driver.pool import FIXTURE_SERIAL
from shared.schemas import Action, AgentState, ExecutorStepSubmit
from tests.fake_agents import FakeExecutor


# ---------------------------------------------------------------------------
# live-screen-mirror: /api/device/mirror/stream lifecycle
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal async WebSocket double for MirrorRegistry unit tests.

    The full ASGI WebSocket test surface is heavy; the lifecycle tests only
    need to verify (a) a ``start()`` spawns exactly one subprocess per
    serial, (b) two consumers share that subprocess, (c) the subprocess is
    killed on the last disconnect, (d) shutdown walks the registry. We
    exercise the registry directly with a fake Popen — the WebSocket route
    is covered separately by a smoke test that confirms the route is
    declared.
    """

    def __init__(self) -> None:
        self.accepted = False
        self.closed: tuple[int, str] | None = None
        self.sent: list[str | bytes] = []
        self.next_text: str | None = None
        self.next_bytes: bytes | None = None
        self.disconnected = False

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        if self.next_text is None:
            raise RuntimeError("no queued text message")
        msg, self.next_text = self.next_text, None
        return msg

    async def receive_bytes(self) -> bytes:
        if self.next_bytes is None:
            raise RuntimeError("no queued bytes message")
        msg, self.next_bytes = self.next_bytes, None
        return msg


class _FakePopen:
    """Stand-in for ``subprocess.Popen`` that records lifecycle calls.

    The MirrorRegistry only touches ``.terminate()``, ``.kill()``, ``.wait()``,
    ``.poll()``, ``.stdout``, ``.stderr``, ``.pid``. Everything else stays
    inert so the registry's surface is exercised without spawning anything.
    """

    instances: list["_FakePopen"] = []

    def __init__(
        self,
        args: list[str],
        stdout=None,
        stderr=None,
        stdin=None,
        start_new_session: bool = False,
    ) -> None:
        self.args = args
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.pid = 1000 + len(_FakePopen.instances)
        self._poll_value: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls: list[float | None] = []
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self._poll_value

    def terminate(self) -> None:
        self.terminated = True
        self._poll_value = 0  # exited

    def kill(self) -> None:
        self.killed = True
        self._poll_value = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0


class _FakeStream:
    def __init__(self) -> None:
        self.buf = b""

    def read(self, n: int = -1) -> bytes:
        if not self.buf:
            return b""
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def readline(self) -> bytes:
        if not self.buf:
            return b""
        idx = self.buf.find(b"\n")
        if idx == -1:
            out, self.buf = self.buf, b""
            return out
        out = self.buf[: idx + 1]
        self.buf = self.buf[idx + 1 :]
        return out


@pytest.mark.asyncio
async def test_mirror_registry_lifecycle() -> None:
    """First start creates source, two consumers share, last stop tears down."""
    from driver import scrcpy_mirror as mirror_mod
    from typing import AsyncIterator

    class FakeSrc:
        def __init__(self) -> None:
            self.starts = 0
            self.stops = 0
            self._alive = False
            self.codec_string = "avc1.42E01E"

        async def start(self) -> None:
            self.starts += 1
            self._alive = True

        async def stop(self) -> None:
            self.stops += 1
            self._alive = False

        def is_alive(self) -> bool:
            return self._alive

        async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
            if False:
                yield b""

    src = FakeSrc()
    registry = mirror_mod.MirrorRegistry()
    registry.set_source_factory(lambda _k: src)
    session_a = await registry.start("S1")
    session_b = await registry.start("S1")
    assert session_a is session_b
    assert src.starts == 1

    await registry.stop("S1")
    assert src.stops == 0
    await registry.stop("S1")
    assert src.stops == 1


@pytest.mark.asyncio
async def test_mirror_registry_idempotent_stop() -> None:
    """stop() on an unknown key is a no-op."""
    from driver import scrcpy_mirror as mirror_mod

    registry = mirror_mod.MirrorRegistry()
    await registry.stop("never-seen")


@pytest.mark.asyncio
async def test_mirror_registry_shutdown_walks_active() -> None:
    """``shutdown()`` stops every active session."""
    from driver import scrcpy_mirror as mirror_mod
    from typing import AsyncIterator

    class FakeSrc:
        def __init__(self) -> None:
            self.stops = 0
            self._alive = False
            self.codec_string = None

        async def start(self) -> None:
            self._alive = True

        async def stop(self) -> None:
            self.stops += 1
            self._alive = False

        def is_alive(self) -> bool:
            return self._alive

        async def frames(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
            if False:
                yield b""

    sources: dict[str, FakeSrc] = {}

    def factory(key: str) -> FakeSrc:
        src = FakeSrc()
        sources[key] = src
        return src

    registry = mirror_mod.MirrorRegistry()
    registry.set_source_factory(factory)
    await registry.start("S1")
    await registry.start("S2")
    assert len(sources) == 2

    await registry.shutdown()
    assert all(s.stops == 1 for s in sources.values())
    assert registry._sessions == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_console_spa_deep_link_serves_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """React Router paths must fall back to index.html, not JSON 404."""
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>console</title><div id='root'></div>\n",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("window.__console = 1;\n", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"ico")

    monkeypatch.setattr("control_api.main.DIST_DIR", dist)

    from control_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert "console" in root.text

        deep = await client.get(
            "/tasks/ea3a3a8c-5e53-4a9a-8301-3866970588c0"
        )
        assert deep.status_code == 200
        assert "console" in deep.text
        assert "text/html" in deep.headers["content-type"]

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "window.__console" in asset.text

        favicon = await client.get("/favicon.ico")
        assert favicon.status_code == 200
        assert favicon.content == b"ico"

        missing_api = await client.get("/api/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.json()["detail"] == "Not Found"
