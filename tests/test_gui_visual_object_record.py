"""The isolated recorder checks grounded object selections before writing a bundle."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from agent.skills import script_visual_learning
from agent.skills.script_schema import GuiScript
from evaluation.gui_scripts import main
from evaluation.gui_visual_record import record_visual_script


CARD_A = (13, 20, 154, 59)
CARD_B = (13, 87, 154, 126)
CLICK = (126, 39)


def capture(*, first_active: bool, dynamic: str = "soon", identity="ALARM A", tint="white") -> bytes:
    image = Image.new("RGB", (184, 148), "#121722")
    draw = ImageDraw.Draw(image)
    for index, (left, top, right, bottom) in enumerate((CARD_A, CARD_B)):
        draw.rectangle((left, top, right - 1, bottom - 1), fill="#3a465d")
        if not index:
            draw.text((24, top + 8), identity, fill=tint)
        else:
            draw.text((24, top + 8), "TIMER B", fill="#ff9e65")
            draw.line((26, top + 23, 61, top + 23), fill="#ff9e65", width=2)
        draw.text((82, top + 8), dynamic, fill="white")
        on = first_active or index == 1
        draw.rectangle((113, top + 11, 138, top + 29), fill="#8bf0ba" if on else "#ac535b")
        draw.ellipse((126 if on else 114, top + 13, 136 if on else 124, top + 27), fill="#192432")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def side(card, role):
    offset = card[1] - CARD_A[1]
    return {"box": card, "container": "card", "parts": [
        {"role": "identity", "box": (22, 27 + offset, 69, 46 + offset)},
        {"role": role, "box": (112, 29 + offset, 140, 52 + offset)},
        {"role": "dynamic", "box": (80, 26 + offset, 109, 44 + offset)},
    ]}


def selection():
    return {"before": side(CARD_A, "control"), "after": side(CARD_A, "outcome")}


def record(tmp_path, **options):
    defaults = {"before_png": capture(first_active=True), "after_png": capture(first_active=False),
                "click": CLICK, "selection": selection(), "destination": tmp_path / "bundle",
                "script_id": "disable-alarm-a", "app_id": "example.alarms", "scales": (1.0,)}
    return record_visual_script(**{**defaults, **options})


def test_object_recording_ignores_dynamic_pixels_and_binds_result_to_tapped_object(tmp_path):
    script = record(tmp_path, reference_before=(capture(first_active=True, dynamic="later"),))
    assert GuiScript.model_validate_json((tmp_path / "bundle" / "script.json").read_bytes()) == script
    step = script.steps[0]
    assert step.action.object.parts[0].role == "identity"
    assert [condition.type for condition in step.before] == [
        "app_is", "visual_object_present", "visual_object_absent",
    ]
    assert step.after[-1].type == "visual_object_present"
    source = tmp_path / "bundle" / step.action.object.layout.png_path
    with Image.open(source) as image:
        assert image.getpixel((84 - CARD_A[0], 31 - CARD_A[1])) == (0, 0, 0)


def test_recording_cannot_assert_effect_for_a_different_object(tmp_path):
    altered = {"before": side(CARD_A, "control"), "after": side(CARD_B, "outcome")}
    with pytest.raises(ValueError, match="before object identity"):
        record(tmp_path, selection=altered)
    assert not (tmp_path / "bundle").exists()


def test_identity_link_ignores_tint_but_preserves_the_label_shape(tmp_path):
    script = record(tmp_path, after_png=capture(first_active=False, tint="#889caf"))
    assert script.steps[0].after[-1].type == "visual_object_present"
    with pytest.raises(ValueError, match="before object identity"):
        record(tmp_path, after_png=capture(first_active=False, identity="ALARM Z", tint="#889caf"),
               destination=tmp_path / "unrelated")


@pytest.mark.parametrize("tint", ["white", "#889caf"])
def test_recording_requires_independent_evidence_of_the_effect(tmp_path, tint):
    with pytest.raises(ValueError, match="verifiably absent before the tap"):
        record(tmp_path, after_png=capture(first_active=True, dynamic="later", tint=tint))
    assert not (tmp_path / "bundle").exists()


def test_cli_uses_vision_learner_without_manual_crop(tmp_path, monkeypatch, capsys):
    requests = []

    async def gateway(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        requests.append(messages)
        return SimpleNamespace(
            content=json.dumps({"decision": "select", "selection": selection()}), usage=None,
        )

    monkeypatch.setattr(script_visual_learning, "complete", gateway)
    before, after = tmp_path / "before.png", tmp_path / "after.png"
    before.write_bytes(capture(first_active=True))
    after.write_bytes(capture(first_active=False))
    output = tmp_path / "bundle"
    assert main(["--env-file", str(tmp_path / "test.env"), "record",
                 "--before", str(before), "--after", str(after), "--click", "126", "39",
                 "--script-id", "disable-alarm-a", "--app-id", "example.alarms", "--output", str(output)]) == 0
    assert len(requests) == 1
    assert [part["type"] for part in requests[0][1]["content"]].count("image_url") == 2
    assert json.loads(capsys.readouterr().out)["learner_selected_steps"] == 1
    saved = GuiScript.model_validate_json((output / "script.json").read_bytes())
    assert saved.steps[0].action.object is not None
