"""Bind app knowledge to the package actually shipped by MobileWorld."""
from pathlib import Path

from agent.session import AgentSession
from agent.skills.library import SkillLibrary
from evaluation.mobileworld.long_run_health import required_packages


def test_mastodon_workflow_reaches_executor_for_installed_package_only():
    library = SkillLibrary(Path(__file__).resolve().parents[1] / "skills")
    app = required_packages(["Mastodon"])["Mastodon"]
    skill_id = "mastodon-account-web-settings"
    assert skill_id in {row["id"] for row in library.workflow_catalog([app])}
    assert skill_id not in {row["id"] for row in library.workflow_catalog(["com.gmailclone"])}
    skill = library.get(skill_id)
    assert skill is not None
    assert not [error for error in library.lint_authored_modules() if str(skill.path) in error]

    session = AgentSession("executor", "m", library=library)
    session.reset_lifecycle("task:mastodon-binding")
    session.freeze_allow_dirs(["generic"])
    session.set_target_app(app, [skill_id])
    assert skill_id in {row["skill_id"] for row in session.active_skill_metadata}
    assert any("Posting language" in message.get("content", "") for message in session._k_wire())
    session.set_target_app("com.gmailclone", [])
    assert skill_id not in {row["skill_id"] for row in session.active_skill_metadata}
