"""Platform options for noninteractive device-tool subprocesses."""

import subprocess
from sys import platform


def background_process_kwargs() -> dict[str, int]:
    """Hide Windows console windows without changing pipes or cancellation."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if platform == "win32" else {}
