"""Explicit runtime application allowlist, independent of checkout contents."""

from pathlib import Path

PACKAGES = ("agent", "shared", "driver", "perception", "control_api", "desktop")
USER_GUIDES = ("local-mcp.zh-CN.md", "desktop-setup.zh-CN.md")
RUNTIME_JSON = ("shared/app_aliases.json", "shared/app_alias_profiles.json")
RUNTIME_ASSETS = ("desktop/assets/clickclick-app.png",)
SKILL_EVALUATORS = (
    "__init__", "gui_scripts", "gui_script_assist", "gui_script_auto_repair", "gui_script_case",
    "gui_script_host", "gui_script_ocr", "gui_script_release", "gui_visual_record",
    "gui_skill_auto_plan", "gui_skill_budget", "gui_skill_case_recording",
    "gui_skill_case_registry", "gui_skill_console_worker", "gui_skill_loop",
    "gui_skill_regression", "gui_skill_replay_evidence", "skill_fixture_restoration", "skill_learning",
    "mobileworld/__init__", "mobileworld/app_session", "mobileworld/budgets",
    "mobileworld/clock_health", "mobileworld/environment_fixes", "mobileworld/fixture_context",
    "mobileworld/hitl", "mobileworld/long_run_health", "mobileworld/patch_mattermost_readiness",
    "mobileworld/photo_picker_health", "mobileworld/recording", "mobileworld/recovery",
    "mobileworld/run_single", "mobileworld/taodian_fixture", "mobileworld/video_integrity",
)


def application_files(repo: Path):
    files = set()
    for package in PACKAGES:
        for path in (repo / package).rglob("*.py"):
            relative = path.relative_to(repo)
            if "__pycache__" not in relative.parts and "integrations" not in relative.parts:
                if package == "desktop" and path.name in {"packaging.py", "bootstrap.py"}:
                    continue
                files.add(relative)
    # The isolated Skill regression process imports the shared evaluators. Ship
    # their Python modules without recordings, reproduction trees or fixtures.
    files.update(Path("evaluation") / (module + ".py") for module in SKILL_EVALUATORS)
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
