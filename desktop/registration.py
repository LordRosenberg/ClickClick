"""Standard MCP configuration registration, never per-assistant task logic."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid

from desktop.files import private_write, read_json
from control_api.mcp_launcher import local_url, startup_lock

CLIENTS = ("codex", "claude-code", "claude-desktop", "kimi-code")


def server_entry(*, client, transport, base_url, token, command, args):
    base_url = local_url(base_url)
    if client == "claude-desktop" and transport != "stdio":
        raise ValueError("Use Claude Desktop's local stdio configuration; account remote connectors are different")
    if transport == "stdio":
        entry = {"command": str(command), "args": list(args)}
        if client == "claude-code":
            entry["type"] = "stdio"
        return entry
    if transport != "http":
        raise ValueError("Transport must be http or stdio")
    if not token or len(token) < 32:
        raise ValueError("A local HTTP MCP credential is required")
    entry = {"url": base_url.rstrip("/") + "/mcp/"}
    entry["http_headers" if client == "codex" else "headers"] = {"Authorization": "Bearer " + token}
    if client == "claude-code":
        entry["type"] = "http"
    return entry


def client_path(client, *, user_dir=None, system=None):
    user_dir = Path(user_dir or Path.home())
    system = system or sys.platform
    if client == "codex":
        # Only use the real environment override for real-user registration.
        root = Path(os.environ.get("CODEX_HOME", user_dir / ".codex")) if user_dir == Path.home() else user_dir / ".codex"
        return root / "config.toml"
    if client == "claude-code":
        return user_dir / ".claude.json"
    if client == "kimi-code":
        root = Path(os.environ.get("KIMI_CODE_HOME", user_dir / ".kimi-code")) if user_dir == Path.home() else user_dir / ".kimi-code"
        return root / "mcp.json"
    if client == "claude-desktop":
        if system == "win32":
            root = Path(os.environ.get("APPDATA", user_dir / "AppData/Roaming")) if user_dir == Path.home() else user_dir / "AppData/Roaming"
            return root / "Claude/claude_desktop_config.json"
        if system == "darwin":
            return user_dir / "Library/Application Support/Claude/claude_desktop_config.json"
        raise ValueError("Claude Desktop registration supports Windows/macOS")
    raise ValueError("Unknown client; export the generic MCP configuration instead")


def register(client, entry, *, user_dir=None, system=None, replace=False):
    path = client_path(client, user_dir=user_dir, system=system)
    with startup_lock(path.parent / ".clickclick-register.lock"):
        old_text = path.read_text(encoding="utf-8") if path.exists() else ""
        if path.suffix == ".toml":
            import tomlkit
            try:
                doc = tomlkit.parse(old_text)
            except ValueError as exc:
                raise ValueError("Existing assistant TOML is invalid; repair it before registration") from exc
            servers = doc.get("mcp_servers")
            if servers is not None and not hasattr(servers, "get"):
                raise ValueError("Existing mcp_servers is not a table")
            if servers is None:
                doc["mcp_servers"] = tomlkit.table()
                servers = doc["mcp_servers"]
            previous = servers.get("clickclick")
            if previous is not None and not hasattr(previous, "items"):
                raise ValueError("Existing ClickClick registration is not a table")
            if previous is not None and dict(previous) != entry and not replace:
                raise ValueError("ClickClick registration conflicts; inspect it or explicitly select replace")
            if previous is not None and dict(previous) == entry:
                return {"registered": True, "changed": False, "connection_verified": False, "path": str(path)}
            servers["clickclick"] = entry
            text = tomlkit.dumps(doc)
        else:
            doc = read_json(path)
            servers = doc.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("Existing mcpServers is not an object")
            previous = servers.get("clickclick")
            if previous is not None and previous != entry and not replace:
                raise ValueError("ClickClick registration conflicts; inspect it or explicitly select replace")
            if previous == entry:
                return {"registered": True, "changed": False, "connection_verified": False, "path": str(path)}
            servers["clickclick"] = entry
            text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        backup = None
        if path.exists():
            backup = path.with_name(path.name + ".clickclick-backup-" + uuid.uuid4().hex[:12])
            private_write(backup, old_text)
        private_write(path, text)
    return {"registered": True, "changed": True, "connection_verified": False,
            "path": str(path), "backup": str(backup) if backup else None,
            "guidance": "重新加载/重启助手，检查 MCP 工具列表并调用 get_status；配置保存不等于连接验证。"}
