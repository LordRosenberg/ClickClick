"""Platform options for noninteractive device-tool subprocesses."""

import subprocess
import os
import json
from pathlib import Path
from sys import platform


def background_process_kwargs() -> dict[str, int]:
    """Hide Windows console windows without changing pipes or cancellation."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if platform == "win32" else {}


def owned_process_args(args,owned):
    if owned is None or os.name=="nt":
        return args,background_process_kwargs()
    import sys
    journal,envelope=owned
    identity={key:envelope[key] for key in ("operation_id","device_key","device_fence","task_id","epoch")}
    env={**os.environ,"CLICKCLICK_INPUT_GROUP_NONCE":envelope["operation_id"]}
    return [sys.executable,str(Path(__file__).with_name("owned_subprocess.py")),str(journal.root),json.dumps(identity),*args],{"env":env}
