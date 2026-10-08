"""Remove old MobileWorld video files, preserving two explicitly selected runs.

Preview: .venv/Scripts/python.exe scripts/cleanup_mobileworld_videos.py
Delete:  .venv/Scripts/python.exe scripts/cleanup_mobileworld_videos.py --apply

The retention list is fixed to this request (2026-10-08), not a moving
"yesterday" rule. All contents of retained runs are protected. Only video
files are removed elsewhere; screenshots, logs, JSON and directories remain.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import stat


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "data/mobileworld/runs"
KEEP_RUNS = frozenset({
    "full-gpt56-jev-official-20261007",
    "full-gui-paired-20261001-v2",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".3gp",
})


def is_link(path: Path) -> bool:
    """Also reject Windows junctions and other reparse points."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def validate_root(root: Path) -> Path:
    if is_link(root):
        raise ValueError(f"Runs root must not be a link/junction: {root}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    for name in sorted(KEEP_RUNS):
        protected = root / name
        if not protected.is_dir() or is_link(protected):
            raise ValueError(f"Required retained run is missing or linked: {protected}")
    return root


def checked_file(root: Path, path: Path) -> os.stat_result:
    relative = path.relative_to(root)
    if not relative.parts or relative.parts[0] in KEEP_RUNS:
        raise ValueError(f"Refusing protected path: {path}")
    current = root
    for part in relative.parts:
        current = current / part
        if is_link(current):
            raise ValueError(f"Refusing linked path: {current}")
    path.resolve(strict=True).relative_to(root)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError(f"Not a regular video file: {path}")
    return info


def scan(root: Path) -> list[tuple[Path, os.stat_result]]:
    candidates = []
    pending = [root]
    while pending:
        parent = pending.pop()
        with os.scandir(parent) as entries:
            for entry in entries:
                if parent == root and entry.name in KEEP_RUNS:
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    continue
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif path.suffix.lower() in VIDEO_EXTENSIONS:
                    candidates.append((path, checked_file(root, path)))
    return sorted(candidates, key=lambda item: str(item[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--apply", action="store_true", help="Actually delete videos")
    parser.add_argument("--report", type=Path, help="Save candidate paths/sizes as JSON")
    args = parser.parse_args()
    root = validate_root(args.root)
    print(f"Scanning: {root} (runtime snapshots may take a few minutes)", flush=True)
    candidates = scan(root)
    total = sum(info.st_size for _, info in candidates)
    print(f"Root: {root}")
    print("Protected runs:")
    for name in sorted(KEEP_RUNS):
        print(f"  {name}")
    print(f"{'DELETE' if args.apply else 'PREVIEW'}: {len(candidates)} videos, "
          f"{total / (1024 ** 3):.3f} GiB ({total:,} bytes)")
    groups = defaultdict(lambda: [0, 0])
    for path, info in candidates:
        group = path.relative_to(root).parts[0]
        groups[group][0] += 1
        groups[group][1] += info.st_size
    for name, (count, size) in sorted(groups.items()):
        print(f"  {name}: {count} videos, {size / (1024 ** 3):.3f} GiB")
    if args.report:
        report = args.report.resolve()
        if report == root or root in report.parents:
            raise ValueError("Save the report outside the runs directory")
        # Exclusive creation prevents accidental overwriting of an existing file.
        with report.open("x", encoding="utf-8") as output:
            json.dump({
                "root": str(root), "apply": args.apply,
                "protected_runs": sorted(KEEP_RUNS),
                "video_count": len(candidates), "total_bytes": total,
                "videos": [{"path": str(path.relative_to(root)), "bytes": info.st_size}
                           for path, info in candidates],
            }, output, ensure_ascii=False, indent=2)
        print(f"Candidate report: {report}")
    if not args.apply:
        print("Preview only. Add --apply to delete the listed videos.")
        return 0
    deleted = 0
    freed = 0
    errors = 0
    for path, original in candidates:
        try:
            current = checked_file(root, path)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns
            ):
                raise ValueError("File changed since preview; skipped")
            path.unlink()
            deleted += 1
            freed += original.st_size
        except (OSError, ValueError) as error:
            errors += 1
            print(f"ERROR {path}: {error}")
    print(f"Deleted {deleted} videos, {freed / (1024 ** 3):.3f} GiB; {errors} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        raise SystemExit(f"ERROR: {error}") from error
