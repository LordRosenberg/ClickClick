"""Local handoff does not mutate host configuration or depend on HTTP credentials."""

import json
from pathlib import Path
import subprocess
import sys

from desktop.connection import connection_info, shell_command


def test_english_handoff_retains_transport_commands_and_configuration(tmp_path):
    entry = {"command": "python", "args": ["-m", "desktop.main", "mcp"]}
    chinese = connection_info(tmp_path, entry, system="darwin")
    english = connection_info(tmp_path, entry, system="darwin", locale="en")
    assert english["configuration"] == chinese["configuration"]
    assert english["manual"][0]["command"] == chinese["manual"][0]["command"]
    assert "stdio" in english["assistant_prompt"]
    assert "Registered, verification pending" in english["assistant_prompt"]
    assert "Do not send model requests" in english["verification_prompt"]


def test_handoff_file_and_client_commands_preserve_assistant_configuration(tmp_path):
    user = tmp_path / "user"
    config = user / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[mcp_servers.other]\ncommand = "keep"\n', encoding="utf-8")
    entry = {"command": str(tmp_path / "private runtime/python.exe"),
             "args": ["-I", "-m", "desktop.main", "--home", str(tmp_path / "install space"), "mcp"],
             "headers": {"Authorization": "Bearer must-not-export"}}
    info = connection_info(tmp_path / "data", entry, user_dir=user, system="win32")
    exported = json.loads(Path(info["config_path"]).read_text(encoding="utf-8"))
    assert exported["mcpServers"]["clickclick"] == {"command": entry["command"], "args": entry["args"]}
    assert Path(info["config_path"]).is_absolute()
    assert info["config_path"] in info["assistant_prompt"]
    assert "must-not-export" not in json.dumps(info)
    assert config.read_text(encoding="utf-8") == '[mcp_servers.other]\ncommand = "keep"\n'
    assert not (user / ".claude.json").exists()
    assert info["verification_prompt"] in info["assistant_prompt"]
    commands = {item["id"]: item for item in info["manual"]}
    assert "'codex' 'mcp' 'add'" in commands["codex"]["command"]
    assert "'--scope' 'user'" in commands["claude-code"]["command"]
    assert commands["kimi-code"]["config_path"].endswith("mcp.json")


def test_generated_shell_command_preserves_spaces_quotes_and_metacharacters(tmp_path):
    reader = tmp_path / "read args.py"
    reader.write_text("import json, sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    values = ["path with space", "owner's directory", 'a"b', "$(not-a-command); $HOME & |", "中文"]
    command = shell_command([sys.executable, str(reader), *values])
    shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command"] if sys.platform == "win32" else ["sh", "-c"]
    result = subprocess.run([*shell, command], capture_output=True, text=True, timeout=20, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == values
