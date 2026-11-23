from io import BytesIO

from PIL import Image, ImageDraw

from agent.skills.script_visual import VisualTemplate
from agent.skills.script_visual_object import VisualObject, VisualPart, match_visual_object_pixels


SIZE = (300, 220)
CARD = (34, 36, 52)
PAGE = (18, 19, 26)
WIDTH, HEIGHT = 130, 72
TITLE = (9, 9, 53, 19)
TIME = (9, 28, 49, 39)
SWITCH = (97, 28, 121, 42)
STABLE = tuple(VisualPart(role=role, box=box) for role, box in (
    ("identity", TITLE), ("identity", TIME), ("control", SWITCH),
))


def png(image):
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def screen():
    return Image.new("RGB", SIZE, PAGE)


def shifted(box, x, y):
    return box[0] + x, box[1] + y, box[2] + x, box[3] + y


def label(frame, box, accent):
    left, top, right, bottom = box
    draw = ImageDraw.Draw(frame)
    draw.rectangle((left + 1, top + 1, right - 2, bottom - 2), fill=CARD)
    draw.polygon(((left + 2, bottom - 2), (left + 12, top + 2), (right - 5, bottom - 4)), fill=accent)
    draw.rectangle((right - 12, top + 2, right - 4, top + 5), fill=accent)


def knob(frame, box, enabled=True):
    left, top, right, bottom = box
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle((left + 1, top + 1, right - 2, bottom - 2), radius=4,
                           fill="#6ecb98" if enabled else "#b64748")
    thumb = right - 4 if enabled else left + 11
    draw.ellipse((thumb - 7, top + 3, thumb, bottom - 4), fill="white")


def card(frame, x, y, *, title="#ffe25e", time="#f0e3fd", enabled=True, dynamic="#5363f0", reflow=False):
    ImageDraw.Draw(frame).rectangle((x, y, x + WIDTH - 1, y + HEIGHT - 1), fill=CARD)
    title_box = (52, 6, 96, 16) if reflow else TITLE
    time_box = (15, 42, 55, 53) if reflow else TIME
    switch_box = (90, 46, 114, 60) if reflow else SWITCH
    label(frame, shifted(title_box, x, y), title)
    label(frame, shifted(time_box, x, y), time)
    knob(frame, shifted(switch_box, x, y), enabled)
    label(frame, (x + 7, y + 57, x + 36, y + 66), dynamic)
    return shifted(switch_box, x, y)


def recording(*, parts=STABLE):
    frame = screen()
    card(frame, 12, 20)
    layout = png(frame.crop((12, 20, 12 + WIDTH, 20 + HEIGHT)))
    template = VisualTemplate(png_path="card.png", reference_width=SIZE[0], reference_height=SIZE[1],
                              roi=(0, 0, 0.5, 0.5), fallback_roi=(0, 0, 1, 1),
                              scales=(1,), point=(103 / 129, 33 / 71))
    return VisualObject(layout=template, parts=parts, container="card"), layout


def test_move_ignores_dynamic_region_and_preserves_offset():
    object, source = recording()
    frame = screen()
    card(frame, 12, 20, title="#1944b0")
    correct = card(frame, 155, 110, dynamic="#fb4e18")
    result = match_visual_object_pixels(object, source, png(frame))
    hit, = result.matches
    assert hit.bounds == correct
    assert (hit.x, hit.y) == (258.5, 143.5)


def test_a_single_lookalike_is_not_the_object():
    object, source = recording()
    frame = screen()
    card(frame, 150, 100, title="#1944b0")
    assert match_visual_object_pixels(object, source, png(frame)).status == "not_found"


def test_one_changed_digit_is_not_averaged_out_of_a_long_identity_label():
    def document(label_text):
        frame = screen()
        draw = ImageDraw.Draw(frame)
        draw.rectangle((10, 22, 280, 126), fill=CARD)
        draw.text((18, 44), label_text, fill="white")
        knob(frame, (238, 83, 262, 97))
        return frame

    saved = document("Quarterly Forecast Revision 12.pdf")
    layout = VisualTemplate(png_path="file.png", reference_width=SIZE[0], reference_height=SIZE[1],
                            scales=(1,), point=(238 / 270, 68 / 104))
    object = VisualObject(layout=layout, container="card", parts=(
        VisualPart(role="identity", box=(5, 16, 210, 38)),
        VisualPart(role="control", box=(228, 61, 252, 75)),
    ))
    source = png(saved.crop((10, 22, 281, 127)))
    assert match_visual_object_pixels(object, source, png(saved)).status == "matched"
    other = document("Quarterly Forecast Revision 13.pdf")
    assert match_visual_object_pixels(object, source, png(other)).status == "not_found"


