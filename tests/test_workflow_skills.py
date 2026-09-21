"""Stable workflow parsing and exact-foreground-App delivery boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.prompts import render_executor_system, render_planner_system
from agent.revisable.session import ExecutionSession
from agent.session import AgentSession, tools_for_role
from agent.skills.library import SkillLibrary, parse_skill_markdown, serialize_skill_markdown
from agent.skills.scope import scan_instruction_for_skill_apps
from agent.tool_registry import AgentRole, ToolExecutionContext, ToolStatus


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
def test_foreground_app_delivers_core_without_workflow_bodies(role: str, tmp_path: Path):
    session = AgentSession(role, "m", library=_workflow_library(tmp_path))  # type: ignore[arg-type]
    session.reset_lifecycle(f"task:{role}")
    session.freeze_allow_dirs(["generic"])
    session.set_foreground_app("com.xingin.xhs")
    bodies = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert "xhs-core" in bodies
    assert "TOP_VIDEO_BODY" not in bodies and "COMMENT_BODY" not in bodies
    assert "BILI_VIDEO_BODY" not in bodies
    assert {item["skill_id"] for item in session.active_skill_metadata} == {"xhs-core"}


@pytest.mark.parametrize("role", ["reviewer", "executor"])
def test_target_app_handoff_survives_unrelated_foreground(role: str, tmp_path: Path):
    session = AgentSession(role, "m", library=_workflow_library(tmp_path))  # type: ignore[arg-type]
    session.reset_lifecycle("task:switch")
    session.set_target_app("com.xingin.xhs", ["xhs-top-video"])
    session.set_foreground_app("com.android.launcher")
    bodies = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert "TOP_VIDEO_BODY" in bodies
    assert "COMMENT_BODY" not in bodies
    assert "BILI_VIDEO_BODY" not in bodies
    assert {item["skill_id"] for item in session.active_skill_metadata} == {
        "xhs-core", "xhs-top-video",
    }


def test_new_task_clears_catalog_and_selected_handoff(tmp_path: Path):
    session = AgentSession("planner", "m", library=_workflow_library(tmp_path))
    session.reset_lifecycle("task:one")
    session.set_workflow_catalog(["com.xingin.xhs"])
    session.set_target_app("com.xingin.xhs", ["xhs-comment"])
    assert session.reset_lifecycle("task:two") is True
    assert session._workflow_catalog == []  # noqa: SLF001
    assert session._target_app == ""  # noqa: SLF001
    assert session._selected_workflow_ids == []  # noqa: SLF001
    assert session._k_wire() == []  # noqa: SLF001


def test_observed_core_survives_wrong_target_and_retires_on_departure(tmp_path: Path):
    session = AgentSession("executor", "m", library=_workflow_library(tmp_path))
    session.reset_lifecycle("task:wrong-package")
    session.set_target_app("com.missing.camera")
    session.set_foreground_app("com.xingin.xhs")
    assert {item["skill_id"] for item in session.active_skill_metadata} == {"xhs-core"}
    bodies = json.dumps(session._k_wire())
    assert "xhs-core" in bodies and "TOP_VIDEO_BODY" not in bodies
    assert session._target_app == "com.missing.camera"
    session.set_foreground_app("com.android.launcher")
    assert session.active_skill_metadata == []
    assert "xhs-core" not in json.dumps(session._k_wire())


def test_foreground_core_does_not_replace_target_workflow_or_duplicate(tmp_path: Path):
    _workflow_library(tmp_path)
    _write_module(tmp_path, "apps/tv.danmaku.bili/core", _module_text(
        name="bili-core", app="tv.danmaku.bili", kind="app_core",
        body="## Hints\n- Bili-specific controls",
    ))
    session = AgentSession("executor", "m", library=SkillLibrary(tmp_path))
    session.set_target_app("com.xingin.xhs", ["xhs-top-video"])
    session.set_foreground_app("tv.danmaku.bili")
    bodies = json.dumps(session._k_wire())
    assert "TOP_VIDEO_BODY" in bodies and "bili-core" in bodies
    assert "BILI_VIDEO_BODY" not in bodies
    session.set_foreground_app("com.xingin.xhs")
    ids = [row["skill_id"] for row in session.active_skill_metadata]
    assert sorted(ids) == ["xhs-core", "xhs-top-video"]


def test_planner_catalog_is_compact_and_instruction_candidates_are_bounded(tmp_path: Path):
    library = _workflow_library(tmp_path)
    candidates = scan_instruction_for_skill_apps(
        "请在小红书找视频，再去哔哩哔哩打开",
        library=library,
        alias_seed={"小红书": "com.xingin.xhs", "哔哩哔哩": "tv.danmaku.bili"},
    )
    cards = library.workflow_catalog(candidates)
    assert candidates == ["com.xingin.xhs", "tv.danmaku.bili"]
    assert {tuple(card) for card in cards} == {
        ("id", "app", "capability", "description"),
    }
    assert "TOP_VIDEO_BODY" not in json.dumps(cards, ensure_ascii=False)


def test_role_catalogs_close_skill_authority_at_executor():
    planner = {tool["function"]["name"] for tool in tools_for_role("planner")}
    executor = {tool["function"]["name"] for tool in tools_for_role("executor")}
    reviewer = {tool["function"]["name"] for tool in tools_for_role("reviewer")}
    assert "load_skill" in planner
    assert "search_skills" not in planner
    assert "load_skill" in executor
    assert "search_skills" not in executor
    assert {"load_skill", "search_skills"}.isdisjoint(reviewer)


def test_planner_workflow_selection_is_exact_and_owned(tmp_path: Path):
    session = AgentSession("planner", "m", library=_workflow_library(tmp_path))
    packs = session.validate_stage_skills("com.xingin.xhs", ["xhs-top-video"])
    assert [p.id for p in packs] == ["xhs-top-video"]
    with pytest.raises(ValueError):
        session.validate_stage_skills("com.xingin.xhs", ["bili-open-video"])
    assert session.validate_stage_skills("com.android.deskclock", []) == []


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


def test_role_prompts_preserve_values_and_do_not_claim_missing_app_skills():
    planner = render_planner_system()
    executor = " ".join(render_executor_system().split())
    assert "exact" in planner
    assert "A skill is guidance" in executor
    assert "current evidence controls" in executor
    assert executor.count("unauthorized invocation is prohibited") == 1
    assert "Ordinary actions are atomic" in executor
    assert "ground later actions only in the returned latest state" in executor
    assert "authorized action" not in planner.lower()
    assert "tap_capture_key" not in executor
    assert "broccoli.inspect_recipe_and_return" not in executor
    assert "chords such as CTRL+A are unsupported" in json.dumps(
        tools_for_role("executor"), ensure_ascii=False,
    )


def test_markor_core_forbids_temporary_text_without_forcing_an_empty_note():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/net.gsantner.markor/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert "paste it at the intended insertion point" in pack.body
    assert "Do not type temporary content" in pack.body
    assert "leave its editor empty" not in pack.body


def test_settings_clipboard_workflow_is_globally_discoverable_on_androidworld():
    root = Path(__file__).resolve().parents[1] / "skills"
    library = SkillLibrary(root, device_profiles=["androidworld_api33"])

    apps = library.planner_catalog_apps()
    cards = library.workflow_catalog(apps)
    card = next(
        item for item in cards
        if item["id"] == "androidworld-settings-copy-to-clipboard"
    )

    assert card["app"] == "com.android.settings"
    assert card["capability"] == "copy_text_to_clipboard"
    assert "clipboard" in card["description"]


def test_tasks_date_workflow_is_discoverable_for_androidworld_tasks_app():
    root = Path(__file__).resolve().parents[1] / "skills"
    library = SkillLibrary(root, device_profiles=["androidworld_api33"])

    cards = library.workflow_catalog(["org.tasks"])
    card = next(
        item for item in cards
        if item["id"] == "androidworld-tasks-find-by-date"
    )

    assert card["app"] == "org.tasks"
    assert card["capability"] == "find_tasks_by_date"


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


def test_retro_music_core_routes_song_additions_through_songs_library():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/code.name.monkey.retromusic/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert pack.id == "retro-music-playlists"
    assert pack.app == "code.name.monkey.retromusic"
    assert "switch to the `Songs` library section" in pack.body
    assert "Do not require reopening" in pack.body


def test_vlc_core_uses_media_entry_and_focus_then_type_for_playlist_creation():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/org.videolan.vlc/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert pack.id == "vlc-playlists"
    assert pack.app == "org.videolan.vlc"
    assert [action.id for action in pack.verified_actions] == [
        "vlc.open_and_pause", "vlc.close_tips_and_pause",
    ]
    assert [action.key for action in pack.verified_actions] == [
        "media_pause", "media_pause",
    ]
    assert pack.verified_actions[1].delay_ms == 100
    assert "empty `Playlists` tab has no direct create control" in pack.body
    assert "Indexed `replace_text` is therefore unsupported" in pack.body
    assert "separate focus and `type`" in pack.body
    assert "Player controls" in pack.body
    assert "tap_then_key" not in pack.body
    assert "automatically advance" in pack.body
    assert "dynamic-video-content-reading" in pack.body


def test_verified_action_frontmatter_is_typed_and_hidden_from_role_body():
    action = {
        "id": "demo.inspect_and_return",
        "template": "tap_capture_key",
        "key": "back",
        "purpose": "Open the target, preserve detail evidence, and return.",
    }
    text = serialize_skill_markdown(
        name="demo-inspection", description="demo", version="1.0.0",
        app="com.demo", kind="workflow", capability="inspect",
        body=_workflow_body("INSPECT"),
        extra_frontmatter={"source": "authored", "verified_actions": [action]},
    )
    pack = parse_skill_markdown(text)

    assert [item.id for item in pack.verified_actions] == ["demo.inspect_and_return"]
    assert pack.verified_actions[0].template == "tap_capture_key"
    assert "verified_actions" not in pack.section_for("executor")
    assert "tap_capture_key" not in pack.section_for("executor")


@pytest.mark.parametrize(
    ("kind", "app", "actions", "message"),
    [
        ("generic", "", [{"id": "demo.inspect", "template": "tap_capture_key", "key": "back", "purpose": "Inspect"}], "App core or workflow"),
        ("workflow", "com.demo", [
            {"id": "demo.inspect", "template": "tap_capture_key", "key": "back", "purpose": "Inspect"},
            {"id": "demo.inspect", "template": "tap_capture_key", "key": "back", "purpose": "Inspect again"},
        ], "duplicate verified action id"),
        ("workflow", "com.demo", [{"id": "demo.inspect", "template": "free_form", "key": "back", "purpose": "Inspect"}], "unknown verified action template"),
        ("workflow", "com.demo", [{"id": "demo.inspect", "template": "tap_capture_key", "key": "home", "purpose": "Inspect"}], "require key: back"),
        ("workflow", "com.demo", [{"id": "demo.inspect", "template": "tap_capture_key", "key": "back", "purpose": "Inspect", "sequence": ["tap", "back"]}], "unsupported fields"),
    ],
)
def test_verified_action_frontmatter_rejects_unauthorized_or_open_recipes(
    kind: str, app: str, actions: list[dict], message: str,
):
    text = serialize_skill_markdown(
        name="bad-action", description="bad", version="1.0.0",
        app=app or None, kind=kind, capability="inspect" if kind == "workflow" else "",
        body=_workflow_body("BAD") if kind == "workflow" else "## Hints\n- bad",
        extra_frontmatter={"source": "authored", "verified_actions": actions},
    )
    with pytest.raises(ValueError, match=message):
        parse_skill_markdown(text)


def test_authorized_action_registry_is_stage_stable_and_execution_foreground_scoped(
    tmp_path: Path,
):
    workflow = serialize_skill_markdown(
        name="demo-inspection", description="inspect details", version="1.0.0",
        app="com.demo", kind="workflow", capability="inspect",
        body=_workflow_body("INSPECT"),
        extra_frontmatter={
            "source": "authored",
            "verified_actions": [{
                "id": "demo.inspect_and_return",
                "template": "tap_capture_key",
                "key": "back",
                "purpose": "Open the target, preserve detail evidence, and return.",
            }],
        },
    )
    _write_module(tmp_path, "apps/com.demo/workflows/inspect", workflow)
    session = AgentSession("executor", "m", library=SkillLibrary(tmp_path))
    session.reset_lifecycle("task:authorized-action")
    session.freeze_allow_dirs(["generic"])
    catalog_before = session._build_registry({}).catalog_hash(AgentRole.EXECUTOR)  # noqa: SLF001

    session.set_target_app("com.demo", ["demo-inspection"])
    session.set_foreground_app("com.other")
    available_before_launch = session.available_authorized_actions()
    assert [item["id"] for item in available_before_launch] == [
        "demo.inspect_and_return"
    ]
    assert session.authorized_action("demo.inspect_and_return", "com.demo") is None
    stage_catalog_before_launch = session._authorized_actions_message()  # noqa: SLF001

    session.set_foreground_app("com.demo")
    available = session.available_authorized_actions()
    assert [item["id"] for item in available] == ["demo.inspect_and_return"]
    assert "one submitted action unit" in available[0]["budget"]
    resolved = session.authorized_action("demo.inspect_and_return", "com.demo")
    assert resolved is not None and resolved[1].key == "back"
    assert session.authorized_action("demo.not_active", "com.demo") is None
    assert session._authorized_actions_message() == stage_catalog_before_launch  # noqa: SLF001
    wire = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert "STAGE-AUTHORIZED ACTIONS" in wire
    assert "verified foreground App" in wire
    assert "tap_capture_key" not in wire and '"key"' not in wire
    assert session._build_registry({}).catalog_hash(AgentRole.EXECUTOR) == catalog_before  # noqa: SLF001


def test_second_app_action_appears_only_after_its_workflow_handoff(tmp_path: Path):
    for app, name in (("com.first", "first"), ("com.second", "second")):
        workflow = serialize_skill_markdown(
            name=f"{name}-inspection", description=f"{name} inspect", version="1.0.0",
            app=app, kind="workflow", capability="inspect",
            body=_workflow_body(name.upper()),
            extra_frontmatter={
                "source": "authored",
                "verified_actions": [{
                    "id": f"{name}.inspect",
                    "template": "tap_capture_key",
                    "key": "back",
                    "purpose": f"Inspect {name} and return.",
                }],
            },
        )
        _write_module(tmp_path, f"apps/{app}/workflows/inspect", workflow)
    session = AgentSession("executor", "m", library=SkillLibrary(tmp_path))
    session.reset_lifecycle("task:cross-app")
    session.freeze_allow_dirs(["generic"])
    session.set_target_app("com.first", ["first-inspection"])
    session.set_foreground_app("com.first")
    assert [item["id"] for item in session.available_authorized_actions()] == ["first.inspect"]

    session.set_foreground_app("com.second")
    assert [item["id"] for item in session.available_authorized_actions()] == ["first.inspect"]
    assert session.authorized_action("first.inspect", "com.first") is None
    session.set_target_app("com.second", ["second-inspection"])
    assert [item["id"] for item in session.available_authorized_actions()] == ["second.inspect"]
    assert session.authorized_action("second.inspect", "com.second") is not None


def test_dynamic_video_reading_is_generic_and_stage_selected_only():
    library = SkillLibrary()
    pack = library.get("dynamic-video-content-reading")
    assert pack is not None
    assert pack.kind == "generic" and not pack.app
    assert "static metadata" in pack.description
    assert "Do not use pure binary search" in pack.body
    assert "four timeline anchors" in pack.body
    assert "near `T/3`" in pack.body
    assert "first readable paused frame" in pack.body
    assert "use it as the beginning" in pack.body
    assert "pause before any further seek" in pack.body
    assert "leaves enough budget for completion" in pack.body
    assert "Do not revisit a reliably read timestamp" in pack.body
    assert "recompute it before every refinement" in pack.body
    assert "ordered values rather than transition timestamps" in pack.body
    assert "unresolved intervals" in pack.body
    assert pack.verified_actions == []

    session = ExecutionSession("executor", "m", library=library)
    session.reset_lifecycle("task:unrelated-video-metadata")
    session.freeze_allow_dirs(["generic"])
    assert session._index == []
    session.set_stage_skills("org.videolan.vlc", [])
    assert "dynamic-video-content-reading" not in {
        item["skill_id"] for item in session.active_skill_metadata
    }
    assert "four timeline anchors" not in json.dumps(session._k_wire())

    session.set_stage_skills(
        "org.videolan.vlc", ["dynamic-video-content-reading"],
    )
    assert "dynamic-video-content-reading" in {
        item["skill_id"] for item in session.active_skill_metadata
    }
    assert "four timeline anchors" in json.dumps(session._k_wire())


def test_osmand_core_distinguishes_country_results_and_persists_routes():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/net.osmand/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert pack.id == "osmand-routing-and-markers"
    assert pack.app == "net.osmand"
    assert "`INCREASE SEARCH RADIUS` repeatedly" in pack.body
    assert "country result" in pack.body
    assert "`Save as new track file`" in pack.body
    assert "`Show on map`" in pack.body


def test_broccoli_dedup_workflow_authorizes_only_the_verified_inspection_recipe():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/com.flauschcode.broccoli/workflows/deduplicate-recipes/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert pack.id == "broccoli-deduplicate-recipes"
    assert [action.id for action in pack.verified_actions] == [
        "broccoli.inspect_recipe_and_return"
    ]
    assert pack.verified_actions[0].key == "back"
    executor_body = pack.section_for("executor")
    assert "same-title" in executor_body
    assert "tap_capture_key" not in executor_body
    assert "key: back" not in executor_body


def test_broccoli_condition_workflow_is_selected_and_app_scoped():
    root = Path(__file__).resolve().parents[1] / "skills"
    library = SkillLibrary(root)
    workflow_id = "broccoli-delete-recipes-by-condition"
    app = "com.flauschcode.broccoli"
    cards = library.workflow_catalog([app])
    assert workflow_id in {card["id"] for card in cards}
    assert workflow_id not in {
        card["id"] for card in library.workflow_catalog(["net.gsantner.markor"])
    }
    session = AgentSession("executor", "m", library=library)
    session.reset_lifecycle("task:broccoli-scope")
    session.freeze_allow_dirs(["generic"])
    session.set_foreground_app(app)
    assert "# Delete recipes by condition" not in json.dumps(session._k_wire())
    packs = session.validate_stage_skills(app, [workflow_id])
    assert len(packs) == 1 and packs[0].id == workflow_id
    action_id = "broccoli.inspect_recipe_and_return"
    dedup = library.get("broccoli-deduplicate-recipes")
    assert packs[0].verified_actions == dedup.verified_actions
    assert library.app_core(app).verified_actions == []
    session.set_target_app(app, [])
    assert session.authorized_action(action_id, app) is None
    catalog_hash = session._build_registry({}).catalog_hash(AgentRole.EXECUTOR)
    session.set_target_app(app, [workflow_id])
    resolved = session.authorized_action(action_id, app)
    assert resolved is not None and resolved[0].id == workflow_id
    assert resolved[1].template == "tap_capture_key" and resolved[1].key == "back"
    assert session._build_registry({}).catalog_hash(AgentRole.EXECUTOR) == catalog_hash
    session.set_target_app(app, [workflow_id, dedup.id])
    assert [item["id"] for item in session.available_authorized_actions()] == [action_id]
    session.set_foreground_app("net.gsantner.markor")
    assert session.authorized_action(action_id, app) is None
    with pytest.raises(ValueError):
        session.validate_stage_skills("net.gsantner.markor", [workflow_id])


@pytest.mark.parametrize("role", ["planner", "executor", "reviewer"])
def test_opentracks_route_and_calendar_mechanics_are_shared(role: str):
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/de.dennisguse.opentracks/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)
    body = pack.section_for(role)
    assert "For type-based counting, start with main-list Search" in body
    assert "selection menu > Aggregated stats" in body
    assert "Bare weekdays can also appear for future dates" in body
    assert "expands a calendar" in body
    assert "date label is not a text-entry" in body


def test_reorganized_app_skills_have_classified_rules():
    root = Path(__file__).resolve().parents[1] / "skills"
    library = SkillLibrary(root)
    affected_apps = ("com.flauschcode.broccoli", "de.dennisguse.opentracks")
    errors = [
        error for error in library.lint_authored_modules()
        if any(app in error for app in affected_apps)
    ]
    assert errors == []


def test_retro_music_core_batches_duration_only_song_selection():
    path = (
        Path(__file__).resolve().parents[1]
        / "skills/apps/code.name.monkey.retromusic/core/SKILL.md"
    )
    pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)

    assert "Duration-constrained playlists" in pack.body
    assert "`Now playing queue`" in pack.body
    assert "queue retains" in pack.body
    assert "top-right `More options`" in pack.body
    assert "known play order" in pack.body
    assert "If the required songs are not all present" in pack.body
    assert "prefer one with fewer songs" in pack.body
    assert "overlap" in pack.body and "bottom navigation exits selection mode" in pack.body
    assert "library inventory is unnecessary" in pack.body


@pytest.mark.parametrize("app,workflow", [
    ("com.flauschcode.broccoli", "broccoli-deduplicate-recipes"),
    ("org.tasks", "androidworld-tasks-find-by-date"),
    ("code.name.monkey.retromusic", None),
])
@pytest.mark.parametrize("role", ["planner", "executor", "reviewer"])
def test_list_traversal_is_selected_cross_app_and_retired_with_stage(app, workflow, role):
    library = SkillLibrary(Path(__file__).resolve().parents[1] / "skills")
    generic = library.get("adaptive-list-traversal")
    assert generic is not None and generic.kind == "generic" and not generic.app
    assert not generic.verified_actions
    session = AgentSession(role, "m", library=library)
    session.reset_lifecycle("task:traversal-scope")
    session.freeze_allow_dirs(["generic"])
    assert generic.id in library.index_summaries(allow_dirs=["generic"])
    session.set_stage_skills(app, [workflow] if workflow else [])
    assert generic.id not in {p["skill_id"] for p in session.active_skill_metadata}
    session.set_stage_skills(app, ([workflow] if workflow else []) + [generic.id])
    assert sum(p["skill_id"] == generic.id for p in session.active_skill_metadata) == 1
    assert any(generic.section_for(role).strip() in message.get("content", "")
               for message in session._k_wire())
    session.set_stage_skills(app, [workflow] if workflow else [])
    assert generic.id not in {p["skill_id"] for p in session.active_skill_metadata}
