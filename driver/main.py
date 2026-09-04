"""Driver process entrypoint."""

from __future__ import annotations

import argparse
from urllib.parse import urlparse

import uvicorn

from driver.pool import DriverPool
from driver.rpc_server import create_driver_app
from shared.config import get_settings


def _default_listen_addr() -> tuple[str, int]:
    """Derive the driver's listen (host, port) from Settings.driver_url.

    The agent connects to the driver via `CLICKCLICK_DRIVER_URL`; the driver
    process listens on the same host/port so they stay in sync without a
    second hardcoded value. Falls back to (127.0.0.1, 8765) when the URL
    is empty (local ADB mode, no remote driver).
    """
    url = get_settings().driver_url
    if url:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8765
        return host, port
    return "127.0.0.1", 8765


def main() -> None:
    """Start the Driver HTTP RPC server (local multi-device hub)."""
    default_host, default_port = _default_listen_addr()
    parser = argparse.ArgumentParser(description="ClickClick Device Driver")
    parser.add_argument("--platform", default="android", choices=["android", "windows", "ios"])
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    settings = get_settings()
    # force_local: CLICKCLICK_DRIVER_URL only configures listen address here;
    # the hub always talks to ADB/fixture on this host.
    if args.platform != "android" and not settings.use_fixture_driver:
        raise SystemExit(
            f"multi-device driver hub currently supports android/fixture; got {args.platform}"
        )
    pool = DriverPool(settings, force_local=True)
    app = create_driver_app(pool=pool)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
