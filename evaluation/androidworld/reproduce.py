"""Prepare and run the published 116-instance AndroidWorld evaluation."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import importlib.metadata
import io
import json
import os
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
UPSTREAM = "e3fea3ccc69787570e282c99573298f1c3019a34"
FIXTURE_SHA256 = "00121d1325027ce6ee1926284e1546ad031f9371e1a59d8e733f55af1ed7064f"
RUNNER_FILES = (
    "run_emulator.py", "agent_worker.py", "device_settings.py", "scoring.py",
    "result_classification.py", "quota_resume.py", "episode_cleanup.py",
    "long_run_health.py", "setup_full.py", "portable.py",
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_digest(path):
    # Git's Windows checkout may convert LF to CRLF; fixture content is unchanged.
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def download_upstream(destination):
    """Download into a new directory; no symlinks or archive path traversal."""
    url = f"https://api.github.com/repos/google-research/android_world/zipball/{UPSTREAM}"
    request = urllib.request.Request(url, headers={"User-Agent": "ClickClick-reproduction"})
    with urllib.request.urlopen(request, timeout=60) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()
    for member in archive.infolist():
        pieces = member.filename.split("/", 1)
        if len(pieces) != 2 or not pieces[1] or member.is_dir():
            continue
        target = (root / pieces[1]).resolve()
        if root not in target.parents or (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Unsafe upstream archive entry")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(member))
    write_json(destination / "clickclick-upstream.json", {"commit": UPSTREAM})


def prepare_upstream(destination):
    """Apply the disclosed database-isolation repair; keep scoring unchanged."""
    controller = destination / "android_world/env/android_world_controller.py"
    source = controller.read_text(encoding="utf-8")
    before = "    # First delete old .db, .db-wal, and .db-shm files.\n    file_utils.clear_directory(remote_db_directory, self)"
    after = '''    # ClickClick setup repair: preserve unrelated databases.
    remote_db_name = os.path.basename(remote_db_file_path)
    for file_name in (remote_db_name, remote_db_name + "-wal", remote_db_name + "-shm"):
      file_utils.remove_single_file(file_name, remote_db_directory, self)'''
    if source.count(before) != 1:
        raise ValueError("Pinned AndroidWorld controller changed; review setup repair")
    controller.write_text(source.replace(before, after), encoding="utf-8")
    from grpc_tools import protoc
    for path in sorted(destination.rglob("*.proto")):
        result = protoc.main(["grpc_tools.protoc", "-I" + str(destination),
                              "--python_out=" + str(destination),
                              "--grpc_python_out=" + str(destination), str(path)])
        if result:
            raise RuntimeError("AndroidWorld protobuf generation failed")


def snapshot_runtime(project, target):
    """Freeze product files only; never copy environment files or raw data."""
    tracked = None
    if (project / ".git").exists():
        tracked = set(subprocess.check_output(
            ["git", "-C", str(project), "ls-files", "-z"]).decode().split("\0"))
    for folder in ("agent", "driver", "perception", "shared", "skills"):
        for source in (project / folder).rglob("*"):
            rel = source.relative_to(project)
            if tracked is not None and rel.as_posix() not in tracked:
                continue
            if (not source.is_file() or source.is_symlink() or
                    any(part in {"__pycache__", "_pending", ".git"} for part in rel.parts)):
                continue
            if source.suffix not in {".py", ".md", ".json", ".txt"} and rel.as_posix() != "driver/vendor/scrcpy-server-v3.3.1.jar":
                continue
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    shutil.copytree(HERE / "skills", target / "evaluation/androidworld/skills")


def prepare(project, output, upstream=None):
    """Create all inputs from public source; no historical batch is required."""
    from evaluation.androidworld import fixture_codec
    from evaluation.androidworld.audit_observation_contract import audit_observation_contract
    from evaluation.androidworld.build_oracle import build_oracle
    from evaluation.androidworld.scoring import POLICY
    project, output = project.resolve(), output.resolve()
    if output.parent != project / "data":
        raise ValueError("Output must be a fresh direct child of the project's data directory")
    if fixture_digest(HERE / "fixtures/instances.json") != FIXTURE_SHA256:
        raise ValueError("Published fixture checksum mismatch")
    output.mkdir(parents=True, exist_ok=False)
    official = output / "latest-shallow"
    if upstream is None:
        download_upstream(official)
    else:
        # Offline source overrides must be an unmodified checkout of the pin.
        commit = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(upstream), "status", "--porcelain"], text=True)
        if commit != UPSTREAM or dirty.strip():
            raise ValueError("Offline AndroidWorld source must be clean at the pinned commit")
        shutil.copytree(upstream, official, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    prepare_upstream(official)
    sys.path.insert(0, str(official))
    # A running process must not accidentally deserialize against another checkout.
    if "android_world" in sys.modules:
        raise RuntimeError("Prepare in a fresh process before importing AndroidWorld")
    records = fixture_codec.load(HERE / "fixtures/instances.json")
    from android_world import registry
    classes = registry.TaskRegistry().get_registry("android_world")
    if set(classes) != {r["task"] for r in records}:
        raise ValueError("Frozen instances do not match the pinned registry")
    (output / "frozen-params.pkl").write_bytes(pickle.dumps(records))
    # JSON is an inspectable index; the local pickle preserves typed parameters.
    write_json(output / "matrix.json", json.loads((HERE / "fixtures/instances.json").read_text(encoding="utf-8"))["records"])
    runner = output / "runner"
    runner.mkdir()
    for name in RUNNER_FILES:
        shutil.copy2(HERE / name, runner / name)
    shutil.copytree(HERE / "oracle", runner / "oracle", ignore=shutil.ignore_patterns("*.jar", "*.class"))
    for name in ("run_full.py", "setup_full.py", "probe_model.py"):
        shutil.copy2(HERE / name, output / name)
    snapshot_runtime(project, output / "runtime")
    contract = audit_observation_contract(official / "android_world", runner / "oracle/OracleDump.java")
    write_json(output / "observation-contract.json", contract)
    build_oracle(runner / "oracle/OracleDump.java", runner / "oracle/oracle.jar")
    write_json(output / "protocol.json", {
        "upstream_commit": UPSTREAM, "task_count": 116, "fixture_sha256": FIXTURE_SHA256,
        "seed": 20260914, "history_tokens": 16000, "architecture": "plan_executor",
        "profile": "androidworld", "model": os.environ.get("CLICKCLICK_EVAL_MODEL", "chatgpt/gpt-5.6-sol"),
        "scoring_policy": POLICY, "setup_repair": "preserve unrelated SQLite databases",
        "runtime": "runtime", "instances": "frozen-params.pkl", "official_submission": False,
    })
    write_json(output / "scoring-policy.json", {"policy": POLICY, "predicate": "Unmodified official task.is_successful"})
    write_json(output / "dependency-versions.json", {
        dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions()
        if dist.metadata["Name"]
    })
    # Manifest names are always batch-relative, including on Windows.
    write_json(output / "source-hashes.json", {
        p.relative_to(output).as_posix(): digest(p) for p in sorted(output.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    })


def export_summary(batch, output):
    """Export a small explicit field allowlist; raw traces remain local."""
    rows = []
    allowed = {r["task"] for r in json.loads((HERE / "fixtures/instances.json").read_text(encoding="utf-8"))["records"]}
    for path in sorted((batch / "episodes").glob("*/plan_executor/result.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        case = path.parent.parent.name
        if case not in allowed:
            raise ValueError("Unknown task in result directory")
        clean = {"task": case}
        for key in ("score", "valid", "budgeted_success", "episode_steps", "role_invocations", "elapsed_s", "teardown_ok"):
            value = row.get(key)
            if value is not None and type(value) not in (bool, int, float):
                raise ValueError("Unexpected result field type: " + key)
            clean[key] = value
        rows.append(clean)
    write_json(output, {"tasks": rows, "evaluated": len(rows),
                        "predicate_successes": sum(r["valid"] is True and r["score"] == 1 for r in rows)})


def verify_batch(batch):
    """Reject drift before setup, model probes or scored execution."""
    root = batch.resolve()
    hashes = json.loads((root / "source-hashes.json").read_text(encoding="utf-8"))
    if not hashes:
        raise ValueError("Empty frozen source manifest")
    for name, expected in hashes.items():
        path = (root / name).resolve()
        if root not in path.parents or path.is_symlink() or digest(path) != expected:
            raise ValueError("Frozen evaluation inputs changed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/androidworld-run"))
    parser.add_argument("--install", action="store_true", help="Create a separate evaluation environment and install pinned dependencies")
    parser.add_argument("--eval-python", type=Path, help="Use an existing evaluation Python environment")
    parser.add_argument("--agent-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--env-file", type=Path, default=PROJECT / ".env")
    parser.add_argument("--adb", help="ADB executable; otherwise resolve from PATH or Android SDK environment")
    parser.add_argument("--model", default="chatgpt/gpt-5.6-sol")
    parser.add_argument("--upstream", type=Path, help="Optional clean offline checkout at the pinned AndroidWorld revision")
    parser.add_argument("--prepare-only", action="store_true", help="Freeze inputs and build the oracle without device changes or model calls")
    parser.add_argument("--resume", action="store_true", help="Continue the existing sealed batch without discarding completed attempts")
    parser.add_argument("--resume-after-quota", action="store_true")
    parser.add_argument("--export-summary", type=Path, help="Export shareable results only; no device or model access")
    args = parser.parse_args(argv)
    batch = (PROJECT / args.output).resolve()
    if args.export_summary:
        export_summary(batch, args.export_summary)
        return
    environment = os.environ.copy()
    environment.update(CLICKCLICK_EVAL_AGENT_PYTHON=str(args.agent_python.resolve()),
                       CLICKCLICK_EVAL_ENV_FILE=str(args.env_file.resolve()), CLICKCLICK_EVAL_MODEL=args.model,
                       PYTHONIOENCODING="utf-8")
    if args.adb:
        environment["CLICKCLICK_EVAL_ADB"] = args.adb
    eval_python = args.eval_python
    if args.install:
        location = PROJECT / "data/androidworld-eval-venv"
        if not location.exists():
            venv.EnvBuilder(with_pip=True).create(location)
        eval_python = location / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(eval_python), "-m", "pip", "install", "-r", str(HERE / "requirements.txt"),
                        "-e", str(PROJECT) + "[decode]"], check=True)
    if eval_python and eval_python.resolve() != Path(sys.executable).resolve():
        forwarded = list(argv if argv is not None else sys.argv[1:])
        if "--install" in forwarded:
            forwarded.remove("--install")
        if "--eval-python" in forwarded:
            index = forwarded.index("--eval-python")
            del forwarded[index:index + 2]
        if "--agent-python" not in forwarded:
            forwarded += ["--agent-python", str(args.agent_python.resolve())]
        for option in ("--output", "--env-file", "--agent-python", "--upstream"):
            if option in forwarded:
                index = forwarded.index(option) + 1
                forwarded[index] = str(batch if option == "--output" else Path(forwarded[index]).resolve())
        subprocess.run([str(eval_python), "-m", "evaluation.androidworld.reproduce", *forwarded],
                       cwd=PROJECT, env=environment, check=True)
        return
    os.environ.update({k: v for k, v in environment.items() if k.startswith("CLICKCLICK_EVAL_")})
    if any(importlib.util.find_spec(name) is None for name in ("android_env", "grpc_tools", "numpy", "PIL")):
        parser.error("Evaluation dependencies missing: rerun with --install")
    if not (args.resume or args.resume_after_quota):
        prepare(PROJECT, batch, args.upstream)
    else:
        protocol = json.loads((batch / "protocol.json").read_text(encoding="utf-8"))
        if protocol["model"] != args.model:
            parser.error("Resume must use the original model")
    verify_batch(batch)
    if args.prepare_only:
        print("Prepared 116 instances; no device or model calls were made.")
        return
    from evaluation.androidworld.portable import adb_executable
    environment["CLICKCLICK_EVAL_ADB"] = adb_executable()
    environment["CLICKCLICK_EVAL_MODEL"] = args.model
    if not (batch / "setup-complete.json").is_file():
        subprocess.run([sys.executable, str(batch / "setup_full.py"), "--root", str(batch),
                        "--adb", environment["CLICKCLICK_EVAL_ADB"]], env=environment, check=True)
    subprocess.run([str(args.agent_python.resolve()), str(batch / "probe_model.py")], env=environment, check=True)
    command = [sys.executable, str(batch / "run_full.py")]
    if args.resume_after_quota:
        command.append("--resume-after-quota")
    subprocess.run(command, env=environment, check=True)
    export_summary(batch, batch / "summary.public.json")


if __name__ == "__main__":
    main()
