"""Generate local MCP handoff information without editing assistant settings."""

import json
import re
import shlex
import sys
from pathlib import Path

from desktop.files import write_json
from desktop.registration import client_path


VERIFY_PROMPT = "请调用 ClickClick 的 get_status，检查 MCP 连接、模型配置和手机初始化状态，说明是否可以执行手机任务及尚未完成的步骤。不要发起模型请求或手机测试任务。"


def shell_command(argv, *, system=None):
    """Quote for the terminal shown to users: PowerShell on Windows, sh on macOS."""
    if (system or sys.platform) == "win32":
        # Windows PowerShell 5.1 passes native arguments through the CRT parser.
        # Literal double quotes need escaping there as well as PowerShell quoting.
        quoted = [re.sub(r'(\\*)"', lambda match: "\\" * (2 * len(match[1]) + 1) + '"', str(value)) for value in argv]
        return "& " + " ".join("'" + value.replace("'", "''") + "'" for value in quoted)
    return shlex.join([str(value) for value in argv])


def connection_info(data_dir, entry, *, user_dir=None, system=None, locale="zh-CN"):
    # stdio needs no bearer credential in the handoff file or clipboard.
    configuration = {"mcpServers": {"clickclick": {
        "command": entry["command"], "args": entry["args"],
    }}}
    path = Path(data_dir).resolve() / "mcp-connection-stdio.json"
    write_json(path, configuration)
    launch = [entry["command"], *entry["args"]]
    manual = [
        {"id": "codex", "label": "Codex", "command": shell_command(
            ["codex", "mcp", "add", "clickclick", "--", *launch], system=system),
         "guidance": "在这台电脑的终端执行。若已有同名配置，先检查现有配置，不要直接删除或覆盖。"},
        {"id": "claude-code", "label": "Claude Code", "command": shell_command(
            ["claude", "mcp", "add", "--transport", "stdio", "--scope", "user", "clickclick", "--", *launch], system=system),
         "guidance": "在这台电脑的终端执行，注册到用户范围。若已有同名配置，先检查现有配置。"},
    ]
    for client, label in (("claude-desktop", "Claude Desktop"), ("kimi-code", "Kimi Code")):
        if client == "claude-desktop" and (system or sys.platform) not in {"win32", "darwin"}:
            continue
        target = client_path(client, user_dir=user_dir, system=system)
        manual.append({"id": client, "label": label, "config_path": str(target),
            "guidance": "打开以下配置文件，将下方 mcpServers.clickclick 合并进去，保留其他服务器和设置；不要用整段内容覆盖已有文件。"})
    manual.append({"id": "generic", "label": "其他 MCP 助手",
        "guidance": "在助手的本地 MCP 设置入口选择 stdio，填写下面的 command 和 args；或按客户端支持的格式导入。"})
    result = {
        "transport": "stdio", "config_path": str(path), "configuration": json.dumps(configuration, ensure_ascii=False, indent=2),
        "shell": "Windows PowerShell" if (system or sys.platform) == "win32" else "终端",
        "manual": manual, "verification_prompt": VERIFY_PROMPT,
        "assistant_prompt": (
            f"请将这台电脑上已经运行的 ClickClick 添加到你自己的 MCP。读取本机文件：{path}\n"
            "该文件是 stdio MCP 接入配置，包含准确的 command 和 args，无需猜测路径或 HTTP 地址。"
            "使用你自己的官方 MCP 管理命令或配置入口注册名为 clickclick 的服务；优先使用用户范围。"
            "保留已有服务器和设置；已有同名配置时先检查，冲突需说明。实际执行注册，不要只给我示例。"
            "遵守客户端的操作授权；若你不能访问本机或注册自己的 MCP，请给我具体手动步骤。\n"
            "如果当前会话可以重新加载 MCP，请加载后调用 get_status 检查连接、模型和手机初始化。"
            "如果必须由我重启客户端或新开会话，请报告“注册完成，待验证”，说明具体操作，并让我在新会话发送以下指令：\n"
            + VERIFY_PROMPT + "\n配置已写入不等于连接成功，设备在线不等于手机环境完全就绪。"
        ),
    }
    if locale == "en":
        result["shell"] = "Windows PowerShell" if (system or sys.platform) == "win32" else "Terminal"
        result["verification_prompt"] = (
            "Call ClickClick get_status to check the MCP connection, model configuration and phone initialization. "
            "Explain whether phone tasks can run and what steps remain. Do not send model requests or start a phone test task."
        )
        result["assistant_prompt"] = (
            f"Add the ClickClick running on this computer to your own MCP. Read this local file: {path}\n"
            "It contains stdio MCP configuration with the exact command and args; no HTTP address or guessed paths are needed. "
            "Use your official MCP management command or settings to register clickclick, preferably at user scope. "
            "Preserve other servers and settings. Check any existing clickclick entry and report conflicts before replacing it. "
            "Perform the registration rather than just showing an example, respecting your client's authorization requirements. "
            "If you cannot access local files or register your own MCP, give me specific manual steps.\n"
            "If this session supports MCP reload, reload and call get_status to verify. If I must restart the client or open a new session, "
            "report ‘Registered, verification pending’, explain exactly what to do, and give me this instruction for the new session:\n"
            + result["verification_prompt"] + "\nWriting configuration does not prove the connection works; an online device does not prove initialization is complete."
        )
        for item in manual:
            if item["id"] in {"codex", "claude-code"}:
                item["guidance"] = "Run in this computer's terminal. Preserve existing configuration; inspect any entry named clickclick before replacing it."
            elif item["id"] == "generic":
                item["label"] = "Other MCP assistant"
                item["guidance"] = "Choose stdio in your assistant's local MCP settings and enter command and args below, or import in the client's supported format."
            else:
                item["guidance"] = "Open this configuration file and merge mcpServers.clickclick below, preserving other servers and settings. Do not overwrite the entire file."
    return result
