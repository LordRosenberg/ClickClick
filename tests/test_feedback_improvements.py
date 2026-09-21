"""Regression cases for shared plans, simple recall and indexed clean-pixel reads."""
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from agent.read_tools import make_inspect_image_regions_handler
from agent.revisable.dialogue import restore_dialogue
from agent.revisable.store import TaskStore
from agent.revisable.tools import register_memory_tools
from agent.session import AgentSession
from agent.skills.library import SkillLibrary
from agent.tool_registry import AgentRole, AgentToolRegistry, ToolExecutionContext, ToolStatus
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.revisable import Plan, Stage
from shared.schemas import AgentState, CanonicalUI, ObservationMode, UIElement


@pytest.fixture
def task(tmp_path):
    db = Database(tmp_path / "task.db")
    store = TaskStore(db, ArtifactStore(tmp_path / "artifacts"), "regression")
    state = AgentState(instruction="Draw then enter the recorded values")
    state.revisable.plan = Plan(current_stage=Stage(goal="Collect values"),
                                assumption_roadmap=["Use the values in the next screen"])
    state.revisable.revision = 2
    yield store, state
    db.close()


def registry_for(store, state, role="executor"):
    registry = AgentToolRegistry()
    register_memory_tools(registry, role, store, state)
    return registry, ToolExecutionContext(AgentRole(role), "read", {})


async def test_plan_is_restored_without_dialogue_and_delivered_on_revision(task):
    store, state = task
    state.revisable.plan_reason = "Retain values for later entry"
    first = store.execution_context(state, "current")["runtime_update"]
    assert first["latest_plan"]["assumption_roadmap"] == ["Use the values in the next screen"]
    assert first["plan_reason"] == "Retain values for later entry"
    assert first["feedback"] == ""
    assert "latest_plan" not in store.execution_context(state, "next")["runtime_update"]
    state.revisable.revision += 1
    unchanged_body = store.execution_context(state, "next")["runtime_update"]
    assert unchanged_body["latest_plan"] == first["latest_plan"]
    state.revisable.revision += 1
    state.revisable.plan.assumption_roadmap = ["A changed future dependency"]
    revised = store.execution_context(state, "next")["runtime_update"]
    assert revised["plan_revision"] == 4
    assert revised["latest_plan"]["assumption_roadmap"] == ["A changed future dependency"]
    await restore_dialogue(store, state, model="unused", settings=Settings(_env_file=None),
                           meter=None, event_sink=None)
    restored = store.execution_context(state, "next")["runtime_update"]
    assert restored["latest_plan"] == revised["latest_plan"]


async def test_history_supplied_whole_note_is_skipped_but_update_is_readable(task):
    store, state = task
    store.put("note", "color", {"title": "Color", "content": "RGB 12,34,56", "observation_ids": []})
    registry, ctx = registry_for(store, state, "planner")
    initial = await registry.execute("read_history", {"query": "RGB"}, ctx)
    assert initial.data["items"][0]["already_supplied"]
    assert "text" not in initial.data["items"][0]
    assert not ctx.state.get("evidence_saturated")
    store.put("note", "color", {"title": "Color", "content": "Corrected RGB 65,43,21", "observation_ids": []})
    update = await registry.execute("read_history", {"source": "color"}, ctx)
    assert update.data["items"][0]["text"].endswith("65,43,21")
    old = await registry.execute("read_history", {"source": "note:color@1", "full": True}, ctx)
    assert old.data["items"][0]["text"].endswith("12,34,56")


