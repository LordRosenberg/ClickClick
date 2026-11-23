from io import BytesIO
import hashlib
import json
from math import floor

from PIL import Image, ImageDraw
import pytest

from agent.skills.script_runner import run_gui_script
from agent.skills.script_schema import GuiScript, VisualObjectCondition
from agent.skills.script_visual import VisualTemplate, read_visual_asset
from agent.skills.script_visual_object import VisualObject, VisualPart
from evaluation.gui_script_release import _copy_templates
from evaluation.gui_scripts import _assets, main
from evaluation.gui_visual_record import record_visual_script
from shared.schemas import ActionResult, ObservationMode


APP = "com.example.recorded"
BEFORE_BOX = (24, 22, 72, 54)
AFTER_BOX = (20, 110, 80, 137)
CLICK = (42, 36)
BEFORE_LAYOUT = (4, 18, 75, 55)
AFTER_LAYOUT = (2, 107, 84, 139)
IDENTITY = (5, 27, 21, 46)
AFTER_IDENTITY = (3, 114, 19, 133)


def image(*, shift=(0, 0), duplicate=False, same_identity=False, wrong_object=False, done=False):
    frame = Image.new("RGB", (220, 160), "#e8eaf0")
    draw = ImageDraw.Draw(frame)

    def button(x, y):
        draw.rectangle((x, y, x + 47, y + 31), fill="#172947")
        draw.line((x + 7, y + 24, x + 19, y + 9, x + 29, y + 22, x + 36, y + 7), fill="#91e8ff", width=3)
        draw.rectangle((x + 15, y + 4, x + 20, y + 7), fill="#ffb300")

    def label(x, y, correct):
        draw.rectangle((x, y, x + 15, y + 18), fill="#48265a" if correct else "#f4d490")
        if correct:
            draw.line((x + 2, y + 15, x + 7, y + 3, x + 13, y + 14), fill="white", width=2)
        else:
            draw.ellipse((x + 2, y + 3, x + 13, y + 15), fill="#710052")

    if done:
        label(*AFTER_IDENTITY[:2], True)
        draw.rectangle((20, 110, 79, 136), fill="#285925")
        draw.line((27, 124, 42, 132, 66, 114), fill="white", width=4)
        draw.ellipse((69, 116, 75, 122), fill="#ffd700")
    else:
        button(24 + shift[0], 22 + shift[1])
        label(IDENTITY[0] + shift[0], IDENTITY[1] + shift[1], not wrong_object)
        if duplicate:
            button(125, 22)
            label(106, 27, same_identity)
    buffer = BytesIO()
    frame.save(buffer, format="PNG")
    return buffer.getvalue()


class Device:
    serial = "fixture"

    def __init__(self, *, visible_tree=False, shift=(0, 0), duplicate_picture=False,
                 same_identity=False, wrong_object=False):
        self.visible_tree = visible_tree
        self.shift = shift
        self.duplicate_picture = duplicate_picture
        self.same_identity = same_identity
        self.wrong_object = wrong_object
        self.actions = []

    async def get_frame(self):
        done = bool(self.actions)
        children = []
        if self.visible_tree:
            for x, y in ((24 + self.shift[0], 22 + self.shift[1]), (125, 22)):
                children.append({"class": "android.widget.Button", "text": "Open", "clickable": True,
                                 "bounds": [x, y, x + 48, y + 32]})
            children.append({"class": "TextView", "text": "Control row", "bounds": [4, 23, 22, 50]})
            if done:
                children.append({"class": "TextView", "text": "Result", "bounds": list(AFTER_BOX)})
        return {"class": "Root", "package": APP, "children": children,
                "_capture": {"complete": self.visible_tree, "coordinate_compatible": self.visible_tree,
                             "tree_providers_exhausted": not self.visible_tree, "frame_geometry": [220, 160]}}, image(
                                 shift=self.shift, duplicate=self.duplicate_picture,
                                 same_identity=self.same_identity, wrong_object=self.wrong_object, done=done)

    async def current_foreground_identity(self, *, timeout_s=None):
        return {"package": APP, "activity": ".Example", "component": f"{APP}/.Example",
                "sources": [], "conflict": False}

    async def act(self, action):
        self.actions.append(action)
        return ActionResult(success=True, message="test dispatch")

    def set_last_ui(self, ui):
        pass


