"""Per-user visible application entries, regenerated after version activation."""

import json
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

from desktop.files import private_write
from desktop.install import installed_command
from desktop.icons import write_app_icon


def install_entries(home, *, user_dir=None):
    home = Path(home).resolve()
    user = Path(user_dir or Path.home())
    command, args = installed_command(home, "tray")
    if sys.platform == "win32":
        command = str(Path(command).with_name("pythonw.exe"))
        line = subprocess.list2cmdline(args)
        launcher = home / "bin/ClickClick.lnk"
        # Structured stdin preserves Unicode, quoting and dash-prefixed arguments.
        script = home / "bin/create-shortcuts.ps1"
        private_write(script, '''$ErrorActionPreference = 'Stop'
$settings = [Console]::In.ReadToEnd() | ConvertFrom-Json
$Command = $settings.command
$Arguments = $settings.arguments
$Launcher = $settings.launcher
$UserRoot = $settings.user_root
$Icon = $settings.icon
$shell = New-Object -ComObject WScript.Shell
if ($UserRoot -ne '-') {
    $desktop = Join-Path $UserRoot 'Desktop'
    $programs = Join-Path $UserRoot 'AppData/Roaming/Microsoft/Windows/Start Menu/Programs'
} else {
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $programs = [Environment]::GetFolderPath('Programs')
}
foreach ($folder in @((Split-Path $Launcher), $desktop, $programs)) {
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $link = $shell.CreateShortcut((Join-Path $folder 'ClickClick.lnk'))
    $link.TargetPath = $Command
    $link.Arguments = $Arguments
    $link.WorkingDirectory = Split-Path $Command
    $link.IconLocation = $Icon
    $link.Description = 'ClickClick mobile assistant manager'
    $link.Save()
}
''')
        icon = home / "bin/ClickClick.ico"
        write_app_icon(icon)
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-File", str(script)], input=json.dumps({"command": command, "arguments": line,
                        "launcher": str(launcher), "user_root": str(user) if user_dir else "-", "icon": str(icon)}),
                       encoding="utf-8", check=True, capture_output=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return {"launcher": str(launcher), "entries": "Desktop and Start Menu"}
    if sys.platform == "darwin":
        bundle = user / "Applications/ClickClick.app"
        binary = bundle / "Contents/MacOS/ClickClick"
        private_write(binary, "#!/bin/sh\nexec " + " ".join(shlex.quote(p) for p in [command, *args]) + "\n")
        binary.chmod(0o755)
        write_app_icon(bundle / "Contents/Resources/ClickClick.icns")
        private_write(bundle / "Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.clickclick.manager", "CFBundleName": "ClickClick",
            "CFBundleExecutable": "ClickClick", "CFBundlePackageType": "APPL", "LSUIElement": True,
            "NSHighResolutionCapable": True, "CFBundleIconFile": "ClickClick.icns"}).decode())
        return {"launcher": str(bundle), "entries": "User Applications"}
    raise ValueError("Desktop entries support Windows/macOS")
