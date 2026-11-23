"""Offline visual script recording with the actual template matcher."""

import hashlib
import io
import math

import pytest
from PIL import Image, ImageDraw

from agent.skills.script_schema import GuiScript
from agent.skills.script_visual import match_visual_template
from evaluation.gui_visual_record import record_visual_script, record_visual_sequence


BUTTON = (28, 22, 56, 42)
RESULT = (79, 56, 110, 78)
CLICK = (33, 35)
DONE = (112, 12, 137, 34)


def new_frame(*, size=(140, 92)):
    return Image.new("RGB", size, (13, 19, 23))


def draw_button(frame, box=BUTTON):
    left, top, right, bottom = box
    draw = ImageDraw.Draw(frame)
    draw.rectangle((left + 2, top + 2, right - 3, bottom - 3), fill=(193, 76, 42))
    draw.line((left + 4, bottom - 5, right - 4, top + 3), fill="white", width=4)
    draw.rectangle((left + 5, top + 4, left + 9, top + 11), fill="black")


def draw_result(frame):
    left, top, right, bottom = RESULT
    draw = ImageDraw.Draw(frame)
    draw.rectangle((left + 2, top + 2, right - 3, bottom - 3), fill=(52, 188, 90))
    draw.line((left + 6, top + 10, left + 13, bottom - 4, right - 7, top + 4), fill="white", width=5)
    draw.rectangle((right - 10, bottom - 8, right - 4, bottom - 4), fill="black")


def png(frame):
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def captures():
    before = new_frame()
    draw_button(before)
    after = before.copy()
    draw_result(after)
    return png(before), png(after)


def record(tmp_path, captures, **options):
    values = {"before_png": captures[0], "after_png": captures[1], "click": CLICK, "after_crop": RESULT,
              "before_crop": BUTTON, "destination": tmp_path / "draft", "script_id": "confirm-button",
              "app_id": "example.visual", "scales": (1.0,)}
    return record_visual_script(**{**values, **options})


def test_record_writes_portable_guards_and_resolves_recorded_click(tmp_path, captures):
    screenshot = tmp_path / "source.png"
    screenshot.write_bytes(captures[0])
    script = record(tmp_path, captures, before_png=screenshot)
    bundle = tmp_path / "draft"
    assert GuiScript.model_validate_json((bundle / "script.json").read_bytes()) == script
    step, = script.steps
    assert step.action.type == "tap_visual"
    assert [condition.type for condition in script.initial] == ["app_is", "visual_present", "visual_absent"]
    assert script.initial == step.before
    assert script.final == step.after
    before, after = step.action.target, script.final[-1].target
    assert (before.reference_width, before.reference_height) == (140, 92)
    assert before.fallback_roi == (0.0, 0.0, 1.0, 1.0)
    assert after.fallback_roi is None
    for template in (before, after):
        path = bundle / template.png_path
        assert template.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert path.parent == bundle / "assets"
    match, = match_visual_template(before, captures[0], asset_root=bundle).matches
    assert match.bounds == BUTTON
    assert (math.floor(match.x), math.floor(match.y)) == CLICK
    assert match_visual_template(after, captures[0], asset_root=bundle).status == "not_found"
    assert match_visual_template(after, captures[1], asset_root=bundle).status == "matched"


def test_default_crop_relocalizes_to_a_shifted_pattern(tmp_path, captures):
    script = record(tmp_path, captures, before_crop=None, scales=None)
    shifted = new_frame()
    shift = (5, 4)
    relocated = (BUTTON[0] + shift[0], BUTTON[1] + shift[1], BUTTON[2] + shift[0], BUTTON[3] + shift[1])
    draw_button(shifted, relocated)
    result = match_visual_template(script.steps[0].action.target, png(shifted), asset_root=tmp_path / "draft")
    match, = result.matches
    assert (math.floor(match.x), math.floor(match.y)) == (CLICK[0] + shift[0], CLICK[1] + shift[1])


def test_recent_capture_must_distinguish_the_postcondition(tmp_path, captures):
    unchanged = (captures[1], captures[1])
    with pytest.raises(ValueError, match="after crop must be verifiably absent"):
        record(tmp_path, unchanged)
    assert not (tmp_path / "draft").exists()


