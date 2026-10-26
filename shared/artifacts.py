"""Local filesystem artifact store for screenshots, trees, and LLM payloads."""

from __future__ import annotations

import json
import hashlib
import uuid
import time
import os
import threading
from contextlib import contextmanager
from collections import Counter
from pathlib import Path
from typing import Any


def image_suffix(data: bytes) -> str:
    """Return a browser-renderable suffix for supported encoded images."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".bin"


class ArtifactStore:
    """Store large binary/text artifacts as files and return relative refs."""

    def __init__(self, root: Path, *, create: bool = True) -> None:
        self.root = root
        self._lock = threading.RLock()
        self.protected_refs = lambda: set()
        self._pending_refs: Counter[str] = Counter()
        self._pin_id = f"{os.getpid()}-{uuid.uuid4().hex}"
        import psutil
        self._process_started_at = psutil.Process().create_time()
        if not create:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        # `shots/` is intentionally NOT created: raw screenshots are not
        # persisted (only the SoM annotated frame under `som/`). The `trees/`
        # kind stores rendered semantic-tree JSON, `som/` stores annotated
        # PNGs, and `llm/` stores LLM input/output payloads.
        for sub in ("trees", "som", "llm"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def save_bytes(self, kind: str, data: bytes, suffix: str = ".bin") -> str:
        """Persist bytes under kind/ and return a relative reference path."""
        name = f"{uuid.uuid4().hex}{suffix}"
        rel = f"{kind}/{name}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        self._publish(rel, data)
        return rel

    def save_content_addressed_bytes(
        self, kind: str, data: bytes, suffix: str = ".bin",
    ) -> str:
        """Persist immutable bytes by digest and reuse identical content."""
        digest = hashlib.sha256(data).hexdigest()
        rel = f"{kind}/{digest}{suffix}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        self._publish(rel, data, expected_digest=digest)
        return rel

    def save_json(self, kind: str, payload: Any) -> str:
        """Persist JSON by content hash and return a stable, deduplicated ref.

        Existing content is verified before reuse; publication uses a flushed
        temporary file and atomic replacement, including after interrupted writes.
        """
        encoded = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{digest}.json"
        rel = f"{kind}/{name}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        self._publish(rel, encoded, expected_digest=digest)
        return rel

    @contextmanager
    def publication(self):
        """Coordinate publication and retention across store instances/processes."""
        from shared.file_lock import FileLock
        with self._lock:
            with FileLock(self.root / ".publication.lock",timeout=5):
                yield

    def _publish(self, ref: str, data: bytes, *, expected_digest=None):
        path = self.resolve(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.publication():
            self._pending_refs[ref]+=1
            self._write_pins()
            if expected_digest and path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest:
                    return
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if expected_digest and hashlib.sha256(temporary.read_bytes()).hexdigest() != expected_digest:
                    raise OSError("artifact publication integrity failure")
                os.replace(temporary, path)
                if os.name != "nt":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)

    def _write_pins(self):
        path = self.root / f".pins-{self._pin_id}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w",encoding="utf-8") as stream:
            json.dump({"pid":os.getpid(),"started_at":self._process_started_at,"refs":sorted(self._pending_refs)},stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,path)

    def release_pins(self,refs):
        with self.publication():
            for ref in refs:
                if self._pending_refs[ref]>1:
                    self._pending_refs[ref]-=1
                else:
                    self._pending_refs.pop(ref,None)
            self._write_pins()

    def reconcile_pins(self):
        """Called after durable references are available, before startup retention."""
        import psutil
        with self.publication():
            for path in self.root.glob(".pins-*.json"):
                packet=json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(packet,dict):
                    continue  # Retain an older pin format conservatively.
                try:
                    alive=abs(psutil.Process(packet["pid"]).create_time()-packet["started_at"])<.01
                except psutil.NoSuchProcess:
                    alive=False
                except psutil.AccessDenied:
                    alive=True
                if not alive:
                    path.unlink()  # Committed refs remain protected by the store callback.

    def verify(self, ref: str) -> bytes:
        data = self.read_bytes(ref)
        stem = Path(ref).stem
        if len(stem) == 64 and all(ch in "0123456789abcdef" for ch in stem):
            if hashlib.sha256(data).hexdigest() != stem:
                raise ValueError(f"artifact integrity failure: {ref}")
        return data

    def resolve(self, ref: str) -> Path:
        """Resolve an authorized relative artifact ref within the store."""
        candidate = (self.root / ref).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("artifact ref escapes store")
        return candidate

    def read_bytes(self, ref: str) -> bytes:
        """Read artifact bytes by relative ref."""
        return self.resolve(ref).read_bytes()

    def read_text(self, ref: str) -> str:
        """Read artifact text by relative ref."""
        return self.resolve(ref).read_text(encoding="utf-8")

    def prune(self, kind: str, *, max_age_seconds: float | None = None, max_files: int | None = None) -> int:
        """Apply bounded retention to one artifact namespace.

        Callers choose policy explicitly; active refs are not guessed here.
        Oldest files are removed first after the optional age filter.
        """
        with self.publication():
            return self._prune_locked(kind, max_age_seconds=max_age_seconds, max_files=max_files)

    def _prune_locked(self, kind, *, max_age_seconds=None, max_files=None):
        directory = self.resolve(kind)
        if not directory.exists() or not directory.is_dir():
            return 0
        protected = set(self.protected_refs()) | set(self._pending_refs)
        for pins in self.root.glob(".pins-*.json"):
            # Incomplete pin metadata is a storage error, never a reason to delete.
            packet=json.loads(pins.read_text(encoding="utf-8"))
            protected.update(packet.get("refs",[]) if isinstance(packet,dict) else packet)
        files = sorted((path for path in directory.iterdir()
                        if path.is_file() and not path.name.startswith(".")
                        and path.relative_to(self.root).as_posix() not in protected),
                       key=lambda path: path.stat().st_mtime)
        targets = set()
        if max_age_seconds is not None:
            cutoff = time.time() - max(0.0, max_age_seconds)
            targets.update(path for path in files if path.stat().st_mtime < cutoff)
        survivors = [path for path in files if path not in targets]
        if max_files is not None and len(survivors) > max(0, max_files):
            targets.update(survivors[:len(survivors) - max(0, max_files)])
        for path in targets:
            path.unlink(missing_ok=True)
        return len(targets)
