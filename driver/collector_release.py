"""Pinned Collector release download with a verified, non-versioned-code cache."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import tempfile

import httpx


COLLECTOR_VERSION = "0.4.5"
COLLECTOR_SHA256 = "23a012031225a7767ec3671f1347b4f03929226a789fa03c28f2ddd30d1a6652"
COLLECTOR_ASSET = f"clickclick-collector-{COLLECTOR_VERSION}-debug.apk"
COLLECTOR_TAG = f"collector-v{COLLECTOR_VERSION}"
DEFAULT_RELEASE_REPO = "LordRosenberg/ClickClick"


def collector_cache_path(*, root: Path | None = None) -> Path:
    root = root or Path(__file__).resolve().parent.parent
    cache = Path(os.environ.get("CLICKCLICK_DEVICE_APK_CACHE") or root / "data/device-apks")
    return cache / COLLECTOR_ASSET


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


async def download_collector_release(path: Path, *, timeout_s: float = 30.0) -> Path:
    """Fetch the exact pinned asset; never accept latest or an unchecked cache."""
    if path.is_file() and file_sha256(path) == COLLECTOR_SHA256:
        return path
    repo = os.environ.get("CLICKCLICK_COLLECTOR_RELEASE_REPO", DEFAULT_RELEASE_REPO)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid CLICKCLICK_COLLECTOR_RELEASE_REPO")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    temporary: Path | None = None
    try:
        async with asyncio.timeout(timeout_s):
            async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
                response = await client.get(
                    f"https://api.github.com/repos/{repo}/releases/tags/{COLLECTOR_TAG}",
                )
                if response.status_code in {401, 403, 404}:
                    raise RuntimeError(
                        f"Collector Release {repo}/{COLLECTOR_TAG} unavailable; "
                        "check release publication and GH_TOKEN/GITHUB_TOKEN access",
                    )
                response.raise_for_status()
                asset = next((a for a in response.json().get("assets", [])
                              if a.get("name") == COLLECTOR_ASSET), None)
                if asset is None or not isinstance(asset.get("id"), int):
                    raise RuntimeError(f"Collector Release asset missing: {COLLECTOR_ASSET}")
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".partial", delete=False) as stream:
                    temporary = Path(stream.name)
                    async with client.stream(
                        "GET", f"https://api.github.com/repos/{repo}/releases/assets/{asset['id']}",
                        headers={"Accept": "application/octet-stream"},
                    ) as download:
                        download.raise_for_status()
                        size = 0
                        async for chunk in download.aiter_bytes():
                            size += len(chunk)
                            if size > 20 * 1024 * 1024:
                                raise RuntimeError("Collector Release asset exceeds size limit")
                            stream.write(chunk)
                if file_sha256(temporary) != COLLECTOR_SHA256:
                    raise RuntimeError("Collector Release SHA-256 mismatch; refusing installation")
                temporary.replace(path)
                return path
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def stage_local_release() -> Path:
    """Stage a locally built APK for publishing, without committing binaries."""
    from driver.environment import resolve_collector_apk_path
    import shutil

    source = Path(resolve_collector_apk_path(
        os.environ.get("CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH", ""),
    ))
    if source.resolve() == collector_cache_path().resolve() or not source.is_file():
        raise RuntimeError("Build Collector locally or specify an explicit local APK before publishing")
    if file_sha256(source) != COLLECTOR_SHA256:
        raise RuntimeError("Local build differs from release pin; update version and COLLECTOR_SHA256 before publishing")
    staged = Path(__file__).resolve().parent.parent / "data/collector-release" / COLLECTOR_ASSET
    staged.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != staged.resolve():
        shutil.copy2(source, staged)
    return staged


if __name__ == "__main__":
    import argparse
    from driver.environment import resolve_collector_apk_path

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage-local", action="store_true")
    mode.add_argument("--resolve", action="store_true")
    args = parser.parse_args()
    if args.stage_local:
        print(stage_local_release().as_posix())
    else:
        selected = Path(resolve_collector_apk_path(
            os.environ.get("CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH", ""),
        ))
        if selected.resolve() == collector_cache_path().resolve():
            selected = asyncio.run(download_collector_release(selected))
        if not selected.is_file():
            raise SystemExit(f"Collector APK does not exist: {selected}")
        print(selected.as_posix())