def recorded(tmp_path, *, roi=None, legacy=False):
    bundle = tmp_path / "bundle"
    script = record_visual_script(before_png=image(), after_png=image(done=True), click=CLICK,
                                  before_crop=BEFORE_BOX, after_crop=AFTER_BOX, before_roi=roi,
                                  destination=bundle, script_id="open-visual", app_id=APP)
    if legacy:
        return script

    def group(name, layout, identity, evidence, role, *, done=False):
        path = f"assets/{name}.png"
        frame = Image.open(BytesIO(image(done=done)))
        frame.crop(layout).save(bundle / path)
        x, y, right, bottom = layout
        point = ((CLICK[0] - x) / (right - x - 1), (CLICK[1] - y) / (bottom - y - 1))
        appearance = VisualTemplate(
            png_path=path, reference_width=frame.width, reference_height=frame.height,
            roi=roi or (0.0, 0.0, 0.5, 0.5) if not done else (0.0, 0.5, 0.5, 1.0),
            fallback_roi=(0.0, 0.0, 1.0, 1.0) if roi is None and not done else None,
            point=point if not done else (0.5, 0.5),
            sha256=hashlib.sha256((bundle / path).read_bytes()).hexdigest(),
        )

        def part(kind, rect):
            return VisualPart(role=kind, box=(rect[0] - x, rect[1] - y, rect[2] - x, rect[3] - y))

        return VisualObject(layout=appearance, parts=(part("identity", identity), part(role, evidence)),
                            container="row")

    obj = group("object-before", BEFORE_LAYOUT, IDENTITY, BEFORE_BOX, "control")
    outcome = group("object-after", AFTER_LAYOUT, AFTER_IDENTITY, AFTER_BOX, "outcome", done=True)
    guard = script.initial[0]
    absent = VisualObjectCondition(type="visual_object_absent", target=outcome)
    present = VisualObjectCondition(type="visual_object_present", target=outcome)
    raw = script.model_dump(mode="json")
    raw["initial"] = [guard.model_dump(), absent.model_dump(mode="json")]
    raw["final"] = [guard.model_dump(), present.model_dump(mode="json")]
    raw["steps"][0]["before"] = raw["initial"]
    raw["steps"][0]["after"] = raw["final"]
    raw["steps"][0]["action"]["object"] = obj.model_dump(mode="json")
    return GuiScript.model_validate_json(json.dumps(raw))


def test_registered_script_resolves_and_checks_its_own_templates(tmp_path):
    script = recorded(tmp_path)
    original = tmp_path / "bundle"
    registry = tmp_path / "registry"
    bundle = _copy_templates(script, registry, original)
    published = _assets(registry, bundle, None)
    target = script.steps[0].action.target
    expected = read_visual_asset(target, original)
    (original / target.png_path).write_bytes(b"modified source")
    assert read_visual_asset(target, published) == expected
    with pytest.raises(ValueError):
        _copy_templates(script, registry, original)


@pytest.mark.asyncio
@pytest.mark.parametrize("shift", [(15, 9), (80, 55)], ids=["within-local-roi", "outside-local-roi"])
async def test_recorded_visual_tap_uses_moved_frame_coordinates_and_verified_postcondition(tmp_path, shift):
    script = recorded(tmp_path)
    device = Device()
    modes = []
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle",
                                  before_dispatch=lambda: setattr(device, "shift", shift),
                                  on_observation=lambda frame: modes.append(frame.mode))
    assert result.status == "succeeded" and result.final_asserted, result
    assert set(modes) == {ObservationMode.IMAGE_ONLY}
    assert len(device.actions) == 1 and device.actions[0].type == "tap_xy"
    assert (floor(device.actions[0].x), floor(device.actions[0].y)) == (CLICK[0] + shift[0], CLICK[1] + shift[1])


@pytest.mark.asyncio
async def test_expansion_also_checks_the_initial_condition(tmp_path):
    script = recorded(tmp_path)
    device = Device(shift=(80, 55))
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.status == "succeeded" and result.final_asserted, result
    assert len(device.actions) == 1
    assert (floor(device.actions[0].x), floor(device.actions[0].y)) == (122, 91)


