"""Stable workflow parsing and exact-foreground-App delivery boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.prompts import render_executor_system, render_planner_system
from agent.session import AgentSession, tools_for_role
from agent.skills.library import SkillLibrary, parse_skill_markdown, serialize_skill_markdown


def _module_text(
    *, name: str, app: str, kind: str, capability: str = "",
    body: str = "## Hints\n- general guidance",
) -> str:
    return serialize_skill_markdown(
        name=name, description=f"{name} description", version="1.0.0",
        app=app, kind=kind, capability=capability, body=body,  # type: ignore[arg-type]
        extra_frontmatter={"source": "authored"},
    )


def _workflow_body(marker: str) -> str:
    return (
        "## Procedure\n"
        f"1. {marker} procedure\n\n"
        "## Verification\n"
        f"- {marker} verified\n\n"
        "## Hints\n"
        "- follow current visual evidence"
    )


def _write_module(root: Path, relative: str, text: str) -> None:
    path = root / relative / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workflow_library(root: Path) -> SkillLibrary:
    modules = (
        (
            "apps/com.xingin.xhs/core",
            _module_text(name="xhs-core", app="com.xingin.xhs", kind="app_core"),
        ),
        (
            "apps/com.xingin.xhs/workflows/top-video",
            _module_text(
                name="xhs-top-video", app="com.xingin.xhs", kind="workflow",
                capability="search_top_video", body=_workflow_body("TOP_VIDEO_BODY"),
            ),
        ),
        (
            "apps/com.xingin.xhs/workflows/comment",
            _module_text(
                name="xhs-comment", app="com.xingin.xhs", kind="workflow",
                capability="post_comment", body=_workflow_body("COMMENT_BODY"),
            ),
        ),
        (
            "apps/tv.danmaku.bili/core",
            _module_text(name="bili-core", app="tv.danmaku.bili", kind="app_core"),
        ),
        (
            "apps/tv.danmaku.bili/workflows/open-video",
            _module_text(
                name="bili-open-video", app="tv.danmaku.bili", kind="workflow",
                capability="search_open_video", body=_workflow_body("BILI_VIDEO_BODY"),
            ),
        ),
    )
    for relative, text in modules:
        _write_module(root, relative, text)
    return SkillLibrary(root)


def test_workflow_parser_serialization_scope_and_validation(tmp_path: Path):
    path = tmp_path / "apps/com.demo/workflows/find-item/SKILL.md"
    text = _module_text(
        name="find-item", app="com.demo", kind="workflow",
        capability="find_item", body=_workflow_body("FIND_ITEM"),
    )
    pack = parse_skill_markdown(text, path=path)
    assert pack.kind == "workflow"
    assert pack.capability == "find_item"
    assert pack.scope_key() == "apps/com.demo"
    assert parse_skill_markdown(
        serialize_skill_markdown(
            name=pack.id, description=pack.description, version=pack.version,
            app=pack.app, kind=pack.kind, capability=pack.capability, body=pack.body,
        ),
    ).kind == "workflow"
    with pytest.raises(ValueError, match="Verification"):
        parse_skill_markdown(_module_text(
            name="bad", app="com.demo", kind="workflow", capability="bad",
            body="## Procedure\n1. do it",
        ))


def test_authored_skill_lint_requires_classified_rules(tmp_path: Path):
    _write_module(
        tmp_path, "generic/unclassified",
        serialize_skill_markdown(
            name="unclassified-authored", kind="generic", body="old monolith",
            extra_frontmatter={"source": "authored"},
        ),
    )
    assert any(
        "has no classified rules" in error
        for error in SkillLibrary(tmp_path).lint_authored_modules()
    )


def test_duplicate_active_skill_ids_fail_closed_across_scopes(tmp_path: Path):
    _write_module(
        tmp_path, "generic/duplicate",
        _module_text(name="duplicate", app="", kind="generic"),
    )
    _write_module(
        tmp_path, "apps/com.demo/workflows/duplicate",
        _module_text(
            name="duplicate", app="com.demo", kind="workflow",
            capability="demo", body=_workflow_body("DEMO"),
        ),
    )
    with pytest.raises(ValueError, match="duplicate active skill id"):
        SkillLibrary(tmp_path).load_all()


@pytest.mark.parametrize("role", ["planner", "reviewer", "executor"])
def test_exact_foreground_app_delivers_core_and_all_workflows(role: str, tmp_path: Path):
    session = AgentSession(role, "m", library=_workflow_library(tmp_path))  # type: ignore[arg-type]
    session.reset_lifecycle(f"task:{role}")
    session.freeze_allow_dirs(["generic"])
    session.set_foreground_app("com.xingin.xhs")
    bodies = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert "xhs-core" in bodies
    assert "TOP_VIDEO_BODY" in bodies and "COMMENT_BODY" in bodies
    assert "BILI_VIDEO_BODY" not in bodies
    assert {item["skill_id"] for item in session.active_skill_metadata} == {
        "xhs-core", "xhs-top-video", "xhs-comment",
    }


def test_foreground_app_switch_replaces_active_bundle(tmp_path: Path):
    session = AgentSession("executor", "m", library=_workflow_library(tmp_path))
    session.reset_lifecycle("task:switch")
    session.set_foreground_app("com.xingin.xhs")
    session.set_foreground_app("tv.danmaku.bili")
    bodies = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert "BILI_VIDEO_BODY" in bodies
    assert "TOP_VIDEO_BODY" not in bodies
    assert {item["skill_id"] for item in session.active_skill_metadata} == {
        "bili-core", "bili-open-video",
    }


def test_role_catalog_has_no_workflow_discovery_state_machine():
    names = {
        tool["function"]["name"]
        for role in ("planner", "reviewer", "executor")
        for tool in tools_for_role(role)
    }
    assert names.isdisjoint({
        "list_app_workflows", "select_workflow_for_next_subgoal",
        "list_current_app_workflows", "load_current_app_workflow",
    })


def test_repository_xhs_workflows_keep_reviewed_rules():
    library = SkillLibrary()
    top_video = library.get("xiaohongshu-search-and-sort")
    comment = library.get("xiaohongshu-post-comment")
    assert top_video is not None and top_video.kind == "workflow"
    assert "「全部」" in top_video.body and "「最多点赞」" in top_video.body
    assert any(rule.category == "constraint" for rule in top_video.rules)
    assert comment is not None and comment.kind == "workflow"
    constraints = [rule for rule in comment.rules if rule.category == "constraint"]
    assert len(constraints) == 1
    assert "右下角评论图标" in constraints[0].text


def test_role_prompts_preserve_values_and_describe_complete_app_bundle():
    planner = render_planner_system()
    executor = " ".join(render_executor_system().split())
    assert "exact task literals" in planner
    assert "exact foreground App core and all its workflows" in executor
    assert "chords such as CTRL+A are unsupported" in json.dumps(
        tools_for_role("executor"), ensure_ascii=False,
    )


def test_bilibili_core_explains_player_action_labels_without_general_retry_policy():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/tv.danmaku.bili/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert pack.id == "bilibili-core"
    assert any(rule.category == "constraint" for rule in pack.rules)
    assert "表示可执行暂停，不证明当前已暂停" in pack.body
    assert "只控制弹幕，不负责显隐播放控件或切换播放状态" in pack.body
    assert "同一目的点击一次后" not in pack.body
