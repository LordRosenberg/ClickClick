"""Bounded ADB connectivity; never execute arbitrary shell/device actions."""

from __future__ import annotations

import asyncio
import ipaddress
import re

from driver import adb

GUIDES = {
    "wifi": "Android 11+：电脑和手机连接同一 Wi-Fi；设置→开发者选项→无线调试→使用配对码配对。提供该页面的 IP:配对端口和六位配对码；配对后返回无线调试主页面，使用 IP:连接端口。两种端口不同，不猜测 5555。",
    "usb": "开启开发者选项和 USB 调试；使用支持数据传输的数据线，解锁手机并接受此电脑的调试授权。Windows 若不识别设备，按手机厂商说明安装 USB 驱动。",
    "emulator": "启动 Android 8+/API 26 模拟器并等待开机。ADB 可自动识别时直接检查设备；否则使用模拟器显示的实际 ADB 地址/端口连接，不重建或清空已有实例。",
    "legacy_wifi": "Android 10 及以下通常需先 USB 连接并授权，再由用户明确选择开启 adb tcpip；之后用手机实际 IP 和端口连接。手机重启后可能需重新开启。",
}


def endpoint(value):
    value = value.strip()
    match = re.fullmatch(r"(\[[0-9A-Fa-f:]+\]|[0-9.]+):(\d{1,5})", value)
    if not match:
        raise ValueError("Use the device's actual IP:port, not a URL or command")
    address = ipaddress.ip_address(match[1].strip("[]"))
    if not (address.is_private or address.is_loopback) or address.is_unspecified or address.is_multicast:
        raise ValueError("Only local/private-network device endpoints are supported")
    if not 0 < int(match[2]) <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    return value


async def inventory():
    output = await adb._run_async([adb.adb_bin(), "devices", "-l"], timeout=10)
    devices = []
    for line in output.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] in {"List", "*"}:
            continue
        state = fields[1]
        if state in {"device", "unauthorized", "offline", "recovery", "bootloader"}:
            devices.append({"serial": fields[0], "state": state, "online": state == "device"})
    return {"devices": devices, "guides": GUIDES,
            "guidance": "device 才表示 ADB 在线；unauthorized 请在手机上授权，offline 请检查网络/设备并重连。环境初始化结果仍需通过 get_status 检查。"}


async def pair(pairing_endpoint, pairing_code):
    target = endpoint(pairing_endpoint)
    if not re.fullmatch(r"\d{6}", pairing_code):
        raise ValueError("Pairing code must be six digits")
    kwargs = {}
    import os, subprocess
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    # Pairing code is stdin, not visible in argv or reflected in adb errors.
    process = await asyncio.create_subprocess_exec(adb.adb_bin(), "pair", target,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        output, _ = await asyncio.wait_for(process.communicate((pairing_code + "\n").encode()), 20)
        success = process.returncode == 0 and b"successfully paired" in output.lower()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if not success:
        return {"paired": False, "guidance": "配对失败：在手机重新打开配对码页面，检查同一 Wi-Fi、地址/配对端口和当前配对码后重试。"}
    return {"paired": True, "guidance": "配对成功；返回手机无线调试主页面，提供连接端口后连接。配对成功不等于设备/Collector 已就绪。"}


async def connect(connection_endpoint):
    target = endpoint(connection_endpoint)
    try:
        await adb._run_async([adb.adb_bin(), "connect", target], timeout=15)
    except adb.AdbError:
        return {"connected": False, "guidance": "连接失败：检查无线调试已开启、实际连接端口与局域网可达性。不要使用配对端口。"}
    state = await inventory()
    connected = any(d["serial"] == target and d["online"] for d in state["devices"])
    return {"connected": connected, **state}
