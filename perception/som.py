"""Accessibility-only Set-of-Mark renderer for numbered action targets.

Typography is adaptive and local-size-aware: labels scale with both screenshot
height and the target bounds, shrink for multi-digit indices, and remain
visually owned by the target they index.
"""

from __future__ import annotations

import io
from functools import lru_cache
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from shared.schemas import UIElement


# --- font loading ---------------------------------------------------------


_FONT_CANDIDATES = (
    # Bundled / common Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


@lru_cache(maxsize=32)
def _load_font(size: int) -> ImageFont.ImageFont | None:
    """Load a TrueType font at `size`, trying candidates then load_default."""
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    # Last resort: PIL's built-in (still better than load_default for size).
    try:
        return ImageFont.load_default(size=size)  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        try:
            return ImageFont.load_default()
        except Exception:  # noqa: BLE001
            return None


# --- rendering ------------------------------------------------------------

# Accessibility marks use one solid cyan style.
A11Y_COLOR: tuple[int, int, int] = (0, 200, 255)
# Typography/placement tunables. Local bounds cap the font size so labels stay
# compact on small targets even when the screenshot is very tall.
MIN_FONT_SIZE = 12
MAX_FONT_SIZE = 34
BASE_FONT_DIVISOR = 58
LABEL_MARGIN = 2


def _color_for(element: UIElement) -> tuple[int, int, int]:
    return A11Y_COLOR


def _outline_width(width: int, height: int) -> int:
    """Return the target-outline width for one rendered frame."""
    return max(2, max(width, height) // 540)


def render_som(
    screenshot: bytes,
    elements: list[UIElement],
    *,
    src_size: tuple[int, int] | None = None,
    dst_size: tuple[int, int] | None = None,
    output_format: str = "PNG",
    quality: int = 95,
) -> bytes:
    """Draw SoM onto a screenshot and return encoded image bytes.

    Every current runtime element is accessibility-backed and receives a
    solid cyan box plus its per-frame index.

    `src_size` / `dst_size` (both `(w, h)`) opt in to a coordinate rescale
    of every element's bounds before drawing. When both are supplied, each
    bound is multiplied by `dst_axis / src_axis` independently, then clamped
    to the destination frame. This is what the Executor uses to re-render
    the SoM on the compressed JPEG that is actually sent to the model so
    index labels are sized for the model's effective viewing area (otherwise
    small-icon labels become unreadable on tall screenshots). When either is
    `None`, no rescale happens and `_effective_font_size` uses the opened
    image's `size` — preserving the existing in-`build` call site that
    always drew onto the original frame.
    """
    img = Image.open(io.BytesIO(screenshot)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    font_h = dst_size[1] if dst_size is not None else h
    font_w = dst_size[0] if dst_size is not None else w

    # A 1080-long-edge model image uses a compact 2 px outline. Larger source
    # replay frames may scale up, while role-facing 1080 images stay precise.
    solid_width = _outline_width(w, h)

    # Rescale element bounds into the destination frame for SoM drawing when
    # the caller hands us src_size + dst_size. Element bounds stay in the
    # original-frame coordinate system on `package.ui.elements`; this
    # rescale is purely a rendering convenience and never touches the
    # canonical element records.
    rescaled: list[tuple[UIElement, list[int]]] = []
    for e in elements:
        if len(e.bounds) != 4:
            rescaled.append((e, list(e.bounds)))
            continue
        if src_size is None or dst_size is None:
            rescaled.append((e, list(e.bounds)))
            continue
        x1, y1, x2, y2 = e.bounds
        nx1 = max(0, min(font_w, int(round(x1 * dst_size[0] / src_size[0]))))
        ny1 = max(0, min(font_h, int(round(y1 * dst_size[1] / src_size[1]))))
        nx2 = max(0, min(font_w, int(round(x2 * dst_size[0] / src_size[0]))))
        ny2 = max(0, min(font_h, int(round(y2 * dst_size[1] / src_size[1]))))
        if nx2 <= nx1 or ny2 <= ny1:
            rescaled.append((e, [nx1, ny1, max(nx1 + 1, nx2), max(ny1 + 1, ny2)]))
        else:
            rescaled.append((e, [nx1, ny1, nx2, ny2]))
    draw_bounds = [b for _, b in rescaled if len(b) == 4]
    all_bounds = draw_bounds
    placed_label_rects: list[list[int]] = []

    for e, b in rescaled:
        if len(b) != 4:
            continue
        x1, y1, x2, y2 = b
        color = _color_for(e)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=solid_width)
        label_rect = _draw_index_label(
            draw,
            str(e.index),
            b,
            color,
            font_w,
            font_h,
            all_bounds,
            placed_label_rects,
        )
        if label_rect is not None:
            placed_label_rects.append(label_rect)

    out = io.BytesIO()
    format_name = output_format.upper()
    save_kwargs = {"quality": quality} if format_name in {"JPEG", "WEBP"} else {}
    img.save(out, format=format_name, **save_kwargs)
    return out.getvalue()


def _effective_font_size(label: str, bounds: Sequence[int], img_w: int, img_h: int) -> int:
    """Font size driven by screenshot height AND local target size.

    Large screenshots still get legible labels, but tiny icons, status items,
    and pagination dots cap the font much lower than fullscreen cards do.
    Multi-digit labels shrink further so the background box stays compact.
    """
    if len(bounds) != 4:
        return MIN_FONT_SIZE
    bw = max(1, int(bounds[2]) - int(bounds[0]))
    bh = max(1, int(bounds[3]) - int(bounds[1]))
    short_edge = max(1, min(bw, bh))
    long_edge = max(bw, bh)

    global_size = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, img_h // BASE_FONT_DIVISOR))
    local_cap = max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, short_edge // 2 + 4))
    if short_edge <= 24:
        local_cap = min(local_cap, 11)
    elif short_edge <= 36:
        local_cap = min(local_cap, 13)
    elif short_edge <= 56:
        local_cap = min(local_cap, 16)
    elif short_edge <= 84:
        local_cap = min(local_cap, 20)

    size = min(global_size, local_cap)
    if long_edge <= 48:
        size = min(size, 11)

    digits = max(1, len(label))
    if digits > 1:
        size = max(MIN_FONT_SIZE, int(round(size * (0.9 ** (digits - 1)))))
    return max(MIN_FONT_SIZE, size)


def _measure_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    font: ImageFont.ImageFont | None,
) -> tuple[int, int, int]:
    """Return (label_w, label_h, pad) for the given text/font."""
    if font is not None:
        try:
            tb = draw.textbbox((0, 0), label, font=font)
            text_w = tb[2] - tb[0]
            text_h = tb[3] - tb[1]
        except Exception:  # noqa: BLE001
            text_w, text_h = len(label) * 8, 12
    else:
        text_w, text_h = len(label) * 8, 12

    pad = 2
    label_w = text_w + pad * 2
    label_h = text_h + pad * 2
    return label_w, label_h, pad


def _draw_index_label(
    draw: ImageDraw.ImageDraw,
    label: str,
    bounds: Sequence[int],
    color: tuple[int, int, int],
    img_w: int,
    img_h: int,
    avoid_bounds: Sequence[Sequence[int]],
    placed_label_rects: Sequence[Sequence[int]],
) -> list[int] | None:
    """Draw a compact owned label, connecting every external placement."""
    font_size = _effective_font_size(label, bounds, img_w, img_h)
    font = _load_font(font_size)
    label_w, label_h, pad = _measure_label(draw, label, font)
    rect = _pick_label_rect(
        bounds,
        label_w,
        label_h,
        img_w,
        img_h,
        avoid_bounds,
        placed_label_rects,
    )
    if rect is None:
        return None

    if not _rect_inside(rect, bounds):
        start, end = _connector_segment(rect, bounds)
        draw.line([start, end], fill=color, width=max(1, font_size // 8))
        radius = max(1, font_size // 10)
        draw.ellipse(
            [end[0] - radius, end[1] - radius, end[0] + radius, end[1] + radius],
            fill=color,
        )
    tx = rect[0] + pad
    ty = rect[1] + pad - 1
    draw.rectangle(rect, fill=color)
    if font is not None:
        draw.text((tx, ty), label, fill=(255, 255, 255), font=font)
    else:
        draw.text((tx, ty), label, fill=(255, 255, 255))
    return rect


def _pick_label_rect(
    bounds: Sequence[int],
    label_w: int,
    label_h: int,
    img_w: int,
    img_h: int,
    avoid_bounds: Sequence[Sequence[int]],
    placed_label_rects: Sequence[Sequence[int]],
) -> list[int] | None:
    """Prefer a label fully inside its target, then a connected external label."""
    if len(bounds) != 4:
        return None
    x1, y1, x2, y2 = [int(v) for v in bounds]
    margin = LABEL_MARGIN
    target = [x1, y1, x2, y2]

    inside_candidates = [
        [x1 + 2, y1 + 2, x1 + 2 + label_w, y1 + 2 + label_h],
        [x2 - label_w - 2, y1 + 2, x2 - 2, y1 + 2 + label_h],
        [x1 + 2, y2 - label_h - 2, x1 + 2 + label_w, y2 - 2],
        [x2 - label_w - 2, y2 - label_h - 2, x2 - 2, y2 - 2],
    ]
    valid_inside = [
        rect for rect in inside_candidates
        if _rect_fits(rect, img_w, img_h) and _rect_inside(rect, target)
    ]
    if valid_inside:
        return min(
            valid_inside,
            key=lambda rect: sum(
                _overlap_area(rect, previous) for previous in placed_label_rects
            ),
        )

    external_candidates = [
        [x1 + 2, y1 - label_h - margin, x1 + 2 + label_w, y1 - margin],
        [x2 - label_w - 2, y1 - label_h - margin, x2 - 2, y1 - margin],
        [x2 + margin, y1 + 2, x2 + margin + label_w, y1 + 2 + label_h],
        [x1 - label_w - margin, y1 + 2, x1 - margin, y1 + 2 + label_h],
        [x1 + 2, y2 + margin, x1 + 2 + label_w, y2 + margin + label_h],
        [x2 - label_w - 2, y2 + margin, x2 - 2, y2 + margin + label_h],
    ]

    best_rect: list[int] | None = None
    best_score: tuple[float, ...] | None = None

    for idx, rect in enumerate(external_candidates):
        if not _rect_fits(rect, img_w, img_h):
            continue
        other_overlap = 0.0
        for other in avoid_bounds:
            if list(other) == target or len(other) != 4:
                continue
            other_overlap += float(_overlap_area(rect, other))
        label_overlap = sum(float(_overlap_area(rect, prev)) for prev in placed_label_rects)
        distance_penalty = float((rect[0] - x1) ** 2 + (rect[1] - y1) ** 2)
        score = (
            other_overlap,
            label_overlap,
            float(idx),
            distance_penalty,
        )
        if best_score is None or score < best_score:
            best_score = score
            best_rect = rect

    if best_rect is not None:
        return best_rect

    fallback = [
        max(0, min(img_w - label_w, x1 + 2)),
        max(0, min(img_h - label_h, max(0, y1 - label_h - margin))),
    ]
    return [fallback[0], fallback[1], fallback[0] + label_w, fallback[1] + label_h]


def _rect_inside(inner: Sequence[int], outer: Sequence[int]) -> bool:
    return (
        len(inner) == 4
        and len(outer) == 4
        and int(inner[0]) >= int(outer[0])
        and int(inner[1]) >= int(outer[1])
        and int(inner[2]) <= int(outer[2])
        and int(inner[3]) <= int(outer[3])
    )


def _connector_segment(
    label_rect: Sequence[int], target: Sequence[int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    target_x = (int(target[0]) + int(target[2])) // 2
    target_y = (int(target[1]) + int(target[3])) // 2
    label_x = max(int(label_rect[0]), min(target_x, int(label_rect[2])))
    label_y = max(int(label_rect[1]), min(target_y, int(label_rect[3])))
    return (label_x, label_y), (target_x, target_y)


def _rect_fits(rect: Sequence[int], img_w: int, img_h: int) -> bool:
    return (
        len(rect) == 4
        and rect[0] >= 0
        and rect[1] >= 0
        and rect[2] <= img_w
        and rect[3] <= img_h
        and rect[2] > rect[0]
        and rect[3] > rect[1]
    )


def _overlap_area(a: Sequence[int], b: Sequence[int]) -> int:
    if len(a) != 4 or len(b) != 4:
        return 0
    ix1 = max(int(a[0]), int(b[0]))
    iy1 = max(int(a[1]), int(b[1]))
    ix2 = min(int(a[2]), int(b[2]))
    iy2 = min(int(a[3]), int(b[3]))
    if ix2 <= ix1 or iy2 <= iy1:
        return 0
    return (ix2 - ix1) * (iy2 - iy1)


def _draw_dashed_rectangle(
    draw: ImageDraw.ImageDraw,
    xy: list[int],
    color: tuple[int, int, int],
    width: int,
    dash: int = 12,
    gap: int = 8,
) -> None:
    """Draw a dashed rectangle by stroking each edge with dash/gap segments."""
    x1, y1, x2, y2 = xy

    def dashed_line(sx, sy, ex, ey):
        # Walk along the longer axis, drawing dash segments.
        dx = ex - sx
        dy = ey - sy
        length = abs(dx) + abs(dy)
        if length == 0:
            return
        ux, uy = dx / length, dy / length
        pos = 0.0
        on = True
        while pos < length:
            seg = dash if on else gap
            nxt = min(pos + seg, length)
            if on:
                draw.line(
                    [sx + ux * pos, sy + uy * pos, sx + ux * nxt, sy + uy * nxt],
                    fill=color,
                    width=width,
                )
            pos = nxt
            on = not on

    dashed_line(x1, y1, x2, y1)
    dashed_line(x2, y1, x2, y2)
    dashed_line(x2, y2, x1, y2)
    dashed_line(x1, y2, x1, y1)
