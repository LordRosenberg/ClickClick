"""Create a portable visual tap script from explicit before/after PNG pairs."""

from __future__ import annotations

import hashlib
import io
import json
import math
from collections.abc import Iterable, Mapping
from contextlib import suppress
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

from agent.skills.script_schema import (
    AppCondition, GuiScript, ScriptStep, VisualCondition, VisualObjectCondition, VisualTapAction,
)
from agent.skills.script_visual_learning import RecordingObject, VisualRecordingSelection
from agent.skills.script_visual import (
    _FULL_FRAME, _MAX_IMAGE_BYTES, PixelBox, Region, VisualTemplate, _contrast, _decode, match_visual_pixels,
)
from agent.skills.script_visual_object import VisualObject, VisualPart, match_visual_object_pixels


_RecordingOutcome = tuple[VisualCondition | VisualObjectCondition, bytes]


def _read_frame(source: bytes | str | Path) -> tuple[bytes, Image.Image]:
    try:
        if isinstance(source, bytes):
            content = source
        else:
            with Path(source).open("rb") as stream:
                content = stream.read(_MAX_IMAGE_BYTES + 1)
        return content, _decode(content, formats=("PNG",))
    except (Image.DecompressionBombError, OSError, TypeError, ValueError, SyntaxError) as exc:
        raise ValueError("capture must be one opaque PNG within the visual matcher limits") from exc


def _crop_box(box: PixelBox, size: tuple[int, int], label: str) -> PixelBox:
    if not isinstance(box, (tuple, list)) or len(box) != 4 or any(type(value) is not int for value in box):
        raise ValueError(f"{label} requires four integer pixel bounds")
    left, top, right, bottom = box
    width, height = size
    if not (0 <= left < right <= width and 0 <= top < bottom <= height and right - left >= 2 and bottom - top >= 2):
        raise ValueError(f"{label} must be at least 2x2 pixels and stay inside the capture")
    return left, top, right, bottom


