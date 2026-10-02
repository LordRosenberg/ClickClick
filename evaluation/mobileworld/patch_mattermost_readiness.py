"""Install a bounded API-readiness wait in the local official environment.

Run only between episodes, then restart the container to reload the helper.
The original helper and patch hashes are retained; graders are not modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from evaluation.mobileworld.long_run_health import MATTERMOST_HELPER

MARKER = "# clickclick-environment: mattermost-api-ready-v1"
WAIT_CODE = '''def _wait_for_mattermost_api(timeout_s=60):
    """Compose startup is not HTTP readiness; wait before fixture writes."""
    from urllib.request import urlopen
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urlopen("http://127.0.0.1:8065/api/v4/system/ping", timeout=2) as response:
                if response.status == 200 and json.load(response).get("status") == "OK":
                    return
        except (OSError, ValueError):
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError("Mattermost API did not become ready within initialization deadline")


'''


def patch_source(source: str) -> str:
    if MARKER in source:
        return source
    anchor = '        logger.info("Mattermost backend started successfully")'
    definition = "def start_mattermost_backend("
    if source.count(anchor) != 1 or source.count(definition) != 1:
        raise ValueError("Unknown official Mattermost helper; inspect before applying")
    patched = source.replace(definition, MARKER + "\n" + WAIT_CODE + definition, 1)
    patched = patched.replace(anchor, "        _wait_for_mattermost_api()\n" + anchor, 1)
    compile(patched, MATTERMOST_HELPER, "exec")
    return patched


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--container", default="mobile_world_env_0")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--host-helper", type=Path,
                        help="Host source of a read-only bind mount; must match container bytes")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    original = subprocess.check_output(["docker", "exec", args.container, "cat", MATTERMOST_HELPER])
    patched = patch_source(original.decode("utf-8")).encode("utf-8")
    sha = lambda value: hashlib.sha256(value).hexdigest()
    args.archive.mkdir(parents=True, exist_ok=True)
    (args.archive / (sha(original) + ".original.py")).write_bytes(original)
    report = {"before_sha256": sha(original), "after_sha256": sha(patched),
              "changed": original != patched, "applied": False, "restart_required": False}
    if args.apply and original != patched:
        if args.host_helper is not None:
            if args.host_helper.read_bytes() != original:
                raise ValueError("Host helper differs from active container source")
            args.host_helper.write_bytes(patched)
        else:
            with tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "mattermost.py"
                source.write_bytes(patched)
                subprocess.run(["docker", "cp", str(source), f"{args.container}:{MATTERMOST_HELPER}"], check=True)
        installed = subprocess.check_output(["docker", "exec", args.container, "cat", MATTERMOST_HELPER])
        if installed != patched:
            raise RuntimeError("Container source does not match installed readiness patch")
        report.update(applied=True, restart_required=True)
    (args.archive / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
