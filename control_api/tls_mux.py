"""Single-port HTTP→HTTPS upgrade shim for the console.

Browsers default a typed address like ``192.168.x.x:8080`` to plain
``http://``, but a TLS-enabled uvicorn socket can only speak TLS — the
connection would simply fail. This shim owns the public port, peeks at the
first byte, relays TLS handshakes (0x16) to the real uvicorn server on a
loopback port, and answers plain HTTP with a 308 redirect to ``https://``
so typed URLs just work.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

_TLS_HANDSHAKE_BYTE = b"\x16"  # TLS record content type: handshake
_FIRST_BYTE_TIMEOUT_S = 10.0
_HEADER_TIMEOUT_S = 5.0
_PIPE_CHUNK = 64 * 1024


def _is_expected_proactor_disconnect(context: dict[str, Any]) -> bool:
    """Return true for the harmless Windows reset raised during transport close.

    A peer may reset a browser connection before ``StreamWriter.wait_closed``
    finishes.  On the Proactor event loop that reset can surface later from the
    transport's private connection-lost callback, outside the coroutine that
    already handles ``ConnectionError``.  Keep the filter deliberately narrow
    so real application and networking failures still reach asyncio's default
    exception handler.
    """
    exc = context.get("exception")
    message = str(context.get("message", ""))
    return (
        isinstance(exc, ConnectionResetError)
        and getattr(exc, "winerror", None) == 10054
        and "_ProactorBasePipeTransport._call_connection_lost" in message
    )


def _install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    previous = loop.get_exception_handler()

    def handle(current_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_expected_proactor_disconnect(context):
            logger.debug("browser connection reset while TLS relay was closing")
            return
        if previous is not None:
            previous(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(handle)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError, RuntimeError):
        pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_PIPE_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        pass
    finally:
        await _close(writer)


def _redirect_location(head: bytes, writer: asyncio.StreamWriter) -> str:
    text = head.decode("latin-1", "replace")
    lines = text.split("\r\n")
    target = "/"
    if lines:
        parts = lines[0].split(" ")
        if len(parts) >= 2 and parts[1].startswith("/"):
            target = parts[1]
    host = ""
    for line in lines[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip()
            break
    if not host:
        sockname = writer.get_extra_info("sockname")
        host = f"{sockname[0]}:{sockname[1]}" if sockname else "localhost"
    return f"https://{host}{target}"


async def _answer_http_redirect(
    first: bytes,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        rest = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=_HEADER_TIMEOUT_S
        )
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        await _close(writer)
        return
    location = _redirect_location(first + rest, writer)
    writer.write(
        (
            "HTTP/1.1 308 Permanent Redirect\r\n"
            f"Location: {location}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
    )
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    await _close(writer)


async def _relay_tls(
    first: bytes,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream_port: int,
) -> None:
    try:
        up_reader, up_writer = await asyncio.open_connection(
            "127.0.0.1", upstream_port
        )
    except OSError:
        await _close(writer)
        return
    up_writer.write(first)
    try:
        await up_writer.drain()
        await asyncio.gather(
            _pipe(reader, up_writer),
            _pipe(up_reader, writer),
            return_exceptions=True,
        )
    finally:
        await _close(up_writer)
        await _close(writer)


async def _dispatch(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream_port: int,
) -> None:
    try:
        first = await asyncio.wait_for(reader.read(1), timeout=_FIRST_BYTE_TIMEOUT_S)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        await _close(writer)
        return
    if not first:
        await _close(writer)
        return
    if first == _TLS_HANDSHAKE_BYTE:
        await _relay_tls(first, reader, writer, upstream_port)
    else:
        await _answer_http_redirect(first, reader, writer)


async def _serve(
    app: Any,
    *,
    host: str,
    port: int,
    ssl_certfile: str,
    ssl_keyfile: str,
    log_level: str,
) -> None:
    import uvicorn

    _install_loop_exception_handler(asyncio.get_running_loop())
    upstream_port = _free_loopback_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=upstream_port,
        log_level=log_level,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    server = uvicorn.Server(config)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await _dispatch(reader, writer, upstream_port)

    mux = await asyncio.start_server(handle, host, port)
    sockets = ", ".join(str(s.getsockname()) for s in (mux.sockets or []))
    logger.info(
        "console listening on %s (https; plain http:// is redirected)", sockets
    )
    async with mux:
        await server.serve()


def serve_tls_with_http_redirect(
    app: Any,
    *,
    host: str,
    port: int,
    ssl_certfile: str,
    ssl_keyfile: str,
    log_level: str = "info",
) -> None:
    """Run the console behind the single-port HTTP→HTTPS shim (blocking)."""
    asyncio.run(
        _serve(
            app,
            host=host,
            port=port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            log_level=log_level,
        )
    )
