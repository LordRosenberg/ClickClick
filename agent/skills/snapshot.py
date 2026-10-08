"""Immutable active-library snapshots and exact candidate overlays."""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent.skills.library import SkillLibrary


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def active_files(root):
    root = Path(root).resolve()
    if not root.exists():
        return {}
    files = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(("_", ".")) for part in relative.parts):
            continue
        if path.is_symlink() or (hasattr(path,"is_junction") and path.is_junction()):
            raise ValueError("active library symlinks are not snapshot-safe")
        if path.is_file():
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def library_manifest(root):
    files = active_files(root)
    return {"files": files, "hash": _hash(files)}


@dataclass(frozen=True)
class LibrarySnapshot:
    root: Path
    manifest: dict

    @classmethod
    def freeze(cls, source, destination):
        source, destination = Path(source).resolve(), Path(destination).resolve()
        if destination.exists():
            raise ValueError("snapshot destination must be fresh")
        if destination == source or source in destination.parents:
            raise ValueError("snapshot must be outside source library")
        manifest = library_manifest(source)
        destination.mkdir(parents=True)
        for relative in manifest["files"]:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        if library_manifest(destination) != manifest or library_manifest(source) != manifest:
            raise ValueError("library changed while freezing")
        return cls(destination, manifest)

    def overlay(self, target, text, destination):
        return self.overlay_many({target: text}, destination)

    def overlay_many(self, patches, destination):
        if not patches:
            raise ValueError("empty candidate overlay")
        normalized, physical_paths = {}, set()
        for target, text in patches.items():
            relative = Path(target)
            if relative.is_absolute() or ".." in relative.parts or any(p.startswith(("_", ".")) for p in relative.parts):
                raise ValueError("invalid candidate target")
            if relative.name != "SKILL.md":
                raise ValueError("candidate target must be SKILL.md")
            physical = self.root / relative
            if relative.as_posix() in normalized or physical in physical_paths:
                raise ValueError("duplicate candidate target")
            physical_paths.add(physical)
            normalized[relative.as_posix()] = text
        if library_manifest(self.root) != self.manifest:
            raise ValueError("frozen baseline changed")
        destination = Path(destination).resolve()
        if destination.exists() or destination == self.root or self.root in destination.parents:
            raise ValueError("overlay destination must be fresh")
        shutil.copytree(self.root, destination)
        for relative, text in normalized.items():
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(text.encode("utf-8"))
        manifest = library_manifest(destination)
        changed = {p for p in self.manifest["files"].keys() | manifest["files"].keys()
                   if self.manifest["files"].get(p) != manifest["files"].get(p)}
        if changed != set(normalized):
            raise ValueError("overlay must change exactly the declared targets")
        return LibrarySnapshot(destination, manifest)

    def directory(self, max_chars=2500, *, apps=None):
        items, omitted = [], 0
        packs = SkillLibrary(self.root).load_all()
        excluded = 0
        if apps is not None:
            apps = set(apps)
            excluded = sum(bool(pack.app and pack.app not in apps) for pack in packs)
            packs = [pack for pack in packs if not pack.app or pack.app in apps]
            packs.sort(key=lambda pack: (not bool(pack.app), pack.id))
        for pack in packs:
            item = {"id": pack.id, "app": pack.app, "path": pack.path.relative_to(self.root).as_posix()}
            if len(json.dumps(items + [item], ensure_ascii=False)) <= max_chars:
                items.append(item)
            else:
                omitted += 1
        result = {"manifest_hash": self.manifest["hash"], "items": items, "omitted": omitted}
        if apps is not None:
            result.update(outside_observed_apps=excluded,
                policy="Observed-app and generic directory only; the complete frozen library and exact-path read access are unchanged.")
        return result

    def read(self, relative, offset=0, length=4000):
        if relative not in self.manifest["files"]:
            raise ValueError("file is not in frozen official library")
        if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 4000:
            raise ValueError("invalid official skill page")
        path = self.root / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != self.manifest["files"][relative]:
            raise ValueError("frozen library file changed")
        text = path.read_text(encoding="utf-8")
        end = min(len(text), offset + length)
        return {"path": relative, "hash": self.manifest["files"][relative], "text": text[offset:end],
                "truncated": end < len(text), "next_offset": end if end < len(text) else None,
                "policy": "Existing official knowledge, not evidence of current app state."}
