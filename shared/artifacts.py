"""Local filesystem artifact store for screenshots, trees, and LLM payloads."""

from __future__ import annotations

import json
import hashlib
import uuid
import time
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
        path.write_bytes(data)
        return rel

    def save_content_addressed_bytes(
        self, kind: str, data: bytes, suffix: str = ".bin",
    ) -> str:
        """Persist immutable bytes by digest and reuse identical content."""
        digest = hashlib.sha256(data).hexdigest()
        rel = f"{kind}/{digest}{suffix}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        return rel

    def save_json(self, kind: str, payload: Any) -> str:
        """Persist JSON by content hash and return a stable, deduplicated ref.

        Content-addressing is important for model transcripts: adjacent rounds
        often share a large prefix, and retries may produce byte-identical
        artifacts.  Atomic replacement is unnecessary because identical
        digests imply identical bytes; an existing object is simply reused.
        """
        encoded = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        name = f"{digest}.json"
        rel = f"{kind}/{name}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(encoded)
        return rel

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
        directory = self.resolve(kind)
        if not directory.exists() or not directory.is_dir():
            return 0
        files = sorted((path for path in directory.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime)
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