async def test_history_one_query_returns_cross_source_answers_and_equal_occurrences(task):
    store, state = task
    for step in (1, 2):
        store.put("event", str(step), {"step": step, "executor_report": "Read value 9"})
    store.put("note", "saved", {"title": "Values", "content": "Read value 9 twice", "observation_ids": []})
    store.put("observation", "obs_value", {"text": "Read value 9", "step": 2})
    registry, ctx = registry_for(store, state)
    result = await registry.execute("read_history", {"query": "value"}, ctx)
    assert len(result.data["items"]) == 4
    assert all("value" in item["text"] for item in result.data["items"])
    assert {item["source"].split(":")[0] for item in result.data["items"]} == {"note", "event", "observation"}
    assert {item["source"] for item in result.data["items"] if item["source"].startswith("event:")} == {"event:1@1", "event:2@1"}
    assert ctx.state["history_reads"] == 1
    assert sum(len(item["text"]) for item in result.data["items"]) <= 3000
    duplicate = await registry.execute("read_history", {"query": "Read"}, ctx)
    assert all(item["already_supplied"] for item in duplicate.data["items"])
    missing = await registry.execute("read_history", {"query": "neverobserved"}, ctx)
    assert missing.data["items"] == []
    assert "does not prove" in missing.summary


async def test_history_source_continuation_and_images_remain_available(task):
    store, state = task
    text = "1234567890" * 1500 + "EXACT END"
    ref = store.artifacts.save_bytes("images", b"historical image")
    store.put("observation", "obs_long", {"text": text, "image_ref": ref, "step": 1})
    store.put("event", "attempt", {"step": 1, "observation_id": "before", "action_result": {"receipt": {"effect_observation_id": "obs_long"}}})
    registry, ctx = registry_for(store, state)
    ctx.state["active_observation_id"] = "current"
    first = await registry.execute("read_history", {"source": "obs_long", "full": True}, ctx)
    row = first.data["items"][0]
    assert row["event_sources"] == ["event:attempt@1"]
    assert len(row["text"]) == 12000
    rest = await registry.execute("read_history", {"source": row["continue_source"], "full": True}, ctx)
    assert row["text"] + rest.data["items"][0]["text"] == text
    image = await registry.execute("read_history", {"source": "obs_long", "view": "image"}, ctx)
    assert image.attachments[0].content == b"historical image"
    assert not image.attachments[0].actionable_coordinate_reference
    assert ctx.state["active_observation_id"] == "current"
    duplicate = await registry.execute("read_history", {"source": "obs_long", "view": "image"}, ctx)
    assert not duplicate.attachments
    assert duplicate.data["items"][0]["already_supplied"]


@pytest.mark.parametrize("args", [{}, {"source": "x", "query": "y"}, {"query": "x", "view": "image"}])
async def test_single_history_tool_rejects_ambiguous_requests(task, args):
    store, state = task
    registry, ctx = registry_for(store, state)
    assert {spec.name for spec in registry.specs_for_role("executor")} == {"read_history", "write_note"}
    result = await registry.execute("read_history", args, ctx)
    assert result.status != ToolStatus.SUCCEEDED


def png(image):
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("rotation,model_bounds", [
    (0, [5, 10, 15, 20]), (90, [30, 5, 40, 15]),
    (180, [35, 30, 45, 40]), (270, [10, 35, 20, 45]),
])
async def test_indexed_rgb_matches_unindexed_reference_after_crop_rotation_and_resize(rotation, model_bounds):
    frame = Image.new("RGB", (400, 600), "black")
    frame.paste((18, 95, 201), (120, 240, 160, 280))
    clean = frame.crop((100, 200, 300, 400)).rotate(-rotation).resize((100, 100), Image.Resampling.NEAREST)
    package = ObservationPackage(
        ui=CanonicalUI(elements=[UIElement(index=3, bounds=[120, 240, 160, 280], clickable=True)]),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm="[3] swatch", gap_reasons=[],
        clean_png=png(clean), image_for_llm=png(Image.new("RGB", (50, 50), "green")),
        annotated_png=png(Image.new("RGB", (100, 100), "green")),
        observation_id="current", frame_width=400, frame_height=600,
        model_image_width=50, model_image_height=50,
        crop_box=(100, 200, 300, 400), rotation_degrees=rotation, index_actionable=True,
    )
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, "pixels", {"active_observation_id": "current", "active_package": package})
    result = await make_inspect_image_regions_handler()({
        "observation_id": "current", "targets": [{"index": 3, "inset_ratio": .1}], "regions": [
            {"bounds": model_bounds, "inset_ratio": .1},
        ], "metrics": ["median_rgb"], "pairs": [["index:3", "region:1"]],
    }, ctx)
    assert result.data["regions"]["index:3"]["median_rgb"] == [18, 95, 201]
    assert result.data["regions"]["index:3"]["index"] == 3
    assert result.data["regions"]["index:3"]["sample_bounds"] == result.data["regions"]["region:1"]["sample_bounds"]
    assert result.data["regions"]["region:1"]["median_rgb"] == [18, 95, 201]
    assert result.data["delta_e_2000"] == [["index:3", "region:1", 0.0]]
    assert result.data["observation_id"] == "current"


