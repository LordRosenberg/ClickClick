"""ClickClick desktop CLI: shared installer, local setup and lifecycle controls."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import webbrowser

from desktop.files import read_json
from desktop.install import default_home, installed, mcp_command, installed_environment
from desktop.location import remembered_home, remember_home


def emit(value):
    if sys.stdout is not None:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def activate(home):
    env, workspace = installed_environment(home)
    # Clear inherited operational settings, keeping the installed profile authoritative.
    for key in list(os.environ):
        if key.startswith(("CLICKCLICK_", "_PYI_")) or key in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}:
            os.environ.pop(key)
    os.environ.update(env)
    os.chdir(workspace)


def setup_browser(home):
    from desktop.service import base_url, backend_status
    if not backend_status(home)["running"]:
        raise RuntimeError("Backend is not running; run ClickClick start first")
    token = (home / "data/mcp-token").read_text(encoding="utf-8").strip()
    webbrowser.open(base_url(home) + "/setup#token=" + token)
    return {"opened": True, "setup_url": base_url(home) + "/setup"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    subs = parser.add_subparsers(dest="operation", required=True)
    installer = subs.add_parser("install")
    installer.add_argument("--payload", type=Path, required=True)
    installer.add_argument("--no-start", action="store_true")
    installer.add_argument("--no-open", action="store_true")
    for op in ("serve", "backend", "http-mcp", "start", "stop", "restart", "status", "setup", "mcp", "devices", "tray"):
        subs.add_parser(op)
    subs.add_parser("check-update")
    updater = subs.add_parser("update")
    updater.add_argument("--version", required=True)
    updater.add_argument("--yes", action="store_true")
    recovery = subs.add_parser("update-recover")
    recovery.add_argument("--yes", action="store_true")
    worker = subs.add_parser("update-worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True)
    subs.add_parser("configure", help="Read model/base_url/api_key JSON from stdin; never pass keys in argv")
    pair = subs.add_parser("pair", help="Pairing code is read from stdin")
    pair.add_argument("endpoint")
    connect = subs.add_parser("connect")
    connect.add_argument("endpoint")
    for op in ("register", "export"):
        p = subs.add_parser(op)
        p.add_argument("--client", default="generic" if op == "export" else "codex")
        p.add_argument("--transport", choices=("http", "stdio"), default="http")
        if op == "register":
            p.add_argument("--replace", action="store_true")
        else:
            p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    home = (args.home or remembered_home() or default_home()).resolve()
    from desktop import service
    try:
        if args.operation == "install":
            from desktop.upgrade import install
            result = install(home, args.payload, no_start=args.no_start)
            remember_home(home)
            if not args.no_start and not args.no_open:
                result.update(setup_browser(home))
            emit(result)
            return
        if args.operation == "tray":
            from desktop.tray import main as tray
            tray(home)
            return
        if args.operation in {"check-update", "update", "update-worker", "update-recover"}:
            from desktop import updates
            if args.operation == "check-update":
                result = updates.check(home)
            elif args.operation == "update":
                result = updates.request(home, args.version, confirmed=args.yes)
            elif args.operation == "update-recover":
                result = updates.recover(home, confirmed=args.yes)
            else:
                log = open(home / "data/update-worker.log", "a", encoding="utf-8", buffering=1)
                sys.stdout = sys.stderr = log
                updates.run(home, args.job)
                result = updates.status(home)
            emit(result)
            return
        if args.operation in {"start", "restart", "stop", "status", "setup"}:
            status = service.backend_status(home)
            if args.operation == "status":
                if service.has_http_service(home):
                    status["http_mcp"] = service.http_status(home)
                emit(status)
                return
            if args.operation == "setup":
                emit(setup_browser(home))
                return
            if args.operation in {"stop", "restart"} and (status["running"] or service.http_status(home)["running"]):
                if status["running"]:
                    service.require_idle(home)
                service.native("stop", home)
                service.wait_stopped(home)
                if args.operation == "stop":
                    emit({"stop_requested": True})
                    return
            if args.operation == "stop":
                emit({"running": False})
                return
            if args.operation == "start" and status["running"]:
                emit(service.wait_ready(home))
                return
            service.native("install", home)
            service.native("start", home)
            emit(service.wait_ready(home))
            return
        activate(home)
        if args.operation in {"serve", "backend", "http-mcp"}:
            home.joinpath("data").mkdir(exist_ok=True)
            # File logs also work under Windows pythonw (no stdout/stderr).
            name = {"serve": "service", "backend": "backend", "http-mcp": "mcp-http"}[args.operation]
            log = open(home / f"data/{name}.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
            logging.basicConfig(stream=log, level=logging.INFO)
            if args.operation == "serve":
                from desktop.supervisor import run
                run(home)
            elif args.operation == "backend":
                from control_api.main import main as serve
                serve()
            else:
                from control_api.mcp_http import main as serve
                serve()
        elif args.operation == "mcp":
            from control_api.mcp import main as mcp
            sys.argv = ["clickclick-mcp", "--workspace", str(Path.cwd()), "--data-dir", str(home / "data"),
                        "--backend-url", service.base_url(home), "--no-start"]
            mcp()
        elif args.operation == "configure":
            from desktop.configuration import save_model
            value = json.load(sys.stdin)
            emit(save_model(home / "data", model=value["model"], base_url=value["base_url"], api_key=value["api_key"]))
        elif args.operation == "devices":
            from desktop.devices import inventory
            emit(asyncio.run(inventory()))
        elif args.operation == "pair":
            from desktop.devices import pair
            emit(asyncio.run(pair(args.endpoint, sys.stdin.readline().strip())))
        elif args.operation == "connect":
            from desktop.devices import connect
            emit(asyncio.run(connect(args.endpoint)))
        elif args.operation in {"register", "export"}:
            from desktop.registration import server_entry, register
            from desktop.files import write_json, private_write
            from control_api.mcp_http import local_token
            from shared.config import get_settings
            command, command_args = mcp_command(home)
            entry = server_entry(client=args.client, transport=args.transport,
                base_url=service.mcp_base_url(home) if args.transport == "http" else service.base_url(home),
                token=local_token(get_settings()), command=command, args=command_args)
            if args.operation == "register":
                emit(register(args.client, entry, replace=args.replace))
            elif args.client == "codex":
                import tomlkit
                private_write(args.output.resolve(), tomlkit.dumps({"mcp_servers": {"clickclick": entry}}))
                emit({"saved": str(args.output.resolve()), "contains_local_credential": args.transport == "http"})
            else:
                write_json(args.output.resolve(), {"mcpServers": {"clickclick": entry}})
                emit({"saved": str(args.output.resolve()), "contains_local_credential": args.transport == "http"})
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        # Do not dump configuration bodies or arbitrary subprocess outputs here.
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
