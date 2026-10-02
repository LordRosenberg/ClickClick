"""Expose a MobileWorld container's loopback-only emulator ADB endpoint.

The upstream container publishes port 5556, while the emulator listens on
127.0.0.1:5555.  Run this helper inside that container so a benchmark driver
can address the container device by its dedicated published port without
touching any host emulator.
"""

from __future__ import annotations

import argparse
import socket
import threading


def _copy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while chunk := source.recv(64 * 1024):
            destination.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _serve(client: socket.socket, target_host: str, target_port: int) -> None:
    try:
        upstream = socket.create_connection((target_host, target_port), timeout=10)
        # connect() timeout is inherited by recv(); long-lived ADB streams such
        # as screenrecord must not be closed after ten seconds of quiet traffic.
        upstream.settimeout(None)
    except OSError:
        client.close()
        return
    with client, upstream:
        left = threading.Thread(target=_copy, args=(client, upstream), daemon=True)
        right = threading.Thread(target=_copy, args=(upstream, client), daemon=True)
        left.start()
        right.start()
        left.join()
        right.join()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=5556)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=5555)
    args = parser.parse_args()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.listen_host, args.listen_port))
        listener.listen(16)
        while True:
            client, _address = listener.accept()
            threading.Thread(
                target=_serve,
                args=(client, args.target_host, args.target_port),
                daemon=True,
            ).start()


if __name__ == "__main__":
    main()
