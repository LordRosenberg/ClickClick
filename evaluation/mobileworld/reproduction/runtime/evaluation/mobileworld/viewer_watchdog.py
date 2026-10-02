"""Restore only MobileWorld's optional 7860 viewer after container restarts.

VNC-mode upstream entrypoints do not launch the viewer. This sidecar never
touches the emulator, task lifecycle, agent, or scorer.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from evaluation.mobileworld.long_run_health import atomic_json


def responds(url: str) -> bool:
    try:
        with urlopen(url, timeout=5) as response:
            return response.status < 500
    except HTTPError:
        # An HTTP response proves the process is listening, even if a route fails.
        return True
    except (OSError, URLError):
        return False


def run(root: Path, container: str, interval: float) -> None:
    attempts: list[dict] = []
    while True:
        state_path = root / "run-state.json"
        if state_path.exists():
            import json
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if len(state.get("completed", [])) >= 117 or (state.get("current") or {}).get("phase") == "failed":
                break
        if responds("http://127.0.0.1:6800/health") and not responds("http://127.0.0.1:7860/"):
            command = ["docker", "exec", "-w", "/app/service", "-d", container,
                       "uv", "run", "mobile-world", "viewer", "--port", "7860"]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
            attempt = {"at": time.time(), "returncode": completed.returncode,
                       "stderr_tail": completed.stderr[-500:]}
            time.sleep(3)
            attempt["viewer_restored"] = responds("http://127.0.0.1:7860/")
            attempts.append(attempt)
            atomic_json(root / "viewer-watchdog.json", {"attempts": attempts})
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--container", default="mobile_world_env_0")
    parser.add_argument("--interval", type=float, default=20)
    args = parser.parse_args()
    run(Path(args.run_root).resolve(), args.container, args.interval)


if __name__ == "__main__":
    main()
