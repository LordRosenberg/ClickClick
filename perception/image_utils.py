"""Role-facing image preparation and advisory token-cost estimates."""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass
from typing import Sequence

from PIL import Image

from perception.som import render_som
from shared.schemas import UIElement


MODEL_IMAGE_LONG_EDGE = 1080
MODEL_IMAGE_JPEG_QUALITY = 75


@dataclass(frozen=True)
class ModelImageProfile:
    id: str
    long_edge: int
    jpeg_quality: int


TREE_MODEL_IMAGE_PROFILE = ModelImageProfile("tree-som-1080-q75", 1080, 75)
# A 2048/q88 candidate tied 1080/q75 on the combined 16-attempt gradient
# while using ~3.4x estimated image tokens, and regressed on the holdout.
# Keep the smallest measured image-only profile until a later frozen gradient
# demonstrates repeated whole-task benefit.
IMAGE_ONLY_MODEL_IMAGE_PROFILE = ModelImageProfile(
    "image-only-calibrated-1080-q75", 1080, 75,
)


def role_model_image_profile(*, tree_available: bool) -> ModelImageProfile:
    return (
        TREE_MODEL_IMAGE_PROFILE
        if tree_available
        else IMAGE_ONLY_MODEL_IMAGE_PROFILE
    )


def expected_role_image_delivery(
    *,
    role: str,
    accepted: bool,
    tree_available: bool,
    index_actionable: bool,
    pixels_available: bool,
) -> dict[str, object]:
    """Describe deterministic role-image delivery without encoding UI meaning."""
    profile = role_model_image_profile(tree_available=tree_available)
    would_attach = bool(accepted and pixels_available)
    return {
        "would_attach": would_attach,
        "mode": (
            "tree+image" if tree_available and would_attach
            else "image-only" if would_attach
            else "tree-only"
        ),
        "visual_kind": (
            "som" if role == "executor" and index_actionable else "clean"
        ),
        "profile_id": profile.id,
        "long_edge": profile.long_edge,
        "jpeg_quality": profile.jpeg_quality,
    }


def compress_for_model(
    image_bytes: bytes | None,
    *,
    max_dim: int = MODEL_IMAGE_LONG_EDGE,
    quality: int = MODEL_IMAGE_JPEG_QUALITY,
) -> tuple[bytes | None, tuple[int, int], tuple[int, int]]:
    """Return JPEG bytes plus original and model-facing dimensions.

    Invalid bytes are returned unchanged with unknown dimensions so callers
    can preserve the previous safe pass-through behavior without inventing a
    coordinate transform.
    """
    if image_bytes is None:
        return None, (0, 0), (0, 0)
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            original_size = (int(source.width), int(source.height))
            image = source.convert("RGB")
            scale = min(1.0, max_dim / max(original_size))
            if scale < 1.0:
                size = (
                    max(1, int(original_size[0] * scale)),
                    max(1, int(original_size[1] * scale)),
                )
                image = image.resize(size, Image.Resampling.LANCZOS)
            model_size = (int(image.width), int(image.height))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality)
            return output.getvalue(), original_size, model_size
    except Exception:  # noqa: BLE001
        return image_bytes, (0, 0), (0, 0)


def validated_model_image(
    image_bytes: bytes | None,
    model_size: tuple[int, int],
) -> tuple[bytes | None, tuple[int, int]]:
    """Fail closed when an adapter could not decode valid image geometry."""
    if (
        image_bytes is None
        or len(model_size) != 2
        or model_size[0] <= 0
        or model_size[1] <= 0
    ):
        return None, (0, 0)
    return image_bytes, model_size


def estimate_image_input_tokens(
    model: str,
    width: int,
    height: int,
) -> int | None:
    """Estimate image-input tokens only for model families we understand.

    GPT-5.4 uses 32-pixel patches, a 2,500-patch budget, and a 1.2
    multiplier. The value is operational telemetry, never a billing claim or
    an input to agent decisions. Unknown families intentionally return None.
    """
    normalized = str(model or "").casefold().rsplit("/", 1)[-1]
    if not normalized.startswith("gpt-5.4") or width <= 0 or height <= 0:
        return None

    patch_size = 32
    max_patches = 2500
    patch_count = math.ceil(width / patch_size) * math.ceil(height / patch_size)
    if patch_count > max_patches:
        scale = math.sqrt(max_patches / patch_count)
        width = max(1, math.floor(width * scale))
        height = max(1, math.floor(height * scale))
        patch_count = math.ceil(width / patch_size) * math.ceil(height / patch_size)
        patch_count = min(max_patches, patch_count)
    return math.ceil(patch_count * 1.2)


def prepare_som_for_model(
    image_bytes: bytes | None,
    elements: Sequence[UIElement],
    *,
    frame_geometry: tuple[int, int] | None = None,
    max_dim: int = MODEL_IMAGE_LONG_EDGE,
    quality: int = MODEL_IMAGE_JPEG_QUALITY,
    source_already_annotated: bool = False,
) -> tuple[bytes | None, tuple[int, int], tuple[int, int]]:
    """Resize one aligned source and render the role-facing SoM once.

    `source_already_annotated` exists only for legacy/test packages that do
    not retain clean pixels. New observation packages always use the clean
    branch and produce a JPEG at the configured quality.
    """
    compressed, original_size, model_size = compress_for_model(
        image_bytes, max_dim=max_dim, quality=quality,
    )
    if compressed is None or original_size == model_size == (0, 0):
        return compressed, original_size, model_size
    if source_already_annotated and original_size == model_size:
        return compressed, original_size, model_size

    source_size = (
        frame_geometry
        if frame_geometry and all(value > 0 for value in frame_geometry)
        else original_size
    )
    rendered = render_som(
        compressed,
        list(elements),
        src_size=source_size,
        dst_size=model_size,
        output_format="PNG" if source_already_annotated else "JPEG",
        quality=quality,
    )
    return rendered, original_size, model_size


def visual_evidence_metadata(
    *,
    role: str,
    visual_kind: str,
    observation_id: str,
    image_bytes: bytes | None,
    source_image_bytes: bytes | None,
    model_size: tuple[int, int],
    model: str,
    profile: ModelImageProfile,
) -> dict[str, object]:
    """Describe the actual baseline image supplied to one role invocation."""
    delivered = image_bytes is not None
    width, height = model_size if delivered else (0, 0)
    return {
        "role": role,
        "observation_id": observation_id,
        "visual_kind": visual_kind,
        "image_delivered": delivered,
        "delivered_image_byte_count": len(image_bytes or b""),
        "delivered_image_sha256": (
            hashlib.sha256(image_bytes).hexdigest() if image_bytes is not None else None
        ),
        "source_pixel_byte_count": len(source_image_bytes or b""),
        "source_pixel_sha256": (
            hashlib.sha256(source_image_bytes).hexdigest()
            if source_image_bytes is not None else None
        ),
        "model_image_width": int(width),
        "model_image_height": int(height),
        "current_only": True,
        "estimated_image_tokens": (
            estimate_image_input_tokens(model, int(width), int(height))
            if delivered else None
        ),
        "image_profile_id": profile.id,
        "image_profile_long_edge": profile.long_edge,
        "image_profile_jpeg_quality": profile.jpeg_quality,
    }
