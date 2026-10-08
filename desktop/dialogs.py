"""Native user prompts without loading a second GUI framework."""

import subprocess
import sys
from pathlib import Path


def choose_install_parent(suggested):
    """Choose a disk/folder before a first interactive installation writes files."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        class BrowseInfo(ctypes.Structure):
            _fields_ = [("owner", wintypes.HWND), ("root", ctypes.c_void_p),
                        ("display", wintypes.LPWSTR), ("title", wintypes.LPCWSTR),
                        ("flags", wintypes.UINT), ("callback", ctypes.c_void_p),
                        ("parameter", wintypes.LPARAM), ("image", ctypes.c_int)]
        shell = ctypes.WinDLL("shell32")
        ole = ctypes.OleDLL("ole32")
        shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
        shell.SHBrowseForFolderW.restype = ctypes.c_void_p
        shell.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        shell.SHGetPathFromIDListW.restype = wintypes.BOOL
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole.CoInitializeEx(None, 2)
        selected = None
        try:
            info = BrowseInfo(None, None, ctypes.create_unicode_buffer(260),
                "选择安装位置（在所选目录下创建 ClickClick 文件夹，可选择其他磁盘）", 0x41, None, 0, 0)
            selected = shell.SHBrowseForFolderW(ctypes.byref(info))
            if not selected:
                return None
            path = ctypes.create_unicode_buffer(260)
            if not shell.SHGetPathFromIDListW(selected, path):
                raise RuntimeError("请选择本机磁盘中的安装目录。")
            return Path(path.value)
        finally:
            if selected:
                ole.CoTaskMemFree(selected)
            ole.CoUninitialize()
    if sys.platform == "darwin":
        script = '''on run argv
try
return POSIX path of (choose folder with prompt "选择安装位置（创建 ClickClick 文件夹）" default location (POSIX file (item 1 of argv)))
on error number -128
return ""
end try
end run'''
        result = subprocess.run(["osascript", "-e", script, str(suggested)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("无法打开安装目录选择，请使用 --home 指定位置。")
        return Path(result.stdout.strip()) if result.stdout.strip() else None
    return Path(suggested)


def _mac_dialog(message, buttons):
    script = '''on run argv
set reply to display dialog (item 1 of argv) with title "ClickClick" buttons {"返回", "取消任务并关闭", "暂停任务并关闭"} default button "返回" cancel button "返回"
return button returned of reply
end run'''
    if not buttons:
        script = '''on run argv
display alert "ClickClick" message (item 1 of argv)
end run'''
    return subprocess.run(["osascript", "-e", script, message], capture_output=True,
                          text=True, timeout=3600)


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "ClickClick", 0x10)
    elif sys.platform == "darwin":
        _mac_dialog(message, False)


def choose_shutdown(tasks):
    lines = [str(t["instruction"])[:100] for t in tasks[:12]]
    message = f"有 {len(tasks)} 个任务尚未结束：\n" + "\n".join(lines)
    if len(tasks) > 12:
        message += "\n其余任务可在控制台查看。"
    message += "\n\n暂停后可在控制台恢复；取消后任务结束。"
    if sys.platform == "win32":
        import ctypes
        message += "\n\n选择“是”：暂停任务后关闭。\n选择“否”：取消任务后关闭。\n选择“取消”：返回，保持运行。"
        # Default is Cancel, so Enter never silently cancels phone work.
        result = ctypes.windll.user32.MessageBoxW(None, message, "ClickClick — 关闭后台", 0x23 | 0x200)
        return {6: "pause", 7: "cancel"}.get(result, "back")
    if sys.platform == "darwin":
        result = _mac_dialog(message, True)
        if result.returncode:
            return "back"
        return {"暂停任务并关闭": "pause", "取消任务并关闭": "cancel"}.get(result.stdout.strip(), "back")
    return "back"
