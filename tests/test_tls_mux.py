from __future__ import annotations

import asyncio
from unittest.mock import Mock

from control_api.tls_mux import (
    _install_loop_exception_handler,
    _is_expected_proactor_disconnect,
)


def _windows_reset() -> ConnectionResetError:
    exc = ConnectionResetError(10054, "connection reset")
    exc.winerror = 10054
    return exc


def test_recognizes_only_proactor_connection_lost_reset() -> None:
    expected = {
        "message": (
            "Exception in callback "
            "_ProactorBasePipeTransport._call_connection_lost()"
        ),
        "exception": _windows_reset(),
    }
    assert _is_expected_proactor_disconnect(expected)
    assert not _is_expected_proactor_disconnect({**expected, "exception": RuntimeError()})
    assert not _is_expected_proactor_disconnect({**expected, "message": "application callback"})


def test_loop_handler_suppresses_reset_and_delegates_other_errors() -> None:
    loop = asyncio.new_event_loop()
    previous = Mock()
    loop.set_exception_handler(previous)
    try:
        _install_loop_exception_handler(loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        handler(
            loop,
            {
                "message": (
                    "Exception in callback "
                    "_ProactorBasePipeTransport._call_connection_lost()"
                ),
                "exception": _windows_reset(),
            },
        )
        previous.assert_not_called()

        other = {"message": "boom", "exception": RuntimeError("boom")}
        handler(loop, other)
        previous.assert_called_once_with(loop, other)
    finally:
        loop.close()
