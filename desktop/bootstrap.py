"""Tiny frozen installer. Double-click and --unattended use the same engine."""

import argparse
import json
from pathlib import Path
import sys

from desktop.install import install_payload
from desktop.location import choose_home


def main():
    parser = argparse.ArgumentParser(description="Install ClickClick with its private runtime")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--unattended", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    args = parser.parse_args()
    payload = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload.zip"
    try:
        args.home = choose_home(args.home, unattended=args.unattended)
        if args.home is None:
            return  # Cancel before staging, stopping services or changing entries.
        # Stage first, then use the NEW install engine without changing the active
        # version or stopping tasks before it has checked backend readiness.
        stage = install_payload(payload, args.home, activate=False)
        import os
        import subprocess
        command = stage["python"]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLICKCLICK_", "_PYI_"))
               and k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        cwd = Path(stage["root"]) / "app"
        argv = [command, "-I", "-m", "desktop.main", "--home", str(args.home.resolve()), "install", "--payload", str(payload)]
        if args.no_start:
            argv += ["--no-start"]
        if args.unattended:
            argv += ["--no-open"]
        kwargs = {}
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, **kwargs)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "ClickClick setup failed; inspect data/service.log")
        if args.unattended:
            if sys.stdout:
                print(result.stdout)
        elif sys.stdout:
            print(result.stdout)
    except Exception as exc:
        if args.unattended:
            if sys.stderr:
                print(str(exc), file=sys.stderr)
        else:
            # Native error dialog; no browser can open before backend is ready.
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, str(exc), "ClickClick 安装未完成", 0x10)
            elif sys.platform == "darwin":
                import subprocess
                # Pass text as argv, never interpolate it into AppleScript source.
                subprocess.run(["osascript", "-e", 'on run argv\ndisplay alert "ClickClick 安装未完成" message (item 1 of argv)\nend run', str(exc)])
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
