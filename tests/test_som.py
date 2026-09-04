"""Accessibility SoM rendering and adaptive typography."""

import io

from PIL import Image, ImageDraw

from perception.som import (
    A11Y_COLOR,
    _color_for,
    _connector_segment,
    _pick_label_rect,
    _rect_inside,
    render_som,
)
from shared.schemas import UIElement


def _png(w: int = 400, h: int = 400, color=(220, 220, 220)) -> bytes:
    img = Image.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _a11y(index: int, bounds: list[int]) -> UIElement:
    return UIElement(index=index, text="OK", bounds=bounds)


def _count_color(img: Image.Image, target: tuple[int, int, int], tol: int = 30) -> int:
    """Count pixels within `tol` of `target` on each channel."""
    n = 0
    w, h = img.size
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            p = img.getpixel((x, y))
            if all(abs(p[c] - target[c]) <= tol for c in range(3)):
                n += 1
    return n


def test_color_for_a11y_is_cyan():
    assert _color_for(_a11y(0, [0, 0, 10, 10])) == A11Y_COLOR


# --- rendering ---


def test_render_a11y_draws_cyan_pixels():
    png = render_som(_png(), [_a11y(3, [20, 20, 380, 380])])
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert _count_color(img, A11Y_COLOR) > 0, "a11y box did not render in cyan"


def test_render_som_adaptive_font_legible_on_large_screenshot():
    big = _png(1080, 1920)
    png = render_som(big, [_a11y(42, [100, 100, 200, 200])])
    img = Image.open(io.BytesIO(png))
    filled = 0
    for y in range(100, 140):
        for x in range(100, 200):
            if img.getpixel((x, y)) != (220, 220, 220):
                filled += 1
    assert filled > 50, "adaptive label box too small / not rendered at 1080p"


def test_render_som_multi_digit_label_fits_box():
    png = render_som(_png(), [_a11y(123, [50, 50, 150, 150])])
    assert Image.open(io.BytesIO(png)).size == (400, 400)


def test_render_som_small_target_uses_compact_label():
    base = Image.open(io.BytesIO(_png(200, 200))).convert("RGB")
    rendered = Image.open(
        io.BytesIO(render_som(_png(200, 200), [_a11y(7, [80, 80, 98, 98])]))
    ).convert("RGB")
    changed = [
        (x, y)
        for y in range(0, 200)
        for x in range(0, 200)
        if rendered.getpixel((x, y)) != base.getpixel((x, y))
    ]
    xs = [x for x, _ in changed]
    ys = [y for _, y in changed]
    assert (max(xs) - min(xs)) <= 40
    assert (max(ys) - min(ys)) <= 40