def test_ambiguous_before_template_is_not_recorded(tmp_path, captures):
    before = new_frame()
    draw_button(before)
    draw_button(before, (85, 22, 113, 42))
    with pytest.raises(ValueError, match="before crop must match exactly one"):
        record(tmp_path, captures, before_png=png(before), before_roi=(0.0, 0.0, 1.0, 0.56))
    assert not (tmp_path / "draft").exists()


def test_default_widening_requires_a_unique_template_in_recorded_frame(tmp_path, captures):
    before = new_frame()
    draw_button(before)
    draw_button(before, (85, 22, 113, 42))
    script = record(tmp_path, captures, before_png=png(before))
    assert script.steps[0].action.target.fallback_roi is None


@pytest.mark.parametrize("options,reason", [
    ({"click": (3, 4)}, "click must lie inside the before crop"),
    ({"after_crop": (-1, 2, 6, 10)}, "after crop must be at least 2x2 pixels"),
    ({"before_png": b"bad capture"}, "capture must be one opaque PNG"),
    ({"after_png": png(new_frame(size=(142, 96)))}, "must have the same screenshot dimensions"),
    ({"after_roi": (0.0, 0.0, 0.4, 0.5)}, "after crop must match exactly one"),
])
def test_invalid_geometry_or_capture_never_creates_an_artifact(tmp_path, captures, options, reason):
    with pytest.raises(ValueError, match=reason):
        record(tmp_path, captures, **options)
    assert not (tmp_path / "draft").exists()


def test_destination_cannot_overwrite_an_existing_bundle(tmp_path, captures):
    destination = tmp_path / "draft"
    destination.mkdir()
    existing = destination / "script.json"
    existing.write_text("existing record", encoding="utf-8")
    with pytest.raises(FileExistsError):
        record(tmp_path, captures)
    assert existing.read_text(encoding="utf-8") == "existing record"
    assert list(destination.iterdir()) == [existing]


def sequence_actions(captures, *, disconnect=False):
    before_second = new_frame()
    draw_button(before_second)
    if not disconnect:
        draw_result(before_second)
    after_second = before_second.copy()
    draw = ImageDraw.Draw(after_second)
    draw.ellipse((DONE[0] + 1, DONE[1] + 1, DONE[2] - 2, DONE[3] - 2), fill="yellow")
    draw.line((DONE[0] + 6, DONE[1] + 5, DONE[2] - 8, DONE[3] - 4), fill="blue", width=5)
    return [
        {"before_png": captures[0], "after_png": captures[1], "click": CLICK,
         "before_crop": BUTTON, "after_crop": RESULT, "scales": (1.0,)},
        {"before_png": png(before_second), "after_png": png(after_second), "click": CLICK if disconnect else (95, 68),
         "before_crop": BUTTON if disconnect else RESULT, "after_crop": DONE, "scales": (1.0,)},
    ]


def test_visual_sequence_records_distinct_assets_and_carries_forward_previous_result(tmp_path, captures):
    bundle = tmp_path / "sequence"
    actions = sequence_actions(captures)
    script = record_visual_sequence(actions=actions, destination=bundle, script_id="two-taps", app_id="example.visual")
    assert GuiScript.model_validate_json((bundle / "script.json").read_bytes()) == script
    first, second = script.steps
    assert (first.id, second.id) == ("tap-1", "tap-2")
    assert script.initial == first.before
    assert script.final == second.after
    assert first.after[1] in second.before
    assert {p.name for p in (bundle / "assets").iterdir()} == {
        "step-001-before.png", "step-001-after.png", "step-002-before.png", "step-002-after.png",
    }
    for step, action in zip(script.steps, actions, strict=True):
        matched, = match_visual_template(step.action.target, action["before_png"], asset_root=bundle).matches
        assert (math.floor(matched.x), math.floor(matched.y)) == action["click"]


def test_visual_sequence_requires_a_continuous_recording_before_writing_any_assets(tmp_path, captures):
    bundle = tmp_path / "sequence"
    actions = sequence_actions(captures, disconnect=True)
    with pytest.raises(ValueError, match="action 2: before capture must uniquely match the prior step"):
        record_visual_sequence(actions=actions, destination=bundle, script_id="broken-taps", app_id="example.visual")
    assert not bundle.exists()
