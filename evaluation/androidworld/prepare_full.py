"""Freeze a fresh full suite from current source and a trusted prior fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from freeze_runner import freeze_scoring_runner
from scoring import POLICY
from build_oracle import build_oracle
from audit_observation_contract import audit_observation_contract


def prepare(project: Path, baseline: Path, output: Path):
    project, baseline, output = project.resolve(), baseline.resolve(), output.resolve()
    # Full runner's paths intentionally target a direct child of project/data.
    if output.parent != project / "data":
        raise ValueError("Output must be a fresh direct child of <project>/data")
    if output.exists():
        raise FileExistsError(f"Refuse to overwrite existing evidence: {output}")
    matrix = json.loads((baseline / "matrix.json").read_text(encoding="utf-8"))
    if len(matrix) != 116:
        raise ValueError("Requires a trusted frozen 116-task baseline")
    output.mkdir()
    git = ["git", "--git-dir=" + (project / ".git").as_posix()]
    names = subprocess.check_output(git + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=project).decode().split("\0")
    for name in sorted(set(names)):
        if not name:
            continue
        relative = Path(name)
        if relative.parts[0] in ("data", ".venv", ".git") or relative.name == ".env":
            continue
        source = project / relative
        if source.is_file():
            dest = output / "runtime" / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    (output / "working-tree.patch").write_bytes(subprocess.check_output(git + ["diff", "--binary", "HEAD"], cwd=project))
    for name in ("runner", "latest-shallow"):
        shutil.copytree(baseline / name, output / name,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "skills" if name == "runner" else "__pycache__"))
    for name in ("probe_model.py", "status.py", "index_evidence.py", "frozen-params.pkl", "matrix.json"):
        shutil.copy2(baseline / name, output / name)
    # Provisioning fixes must come from the runtime being evaluated, not from a
    # historical fixture.  Otherwise every new batch inherits old setup bugs.
    shutil.copy2(Path(__file__).with_name("setup_full.py"), output / "setup_full.py")
    freeze_scoring_runner(output / "runtime", output)
    contract = audit_observation_contract(output / "latest-shallow/android_world", output / "runner/oracle/OracleDump.java")
    (output / "observation-contract.json").write_text(json.dumps(contract, indent=2), encoding="utf-8")
    build_oracle(output / "runner/oracle/OracleDump.java", output / "runner/oracle/oracle.jar")
    hashes = {}
    for folder in ("runtime", "runner", "latest-shallow/android_world"):
        for path in (output / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                hashes[path.relative_to(output).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("run_full.py", "setup_full.py", "probe_model.py", "matrix.json", "frozen-params.pkl", "scoring-policy.json", "observation-contract.json"):
        hashes[name] = hashlib.sha256((output / name).read_bytes()).hexdigest()
    (output / "source-hashes.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")
    with zipfile.ZipFile(output / "runtime-source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (output / "runtime").rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(output / "runtime"))
    (output / "protocol.json").write_text(json.dumps({
        "task_count": 116, "baseline": str(baseline), "model": "chatgpt/gpt-5.6-sol",
        "params_and_budgets": "unchanged frozen instances", "history_tokens": 16000,
        "scoring_policy": POLICY, "official_submission": False,
        "initialization": "setup_full before run; official initialize_task independently per case",
    }, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.project, args.baseline, args.output)