@pytest.mark.parametrize("region,index_actionable,error", [
    ({"index": 77}, True, "unknown"), ({"index": True}, True, "invalid"),
    ({"index": 3, "bounds": [0, 0, 1, 1]}, True, "targets require"),
    ({}, True, "targets require"), ({"index": 3}, False, "unavailable"),
])
async def test_indexed_rgb_rejects_invalid_namespace_without_sampling(region, index_actionable, error):
    package = ObservationPackage(
        ui=CanonicalUI(elements=[UIElement(index=3, bounds=[0, 0, 10, 10])]),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm="", gap_reasons=[],
        clean_png=png(Image.new("RGB", (10, 10))), image_for_llm=b"x", annotated_png=None,
        observation_id="current", frame_width=10, frame_height=10,
        model_image_width=10, model_image_height=10, index_actionable=index_actionable,
    )
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, "pixels", {"active_observation_id": "current", "active_package": package})
    with pytest.raises(ValueError, match=error):
        await make_inspect_image_regions_handler()({"observation_id": "current", "targets": [region], "metrics": ["median_rgb"]}, ctx)
    assert not ctx.state.get("image_region_inspection_calls")


async def test_generic_canvas_has_one_discoverable_loadable_deduplicated_entry():
    library = SkillLibrary(Path(__file__).parents[1] / "skills")
    session = AgentSession("executor", "unused", library=library)
    session.freeze_allow_dirs(["generic"])
    packs = [pack for pack in library.load_all() if pack.id == "canvas-drawing"]
    assert len(packs) == 1 and packs[0].kind == "generic"
    summaries = library.index_summaries(allow_dirs=["generic"])
    assert str(summaries).count("canvas-drawing") == 1
    registry = session._build_registry({})
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, "skill", {})
    first = await registry.execute("load_skill", {"skill_id": "canvas-drawing"}, ctx)
    assert first.status == ToolStatus.SUCCEEDED
    assert "## Prepare" in first.summary
    assert "## Save" in first.model_metadata("load_skill")["summary"]
    assert packs[0].section_for("executor") in first.model_metadata("load_skill")["summary"]
    repeated = await registry.execute("load_skill", {"skill_id": "canvas-drawing"}, ctx)
    assert "## Prepare" not in repeated.summary
    assert repeated.data["already_loaded"]
    assert len(session._k_wire()) == 1
    assert session.loaded_skill_ids == ["canvas-drawing"]
    assert len(packs[0].body) < 4000


