"""Explicit runtime application allowlist, independent of checkout contents."""

from pathlib import Path

PACKAGES = ("agent", "shared", "driver", "perception", "control_api", "desktop")
USER_GUIDES = ("local-mcp.zh-CN.md", "desktop-setup.zh-CN.md")
RUNTIME_JSON = ("shared/app_aliases.json", "shared/app_alias_profiles.json")
RUNTIME_ASSETS = ("desktop/assets/clickclick-app.png",)


def application_files(repo: Path):
    files = set()
    for package in PACKAGES:
        for path in (repo / package).rglob("*.py"):
            relative = path.relative_to(repo)
            if "__pycache__" not in relative.parts and "integrations" not in relative.parts:
                if package == "desktop" and path.name in {"packaging.py", "bootstrap.py"}:
                    continue
                files.add(relative)
    # Prompts are runtime inputs, not internal project documentation.
    for path in (repo / "agent/prompts").glob("*.md"):
        if " copy" not in path.name:
            files.add(path.relative_to(repo))
    for name in (*RUNTIME_JSON, *RUNTIME_ASSETS):
        files.add(Path(name))
    for path in (repo / "shared/app_aliases").glob("*.json"):
        if not path.name.startswith(("androidworld_", "mobileworld_")):
            files.add(path.relative_to(repo))
    for path in (repo / "skills").rglob("SKILL.md"):
        if "_pending" not in path.parts and not any("mobileworld" in p or "androidworld" in p for p in path.parts):
            files.add(path.relative_to(repo))
    for path in (repo / "web/dist").rglob("*"):
        if path.is_file() and path.suffix != ".map":
            files.add(path.relative_to(repo))
    for name in USER_GUIDES:
        files.add(Path("docs") / name)
    for name in ("LICENSE", "driver/vendor/scrcpy-server-v3.3.1.jar", "driver/vendor/LICENSE.scrcpy.txt"):
        files.add(Path(name))
    missing = [str(p) for p in files if not (repo / p).is_file()]
    if missing:
        raise ValueError("Missing required distribution files: " + ", ".join(sorted(missing)))
    return sorted(files)
