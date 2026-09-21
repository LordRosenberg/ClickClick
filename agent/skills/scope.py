"""Deterministic task-App candidates for Skill routing."""

from __future__ import annotations

from pathlib import Path

from agent.skills.library import SkillLibrary
from shared.app_resolver import DEFAULT_APP_ALIAS_SEED_PATH, load_alias_seed_file


def scan_instruction_for_skill_apps(
    instruction: str,
    *,
    library: SkillLibrary | None = None,
    alias_seed: dict[str, str] | None = None,
    seed_path: Path | str | None = None,
    limit: int = 3,
) -> list[str]:
    """Return ordered App packages mentioned by the task that own Skills."""
    text = (instruction or "").strip().casefold()
    if not text or limit <= 0:
        return []
    lib = library or SkillLibrary()
    skilled = lib.packages_with_skills()
    aliases = alias_seed
    if aliases is None:
        aliases = load_alias_seed_file(seed_path or DEFAULT_APP_ALIAS_SEED_PATH)
    # App names belong to their core skill. Conflicting declarations remain
    # ambiguous and are resolved later through the normal device resolver.
    owners: dict[str, set[str]] = {}
    for alias, package in aliases.items():
        owners.setdefault(alias.strip().casefold(), set()).add(package)
    for pack in lib.load_all():
        if pack.kind != "app_core" or not pack.app:
            continue
        names = pack.frontmatter.get("app_aliases", [])
        if not isinstance(names, list):
            names = []
        for alias in [pack.app, *names]:
            if isinstance(alias, str) and alias.strip():
                owners.setdefault(alias.strip().casefold(), set()).add(pack.app)
    aliases = {alias: next(iter(packages)) for alias, packages in owners.items() if len(packages) == 1}

    claimed = [False] * len(text)
    matches: list[tuple[int, str]] = []
    for alias, package in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        key = alias.casefold()
        if not key or package not in skilled:
            continue
        start = 0
        while True:
            index = text.find(key, start)
            if index < 0:
                break
            end = index + len(key)
            if key.isascii() and (
                (index and text[index - 1].isascii() and text[index - 1].isalnum())
                or (end < len(text) and text[end].isascii() and text[end].isalnum())
            ):
                start = index + 1
                continue
            if not any(claimed[index:end]):
                claimed[index:end] = [True] * len(key)
                matches.append((index, package))
            start = end
    found: list[str] = []
    for _index, package in sorted(matches):
        if package not in found:
            found.append(package)
        if len(found) == limit:
            break
    return found
