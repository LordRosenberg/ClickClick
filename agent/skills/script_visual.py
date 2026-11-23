"""Locate a saved PNG template in a current frame using the local Pillow runtime."""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

from PIL import Image, ImageChops, ImageStat
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_MAX_FRAME_PIXELS = 12_000_000
_MAX_VERIFICATION_PIXELS = 120_000_000
_MAX_RANKED_CANDIDATES = 4096
_FULL_FRAME = (0.0, 0.0, 1.0, 1.0)
_DEFAULT_SCALES = (0.94, 1.0, 1.06)
_DEFAULT_THRESHOLD = 0.92

PixelBox = tuple[int, int, int, int]
Unit = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
Region = Annotated[tuple[Unit, Unit, Unit, Unit], Field(strict=False)]
MatchPoint = Annotated[tuple[Unit, Unit], Field(strict=False)]
Scale = Annotated[float, Field(ge=0.5, le=2.0, allow_inf_nan=False)]


class VisualTemplate(BaseModel):
    """A PNG crop and the geometry of the screenshot it was cut from."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    png_path: str = Field(min_length=1, max_length=512)
    reference_width: int = Field(gt=0)
    reference_height: int = Field(gt=0)
    roi: Region = _FULL_FRAME
    fallback_roi: Region | None = None
    threshold: float = Field(default=_DEFAULT_THRESHOLD, ge=0.5, le=1.0, allow_inf_nan=False)
    scales: tuple[Scale, ...] = Field(default=_DEFAULT_SCALES, min_length=1, max_length=9, strict=False)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)
    point: MatchPoint = (0.5, 0.5)
    min_score_gap: float = Field(default=0.04, ge=0.0, le=1.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_geometry(self):
        left, top, right, bottom = self.roi
        if left >= right or top >= bottom:
            raise ValueError("visual ROI requires x1 < x2 and y1 < y2 in normalized frame coordinates")
        if self.fallback_roi is not None:
            outer_left, outer_top, outer_right, outer_bottom = self.fallback_roi
            if (not (outer_left <= left < right <= outer_right and outer_top <= top < bottom <= outer_bottom)
                    or self.fallback_roi == self.roi):
                raise ValueError("visual fallback ROI must strictly contain the local ROI")
        windows_path = PureWindowsPath(self.png_path)
        if windows_path.drive or windows_path.root or ".." in windows_path.parts or windows_path.suffix.lower() != ".png":
            raise ValueError("visual template requires a relative PNG asset path")
        return self


@dataclass(frozen=True)
class VisualMatch:
    bounds: PixelBox
    confidence: float
    point: tuple[float, float] = (0.5, 0.5)

    @property
    def x(self) -> float:
        return self.bounds[0] + 0.5 + (self.bounds[2] - self.bounds[0] - 1) * self.point[0]

    @property
    def y(self) -> float:
        return self.bounds[1] + 0.5 + (self.bounds[3] - self.bounds[1] - 1) * self.point[1]


@dataclass(frozen=True)
class VisualMatchResult:
    status: Literal["matched", "not_found", "unavailable"]
    matches: tuple[VisualMatch, ...] = ()
    reason: str | None = None
    near_matches: tuple[VisualMatch, ...] = ()


def _decode(data: bytes, *, formats: tuple[str, ...]) -> Image.Image:
    if not isinstance(data, bytes) or not data or len(data) > _MAX_IMAGE_BYTES:
        raise ValueError("invalid_visual_image_size")
    with Image.open(io.BytesIO(data), formats=formats) as image:
        if image.width * image.height > _MAX_FRAME_PIXELS or getattr(image, "n_frames", 1) != 1:
            raise ValueError("invalid_visual_image_dimensions")
        if ("A" in image.getbands() or "transparency" in image.info) and image.convert("RGBA").getchannel("A").getextrema()[0] != 255:
            raise ValueError("visual_image_has_transparency")
        return image.convert("RGB")


def _contrast(template: Image.Image) -> Image.Image:
    background = tuple(ImageStat.Stat(template).median)
    return ImageChops.difference(template, Image.new("RGB", template.size, background)).convert("L")


def _anchors(contrast: Image.Image) -> list[tuple[int, int, bool]]:
    """Sample the strongest detail in each tile so plain backgrounds cannot dominate."""
    width, height = contrast.size
    columns, rows = min(4, width), min(4, height)
    points = []
    for row in range(rows):
        for column in range(columns):
            x, y = column * width // columns, row * height // rows
            tile = contrast.crop((x, y, (column + 1) * width // columns, (row + 1) * height // rows))
            peak = tile.getextrema()[1]
            if peak < 12:
                points.append((x + tile.width // 2, y + tile.height // 2, False))
            else:
                dy, dx = divmod(tile.tobytes().index(peak), tile.width)
                points.append((x + dx, y + dy, True))
    return points


def _candidate_mask(frame: Image.Image, template: Image.Image, contrast: Image.Image, threshold: float) -> Image.Image:
    width = frame.width - template.width + 1
    height = frame.height - template.height + 1
    foreground = Image.new("L", (width, height))
    background = Image.new("L", (width, height))
    coordinates = _anchors(contrast)
    tolerance = max(30, round((1.0 - threshold) * 510))
    tolerance_table = [int(value <= tolerance) for value in range(256)]
    for x, y, feature in coordinates:
        positions = frame.crop((x, y, x + width, y + height))
        solid = Image.new("RGB", positions.size, template.getpixel((x, y)))
        differences = ImageChops.difference(positions, solid).convert("L")
        vote = differences.point(tolerance_table)
        if feature:
            foreground = ImageChops.add(foreground, vote)
        else:
            background = ImageChops.add(background, vote)
    features = sum(feature for _, _, feature in coordinates)
    detail_needed = math.ceil(features * 0.75)
    background_needed = math.ceil((len(coordinates) - features) * 0.5)
    return ImageChops.darker(
        foreground.point([int(value >= detail_needed) for value in range(256)]),
        background.point([int(value >= background_needed) for value in range(256)]),
    )


def _score(patch: Image.Image, template: Image.Image, contrast: Image.Image, contrast_total: float) -> float:
    differences = ImageChops.difference(patch, template).convert("L")
    coarse_difference = ImageStat.Stat(differences).mean[0] / 255
    weighted_difference = ImageStat.Stat(ImageChops.multiply(differences, contrast)).sum[0] / contrast_total
    return 1.0 - max(coarse_difference, weighted_difference)


def _overlap(first: PixelBox, second: PixelBox) -> float:
    horizontal = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    vertical = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    smallest = min((box[2] - box[0]) * (box[3] - box[1]) for box in (first, second))
    return horizontal * vertical / smallest


def _rank(candidates: list[VisualMatch]) -> tuple[VisualMatch, ...]:
    if not candidates:
        return ()
    cell_width = max(match.bounds[2] - match.bounds[0] for match in candidates)
    cell_height = max(match.bounds[3] - match.bounds[1] for match in candidates)
    cells: dict[tuple[int, int], list[VisualMatch]] = {}
    distinct = []
    for candidate in sorted(candidates, key=lambda match: (-match.confidence, match.bounds)):
        column, row = candidate.bounds[0] // cell_width, candidate.bounds[1] // cell_height
        neighbors = (
            previous
            for dy in (-1, 0, 1) for dx in (-1, 0, 1)
            for previous in cells.get((column + dx, row + dy), ())
        )
        if all(_overlap(candidate.bounds, previous.bounds) < 0.60 for previous in neighbors):
            distinct.append(candidate)
            cells.setdefault((column, row), []).append(candidate)
    return tuple(distinct)


def _match(config: VisualTemplate, template_png: bytes, screenshot: bytes) -> VisualMatchResult:
    if config.sha256 is not None and (
        not isinstance(template_png, bytes) or hashlib.sha256(template_png).hexdigest() != config.sha256
    ):
        return VisualMatchResult("unavailable", reason="visual_template_hash_mismatch")
    try:
        crop = _decode(template_png, formats=("PNG",))
        frame = _decode(screenshot, formats=("PNG", "JPEG"))
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        return VisualMatchResult("unavailable", reason="visual_image_decode_failed")
    if crop.width > config.reference_width or crop.height > config.reference_height:
        return VisualMatchResult("unavailable", reason="template_exceeds_reference_frame")
    if min(crop.size) < 2 or _contrast(crop).getextrema()[1] < 12:
        return VisualMatchResult("unavailable", reason="template_has_insufficient_visual_detail")

    result = _search(config, crop, frame, config.roi, candidate_threshold=config.threshold)
    if result.status == "not_found" and config.fallback_roi is not None:
        return _search(config, crop, frame, config.fallback_roi,
                       candidate_threshold=max(0.0, config.threshold - config.min_score_gap))
    return result


def _search(config: VisualTemplate, crop: Image.Image, frame: Image.Image, region: Region, *,
            candidate_threshold: float) -> VisualMatchResult:
    left, top, right, bottom = region
    roi = (math.ceil(left * frame.width - 1e-9), math.ceil(top * frame.height - 1e-9),
           math.floor(right * frame.width + 1e-9), math.floor(bottom * frame.height + 1e-9))
    if roi[0] >= roi[2] or roi[1] >= roi[3]:
        return VisualMatchResult("unavailable", reason="visual_roi_has_no_pixels")
    search = frame.crop(roi)
    display_scale = min(frame.width / config.reference_width, frame.height / config.reference_height)
    sizes = {(round(crop.width * display_scale * scale), round(crop.height * display_scale * scale))
             for scale in config.scales}
    work = 0
    candidates = []
    for width, height in sorted(sizes):
        if min(width, height) < 2 or width > search.width or height > search.height:
            continue
        template = crop.resize((width, height), Image.Resampling.LANCZOS)
        contrast = _contrast(template)
        contrast_total = ImageStat.Stat(contrast).sum[0]
        if contrast.getextrema()[1] < 12:
            continue
        proposed = _candidate_mask(search, template, contrast, candidate_threshold)
        work += proposed.histogram()[1] * width * height
        if work > _MAX_VERIFICATION_PIXELS:
            return VisualMatchResult("unavailable", reason="visual_search_too_ambiguous")
        region = proposed.getbbox()
        if region is None:
            continue
        proposed = proposed.crop(region)
        for offset, flagged in enumerate(proposed.tobytes()):
            if not flagged:
                continue
            y, x = divmod(offset, proposed.width)
            x += region[0]
            y += region[1]
            confidence = _score(search.crop((x, y, x + width, y + height)), template, contrast, contrast_total)
            if confidence >= candidate_threshold:
                bounds = (x + roi[0], y + roi[1], x + width + roi[0], y + height + roi[1])
                candidates.append(VisualMatch(bounds, confidence, point=config.point))
                if len(candidates) > _MAX_RANKED_CANDIDATES:
                    return VisualMatchResult("unavailable", reason="visual_search_too_ambiguous")
    ranked = _rank(candidates)
    matches = tuple(match for match in ranked if match.confidence >= config.threshold)
    if not matches:
        return VisualMatchResult("not_found")
    return VisualMatchResult("matched", matches, near_matches=ranked[len(matches):])


def match_visual_pixels(template_png: bytes, screenshot: bytes, *, reference_size: tuple[int, int],
                        roi: Region = _FULL_FRAME, fallback_roi: Region | None = None,
                        threshold: float = _DEFAULT_THRESHOLD,
                        scales: tuple[float, ...] = _DEFAULT_SCALES, sha256: str | None = None,
                        point: MatchPoint = (0.5, 0.5)) -> VisualMatchResult:
    """Match supplied image bytes. Confidence measures contrast-weighted patch similarity."""
    try:
        width, height = reference_size
        config = VisualTemplate(png_path="template.png", reference_width=width, reference_height=height,
                                roi=roi, fallback_roi=fallback_roi, threshold=threshold,
                                scales=scales, sha256=sha256, point=point)
    except (TypeError, ValueError):
        return VisualMatchResult("unavailable", reason="visual_geometry_invalid")
    return _match(config, template_png, screenshot)


def read_visual_asset(template: VisualTemplate, asset_root: Path) -> bytes:
    """Read an image inside the bundle and verify any declared digest."""
    try:
        root = asset_root.resolve()
        path = (root / template.png_path).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file() or path.stat().st_size > _MAX_IMAGE_BYTES:
            raise ValueError("visual_template_asset_invalid")
        with path.open("rb") as file:
            data = file.read(_MAX_IMAGE_BYTES + 1)
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise ValueError("visual_template_asset_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("visual_template_asset_invalid") from exc
    if template.sha256 is not None and hashlib.sha256(data).hexdigest() != template.sha256:
        raise ValueError("visual_template_hash_mismatch")
    return data


def match_visual_template(template: VisualTemplate, screenshot: bytes, *, asset_root: Path) -> VisualMatchResult:
    """Match a confined template against the caller's current screenshot."""
    try:
        data = read_visual_asset(template, asset_root)
    except ValueError as exc:
        return VisualMatchResult("unavailable", reason=str(exc))
    return _match(template, data, screenshot)