async def test_recorded_drawing_replan_supplied_both_notes_before_eleven_reads(task):
    store, state = task
    fixture = json.loads((Path(__file__).parent / "fixtures/mobile_feedback_history.json").read_text(encoding="utf-8"))
    packet = fixture["initial_packet"]
    assert fixture["baseline_model_rounds"] == 7
    assert len(fixture["baseline_reads"]) == 11
    assert len(packet["operation_summary"]["events"]) == 9
    assert len(packet["note_contents"]) == 2
    for note in packet["note_contents"]:
        store.put("note", note["note_key"], {key: value for key, value in note.items() if key not in {"note_key", "version"}})
    for event in packet["operation_summary"]["events"]:
        store.put("event", str(event["step"]), event)
    registry, ctx = registry_for(store, state, "planner")
    for note in packet["note_contents"]:
        result = await registry.execute("read_history", {"source": note["note_key"]}, ctx)
        assert result.data["items"][0]["already_supplied"]
        assert "text" not in result.data["items"][0]
    event = packet["operation_summary"]["events"][0]
    supplied_event = await registry.execute("read_history", {"source": f"event:{event['step']}@1"}, ctx)
    assert supplied_event.data["items"][0]["already_supplied"]
    # Omitted original evidence still has an escape hatch; no model verdict is
    # simulated here, and this does not claim improved real-model task success.
    full = await registry.execute("read_history", {"source": "palette_geometry", "full": True}, ctx)
    assert packet["note_contents"][1]["content"] in full.data["items"][0]["text"]
    assert full.data["items"][0]["observation_ids"] == packet["note_contents"][1]["observation_ids"]
    assert not ctx.state.get("evidence_saturated")


async def test_executor_generic_loading_does_not_grant_app_workflow_selection(tmp_path):
    directory = tmp_path / "apps/com.example/workflows/edit"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: app-edit\ndescription: Edit\nversion: 0.1.0\nkind: workflow\ncapability: edit\napp: com.example\n---\n\n## Procedure\nEdit this app.\n## Verification\nInspect result.")
    session = AgentSession("executor", "unused", library=SkillLibrary(tmp_path))
    session.freeze_allow_dirs(["generic", "apps/com.example"])
    result = await session._build_registry({}).execute("load_skill", {"skill_id": "app-edit"}, ToolExecutionContext(AgentRole.EXECUTOR, "load", {}))
    assert result.error == "skill_scope_denied"
    assert not session.loaded_skill_ids


async def test_history_note_keys_with_punctuation_have_copyable_sources(task):
    store, state = task
    store.put("note", "canvas:color#1@v", {"title": "Color", "content": "RGB=1,2,3", "observation_ids": []})
    source = store.note_index()[0]["source"]
    registry, ctx = registry_for(store, state)
    bare = await registry.execute("read_history", {"source": "canvas:color#1@v"}, ctx)
    typed = await registry.execute("read_history", {"source": source}, context_for_test())
    assert bare.data["items"][0]["text"] == typed.data["items"][0]["text"] == "Color\nRGB=1,2,3"


def context_for_test():
    return ToolExecutionContext(AgentRole.EXECUTOR, "read", {})


async def test_indexed_rgb_reports_mixed_pixels_and_rejects_offscreen_indices():
    clean = Image.new("RGB", (20, 10), "blue")
    clean.paste("white", (0, 0, 10, 10))
    package = ObservationPackage(
        ui=CanonicalUI(elements=[UIElement(index=3, bounds=[0, 0, 20, 10]),
                                UIElement(index=4, bounds=[0, 0, 30, 20])]),
        mode=ObservationMode.TREE_PLUS_IMAGE, text_for_llm="", gap_reasons=[],
        clean_png=png(clean), image_for_llm=b"x", annotated_png=None,
        observation_id="current", frame_width=20, frame_height=10,
        model_image_width=20, model_image_height=10,
    )
    ctx = ToolExecutionContext(AgentRole.EXECUTOR, "pixels", {"active_observation_id": "current", "active_package": package})
    handler = make_inspect_image_regions_handler()
    result = await handler({"observation_id": "current", "targets": [{"index": 3}], "metrics": ["median_rgb"]}, ctx)
    assert result.data["mixed_regions"] == ["index:3"]
    assert result.data["regions"]["index:3"]["median_rgb"] != [0, 0, 255]
    with pytest.raises(ValueError, match="out-of-bounds"):
        await handler({"observation_id": "current", "targets": [{"index": 4}], "metrics": ["median_rgb"]}, ctx)
    assert ctx.state["image_region_inspection_calls"] == 1
