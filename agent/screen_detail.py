"""On-demand native-pixel reading, always paired with a fresh global observation."""

import copy
import io
import math

from PIL import Image, ImageDraw

from agent.tool_registry import ToolAttachment


def detail_parameters(parameters):
    result = copy.deepcopy(parameters)
    result["properties"]["mode"]["enum"].append("detail")
    result["properties"]["region"] = {
        "type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
        "minItems": 4, "maxItems": 4,
        "description": "Detail only: approximate normalized [left,top,right,bottom] on the full screen. Omit for four overlapping tiles with a locator overview.",
    }
    # The base snapshot/sequence schema has a mode-specific union.
    if "oneOf" in result:
        result["oneOf"].append({"properties": {"mode": {"const": "detail"}}, "required": ["mode"]})
    return result


def validate_region(value):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in value)):
        raise ValueError("region requires four finite normalized numbers")
    left, top, right, bottom = value
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("region requires 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
    return value


class NativeCaptureDriver:
    """Use the existing fenced capture transaction without the reduced stream."""
    def __init__(self, driver):
        self.driver = driver

    def __getattr__(self, name):
        return getattr(self.driver, name)

    async def capture_deadline_frame(self, deadline):
        return await self.driver.capture_native_frame(deadline)


def detail_attachments(package, region=None, artifacts=None):
    """Crop one immutable native capture; never upsample or attach old tree boxes."""
    image = Image.open(io.BytesIO(package.clean_png)).convert("RGB")
    width, height = image.size
    regions = [region] if region is not None else [
        [0, 0, .56, .56], [.44, 0, 1, .56],
        [0, .44, .56, 1], [.44, .44, 1, 1],
    ]
    bounds = [(int(l * width), int(t * height), math.ceil(r * width), math.ceil(b * height))
              for l, t, r, b in regions]
    overview = image.copy()
    overview.thumbnail((540, 540))
    draw = ImageDraw.Draw(overview)
    sx, sy = overview.width / width, overview.height / height
    for number, box in enumerate(bounds, 1):
        display_box = tuple(round(v * (sx if i % 2 == 0 else sy)) for i, v in enumerate(box))
        draw.rectangle(display_box, outline="#ff5500", width=2)
        x, y = display_box[0] + 4, display_box[1] + 4
        draw.rectangle((x, y, x + 23, y + 25), fill="#ff5500")
        draw.text((x + 4, y + 2), str(number), fill="white", font_size=18)
    pictures = [("Detail locator; use the fresh global observation for action coordinates", overview, None)]
    for number, box in enumerate(bounds, 1):
        crop = image.crop(box)
        # Bound optional image payload while retaining substantially more source pixels.
        crop.thumbnail((1600, 1600))
        pictures.append((f"Reading tile {number}; native bounds {box} of {width}x{height}; read-only", crop, box))
    attachments = []
    for label, picture, box in pictures:
        output = io.BytesIO()
        picture.save(output, format="PNG")
        content = output.getvalue()
        ref = artifacts.save_bytes("screen_detail", content, suffix=".png") if artifacts else None
        attachments.append(ToolAttachment(
            label=label, content=content, artifact_ref=ref,
            indexed_targets_available=False, actionable_coordinate_reference=False,
            observation_id=package.observation_id, image_size=picture.size,
            frame_geometry=image.size, crop_box=box,
            timestamp_ms=package.captured_monotonic_ms,
        ))
    return attachments
