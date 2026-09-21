"""Host configuration for standalone and frozen evaluation runners."""
import os
from pathlib import Path
import shutil
import sys


def adb_executable():
    configured = os.environ.get("CLICKCLICK_EVAL_ADB") or shutil.which("adb")
    if configured:
        return str(Path(shutil.which(configured) or configured).expanduser().resolve())
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        return str(Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"))
    return "adb"


def agent_python():
    return os.environ.get("CLICKCLICK_EVAL_AGENT_PYTHON", sys.executable)


def env_file(project):
    return os.environ.get("CLICKCLICK_EVAL_ENV_FILE", str(project / ".env"))


def lock_file(handle):
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