@pytest.mark.asyncio
async def test_expansion_refuses_two_equally_good_targets_outside_the_local_region(tmp_path):
    script = recorded(tmp_path)
    step = script.steps[0].model_copy(update={"timeout_seconds": 0.3})
    script = script.model_copy(update={"steps": (step,)})
    device = Device(shift=(80, 55), duplicate_picture=True, same_identity=True)
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.reason == "visual_object_ambiguous" and not device.actions, result


@pytest.mark.asyncio
async def test_manually_specified_search_region_stays_bounded(tmp_path):
    script = recorded(tmp_path, roi=(0.0, 0.0, 0.5, 0.5))
    step = script.steps[0].model_copy(update={"timeout_seconds": 0.3})
    script = script.model_copy(update={"steps": (step,)})
    device = Device(shift=(80, 55))
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.status != "succeeded" and not device.actions, result


@pytest.mark.asyncio
async def test_equal_visual_candidates_need_local_context(tmp_path):
    script = recorded(tmp_path, roi=(0.0, 0.0, 1.0, 0.5))
    step = script.steps[0].model_copy(update={"timeout_seconds": 0.3})
    script = script.model_copy(update={"steps": (step,)})
    device = Device(duplicate_picture=True, same_identity=True)
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.reason == "visual_object_ambiguous" and not device.actions


@pytest.mark.asyncio
async def test_legacy_image_only_taps_need_identity_even_for_a_unique_local_match(tmp_path):
    script = recorded(tmp_path, legacy=True)
    device = Device()
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.reason == "visual_object_evidence_required" and not device.actions, result


@pytest.mark.asyncio
async def test_a_lone_lookalike_cannot_replace_the_target_at_its_old_position(tmp_path):
    script = recorded(tmp_path)
    step = script.steps[0].model_copy(update={"timeout_seconds": 0.3})
    script = script.model_copy(update={"steps": (step,)})
    device = Device(wrong_object=True)
    result = await run_gui_script(script, driver=device, asset_root=tmp_path / "bundle")
    assert result.status != "succeeded" and not device.actions, result


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["relative", "occurrence", "image", "image_only_hybrid"])
async def test_duplicate_semantic_targets_can_use_anchor_ordinal_or_crop(tmp_path, method):
    script = recorded(tmp_path)
    raw = script.model_dump(mode="json")
    step = raw["steps"][0]
    visual = step["action"]["target"]
    target = {"text": "Open", "role": "Button"}
    action = {"type": "tap", "target": target}
    if method == "relative":
        target["relative_to"] = {"anchor": {"text": "Control row"}, "relation": "right_of"}
    elif method == "occurrence":
        target.update(occurrence=1, expected_count=2)
    else:
        action["visual"] = visual
        if method == "image_only_hybrid":
            action["object"] = step["action"]["object"]
    step["action"] = action
    step["before"].append({"type": "target_present", "target": {"text": "Open"}, "visual": visual})
    device = Device(visible_tree=method != "image_only_hybrid", duplicate_picture=True)
    result = await run_gui_script(GuiScript.model_validate_json(json.dumps(raw)), driver=device,
                                  asset_root=tmp_path / "bundle")
    assert result.status == "succeeded", result
    assert len(device.actions) == 1
    if method == "occurrence":
        assert device.actions[0].x > 125
    elif method in {"image", "image_only_hybrid"}:
        assert floor(device.actions[0].x) == CLICK[0]
    else:
        assert 24 <= device.actions[0].x < 72


def test_cli_records_a_portable_bundle(tmp_path, capsys):
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    before.write_bytes(image())
    after.write_bytes(image(done=True))
    output = tmp_path / "recording"
    assert main(["record", "--before", str(before), "--after", str(after), "--click", "42", "36",
                 "--before-crop", *map(str, BEFORE_BOX), "--after-crop", *map(str, AFTER_BOX),
                 "--script-id", "clip", "--app-id", APP, "--output", str(output)]) == 0
    assert (output / "script.json").is_file() and (output / "assets" / "before.png").is_file()
    assert '"status": "recorded"' in capsys.readouterr().out