def test_reflow_matches_visual_parts_inside_the_same_independently_visible_card():
    object, source = recording()
    frame = screen()
    card(frame, 12, 20, title="#1944b0", reflow=True)
    correct = card(frame, 155, 110, reflow=True)
    result = match_visual_object_pixels(object, source, png(frame))
    hit, = result.matches
    assert hit.bounds == correct
    assert (hit.x, hit.y) == (251.5, 161.5)


def test_card_can_reflow_to_list_with_repeated_indented_dividers():
    object, source = recording()
    frame = Image.new("RGB", SIZE, CARD)
    draw = ImageDraw.Draw(frame)
    for y in (75, 143, 211):
        draw.line((38, y, 299, y), fill="#dadada")
    for row, accent in enumerate(("#ffe25e", "#1944b0", "#1944b0")):
        y = row * 68
        label(frame, (38, 13 + y, 82, 23 + y), accent)
        label(frame, (38, 31 + y, 78, 42 + y), "#f0e3fd")
        knob(frame, (125, 22 + y, 149, 36 + y))
    hit, = match_visual_object_pixels(object, source, png(frame)).matches
    assert hit.bounds == (125, 22, 149, 36)
    assert (hit.x, hit.y) == (131.5, 27.5)


def test_two_matching_identities_stay_ambiguous_even_if_one_control_looks_right():
    object, source = recording()
    frame = screen()
    card(frame, 12, 20)
    card(frame, 155, 110, enabled=False, reflow=True)
    for identity_only in (True, False):
        result = match_visual_object_pixels(object, source, png(frame), identity_only=identity_only)
        assert result.status == "unavailable"
        assert result.reason == "visual_object_ambiguous"


def test_unrelated_neighboring_cards_cannot_supply_parts_of_the_same_object():
    object, source = recording()
    frame = screen()
    draw = ImageDraw.Draw(frame)
    draw.rectangle((22, 39, 151, 63), fill=CARD)
    draw.rectangle((22, 66, 151, 135), fill=CARD)
    # These happen at the template offsets, yet the visible gutter separates
    # the first identifier from the second identifier and control.
    label(frame, shifted(TITLE, 22, 40), "#ffe25e")
    label(frame, shifted(TIME, 22, 40), "#f0e3fd")
    knob(frame, shifted(SWITCH, 22, 40))
    assert match_visual_object_pixels(object, source, png(frame)).status == "not_found"


def test_reflow_refuses_a_card_clipped_at_the_screen_edge():
    object, source = recording()
    frame = screen()
    card(frame, -5, 20, reflow=True)
    assert match_visual_object_pixels(object, source, png(frame)).status == "not_found"


def test_exact_ocr_parameter_resolves_current_card_and_never_uses_saved_text_pixels():
    parts = (VisualPart(role="identity", box=TITLE, text_parameter="filename"), STABLE[2])
    object, source = recording(parts=parts)
    frame = screen()
    card(frame, 12, 20)
    correct = card(frame, 155, 110, title="#1944b0", reflow=True)
    assert match_visual_object_pixels(object, source, png(frame)).reason == "visual_identity_ocr_unavailable"
    calls = []

    def read_words(image):
        calls.append(image.size)
        return [("Report.pdf", shifted(TITLE, 12, 20)), ("report.pdf", (207, 116, 251, 126))]

    result = match_visual_object_pixels(object, source, png(frame), ocr=read_words,
                                        parameters={"filename": "report.pdf"})
    hit, = result.matches
    assert hit.bounds == correct
    assert calls == [SIZE]


def test_a_declared_fixed_text_anchor_requires_live_ocr_even_if_pixels_are_exact():
    parts = (VisualPart(role="identity", box=TITLE, text="Report.pdf"), STABLE[2])
    object, source = recording(parts=parts)
    frame = screen()
    card(frame, 12, 20)
    wrong_reading = lambda _: [("report.pdf", shifted(TITLE, 12, 20))]
    result = match_visual_object_pixels(object, source, png(frame), ocr=wrong_reading)
    assert result.status == "not_found"
