"""September 21 v6 startup: prepare the first dependency window, then run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "runner"))
if __package__:
    from .portable import adb_executable, agent_python
    from .long_run_health import (
        upcoming_setup_window, preventive_restart_check,
        initialize_app_scope, restart_same_avd_and_initialize,
    )
else:
    from portable import adb_executable, agent_python
    from long_run_health import (
        upcoming_setup_window, preventive_restart_check,
        initialize_app_scope, restart_same_avd_and_initialize,
    )


def adb(*args):
    return subprocess.check_output([adb_executable(), "-s", "emulator-5554", *args], timeout=40)


def state(status, **extra):
    value = dict(status=status, updated_at=time.time(), **extra)
    (ROOT / "launch-state.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print("LAUNCH", json.dumps(value), flush=True)


def ffmpeg_executable():
    configured = os.environ.get("CLICKCLICK_EVAL_FFMPEG")
    if configured:
        return configured
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def main(resume_after_quota=False, resume_after_environment=False):
    try:
        for relative, digest in json.loads((ROOT / "source-hashes.json").read_text(encoding="utf-8")).items():
            path = (ROOT / relative).resolve()
            if ROOT.resolve() not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Frozen source changed before launch")
        if (ROOT / "STOP").exists() or (ROOT / "PAUSE_AFTER_EPISODE").exists():
            raise RuntimeError("Remove the batch stop/pause marker before launching")
        state("model_probe")
        subprocess.run([agent_python(), "-u", str(ROOT / "probe_model.py")], cwd=PROJECT, check=True)
        if not json.loads((ROOT / "model-probe.json").read_text(encoding="utf-8"))["available"]:
            raise RuntimeError("Model probe failed")
        records = json.loads((ROOT / "matrix.json").read_text(encoding="utf-8"))
        # On resume, run_full owns interrupted-episode cleanup and the next
        # dependency window. Never initialize apps ahead of that boundary.
        if not (ROOT / "run-state.json").exists():
            window = upcoming_setup_window(records)
            maintenance = preventive_restart_check(adb, records[0])
            state("initializing_environment", setup_window=window, maintenance=maintenance)
            adb_path = Path(adb_executable())
            if maintenance["restart_due"]:
                restart_same_avd_and_initialize(ROOT, adb_path, python=Path(sys.executable), apps=window["apps"])
            else:
                initialize_app_scope(ROOT, adb_path, python=Path(sys.executable), apps=window["apps"])
            if preventive_restart_check(adb, records[0])["restart_due"]:
                restart_same_avd_and_initialize(ROOT, adb_path, python=Path(sys.executable), apps=window["apps"])
            if preventive_restart_check(adb, records[0])["restart_due"]:
                raise RuntimeError("Insufficient uptime allowance after startup preparation")
        recording_stdout = (ROOT / "host-recording-sidecar.stdout.log").open("ab")
        recording_stderr = (ROOT / "host-recording-sidecar.stderr.log").open("ab")
        recording = subprocess.Popen([
            agent_python(), "-u", str(ROOT / "runner/host_recording_sidecar.py"),
            "--batch", str(ROOT), "--ffmpeg", ffmpeg_executable(),
        ], cwd=PROJECT, stdout=recording_stdout, stderr=recording_stderr,
           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(1)
        if recording.poll() is not None:
            raise RuntimeError("Host recording sidecar failed before the runner started")
        state("full_runner_started", first_case=records[0]["task"], total=len(records))
        command = [sys.executable, "-u", str(ROOT / "run_full.py")]
        if resume_after_quota:
            command.append("--resume-after-quota")
        if resume_after_environment:
            command.append("--resume-after-environment")
        try:
            result = subprocess.run(command, cwd=PROJECT)
        finally:
            try:
                recording.wait(timeout=30)
            except subprocess.TimeoutExpired:
                recording.terminate()
                recording.wait(timeout=10)
            recording_stdout.close()
            recording_stderr.close()
        state("full_runner_exited", returncode=result.returncode)
        return result.returncode
    except Exception as exc:
        state("launch_failed", error=repr(exc))
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-after-quota", action="store_true")
    parser.add_argument("--resume-after-environment", action="store_true")
    args = parser.parse_args()
    raise SystemExit(main(args.resume_after_quota, args.resume_after_environment))
