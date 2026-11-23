"""Object identity and control location from a fresh screenshot."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.skills.script_visual import (
    PixelBox, Region, VisualMatch, VisualMatchResult, VisualTemplate,
    _MAX_RANKED_CANDIDATES, _MAX_VERIFICATION_PIXELS, _candidate_mask, _contrast,
    _decode, _overlap, _rank, _score, read_visual_asset,
)


class VisualPart(BaseModel):
    """Required pixels, in the recorded group's coordinate space."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    role: Literal["identity", "control", "boundary", "outcome"]
    box: Annotated[PixelBox, Field(strict=False)]
    text: str | None = Field(default=None, min_length=1, max_length=256)
    text_parameter: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def check_box(self):
        left, top, right, bottom = self.box
        if min(left, top) < 0 or right - left < 2 or bottom - top < 2:
            raise ValueError("visual parts require a rectangle of positive size")
        if (self.text is not None and self.text_parameter is not None) or (
            self.role != "identity" and (self.text is not None or self.text_parameter is not None)
        ):
            raise ValueError("only identity parts can specify an exact text or text parameter")
        if self.text is not None and not self.text.strip():
            raise ValueError("visual identity text cannot be blank")
        return self


class VisualObject(BaseModel):
    """A screenshot group and the stable regions used to check its identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    layout: VisualTemplate
    parts: Annotated[tuple[VisualPart, ...], Field(min_length=2, max_length=16, strict=False)]
    container: Literal["row", "card", "panel"]

    @model_validator(mode="after")
    def check_parts(self):
        roles = [part.role for part in self.parts]
        if "identity" not in roles or not ({"control", "outcome"} & set(roles)):
            raise ValueError("a visual object requires identity and a control or outcome")
        if roles.count("control") > 1:
            raise ValueError("a visual object has at most one control part")
        for index, part in enumerate(self.parts):
            if part.box[2] > self.layout.reference_width or part.box[3] > self.layout.reference_height:
                raise ValueError("visual part exceeds the reference screenshot")
            if any(_overlap(part.box, other.box) for other in self.parts[:index]):
                raise ValueError("visual parts must be disjoint")
        return self


@dataclass(frozen=True)
class _Located:
    box: PixelBox
    anchors: tuple[PixelBox, ...]
    confidence: float
    action: VisualMatch | None
    identity_ambiguous: bool = False
    action_ambiguous: bool = False


@dataclass(frozen=True)
class _Patch:
    part: VisualPart
    pixels: Image.Image


class _Unresolved(ValueError):
    pass


OCR = Callable[[Image.Image], Iterable[tuple[str, PixelBox]]]


def _size(box: PixelBox) -> tuple[int, int]:
    return box[2] - box[0], box[3] - box[1]


def _contains(outer: PixelBox, inner: PixelBox) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _clip_to_frame(region: Region, size: tuple[int, int]) -> PixelBox:
    left, top, right, bottom = region
    w, h = size
    return (math.ceil(left * w - 1e-9), math.ceil(top * h - 1e-9),
            math.floor(right * w + 1e-9), math.floor(bottom * h + 1e-9))


def _offset(box: PixelBox, x: int, y: int) -> PixelBox:
    return box[0] + x, box[1] + y, box[2] + x, box[3] + y


def _control_point(layout: VisualTemplate, size: tuple[int, int], box: PixelBox) -> tuple[float, float]:
    x, y = ((length - 1) * fraction - start
            for length, fraction, start in zip(size, layout.point, box[:2], strict=True))
    width, height = _size(box)
    if not (-1e-8 <= x <= width - 1 + 1e-8 and -1e-8 <= y <= height - 1 + 1e-8):
        raise _Unresolved("visual_object_touch_outside_control")
    return min(1.0, max(0.0, x / (width - 1))), min(1.0, max(0.0, y / (height - 1)))


def _prepare(object: VisualObject, source: Image.Image) -> tuple[tuple[_Patch, ...], tuple[float, float]]:
    source_box = (0, 0, *source.size)
    patches = []
    point = (0.5, 0.5)
    for part in object.parts:
        if not _contains(source_box, part.box):
            raise _Unresolved("visual_part_exceeds_layout")
        pixels = source.crop(part.box)
        if _contrast(pixels).getextrema()[1] < 12:
            raise _Unresolved("visual_part_has_insufficient_detail")
        if part.role == "control":
            point = _control_point(object.layout, source.size, part.box)
        patches.append(_Patch(part, pixels))
    return tuple(patches), point


def _available_scales(layout: VisualTemplate, source: Image.Image, frame: Image.Image, scales=None):
    ratio = min(frame.width / layout.reference_width, frame.height / layout.reference_height)
    options = layout.scales if scales is None else scales
    for width, height in sorted({(round(source.width * ratio * s), round(source.height * ratio * s))
                                 for s in options}):
        if min(width, height) >= 2:
            yield width, height


def _resize_part(patch: _Patch, original_size, new_size):
    sx = new_size[0] / original_size[0]
    sy = new_size[1] / original_size[1]
    lx, ty, rx, by = patch.part.box
    box = round(lx * sx), round(ty * sy), round(rx * sx), round(by * sy)
    if min(_size(box)) < 2:
        return None
    pixels = patch.pixels.resize(_size(box), Image.Resampling.LANCZOS)
    contrast = _contrast(pixels)
    total = ImageStat.Stat(contrast).sum[0]
    return (patch.part.role, box, pixels, contrast, total) if total else None


def _part_score(role, current, expected, contrast, total, floor):
    score = _score(current, expected, contrast, total)
    if role != "identity" or score < floor:
        return score
    # A changed digit in a long identifier can be averaged out by the rest
    # of an otherwise matching label. Score its local visual details as well.
    width, height = expected.size
    side = max(8, min(width, height) // 2, math.ceil(max(width, height) / 20))
    salient = ImageChops.lighter(contrast, _contrast(current))
    for y in range(0, height, side):
        for x in range(0, width, side):
            area = x, y, min(width, x + side), min(height, y + side)
            weight = salient.crop(area)
            weight_sum = ImageStat.Stat(weight).sum[0]
            if weight_sum >= 48:
                score = min(score, _score(current.crop(area), expected.crop(area), weight, weight_sum))
                if score < floor:
                    return score
    return score


def _origins(mask: Image.Image, local: PixelBox | None = None):
    region = mask.getbbox()
    if region is None:
        return
    data = mask.crop(region)
    values = data.tobytes()
    for is_local in ((True, False) if local is not None else (False,)):
        for offset, flag in enumerate(values):
            if flag:
                y, x = divmod(offset, data.width)
                x, y = x + region[0], y + region[1]
                if local is None or (local[0] <= x < local[2] and local[1] <= y < local[3]) == is_local:
                    yield x, y


def _coherent(object, source, frame, scope, local, patches, point, floor, *, identity_only=False):
    """Find poses from an identity patch, then verify every patch at that pose."""
    matches = []
    work = 0
    threshold = object.layout.threshold
    stable = tuple(patch for patch in patches if patch.part.role in {"identity", "boundary"})
    for width, height in _available_scales(object.layout, source, frame):
        if width > scope[2] - scope[0] or height > scope[3] - scope[1]:
            continue
        scaled = [(index, _resize_part(patch, source.size, (width, height)))
                  for index, patch in enumerate(stable)]
        if any(details is None for _, details in scaled):
            continue
        seed_index, seed = max((item for item in scaled if item[1][0] == "identity"),
                               key=lambda item: item[1][4])
        _, seed_box, seed_pixels, seed_contrast, _ = seed
        x0, y0, x1, y1 = scope
        search = frame.crop((x0 + seed_box[0], y0 + seed_box[1],
                             x1 - width + seed_box[2], y1 - height + seed_box[3]))
        origins = _candidate_mask(search, seed_pixels, seed_contrast, floor)
        local_origins = (local[0] - x0, local[1] - y0, local[2] - x0 - width + 1,
                         local[3] - y0 - height + 1)
        exact = [(seed_index, seed), *(item for item in scaled if item[0] != seed_index)]
        effects = ([_resize_part(patch, source.size, (width, height))
                    for patch in patches if patch.part.role in {"control", "outcome"}]
                   if not identity_only else ())
        for ox, oy in _origins(origins, local_origins):
            x, y = ox + x0, oy + y0
            evidence = {}
            score = 1.0
            for index, (role, part, pixels, contrast, total) in exact:
                box = _offset(part, x, y)
                work += pixels.width * pixels.height
                if work > _MAX_VERIFICATION_PIXELS:
                    raise _Unresolved("visual_object_search_too_ambiguous")
                cutoff = floor if role == "identity" else threshold
                confidence = _part_score(role, frame.crop(box), pixels, contrast, total, cutoff)
                if confidence < cutoff:
                    break
                if role == "identity":
                    evidence[index] = box
                    score = min(score, confidence)
            else:
                action = None
                outcome_ok = True
                for effect in effects:
                    if effect is None:
                        outcome_ok = False
                        break
                    role, part, pixels, contrast, total = effect
                    box = _offset(part, x, y)
                    work += pixels.width * pixels.height
                    if work > _MAX_VERIFICATION_PIXELS:
                        raise _Unresolved("visual_object_search_too_ambiguous")
                    confidence = _score(frame.crop(box), pixels, contrast, total)
                    if confidence < threshold:
                        outcome_ok = False
                        break
                    if action is None or role == "control":
                        action = VisualMatch(box, confidence, point if role == "control" else (0.5, 0.5))
                matches.append(_Located((x, y, x + width, y + height),
                                        tuple(evidence[index] for index in sorted(evidence)), score,
                                        action if outcome_ok else None))
                if len(matches) > _MAX_RANKED_CANDIDATES:
                    raise _Unresolved("visual_object_search_too_ambiguous")
    return matches


def _flat_containers(frame: Image.Image, min_width: int, min_height: int) -> list[PixelBox]:
    """Find closed background blocks independently of any template or its matches."""
    scale = min(1.0, 384 / max(frame.size))
    reduced = frame.resize(tuple(max(1, round(dimension * scale)) for dimension in frame.size),
                           Image.Resampling.NEAREST)
    width, height = reduced.size
    rgb = reduced.tobytes()
    pixels = [((r >> 2) << 12) + ((g >> 2) << 6) + (b >> 2)
              for r, g, b in zip(rgb[::3], rgb[1::3], rgb[2::3], strict=True)]
    seen = bytearray(width * height)
    boxes = []
    for start in range(len(seen)):
        if seen[start]:
            continue
        seen[start] = 1
        color = pixels[start]
        y0, x0 = divmod(start, width)
        x1, y1 = x0, y0
        pending = [start]
        count = 0
        while pending:
            offset = pending.pop()
            count += 1
            y, x = divmod(offset, width)
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x), max(y1, y)
            for neighbor in (offset - 1 if x else -1, offset + 1 if x + 1 < width else -1,
                             offset - width if y else -1, offset + width if y + 1 < height else -1):
                if neighbor >= 0 and not seen[neighbor] and pixels[neighbor] == color:
                    seen[neighbor] = 1
                    pending.append(neighbor)
        area = (x1 + 1 - x0) * (y1 + 1 - y0)
        if (x0 == 0 or y0 == 0 or x1 + 1 == width or y1 + 1 == height or count * 2 < area
                or x1 + 1 - x0 < min_width * scale or y1 + 1 - y0 < min_height * scale):
            continue
        boxes.append((math.ceil(x0 * frame.width / width), math.ceil(y0 * frame.height / height),
                      math.floor((x1 + 1) * frame.width / width), math.floor((y1 + 1) * frame.height / height)))
    return boxes


def _row_containers(frame: Image.Image, scope: PixelBox, min_height: int) -> list[PixelBox]:
    """Accept rows only between repeated visible separators."""
    x0, y0, x1, y1 = scope
    if y1 - y0 <= 2 * min_height or x1 - x0 < 24:
        return []
    width = x1 - x0
    offsets = tuple(x0 + round(width * index / 12) for index in range(1, 12))
    image = frame.convert("L")
    lines_by_span = {}
    for y in range(y0 + 2, y1 - 2):
        center = [image.getpixel((x, y)) for x in offsets]
        active = []
        for x, value in zip(offsets, center, strict=True):
            above, below = image.getpixel((x, y - 2)), image.getpixel((x, y + 2))
            active.append(value // 8 if abs(above - below) <= 8
                          and min(abs(value - above), abs(value - below)) >= 11 else None)
        # A divider may be indented under the text column on a list row.
        for start in range(len(active) - 6):
            color = active[start]
            if color is None or (start and active[start - 1] == color):
                continue
            end = start + 1
            while end < len(active) and active[end] == color:
                end += 1
            if end - start >= 7:
                lines_by_span.setdefault((start, end, color), []).append(y)
    boxes = []
    for lines in lines_by_span.values():
        separated = [y for index, y in enumerate(lines) if index == 0 or y - lines[index - 1] > 3]
        if len(separated) >= 2:
            boxes.extend((x0, top + 1, x1, bottom) for top, bottom in zip(separated, separated[1:])
                         if bottom - top >= min_height)
        # Some lists draw only trailing dividers. An equally spaced repeat
        # can establish the first row when its inferred top is fully visible
        # and is a clear gutter across the current list.
        if len(separated) >= 3:
            first, second, third = separated[:3]
            pitch = second - first
            preceding = first - pitch
            if (pitch >= min_height and abs(third - second - pitch) <= max(2, pitch // 20)
                    and preceding - 2 > y0):
                colors = [image.getpixel((x, y)) for x in offsets for y in range(preceding - 2, preceding + 3)]
                if max(colors) - min(colors) <= 8:
                    boxes.append((x0, preceding, x1, first))
    return boxes


def _containers(frame, scope, patches, object_type, *, display_scale=1.0):
    required = tuple(patch for patch in patches if patch.part.role != "boundary")
    min_width = max(24, round(max(patch.pixels.width for patch in required) * 0.75 * display_scale))
    min_height = max(12, round(max(patch.pixels.height for patch in required) * 0.75 * display_scale))
    minimum_area = sum(patch.pixels.width * patch.pixels.height for patch in required) * 0.55 * display_scale**2
    boxes = set(_flat_containers(frame, min_width, min_height))
    if object_type != "panel":
        boxes.update(_row_containers(frame, scope, min_height))
    boxes = {box for box in boxes if _contains(scope, box) and math.prod(_size(box)) >= minimum_area}
    # Matching is limited to leaf groups, so an enclosing panel cannot combine
    # visible child cards into a single record.
    return tuple(sorted(box for box in boxes if not any(
        box != other and _contains(box, other) for other in boxes)))


def _same_placement(a: PixelBox, b: PixelBox) -> bool:
    if _overlap(a, b) < 0.80:
        return False
    return all(abs(a[i] + a[i + 2] - b[i] - b[i + 2])
               <= max(3, min(a[i + 2] - a[i], b[i + 2] - b[i]) * 0.40) for i in (0, 1))


def _same_identity(a: _Located, b: _Located) -> bool:
    return all(_same_placement(first, second) for first, second in zip(a.anchors, b.anchors, strict=True))


def _bound_to_one_group(item: _Located, containers: tuple[PixelBox, ...]) -> bool:
    # Partial evidence in a neighboring visible group vetoes a coherent pose.
    # Part placements with no overlapping container can still use the
    # recorded coherent geometry.
    evidence = (*item.anchors, *((item.action.bounds,) if item.action is not None else ()))
    occupied = [
        group for group in containers if any(_overlap(group, part) for part in evidence)
        and (item.action is None or not (_contains(item.action.bounds, group)
                                        or _same_placement(item.action.bounds, group)))
    ]
    return not occupied or (len(occupied) == 1 and all(_contains(occupied[0], part) for part in evidence))


def _norm_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _ocr_words(ocr: OCR, frame: Image.Image) -> tuple[tuple[str, PixelBox], ...]:
    try:
        words = tuple(ocr(frame))
        if any(not isinstance(value, str) or not _contains((0, 0, *frame.size), box)
               for value, box in words):
            raise ValueError("invalid word region")
        return words
    except Exception as exc:
        raise _Unresolved("visual_identity_ocr_unavailable") from exc


def _find_in_group(layout: VisualTemplate, patch: _Patch, frame: Image.Image,
                   box: PixelBox, floor: float) -> tuple[VisualMatch, ...]:
    """Use tight features for proposals and score each complete source part."""
    x0, y0, x1, y1 = box
    proposed = []
    work = 0
    standard = tuple(_available_scales(layout, patch.pixels, frame))
    # Reflow may use differently sized labels and controls. These part-level
    # searches stay confined to an independently established container.
    extra = tuple(size for size in _available_scales(layout, patch.pixels, frame, (0.8, 0.9, 1.125, 1.15, 1.2))
                  if size not in standard)
    for width, height in (*standard, *extra):
        if (width, height) in extra and any(match.confidence >= layout.threshold for match in proposed):
            break
        if width > x1 - x0 or height > y1 - y0:
            continue
        pixels = patch.pixels.resize((width, height), Image.Resampling.LANCZOS)
        contrast = _contrast(pixels)
        total = ImageStat.Stat(contrast).sum[0]
        peak = contrast.getextrema()[1]
        cutoff = max(12, peak // 2)
        detail = contrast.point([0] * cutoff + [255] * (256 - cutoff)).getbbox()
        if detail is None:
            continue
        seed = pixels.crop(detail)
        seed_contrast = contrast.crop(detail)
        if min(seed.size) < 2:
            continue
        haystack = frame.crop((x0 + detail[0], y0 + detail[1], x1 - width + detail[2],
                               y1 - height + detail[3]))
        # Candidate generation focuses on the high-contrast ink and lets the
        # complete part decide the final score. Dark-mode panel fill otherwise
        # dominates the preliminary candidates for sparse labels and switches.
        mask = _candidate_mask(haystack, seed, seed_contrast, max(floor, 0.935))
        for ox, oy in _origins(mask):
            work += width * height
            if work > _MAX_VERIFICATION_PIXELS:
                raise _Unresolved("visual_object_search_too_ambiguous")
            x, y = x0 + ox, y0 + oy
            bounds = x, y, x + width, y + height
            similarity = _part_score(patch.part.role, frame.crop(bounds), pixels, contrast, total, floor)
            if similarity >= floor:
                proposed.append(VisualMatch(bounds, similarity))
                if len(proposed) > _MAX_RANKED_CANDIDATES:
                    raise _Unresolved("visual_object_search_too_ambiguous")
    return _rank(proposed)


def _reflow(object, frame, containers, patches, point, floor, ocr, parameters, *, identity_only=False):
    matches = []
    identities = tuple(patch for patch in patches if patch.part.role == "identity")
    effects = (tuple(patch for patch in patches if patch.part.role in {"control", "outcome"})
               if not identity_only else ())
    words = None
    for box in containers:
        anchors = []
        confidence = 1.0
        ambiguous = False
        for patch in identities:
            expected = (parameters.get(patch.part.text_parameter) if patch.part.text_parameter is not None
                        else patch.part.text)
            if expected is None:
                found = _find_in_group(object.layout, patch, frame, box, floor)
            else:
                if words is None:
                    words = _ocr_words(ocr, frame)
                found = tuple(VisualMatch(bounds, 1.0) for value, bounds in words
                              if _contains(box, bounds) and _norm_text(value) == _norm_text(expected))
            if not found:
                break
            first = found[0]
            ambiguous |= len(found) > 1 and (found[1].confidence >= object.layout.threshold
                                             or first.confidence - found[1].confidence < object.layout.min_score_gap)
            anchors.append(first.bounds)
            confidence = min(confidence, first.confidence)
        else:
            if any(_overlap(anchor, other) for index, anchor in enumerate(anchors) for other in anchors[:index]):
                continue
            action = None
            action_ambiguous = False
            for patch in effects:
                found = _find_in_group(object.layout, patch, frame, box, floor)
                if not found or found[0].confidence < object.layout.threshold:
                    action = None
                    break
                hit = found[0]
                action_ambiguous |= len(found) > 1 and (
                    found[1].confidence >= object.layout.threshold
                    or hit.confidence - found[1].confidence < object.layout.min_score_gap
                )
                if any(_overlap(hit.bounds, anchor) for anchor in anchors):
                    action = None
                    break
                if action is None or patch.part.role == "control":
                    action = VisualMatch(hit.bounds, hit.confidence, point if patch.part.role == "control" else (0.5, 0.5))
            matches.append(_Located(box, tuple(anchors), confidence, action, ambiguous, action_ambiguous))
    return matches


def _select(matches: list[_Located], layout: VisualTemplate, identity_only: bool) -> VisualMatchResult:
    ordered = sorted(matches, key=lambda item: (-item.confidence, item.action is None, item.box))
    if not ordered or ordered[0].confidence < layout.threshold:
        return VisualMatchResult("not_found")
    best = ordered[0]
    alternatives = [other for other in ordered if not _same_identity(best, other)]
    if any(item.identity_ambiguous and _same_identity(best, item) for item in ordered) or any(
            other.confidence >= layout.threshold
            or best.confidence - other.confidence < layout.min_score_gap for other in alternatives):
        return VisualMatchResult("unavailable", reason="visual_object_ambiguous")
    if identity_only:
        return VisualMatchResult("matched", (VisualMatch(best.box, best.confidence),))
    if any(item.action_ambiguous and _same_identity(best, item) for item in ordered):
        return VisualMatchResult("unavailable", reason="visual_object_ambiguous")
    actions = [item.action for item in ordered if item.confidence >= layout.threshold and not item.identity_ambiguous
               and _same_identity(best, item) and item.action is not None]
    if not actions:
        return VisualMatchResult("not_found")
    if any(not _same_placement(actions[0].bounds, other.bounds) for other in actions[1:]):
        return VisualMatchResult("unavailable", reason="visual_object_ambiguous")
    selected = actions[0]
    return VisualMatchResult("matched", (VisualMatch(selected.bounds, min(selected.confidence, best.confidence),
                                                    selected.point),))


def match_visual_object_pixels(object: VisualObject, layout_png: bytes, screenshot: bytes, *,
                               identity_only: bool = False, ocr: OCR | None = None,
                               parameters: Mapping[str, str] | None = None) -> VisualMatchResult:
    """Find a unique object in the authorized screen region and verify its control."""
    try:
        if object.layout.sha256 is not None and (not isinstance(layout_png, bytes)
                or hashlib.sha256(layout_png).hexdigest() != object.layout.sha256):
            return VisualMatchResult("unavailable", reason="visual_template_hash_mismatch")
        source = _decode(layout_png, formats=("PNG",))
        frame = _decode(screenshot, formats=("PNG", "JPEG"))
        if source.width > object.layout.reference_width or source.height > object.layout.reference_height:
            return VisualMatchResult("unavailable", reason="template_exceeds_reference_frame")
        patches, point = _prepare(object, source)
        local = _clip_to_frame(object.layout.roi, frame.size)
        scope = _clip_to_frame(object.layout.fallback_roi or object.layout.roi, frame.size)
        if min(_size(scope)) < 2:
            return VisualMatchResult("unavailable", reason="visual_roi_has_no_pixels")
        floor = max(0.0, object.layout.threshold - object.layout.min_score_gap)
        display_scale = min(frame.width / object.layout.reference_width,
                            frame.height / object.layout.reference_height)
        containers = _containers(frame, scope, patches, object.container, display_scale=display_scale)
        if any(part.text is not None or part.text_parameter is not None for part in object.parts):
            if ocr is None:
                return VisualMatchResult("unavailable", reason="visual_identity_ocr_unavailable")
            if any(not (parameters or {}).get(part.text_parameter) for part in object.parts
                   if part.text_parameter is not None):
                return VisualMatchResult("unavailable", reason="visual_identity_parameter_missing")
            coherent = ()
        else:
            coherent = _coherent(object, source, frame, scope, local, patches, point, floor,
                                 identity_only=identity_only)
        matches = [item for item in coherent if _bound_to_one_group(item, containers)]
        matches.extend(_reflow(object, frame, containers, patches, point, floor, ocr, parameters or {},
                               identity_only=identity_only))
        return _select(matches, object.layout, identity_only)
    except _Unresolved as exc:
        return VisualMatchResult("unavailable", reason=str(exc))
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        return VisualMatchResult("unavailable", reason="visual_image_decode_failed")


def match_visual_object(object: VisualObject, screenshot: bytes, *, asset_root: Path,
                        identity_only: bool = False, ocr: OCR | None = None,
                        parameters: Mapping[str, str] | None = None) -> VisualMatchResult:
    """Read a pinned script asset and match its object on this screenshot."""
    try:
        asset = read_visual_asset(object.layout, asset_root)
    except ValueError as exc:
        return VisualMatchResult("unavailable", reason=str(exc))
    return match_visual_object_pixels(object, asset, screenshot, identity_only=identity_only, ocr=ocr,
                                      parameters=parameters)
