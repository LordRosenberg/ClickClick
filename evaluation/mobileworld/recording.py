"""One uninterrupted device recording per MobileWorld episode."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from driver.adb import adb_bin
from evaluation.mobileworld.long_run_health import MobileWorldTarget, atomic_json, verify_identity
from evaluation.mobileworld.video_integrity import inspect_video


class RecordingError(RuntimeError):
    """The evidence recorder could not capture a complete episode."""


class EpisodeRecorder:
    def __init__(self, target: MobileWorldTarget, directory: Path):
        self.target = target
        self.directory = directory
        self.stop_event = threading.Event()
        self.started_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.remote_pid: int | None = None
        self.remote = f"/data/local/tmp/clickclick-mobileworld-{uuid4().hex}.mp4"
        self.segments: list[dict] = []
        self.error: str | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.stop_requested_at: float | None = None

    def _adb(self, *args: str, timeout: float = 15) -> subprocess.CompletedProcess:
        return subprocess.run(
            [adb_bin(), "-s", self.target.adb_target, *args],
            capture_output=True, check=True, timeout=timeout,
        )

    def start(self) -> None:
        verify_identity(self.target)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self._capture, name="mobileworld-recorder", daemon=True)
        self.thread.start()
        if not self.started_event.wait(20):
            try:
                self.stop()
            except Exception:
                pass
            raise RecordingError("MobileWorld recorder did not become ready")
        if self.error:
            raise RecordingError(f"MobileWorld recorder failed to start: {self.error}")

    def _capture(self) -> None:
        try:
            # exec preserves the shell PID; stop targets only this recorder.
            script = f"echo $$; exec screenrecord --time-limit 0 --bit-rate 4000000 {self.remote}"
            self.process = subprocess.Popen(
                [adb_bin(), "-s", self.target.adb_target, "shell", "sh", "-c", shlex.quote(script)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            line = self.process.stdout.readline().strip()
            if not line.isdigit():
                raise RecordingError("screenrecord did not report its remote PID")
            self.remote_pid = int(line)
            atomic_json(self.directory / "recording-active.json", {
                "device": self.target.adb_target, "pid": self.remote_pid,
                "remote": self.remote, "started_at": time.time(),
            })
            deadline = time.monotonic() + 12
            while not self.stop_event.is_set():
                if self.process.poll() is not None:
                    raise RecordingError("screenrecord exited before recording was ready")
                result = subprocess.run(
                    [adb_bin(), "-s", self.target.adb_target, "shell", "stat", "-c", "%s", self.remote],
                    capture_output=True, timeout=5,
                )
                size = result.stdout.strip()
                if result.returncode == 0 and size.isdigit() and int(size) >= 32:
                    break
                if time.monotonic() >= deadline:
                    raise RecordingError("screenrecord did not create its MP4 header")
                time.sleep(0.2)
            self.started_at = time.time()
            self.started_event.set()
            _, stderr = self.process.communicate()
            self.stopped_at = time.time()
            if not self.stop_event.is_set():
                raise RecordingError(f"screenrecord exited during the episode: {stderr[-500:]!r}")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.started_event.set()

    def _signal_stop(self) -> None:
        if self.remote_pid is None or self.process is None or self.process.poll() is not None:
            return
        cmdline = self._adb("shell", "cat", f"/proc/{self.remote_pid}/cmdline").stdout
        if self.remote.encode() not in cmdline or b"screenrecord" not in cmdline:
            raise RecordingError("refusing to signal a recorder with changed process identity")
        self._adb("shell", "kill", "-2", str(self.remote_pid))

    def stop(self) -> dict:
        if self.stop_event.is_set() and (self.directory / "recording-manifest.json").exists():
            if self.error:
                raise RecordingError(self.error)
            return self._report()
        self.stop_requested_at = time.time()
        self.stop_event.set()
        try:
            self._signal_stop()
            if self.thread:
                self.thread.join(25)
                if self.thread.is_alive():
                    raise RecordingError("screenrecord did not finalize after SIGINT")
            local = self.directory / "screen.mp4"
            self._adb("pull", self.remote, str(local), timeout=90)
            integrity = inspect_video(local)
            with local.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            self.segments = [{"file": local.name, "bytes": local.stat().st_size,
                              "sha256": digest, **integrity}]
            self._adb("shell", "rm", "-f", self.remote)
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"
        finally:
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            report = self._report()
            atomic_json(self.directory / "recording-manifest.json", report)
        if self.error or not self.segments:
            raise RecordingError(f"MobileWorld recording incomplete: {self.error or 'no video'}")
        return report

    def _report(self) -> dict:
        return {"device": self.target.adb_target, "mode": "continuous",
                "segments": self.segments, "interruptions": [], "error": self.error,
                "started_at": self.started_at, "stopped_at": self.stopped_at,
                "stop_requested_at": self.stop_requested_at}


def finalize_interrupted_recording(target: MobileWorldTarget, directory: Path) -> None:
    """Bounded cleanup when the batch deadline kills the episode process."""
    active = directory / "recording-active.json"
    if not active.exists():
        return
    state = json.loads(active.read_text(encoding="utf-8"))
    remote, pid = str(state.get("remote", "")), state.get("pid")
    if (state.get("device") != target.adb_target or not isinstance(pid, int) or pid <= 1
            or not re.fullmatch(r"/data/local/tmp/clickclick-mobileworld-[0-9a-f]{32}\.mp4", remote)):
        raise RecordingError("invalid interrupted recorder identity")
    recorder = EpisodeRecorder(target, directory)
    try:
        cmdline = recorder._adb("shell", "cat", f"/proc/{pid}/cmdline").stdout
    except subprocess.CalledProcessError:
        cmdline = b""  # Already exited; still attempt to preserve the file.
    if cmdline:
        if remote.encode() not in cmdline or b"screenrecord" not in cmdline:
            raise RecordingError("interrupted recorder PID belongs to another process")
        recorder._adb("shell", "kill", "-2", str(pid))
        for _ in range(25):
            time.sleep(0.2)
            try:
                recorder._adb("shell", "cat", f"/proc/{pid}/cmdline")
            except subprocess.CalledProcessError:
                break
        else:
            raise RecordingError("interrupted recorder did not exit")
    recorder._adb("pull", remote, str(directory / "screen-interrupted.mp4"), timeout=90)
    atomic_json(directory / "recording-manifest.json", {
        "mode": "continuous", "segments": [], "error": "episode process deadline exceeded",
        "partial_file": "screen-interrupted.mp4", "device": target.adb_target,
    })
    recorder._adb("shell", "rm", "-f", remote)