def _default_crop(click: tuple[int, int], size: tuple[int, int]) -> PixelBox:
    x, y = click
    width, height = size
    radius = min(48, max(16, min(size) // 16))
    return max(0, x - radius), max(0, y - radius), min(width, x + radius + 1), min(height, y + radius + 1)


def _local_roi(box: PixelBox, size: tuple[int, int]) -> Region:
    left, top, right, bottom = box
    width, height = size
    x_margin = max(12, (right - left) // 2)
    y_margin = max(12, (bottom - top) // 2)
    return (max(0, left - x_margin) / width, max(0, top - y_margin) / height,
            min(width, right + x_margin) / width, min(height, bottom + y_margin) / height)


def _point(box: PixelBox, click: tuple[int, int]) -> tuple[float, float]:
    left, top, right, bottom = box
    x, y = click
    if not (left <= x < right and top <= y < bottom):
        raise ValueError("click must lie inside the before crop")
    return float(x - left) / (right - left - 1), float(y - top) / (bottom - top - 1)


def _template(image, box, path, roi, threshold, scales, *, point=(0.5, 0.5)) -> tuple[VisualTemplate, bytes]:
    buffer = io.BytesIO()
    image.crop(box).save(buffer, format="PNG")
    content = buffer.getvalue()
    options = {"scales": scales} if scales is not None else {}
    template = VisualTemplate(png_path=path, reference_width=image.width, reference_height=image.height,
                              roi=roi if roi is not None else _local_roi(box, image.size), threshold=threshold,
                              sha256=hashlib.sha256(content).hexdigest(), point=point, **options)
    return template, content


def _match(template: VisualTemplate, content: bytes, screenshot: bytes):
    return match_visual_pixels(content, screenshot, reference_size=(template.reference_width, template.reference_height),
                               roi=template.roi, fallback_roi=template.fallback_roi,
                               threshold=template.threshold, scales=template.scales,
                               sha256=template.sha256, point=template.point)


def _expect_unique(template, content, screenshot, box, label):
    result = _match(template, content, screenshot)
    if result.status == "unavailable":
        raise ValueError(f"{label} template cannot be verified ({result.reason})")
    if result.status != "matched" or len(result.matches) != 1 or result.matches[0].bounds != box:
        raise ValueError(f"{label} crop must match exactly one location in its source capture")
    return result.matches[0]


def _overlap(a: PixelBox, b: PixelBox) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _object_assets(selection: RecordingObject, image: Image.Image, path: str, roi, wide, threshold, scales,
                   *, click=None) -> tuple[VisualObject, bytes, PixelBox]:
    box = _crop_box(selection.box, image.size, "object container")
    left, top, right, bottom = box
    masked = Image.new("RGB", (right - left, bottom - top))
    parts, used = [], []
    for part in selection.parts:
        rectangle = _crop_box(part.box, image.size, part.role)
        x0, y0, x1, y1 = rectangle
        if not (left <= x0 < x1 <= right and top <= y0 < y1 <= bottom):
            raise ValueError("every selected region must belong to its recorded container")
        if any(_overlap(rectangle, other) for other in used):
            raise ValueError("selected recording regions must not overlap")
        used.append(rectangle)
        if part.role != "dynamic":
            part_box = (x0 - left, y0 - top, x1 - left, y1 - top)
            parts.append(VisualPart(role=part.role, box=part_box))
            masked.paste(image.crop(rectangle), part_box[:2])
    buffer = io.BytesIO()
    masked.save(buffer, format="PNG")
    content = buffer.getvalue()
    region = roi if roi is not None else _local_roi(box, image.size)
    if wide is None and roi is None and region != _FULL_FRAME:
        wide = _FULL_FRAME
    options = {"scales": scales} if scales is not None else {}
    layout = VisualTemplate(png_path=path, reference_width=image.width, reference_height=image.height,
                            roi=region, fallback_roi=wide, threshold=threshold, sha256=hashlib.sha256(content).hexdigest(),
                            point=_point(box, click) if click is not None else (0.5, 0.5), **options)
    return VisualObject(layout=layout, parts=tuple(parts), container=selection.container), content, box


def _contained(inner: PixelBox, outer: PixelBox) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _source_match(obj: VisualObject, asset: bytes, frame: bytes, *, identity_only: bool = False):
    """Verify the full authorized area even if normal matching starts locally."""
    selector = obj
    if obj.layout.fallback_roi is not None:
        layout = obj.layout.model_copy(update={"roi": obj.layout.fallback_roi, "fallback_roi": None})
        selector = obj.model_copy(update={"layout": layout})
    return match_visual_object_pixels(selector, asset, frame, identity_only=identity_only)


def _same_identity_shape(before: Image.Image, after: Image.Image, *, after_part: PixelBox) -> bool:
    """Compare identity strokes when the demonstrated action changes their tint or weight."""
    contrasts = [_contrast(image) for image in (before, after)]
    for ratio in (0.35, 0.65):
        masks = []
        for contrast in contrasts:
            peak = contrast.getextrema()[1]
            if peak < 12:
                return False
            level = round(peak * ratio)
            masks.append(contrast.point([0 if value < level else 255 for value in range(256)]))
        bounds = [mask.getbbox() for mask in masks]
        if any(box is None for box in bounds) or not _contained(bounds[1], after_part):
            return False
        # A few current-frame pixels account for anti-aliasing and font-weight changes.
        height = min(box[3] - box[1] for box in bounds)
        radius = min(3, max(1, height // 20))
        for pattern, other in (masks, masks[::-1]):
            count = pattern.histogram()[255]
            if not 0 < count < pattern.width * pattern.height:
                return False
            covered = ImageChops.darker(pattern, other.filter(ImageFilter.MaxFilter(radius * 2 + 1))).histogram()[255]
            if count - covered > max(1, count // 200):
                return False
    return True


def _link_recolored_identity(selection, before_image, before_box, after_image, after_box) -> bool:
    """Link proposed, separately verified groups in the same recorded pose without using model text."""
    before_parts = [part.box for part in selection.before.parts if part.role == "identity"]
    after_parts = [part.box for part in selection.after.parts if part.role == "identity"]
    if len(before_parts) != len(after_parts):
        return False
    shift_x, shift_y = after_box[0] - before_box[0], after_box[1] - before_box[1]
    paired = set()
    for region in before_parts:
        x0, y0, x1, y1 = region
        moved = x0 + shift_x, y0 + shift_y, x1 + shift_x, y1 + shift_y
        candidates = [index for index, part in enumerate(after_parts) if _overlap(part, moved)]
        if len(candidates) != 1 or candidates[0] in paired or not _contained(moved, after_box):
            return False
        candidate = candidates[0]
        x0, y0, x1, y1 = after_parts[candidate]
        local_part = (x0 - moved[0], y0 - moved[1], x1 - moved[0], y1 - moved[1])
        if not _same_identity_shape(before_image.crop(region), after_image.crop(moved), after_part=local_part):
            return False
        paired.add(candidate)
    return len(paired) == len(after_parts)


def _record_object_step(selection, *, before_bytes, before_image, after_bytes, after_image, click,
                        before_checks, before_roi, before_fallback_roi, after_roi, threshold, scales,
                        references, app_guard, step_id, asset_prefix):
    selection = VisualRecordingSelection.model_validate(selection)
    before, before_asset, before_box = _object_assets(
        selection.before, before_image, f"assets/{asset_prefix}object-before.png", before_roi, before_fallback_roi,
        threshold, scales, click=click,
    )
    after, after_asset, after_box = _object_assets(
        selection.after, after_image, f"assets/{asset_prefix}object-after.png", after_roi, None,
        threshold, scales,
    )
    control_box = next(part.box for part in selection.before.parts if part.role == "control")
    outcome_box = next(part.box for part in selection.after.parts if part.role == "outcome")
    target, target_asset = _template(before_image, control_box, f"assets/{asset_prefix}control.png",
                                     before_roi, threshold, scales, point=_point(control_box, click))
    found = _source_match(before, before_asset, before_bytes)
    if (found.status != "matched" or len(found.matches) != 1
            or (math.floor(found.matches[0].x), math.floor(found.matches[0].y)) != click
            or not _contained(found.matches[0].bounds, before_box)):
        raise ValueError(f"selected object does not uniquely identify the recorded touch ({found.reason or found.status})")
    for reference in references:
        pixels, image = _read_frame(reference)
        if image.size != before_image.size:
            raise ValueError("before reference frames must use the recording dimensions")
        if _source_match(before, before_asset, pixels).status != "matched":
            raise ValueError("selected object cannot be verified in an additional before reference")
    linked = _source_match(before, before_asset, after_bytes, identity_only=True)
    links_by_location = (linked.status == "matched" and len(linked.matches) == 1
                         and _contained(linked.matches[0].bounds, after_box))
    if not links_by_location and not (
        linked.status == "not_found" and _link_recolored_identity(
            selection, before_image, before_box, after_image, after_box,
        )
    ):
        raise ValueError("before object identity cannot be verified in the selected after container")
    result = _source_match(after, after_asset, after_bytes)
    if (result.status != "matched" or len(result.matches) != 1
            or not _contained(result.matches[0].bounds, outcome_box)):
        raise ValueError("after region does not uniquely verify the recorded outcome")
    width, height = before_image.size
    left, top, right, bottom = before_box
    original_object = (left / width, top / height, right / width, bottom / height)
    effect_template, effect_asset = _template(after_image, outcome_box, after.layout.png_path,
                                              original_object, threshold, scales)
    # The outcome itself must be new within the previous object, even if identity
    # rendering changed and the whole after condition would fail on that basis.
    if (_match(effect_template, effect_asset, before_bytes).status != "not_found"
            or _source_match(after, after_asset, before_bytes).status != "not_found"):
        raise ValueError("the selected outcome must be verifiably absent before the tap")
    outcome = VisualObjectCondition(type="visual_object_present", target=after)
    before_checks.extend((VisualObjectCondition(type="visual_object_present", target=before),
                          VisualObjectCondition(type="visual_object_absent", target=after)))
    step = ScriptStep(id=step_id, before=tuple(before_checks), after=(app_guard, outcome),
                      action=VisualTapAction(type="tap_visual", target=target, object=before))
    assets = {target.png_path: target_asset, before.layout.png_path: before_asset, after.layout.png_path: after_asset}
    return step, assets, (outcome, after_asset)


def _write_bundle(destination: Path, script: GuiScript, contents: dict[str, bytes], *, document=None) -> None:
    assets = destination / "assets"
    output, temporary = destination / "script.json", destination / ".script.json.tmp"
    files = {destination / path: content for path, content in contents.items()}
    destination.mkdir(parents=True, exist_ok=False)
    try:
        assets.mkdir()
        for path, content in files.items():
            path.write_bytes(content)
        # Readers only discover a script after all of its assets have been written.
        body = script.model_dump_json(indent=2) if document is None else json.dumps(document, indent=2, ensure_ascii=False)
        temporary.write_text(body + "\n", encoding="utf-8")
        temporary.replace(output)
    except BaseException:
        for path in (temporary, *files):
            with suppress(OSError):
                path.unlink(missing_ok=True)
        for path in (assets, destination):
            with suppress(OSError):
                path.rmdir()
        raise


def _record_step(
    *, before_png: bytes | str | Path, after_png: bytes | str | Path, click: tuple[int, int],
    app_guard: AppCondition, step_id: str, asset_prefix: str, previous_outcome: _RecordingOutcome | None,
    after_crop: PixelBox | None = None, selection: VisualRecordingSelection | Mapping | None = None,
    reference_before: Iterable[bytes | str | Path] = (),
    before_crop: PixelBox | None = None, before_roi: Region | None = None,
    before_fallback_roi: Region | None = None,
    after_roi: Region | None = None, threshold: float = 0.92, scales: tuple[float, ...] | None = None,
) -> tuple[ScriptStep, dict[str, bytes], _RecordingOutcome]:
    before_bytes, before_image = _read_frame(before_png)
    after_bytes, after_image = _read_frame(after_png)
    before_checks: list[AppCondition | VisualCondition | VisualObjectCondition] = [app_guard]
    if previous_outcome is not None:
        condition, previous_asset = previous_outcome
        if isinstance(condition, VisualObjectCondition):
            previous_match = _source_match(condition.target, previous_asset, before_bytes)
        else:
            previous_match = _match(condition.target, previous_asset, before_bytes)
        if previous_match.status != "matched" or len(previous_match.matches) != 1:
            raise ValueError("before capture must uniquely match the prior step's after crop")
        before_checks.append(condition)
    if before_image.size != after_image.size:
        raise ValueError("the before and after captures must have the same screenshot dimensions")
    if not isinstance(click, (tuple, list)) or len(click) != 2 or any(type(value) is not int for value in click):
        raise ValueError("click requires two integer pixel coordinates")
    x, y = click
    if not (0 <= x < before_image.width and 0 <= y < before_image.height):
        raise ValueError("click must lie inside the before capture")
    click = (x, y)
    if selection is not None:
        if before_crop is not None or after_crop is not None:
            raise ValueError("object selections cannot be combined with manual crops")
        return _record_object_step(
            selection, before_bytes=before_bytes, before_image=before_image, after_bytes=after_bytes, after_image=after_image,
            click=click, before_checks=before_checks, before_roi=before_roi, before_fallback_roi=before_fallback_roi,
            after_roi=after_roi, threshold=threshold, scales=scales, references=reference_before, app_guard=app_guard,
            step_id=step_id, asset_prefix=asset_prefix,
        )
    if after_crop is None or reference_before:
        raise ValueError("recording requires an object selection or an explicit legacy after crop")
    before_box = _crop_box(before_crop if before_crop is not None else _default_crop(click, before_image.size),
                           before_image.size, "before crop")
    after_box = _crop_box(after_crop, after_image.size, "after crop")
    point = _point(before_box, click)
    before_path = f"assets/{asset_prefix}before.png"
    after_path = f"assets/{asset_prefix}after.png"
    before, before_asset = _template(before_image, before_box, before_path, before_roi, threshold, scales, point=point)
    after, after_asset = _template(after_image, after_box, after_path, after_roi, threshold, scales)
    before_match = _expect_unique(before, before_asset, before_bytes, before_box, "before")
    if (math.floor(before_match.x), math.floor(before_match.y)) != (x, y):
        raise ValueError("normalized click does not resolve to the recorded pixel")
    fallback = before_fallback_roi
    if fallback is None and before_roi is None and before.roi != _FULL_FRAME:
        fallback = _FULL_FRAME
    if fallback is not None:
        expanded = VisualTemplate(**{**before.model_dump(), "fallback_roi": fallback})
        # Scan the entire proposed area, including the original target position,
        # to check that the template identifies only the demonstrated control.
        wide = expanded.model_copy(update={"roi": expanded.fallback_roi, "fallback_roi": None})
        proof = _match(wide, before_asset, before_bytes)
        if proof.status == "matched" and proof.matches == (before_match,):
            before = expanded
        elif before_fallback_roi is not None:
            raise ValueError("before crop must match exactly one location in the fallback region")
    _expect_unique(after, after_asset, after_bytes, after_box, "after")
    if _match(after, after_asset, before_bytes).status != "not_found":
        raise ValueError("after crop must be verifiably absent from the before capture")
    before_checks.extend((VisualCondition(type="visual_present", target=before),
                          VisualCondition(type="visual_absent", target=after)))
    outcome = VisualCondition(type="visual_present", target=after)
    after_checks = (app_guard, outcome)
    step = ScriptStep(id=step_id, before=tuple(before_checks), after=after_checks,
                      action=VisualTapAction(type="tap_visual", target=before))
    return step, {before_path: before_asset, after_path: after_asset}, (outcome, after_asset)


def _write_recording(*, steps: list[ScriptStep], assets: dict[str, bytes], destination: Path | str,
                     script_id: str, app_id: str) -> GuiScript:
    script = GuiScript(script_id=script_id, app_id=app_id, steps=tuple(steps),
                       initial=steps[0].before, final=steps[-1].after)
    _write_bundle(Path(destination), script, assets)
    return script


def record_visual_script(
    *, before_png: bytes | str | Path, after_png: bytes | str | Path, click: tuple[int, int],
    destination: str | Path, script_id: str, app_id: str, after_crop: PixelBox | None = None,
    selection: VisualRecordingSelection | Mapping | None = None,
    reference_before: Iterable[bytes | str | Path] = (),
    before_crop: PixelBox | None = None, before_roi: Region | None = None,
    before_fallback_roi: Region | None = None,
    after_roi: Region | None = None, threshold: float = 0.92, scales: tuple[float, ...] | None = None,
) -> GuiScript:
    """Save one visual tap in a new directory with confined PNG assets.

    Capture rectangles use exclusive right and bottom pixel bounds. The after
    crop should describe a meaningful result visible only after the action.
    """
    step, assets, _ = _record_step(
        before_png=before_png, after_png=after_png, click=click, after_crop=after_crop, before_crop=before_crop,
        selection=selection, reference_before=reference_before,
        before_roi=before_roi, before_fallback_roi=before_fallback_roi, after_roi=after_roi,
        threshold=threshold, scales=scales,
        app_guard=AppCondition(type="app_is", app_id=app_id), step_id="tap-1", asset_prefix="", previous_outcome=None,
    )
    return _write_recording(steps=[step], assets=assets, destination=destination, script_id=script_id, app_id=app_id)


def record_visual_sequence(
    *, actions: Iterable[Mapping[str, object]], destination: str | Path, script_id: str, app_id: str,
) -> GuiScript:
    """Save an ordered sequence after validating every pair and transition.

    Each action takes the capture and geometry arguments of record_visual_script.
    The previous result stays an explicit precondition for the next action.
    """
    app_guard = AppCondition(type="app_is", app_id=app_id)
    assets: dict[str, bytes] = {}
    steps: list[ScriptStep] = []
    previous = None
    for index, action in enumerate(actions, start=1):
        if index > 128:
            raise ValueError("visual recording accepts at most 128 actions")
        if not isinstance(action, Mapping):
            raise ValueError(f"visual action {index} must be an object")
        try:
            step, files, previous = _record_step(app_guard=app_guard, step_id=f"tap-{index}",
                                                  asset_prefix=f"step-{index:03d}-", previous_outcome=previous,
                                                  **action)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"visual action {index}: {exc}") from exc
        steps.append(step)
        assets.update(files)
    if not steps:
        raise ValueError("visual recording requires at least one action")
    return _write_recording(steps=steps, assets=assets, destination=destination, script_id=script_id, app_id=app_id)
