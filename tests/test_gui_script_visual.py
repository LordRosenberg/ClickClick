from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import PureWindowsPath

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from agent.skills.script_visual import VisualTemplate, match_visual_pixels, match_visual_template


def png(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def template_image():
    image = Image.new("RGB", (26, 20), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((1, 2, 9, 17), fill=(33, 63, 138))
    draw.ellipse((10, 3, 24, 17), fill=(210, 46, 14))
    draw.polygon(((2, 19), (23, 19), (15, 10)), fill=(20, 190, 60))
    return image


def test_current_pixels_roi_and_template_relative_point():
    template = template_image()
    frame = Image.new("RGB", (200, 160), "white")
    frame.paste(template, (19, 40))
    frame.paste(template, (137, 122))
    result = match_visual_pixels(png(template), png(frame), reference_size=frame.size,
                                 roi=(0.6, 0.7, 1.0, 1.0), point=(0.2, 0.9))
    assert result.status == "matched"
    match, = result.matches
    assert match.bounds == (137, 122, 163, 142) and match.confidence == 1.0
    assert (match.x, match.y) == pytest.approx((142.5, 139.6))


def test_wide_search_keeps_near_threshold_competitors():
    template = template_image()
    frame = Image.new("RGB", (220, 140), "white")
    frame.paste(template.point(lambda value: max(0, value - 17)), (85, 37))
    frame.paste(template.point(lambda value: max(0, value - 24)), (150, 93))
    result = match_visual_pixels(png(template), png(frame), reference_size=frame.size,
                                 roi=(0.0, 0.0, 0.25, 0.4), fallback_roi=(0.0, 0.0, 1.0, 1.0),
                                 scales=(1.0,))
    winner, = result.matches
    near, = result.near_matches
    assert winner.bounds == (85, 37, 111, 57)
    assert near.bounds == (150, 93, 176, 113)
    assert near.confidence < 0.92 <= winner.confidence
    assert winner.confidence - near.confidence < 0.04


def test_scale_is_relative_to_current_screen_size_then_searches_neighboring_scales():
    template = template_image()
    frame = Image.new("RGB", (360, 240), "white")
    scaled = template.resize((55, 42), Image.Resampling.LANCZOS)
    frame.paste(scaled, (216, 151))
    result = match_visual_pixels(png(template), png(frame), reference_size=(180, 120))
    assert result.status == "matched"
    match, = result.matches
    assert match.bounds == (216, 151, 271, 193)
    assert match.confidence == 1.0


def test_overlapping_proposals_merge_and_separate_matches_stay_ambiguous():
    template = template_image()
    frame = Image.new("RGB", (220, 140), "white")
    frame.paste(template, (23, 37))
    frame.paste(template, (143, 93))
    result = match_visual_pixels(png(template), png(frame), reference_size=frame.size,
                                 threshold=0.84, scales=(0.94, 1.0, 1.06))
    assert result.status == "matched"
    assert [match.bounds for match in result.matches] == [(23, 37, 49, 57), (143, 93, 169, 113)]
    assert [match.confidence for match in result.matches] == [1.0, 1.0]


def test_sparse_icon_is_found_without_matching_the_plain_background():
    template = Image.new("RGB", (29, 29), "white")
    draw = ImageDraw.Draw(template)
    draw.line((11, 12, 17, 17, 20, 8), fill="black", width=2)
    frame = Image.new("RGB", (850, 650), "white")
    frame.paste(template, (691, 44))
    result = match_visual_pixels(png(template), png(frame), reference_size=frame.size)
    assert result.status == "matched"
    match, = result.matches
    assert match.bounds == (691, 44, 720, 73)


def test_absence_is_distinct_from_missing_or_invalid_evidence():
    template = png(template_image())
    plain = png(Image.new("RGB", (100, 100), "white"))
    absent = match_visual_pixels(template, plain, reference_size=(100, 100))
    assert absent.status == "not_found" and not absent.matches and absent.reason is None
    unavailable = match_visual_pixels(template, b"unsupported screenshot", reference_size=(100, 100))
    assert unavailable.status == "unavailable" and not unavailable.matches
    assert unavailable.reason == "visual_image_decode_failed"
    incomplete = match_visual_pixels(plain, plain, reference_size=(100, 100))
    assert incomplete.status == "unavailable" and incomplete.reason == "template_has_insufficient_visual_detail"
    invalid = match_visual_pixels(template, plain, reference_size=(100, 100), roi=(0.5, 0.7, 0.2, 1.0))
    assert invalid.status == "unavailable" and invalid.reason == "visual_geometry_invalid"
    tiny = match_visual_pixels(template, plain, reference_size=(100, 100), roi=(0.0, 0.0, 0.001, 0.001))
    assert tiny.status == "unavailable" and tiny.reason == "visual_roi_has_no_pixels"


def test_pinned_local_asset_and_path_scope(tmp_path):
    template_image_bytes = png(template_image())
    asset_root = tmp_path / "assets"
    asset_root.mkdir()
    path = asset_root / "icon.png"
    path.write_bytes(template_image_bytes)
    frame = Image.new("RGB", (100, 80), "white")
    frame.paste(template_image(), (20, 30))
    config = VisualTemplate(png_path="icon.png", reference_width=100, reference_height=80,
                            sha256=sha256(template_image_bytes).hexdigest())
    matched = match_visual_template(config, png(frame), asset_root=asset_root)
    assert matched.status == "matched"
    path.write_bytes(png(Image.new("RGB", (26, 20), "white")))
    mismatch = match_visual_template(config, png(frame), asset_root=asset_root)
    assert mismatch.status == "unavailable" and mismatch.reason == "visual_template_hash_mismatch"
    path.unlink()
    missing = match_visual_template(config, png(frame), asset_root=asset_root)
    assert missing.status == "unavailable" and missing.reason == "visual_template_asset_invalid"
    absolute_windows_fixture = str(PureWindowsPath("C:/", "fixtures", "icon.png"))
    for invalid in ("../icon.png", "/icon.png", absolute_windows_fixture):
        with pytest.raises(ValidationError, match="relative PNG asset path"):
            VisualTemplate(png_path=invalid, reference_width=100, reference_height=80)
    with pytest.raises(ValidationError):
        VisualTemplate(png_path="icon.png", reference_width=100, reference_height=80,
                       sha256=config.sha256.upper())
