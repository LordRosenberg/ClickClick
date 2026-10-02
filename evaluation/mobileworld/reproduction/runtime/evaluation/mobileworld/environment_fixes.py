"""Environment-only repairs and initialization checks for nested Mattermost.

No task answers, grader conditions or device actions are changed here.
"""

from __future__ import annotations

import re
import ast
import hashlib
import subprocess
from typing import Any

from evaluation.mobileworld.long_run_health import MATTERMOST_HELPER, MobileWorldTarget
from evaluation.mobileworld.patch_mattermost_readiness import WAIT_CODE


def verify_mattermost_readiness_fix(target: MobileWorldTarget, apps: list[str]) -> dict[str, Any]:
    """Reject an unpatched/new environment before spending any model budget."""
    if "Mattermost" not in apps:
        return {"required": False}
    raw = subprocess.check_output(
        ["docker", "exec", target.container, "cat", MATTERMOST_HELPER], timeout=20,
    )
    tree = ast.parse(raw.decode("utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    expected = ast.parse(WAIT_CODE).body[0]
    wait = functions.get("_wait_for_mattermost_api")
    start = functions.get("start_mattermost_backend")
    valid = wait is not None and ast.dump(wait) == ast.dump(expected) and start is not None and any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_wait_for_mattermost_api" for node in ast.walk(start)
    )
    if not valid:
        raise RuntimeError(
            "Mattermost readiness prerequisite missing or changed; install/verify "
            "evaluation.mobileworld.patch_mattermost_readiness and restart the container before testing"
        )
    return {"required": True, "ready": True, "helper_sha256": hashlib.sha256(raw).hexdigest()}


def inspect_task_asset_access(target: MobileWorldTarget, apps: list[str]) -> dict[str, Any]:
    """Probe the actual nested-container route after official initialization."""
    if "Mattermost" not in apps:
        return {"required": False, "ready": True}
    try:
        result = subprocess.run(
            ["docker", "exec", target.container, "docker", "exec",
             "mattermost-docker-mattermost-1", "curl", "-fsS", "--max-time", "5",
             "-o", "/dev/null", "-w", "%{http_code}", "http://10.0.2.2:6800/health"],
            capture_output=True, timeout=15,
        )
        ready = result.returncode == 0 and result.stdout.strip() == b"200"
    except (OSError, subprocess.SubprocessError):
        ready = False
    return {"required": True, "ready": ready,
            "failure": None if ready else "nested_task_asset_server_unreachable"}


def ensure_task_asset_route(target: MobileWorldTarget, apps: list[str]) -> dict[str, Any]:
    """Make the emulator's host alias reach the same asset server from Docker.

    Only traffic from nested Docker bridges to 10.0.2.2:6800 is redirected;
    ordinary emulator traffic and all other destinations remain unchanged.
    Reapply after outer-container restarts, which discard namespace rules.
    """
    if "Mattermost" not in apps:
        return {"required": False}
    rule = ["PREROUTING", "-i", "br+", "-d", "10.0.2.2/32", "-p", "tcp",
            "--dport", "6800", "-j", "REDIRECT", "--to-ports", "6800"]
    prefix = ["docker", "exec", target.container, "iptables", "-t", "nat"]
    probe = subprocess.run([*prefix, "-C", *rule], capture_output=True, timeout=20)
    if probe.returncode not in {0, 1}:
        raise RuntimeError("Cannot inspect MobileWorld task asset route")
    if probe.returncode:
        subprocess.run([*prefix, "-A", *rule], check=True, capture_output=True, timeout=20)
    subprocess.run([*prefix, "-C", *rule], check=True, capture_output=True, timeout=20)
    return {"required": True, "ready": True, "installed": bool(probe.returncode),
            "scope": "nested_docker_bridge_to_emulator_host_asset_port"}


def classify_initialization_errors(log: str) -> list[str]:
    """Retain categories only, never log bodies, credentials or task content."""
    failures = set()
    for line in log.splitlines():
        # Match the logger header, not ERROR-like strings in a post/log body.
        match = re.match(
            r"^(?:[^|\n]*\|\s*)?ERROR\s*\|\s*"
            r"mobile_world\.runtime\.app_helpers\.mattermost:(\w+):\d+\s*-", line,
        )
        if match:
            failures.add("mattermost_" + match.group(1) + "_failed")
    return sorted(failures)


def inspect_initialization_errors(target: MobileWorldTarget, apps: list[str],
                                  since: str) -> dict[str, Any]:
    if "Mattermost" not in apps:
        return {"ready": True, "required": False, "failures": []}
    try:
        result = subprocess.run(
            ["docker", "logs", "--since", since, target.container],
            check=True, capture_output=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return {"ready": False, "required": True, "failures": ["initialization_log_unavailable"]}
    failures = classify_initialization_errors(
        (result.stdout + result.stderr).decode("utf-8", errors="replace")
    )
    return {"ready": not failures, "required": True, "failures": failures}