def test_render_som_dense_small_target_cluster_keeps_visible_ownership():
    img = Image.new("RGB", (160, 160), color=(220, 220, 220))
    draw = ImageDraw.Draw(img)
    # Dense 3x3 icon cluster near top-left should push labels outward.
    for x in (20, 42, 64):
        for y in (20, 42, 64):
            draw.rectangle([x, y, x + 12, y + 12], fill=(80, 80, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    cluster = [
        _a11y(0, [20, 20, 32, 32]),
        _a11y(1, [42, 20, 54, 32]),
        _a11y(2, [64, 20, 76, 32]),
        _a11y(3, [20, 42, 32, 54]),
        _a11y(4, [42, 42, 54, 54]),
        _a11y(5, [64, 42, 76, 54]),
        _a11y(6, [20, 64, 32, 76]),
        _a11y(7, [42, 64, 54, 76]),
        _a11y(8, [64, 64, 76, 76]),
    ]
    bare = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    rendered = Image.open(io.BytesIO(render_som(buf.getvalue(), cluster))).convert("RGB")
    changed_inside_cluster = sum(
        1
        for y in range(20, 76)
        for x in range(20, 76)
        if rendered.getpixel((x, y)) != bare.getpixel((x, y))
    )
    changed_above_cluster = sum(
        1
        for y in range(0, 18)
        for x in range(0, 110)
        if rendered.getpixel((x, y)) != bare.getpixel((x, y))
    )
    assert changed_above_cluster > 0  # external labels/connectors remain visible
    assert changed_inside_cluster < 1200


def test_dense_tab_labels_fit_inside_their_own_target():
    targets = [
        [10, 20, 70, 55],
        [70, 20, 130, 55],
        [130, 20, 190, 55],
    ]
    placed: list[list[int]] = []
    for target in targets:
        rect = _pick_label_rect(target, 18, 16, 200, 100, targets, placed)
        assert rect is not None
        assert _rect_inside(rect, target)
        placed.append(rect)


def test_external_label_has_connector_terminating_on_tiny_target():
    target = [80, 80, 84, 84]
    rect = _pick_label_rect(target, 18, 16, 200, 200, [target], [])
    assert rect is not None
    assert not _rect_inside(rect, target)
    start, end = _connector_segment(rect, target)
    assert rect[0] <= start[0] <= rect[2]
    assert rect[1] <= start[1] <= rect[3]
    assert end == (82, 82)


def test_render_som_skips_malformed_bounds():
    png = render_som(_png(), [UIElement(index=0, bounds=[1, 2])])
    assert Image.open(io.BytesIO(png)).size == (400, 400)


def test_render_som_empty_elements_returns_valid_png():
    assert Image.open(io.BytesIO(render_som(_png(), []))).size == (400, 400)


# --- rescale (compressed-frame SoM for the model) ---


def test_render_som_src_dst_rescales_bounds_into_destination_frame():
    """Original-frame bounds [1080, 800, 1620, 1100] on a 1080×2400 source
    should land at compressed-frame [360, 267, 540, 367] when src_size and
    dst_size reflect a 3× downscale.
    """
    png = _png(360, 800)  # 360×800 is the destination frame
    # Element uses ORIGINAL-frame bounds at 3× the destination frame.
    el = _a11y(0, [1080, 800, 1620, 1100])
    rendered = render_som(png, [el], src_size=(1080, 2400), dst_size=(360, 800))
    img = Image.open(io.BytesIO(rendered)).convert("RGB")
    # Cyan pixels should appear inside the rescaled box (≈ 360-540 × 267-367)
    # and NOT inside the original-frame box coords (which would be off-canvas).
    cyan = _count_color_in_rect(img, A11Y_COLOR, 355, 265, 545, 370)
    off_canvas_cyan = _count_color_in_rect(img, A11Y_COLOR, 1050, 790, 1625, 1105)
    assert cyan > 0, "compressed-frame box did not render inside expected rect"
    assert off_canvas_cyan == 0, "rendered onto original-frame coordinates (off-canvas)"


def test_render_som_src_dst_font_scales_with_destination_height():
    """On a small destination frame, a small icon's compact label SHOULD be
    smaller than the same icon rendered onto a large original frame at the
    same original coordinates (because `_effective_font_size` sizes against
    `dst_size` instead of the opened image's full height when src/dst are
    both set).

    Compare label-rect widths (horizontal span of A11Y_COLOR pixels) across
    the two cases.
    """
    # Same original element in both runs.
    el = _a11y(0, [800, 1100, 980, 1280])  # short_edge ~180 in original frame

    # Destination is small (360x800). Short_edge on destination ≈ 60px.
    small_png = render_som(_png(360, 800), [el],
                            src_size=(1080, 2400), dst_size=(360, 800))
    # No rescale — same element, opened against a 1080×2400 frame.
    big_png = render_som(_png(1080, 2400), [el])

    # The label background is a solid block of A11Y_COLOR drawn by
    # `_draw_index_label`. Compare the bounding-box footprint.
    small_img = Image.open(io.BytesIO(small_png)).convert("RGB")
    big_img = Image.open(io.BytesIO(big_png)).convert("RGB")

    def label_width(img: Image.Image) -> int:
        xs = [x for y in range(img.size[1]) for x in range(img.size[0])
              if img.getpixel((x, y)) == A11Y_COLOR]
        return (max(xs) - min(xs)) if xs else 0

    sw = label_width(small_img)
    bw = label_width(big_img)
    assert bw > 0, "expected cyan label pixels on the original-frame render"
    assert sw > 0, "expected cyan label pixels on the compressed-frame render"
    # Destination-frame label is sized for a smaller viewport, so it is
    # narrower than the original-frame label.
    assert sw < bw


def _count_color_in_rect(
    img: Image.Image,
    target: tuple[int, int, int],
    x1: int, y1: int, x2: int, y2: int,
    tol: int = 30,
) -> int:
    """Count pixels matching `target` (with tolerance) inside the rect, clipped to the image."""
    w, h = img.size
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(w, x2); y2 = min(h, y2)
    n = 0
    for y in range(y1, y2):
        for x in range(x1, x2):
            p = img.getpixel((x, y))
            if all(abs(p[c] - target[c]) <= tol for c in range(3)):
                n += 1
    return n
