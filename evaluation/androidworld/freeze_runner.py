"""Copy the selected runtime's scoring code into a new, unsealed full batch."""
import json
from pathlib import Path
import shutil


def freeze_scoring_runner(runtime: Path, batch: Path) -> None:
    # Never patch a sealed or previously executed evaluation in place.
    if (batch / "source-hashes.json").exists() or (batch / "episodes").exists():
        raise RuntimeError("Scoring updates require a fresh, unsealed batch")
    source = runtime / "evaluation/androidworld"
    (batch / "runner").mkdir(parents=True, exist_ok=True)
    for name in ("scoring.py", "run_emulator.py", "agent_worker.py", "device_settings.py", "result_classification.py", "quota_resume.py", "episode_cleanup.py", "long_run_health.py", "setup_full.py", "portable.py"):
        shutil.copy2(source / name, batch / "runner" / name)
    shutil.copy2(source / "run_full.py", batch / "run_full.py")
    shutil.copytree(source / "oracle", batch / "runner/oracle", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.jar", "*.class"))
    (batch / "scoring-policy.json").write_text(json.dumps({
        "official_submission": False,
        "observation": "Independent API 33 UiAutomation forest without content-idle wait; official protobuf and forest conversion; three bounded acquisition attempts; capture errors unscored",
        "predicate": "Unmodified official task.is_successful; no filename or per-task scoring overrides",
        "source": "runtime/evaluation/androidworld",
    }, indent=2), encoding="utf-8")
