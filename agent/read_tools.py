"""Agent-facing observation and installed-app read-tool handlers."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from agent.action_observation import ActionObservationTransaction
from agent.observation_space import ObservationRegistry, make_registry_entry
from agent.tool_registry import (
    AgentToolResult,
    EvidenceRecord,
    ToolAttachment,
    ToolExecutionContext,
    ToolStatus,
)
from perception.image_utils import (
    compress_for_model,
    prepare_som_for_model,
    role_model_image_profile,
    validated_model_image,
    visual_evidence_metadata,
)
from perception.observation import ObservationBuilder, ObservationPackage
from driver.observation_deadline import (
    CURRENT_DEADLINE_MS,
    TEMPORAL_BASELINE_MAX_AGE_MS,
    TEMPORAL_DEADLINE_MS,
    ObservationDeadline,
    ObservationStageError,
)
from shared.app_resolver import ResolverTicketStore, normalize_app_query
from shared.schemas import ObservationMode

OBSERVE_SCREEN_DEFAULT_DURATION_MS = 800
OBSERVE_SCREEN_MIN_DURATION_MS = 300
OBSERVE_SCREEN_MAX_DURATION_MS = 1500
INSTALLED_APP_CANDIDATE_LIMIT = 12


def _image_size(image: bytes | None) -> tuple[int, int]:
    if not image:
        return (0, 0)
    try:
        import io
        from PIL import Image

        with Image.open(io.BytesIO(image)) as parsed:
            return (int(parsed.width), int(parsed.height))
    except Exception:  # noqa: BLE001
        return (0, 0)


def _register_package(
    context: ToolExecutionContext,
    package: ObservationPackage,
    *,
    actionable: bool,
    make_active: bool | None = None,
) -> tuple[int, int]:
    # Only role-validated bytes define a model coordinate space. Persisted
    # replay annotations never become an implicit model attachment.
    image = package.image_for_llm
    model_size = _image_size(image)
    frame_size = (package.frame_width, package.frame_height)
    complete_geometry = all(value > 0 for value in (*model_size, *frame_size))
    package.model_image_width, package.model_image_height = model_size
    # Actionability is the union of the two exact mechanisms: safe indexed
    # tree actions need semantic elements but no pixels, while coordinate
    # actions need complete model/frame geometry. Do not disable a valid
    # tree-only observation merely because it has no image.
    package.actionable = bool(
        package.actionable
        and actionable
        and (complete_geometry or package.index_actionable)
    )
    registry = context.state.get("observation_registry")
    if not isinstance(registry, ObservationRegistry):
        registry = ObservationRegistry()
        context.state["observation_registry"] = registry
    entry = make_registry_entry(
        observation_id=package.observation_id,
        coordinate_space_id=package.coordinate_space_id,
        ui=package.ui,
        actionable=package.actionable,
        index_actionable=package.index_actionable,
        captured_monotonic_ms=package.captured_monotonic_ms,
        model_image_size=model_size,
        frame_geometry=frame_size,
        rotation_degrees=package.rotation_degrees,
        crop_box=package.crop_box,
        transform_id=package.transform_id,
    )
    registry.register(
        entry,
        make_active=(package.actionable if make_active is None else make_active),
    )
    return model_size


def _prepare_role_model_image(
    context: ToolExecutionContext,
    package: ObservationPackage,
) -> dict[str, Any]:
    """Apply the same 1080 role adapter used by baseline observations."""
    source = package.clean_png or package.image_for_llm
    frame_geometry = (
        (package.frame_width, package.frame_height)
        if package.frame_width > 0 and package.frame_height > 0 else None
    )
    tree_available = package.mode != ObservationMode.IMAGE_ONLY
    image_profile = role_model_image_profile(
        tree_available=tree_available
    )
    if context.role.value == "executor" and package.index_actionable:
        image, _original_size, model_size = prepare_som_for_model(
            source,
            package.ui.elements,
            frame_geometry=frame_geometry,
            max_dim=image_profile.long_edge,
            quality=image_profile.jpeg_quality,
            source_already_annotated=package.clean_png is None,
        )
    else:
        image, _original_size, model_size = compress_for_model(
            source,
            max_dim=image_profile.long_edge,
            quality=image_profile.jpeg_quality,
        )
    image, model_size = validated_model_image(image, model_size)
    package.image_for_llm = image
    package.model_image_width, package.model_image_height = model_size
    if image is not None:
        package.mode = (
            ObservationMode.TREE_PLUS_IMAGE
            if tree_available else ObservationMode.IMAGE_ONLY
        )
    else:
        package.mode = ObservationMode.TREE_ONLY
        if not tree_available:
            package.accepted = False
            package.acceptance_reason = "role_image_unavailable"
            package.actionable = False
            package.index_actionable = False
            if "role_image_unavailable" not in package.gap_reasons:
                package.gap_reasons.append("role_image_unavailable")
    return visual_evidence_metadata(
        role=context.role.value,
        visual_kind="som" if package.index_actionable else "clean",
        observation_id=package.observation_id,
        image_bytes=image,
        source_image_bytes=source,
        model_size=model_size,
        model=str(context.state.get("model") or ""),
        profile=image_profile,
    )


def _attachment(
    package: ObservationPackage,
    *,
    label: str,
    kind: str,
    content: bytes | str | None,
    actionable: bool,
    artifact_ref: str | None,
) -> ToolAttachment:
    image_size = (
        (package.model_image_width, package.model_image_height)
        if kind == "image" and package.model_image_width > 0 and package.model_image_height > 0
        else None
    )
    frame_geometry = (
        (package.frame_width, package.frame_height)
        if package.frame_width > 0 and package.frame_height > 0 else None
    )
    return ToolAttachment(
        label=label,
        kind=kind,
        content=content,
        artifact_ref=artifact_ref,
        timestamp_ms=package.captured_monotonic_ms,
        actionable_coordinate_reference=actionable,
        indexed_targets_available=package.index_actionable,
        observation_id=package.observation_id,
        coordinate_space_id=package.coordinate_space_id,
        image_size=image_size,
        frame_geometry=frame_geometry,
        rotation_degrees=package.rotation_degrees,
        crop_box=package.crop_box,
        transform_id=package.transform_id,
        element_set_id=f"elements_{package.observation_id}",
    )


@dataclass(frozen=True)
class TemporalObservationRequest:
    frames: int = 2
    duration_ms: int = 800


@dataclass
class TemporalObservationResult:
    status: str
    frames: list[ObservationPackage] = field(default_factory=list)
    ending: ObservationPackage | None = None
    provider: str = ""
    provider_status: str = ""
    provider_generation: int = 0
    provider_state: dict[str, Any] = field(default_factory=dict)
    fallback_edges: list[dict[str, str]] = field(default_factory=list)
    baseline_reused: bool = False
    temporal_complete: bool = False
    failed_stage: str | None = None


class TemporalObservationProvider(Protocol):
    async def observe_temporal(self, request: TemporalObservationRequest) -> TemporalObservationResult: ...


def _persist_package(package: ObservationPackage, artifacts: Any | None) -> None:
    if artifacts is None:
        return
    if package.annotated_png is not None and package.som_ref is None:
        package.som_ref = artifacts.save_bytes("som", package.annotated_png, suffix=".png")
    if package.tree_ref is None:
        package.tree_ref = artifacts.save_json("trees", {
            "observation_id": package.observation_id,
            "coordinate_space_id": package.coordinate_space_id,
            "actionable": package.actionable,
            "index_actionable": package.index_actionable,
            "app_id": package.ui.app_id,
            "activity": package.ui.activity,
            "text_for_llm": package.text_for_llm,
            "frame_geometry": [package.frame_width, package.frame_height],
            "model_image_size": [package.model_image_width, package.model_image_height],
            "rotation_degrees": package.rotation_degrees,
            "crop_box": list(package.crop_box) if package.crop_box else None,
            "transform_id": package.transform_id,
            "captured_monotonic_ms": package.captured_monotonic_ms,
        })


async def _capture(
    driver: Any,
    builder: ObservationBuilder,
    deadline: ObservationDeadline | None = None,
) -> tuple[ObservationPackage, dict[str, Any]]:
    budget_ms = (
        max(1, int(deadline.remaining_ms))
        if deadline is not None else CURRENT_DEADLINE_MS
    )
    package = await ActionObservationTransaction(
        driver,
        builder,
        current_deadline_ms=budget_ms,
    ).observe_current(
        deadline_ms=budget_ms,
        source="observe_screen_temporal_sample",
        attach_image=True,
    )
    metadata = dict(package.capture_meta or {})
    if not package.accepted or not (package.ui.app_id or "").strip():
        raise ObservationStageError(
            str(metadata.get("error_stage") or "grounding_barrier"),
            package.acceptance_reason or "temporal_sample_not_grounded",
            timed_out=bool(metadata.get("timed_out")),
            fallback_edges=list(metadata.get("fallback_edges") or []),
            provider_attempts=list(
                metadata.get("provider_attempts")
                or metadata.get("tree_provider_attempts")
                or []
            ),
            cancelled_tasks=list(metadata.get("cancelled_capture_tasks") or []),
        )
    setattr(package, "_observation_device", str(getattr(driver, "serial", "") or ""))
    setattr(package, "_observation_generation", int(metadata.get("generation") or 0))
    setattr(package, "_observation_complete", bool(metadata.get("complete", True)))
    return package, metadata


def _provider_state(driver: Any, mode: str) -> dict[str, Any]:
    diagnostics = getattr(driver, "observation_diagnostics", None)
    if callable(diagnostics):
        try:
            state = diagnostics(mode)
            return dict(state) if isinstance(state, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _baseline_eligible(
    package: ObservationPackage | None,
    *,
    driver: Any,
    generation: int,
    max_age_ms: int,
) -> tuple[bool, str]:
    if package is None:
        return False, "missing"
    image = package.clean_png or package.image_for_llm
    if (
        not image
        or not all(value > 0 for value in _image_size(image))
        or package.frame_width <= 0
        or package.frame_height <= 0
    ):
        return False, "incomplete"
    if not bool(getattr(package, "_observation_complete", True)):
        return False, "incomplete"
    age_ms = time.monotonic() * 1000.0 - package.captured_monotonic_ms
    if age_ms < 0 or age_ms > max_age_ms:
        return False, "expired"
    device = str(getattr(driver, "serial", "") or "")
    baseline_device = str(getattr(package, "_observation_device", device) or "")
    if device and baseline_device and device != baseline_device:
        return False, "device_mismatch"
    baseline_generation = int(getattr(package, "_observation_generation", generation) or 0)
    if generation and baseline_generation and generation != baseline_generation:
        return False, "generation_mismatch"
    if package.rotation_degrees not in {0, 90, 180, 270}:
        return False, "orientation_invalid"
    return True, "eligible"


def _adb_current_provider(
    provider_state: dict[str, Any], fallback_edges: list[dict[str, str]] | None = None,
) -> str:
    del provider_state
    if fallback_edges:
        return "adb_fallback"
    return "adb_selected"


async def _fallback_temporal(
    driver: Any,
    builder: ObservationBuilder,
    request: TemporalObservationRequest,
    baseline: ObservationPackage | None,
    deadline: ObservationDeadline,
) -> TemporalObservationResult:
    packages: list[ObservationPackage] = []
    provider_state = _provider_state(driver, "temporal")
    generation = int(provider_state.get("generation") or 0)
    eligible, _ = _baseline_eligible(
        baseline,
        driver=driver,
        generation=generation,
        max_age_ms=TEMPORAL_BASELINE_MAX_AGE_MS,
    )
    baseline_reused = bool(eligible and baseline is not None)
    if baseline_reused and baseline is not None:
        packages.append(baseline)

    last_metadata: dict[str, Any] = {}
    failed_stage: str | None = None
    captures_needed = max(0, request.frames - len(packages))
    sample_interval_s = request.duration_ms / 1000.0 / max(1, request.frames - 1)
    for _ in range(captures_needed):
        if packages:
            await asyncio.sleep(sample_interval_s)
        try:
            deadline.remaining_seconds("capture")
            package, last_metadata = await _capture(driver, builder, deadline)
        except ObservationStageError as exc:
            if not packages:
                raise
            failed_stage = exc.stage
            break

        package_generation = int(last_metadata.get("generation") or generation)
        if packages:
            first_generation = int(
                getattr(packages[0], "_observation_generation", package_generation) or 0
            )
            first_geometry = (
                packages[0].frame_width,
                packages[0].frame_height,
                packages[0].rotation_degrees,
            )
            current_geometry = (
                package.frame_width,
                package.frame_height,
                package.rotation_degrees,
            )
            if (
                (first_generation and package_generation
                 and first_generation != package_generation)
                or first_geometry != current_geometry
            ):
                packages = []
                baseline_reused = False
        packages.append(package)

    # Equal pixels at distinct capture times are valid evidence that no visible
    # change occurred. Preserve the samples; semantic comparison belongs to the model.
    ending = packages[-1] if packages else None
    complete = len(packages) >= request.frames
    if complete:
        status = "complete"
    elif ending is not None:
        status = "degraded_current"
    else:
        status = "temporal_unavailable"
    selected_provider = str(
        last_metadata.get("provider")
        or ("invocation_baseline" if ending is not None else "unselected")
    )
    return TemporalObservationResult(
        status=status, frames=packages, ending=ending,
        provider=selected_provider,
        provider_status="baseline_reused" if baseline_reused else "bounded_sampling",
        provider_generation=int(last_metadata.get("generation") or generation),
        provider_state=last_metadata.get("provider_state") or provider_state,
        fallback_edges=list(last_metadata.get("fallback_edges") or []),
        baseline_reused=baseline_reused,
        temporal_complete=complete,
        failed_stage=None if complete else (
            failed_stage or "insufficient_temporal_frames"
        ),
    )


async def _stream_temporal(
    driver: Any, builder: ObservationBuilder, request: TemporalObservationRequest,
    deadline: ObservationDeadline,
) -> TemporalObservationResult | None:
    """Agent-side adapter for the driver-neutral scrcpy temporal contract."""
    sampler = getattr(driver, "sample_stream_temporal", None)
    query = getattr(driver, "query_stream_temporal", None)
    if not callable(sampler) and not callable(query):
        return None
    start = time.monotonic()
    end = start + request.duration_ms / 1000
    if callable(sampler):
        result = await sampler(start, end, request.frames, deadline)
    else:
        result = query(start, end, request.frames)
    handles = getattr(result, "frames", ()) if result is not None else ()
    if not handles:
        return None
    ending_budget_ms = max(1, int(deadline.remaining_ms))
    ending = await ActionObservationTransaction(
        driver,
        builder,
        current_deadline_ms=ending_budget_ms,
    ).observe_current(
        deadline_ms=ending_budget_ms,
        source="observe_screen_temporal_ending",
        attach_image=True,
    )
    if not ending.accepted or not (ending.ui.app_id or "").strip():
        return None
    ending_capture = dict(ending.capture_meta or {})
    ending_generation = int(ending_capture.get("generation") or 0)
    handle_generation = int(getattr(handles[-1], "generation", 0) or 0)
    if (
        str(ending_capture.get("pixel_provider") or "") != "scrcpy"
        or not ending_generation
        or ending_generation != handle_generation
    ):
        return None
    packages: list[ObservationPackage] = []
    for handle in list(handles)[:max(0, request.frames - 1)]:
        decode_started = time.monotonic()
        decode_budget_s = deadline.remaining_seconds("decode")
        try:
            geometry = handle.geometry
            crop_box = None
            if geometry.crop_width and geometry.crop_height:
                crop_box = (
                    float(geometry.crop_left),
                    float(geometry.crop_top),
                    float(geometry.crop_left + geometry.crop_width),
                    float(geometry.crop_top + geometry.crop_height),
                )
            temporal_tree = {
                "class": "Root",
                "children": [],
                "_capture": {
                    "complete": False,
                    "tree_providers_exhausted": True,
                    "pixel_provider": "scrcpy",
                    "pixel_monotonic_ms": float(handle.timestamp) * 1000.0,
                    "coherence_status": "temporal_evidence_only",
                    "coordinate_compatible": False,
                    "generation": int(handle.generation),
                },
            }
            package = builder.build(
                (temporal_tree, handle.data),
                # Ring frames predate the ending transaction's exact OS
                # identity read. They remain non-actionable visual history and
                # must not impersonate the ending App/activity.
                app_id="",
                activity="",
                will_send_image=True,
                frame_geometry=(geometry.device_width, geometry.device_height),
                rotation_degrees=geometry.rotation,
                crop_box=crop_box,
            )
        except Exception as exc:  # noqa: BLE001
            deadline.record("decode", decode_started, budget_s=decode_budget_s, outcome="error")
            raise ObservationStageError("decode", str(exc)) from exc
        deadline.record("decode", decode_started, budget_s=decode_budget_s)
        package.captured_monotonic_ms = float(handle.timestamp) * 1000.0
        package.actionable = False
        package.index_actionable = False
        package.accepted = True
        package.acceptance_reason = "temporal_evidence_only"
        setattr(package, "_observation_device", str(getattr(driver, "serial", "") or ""))
        setattr(package, "_observation_generation", int(handle.generation))
        setattr(package, "_observation_complete", False)
        packages.append(package)
    packages.append(ending)
    state = _provider_state(driver, "temporal")
    complete = (
        getattr(result, "status", "partial") == "ok"
        and len(packages) >= request.frames
    )
    return TemporalObservationResult(
        status="ok" if complete else "partial", frames=packages, ending=ending,
        provider="scrcpy", provider_status=getattr(result, "status", "partial"),
        provider_generation=handle_generation,
        provider_state=state,
        temporal_complete=complete,
        failed_stage=None if complete else "temporal_completeness",
    )


def make_observe_screen_handler(
    *, driver: Any, builder: ObservationBuilder, artifacts: Any | None,
    baseline_package: ObservationPackage,
):
    async def observe_screen(args: dict[str, Any], context: ToolExecutionContext) -> AgentToolResult:
        mode = str(args.get("mode") or "")
        extras = set(args) - {"mode", "frames", "duration_ms"}
        if mode not in {"current", "temporal"} or extras:
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS,
                summary="mode must be current|temporal and only documented global arguments are allowed",
                error="invalid_arguments",
            )
        if mode == "current":
            # Some OpenAI-compatible providers materialize optional schema
            # defaults even for the other union branch. Current mode owns no
            # temporal semantics, so normalize those values away.
            args = {"mode": "current"}
        context.state["_observe_attempt_count"] = int(
            context.state.get("_observe_attempt_count") or 0
        ) + 1
        attempt_number = int(context.state["_observe_attempt_count"])
        evidence_ref = f"evidence_{uuid.uuid4().hex}"
        if mode == "current":
            initial_provider_state = _provider_state(driver, mode)
            baseline_reused = False
            try:
                transaction = ActionObservationTransaction(
                    driver, builder, current_deadline_ms=CURRENT_DEADLINE_MS,
                )
                package = await transaction.observe_current(
                    deadline_ms=CURRENT_DEADLINE_MS,
                    source="observe_screen_current",
                    attach_image=True,
                )
                capture_meta = dict(package.capture_meta or {})
                capture_meta.setdefault("provider_state", initial_provider_state)
                capture_meta.setdefault("provider", package.ui.capture_provider)
                capture_meta.setdefault("tree_provider", package.ui.capture_provider)
                capture_meta.setdefault("pixel_provider", "driver")
                capture_meta.setdefault("coordinate_compatible", package.accepted)
                capture_meta.setdefault("complete", package.ui.capture_complete)
            except ObservationStageError as exc:
                return AgentToolResult(
                    status=ToolStatus.TIMEOUT if exc.timed_out else ToolStatus.UNAVAILABLE,
                    summary=f"current observation failed at {exc.stage}: {exc.reason}",
                    data={
                        "mode": mode,
                        "status": "timeout" if exc.timed_out else "degraded_current",
                        "attempt": attempt_number,
                        "failed_stage": exc.stage,
                        "frame_count": 0,
                        "accepted": False,
                        "gap_reason": exc.reason,
                    },
                    provider_status=f"failed:{exc.stage}",
                    error="timeout" if exc.timed_out else "degraded_current",
                )
            except Exception as exc:  # noqa: BLE001
                return AgentToolResult(
                    status=ToolStatus.FAILED,
                    summary=f"current observation failed at capture: {exc}",
                    data={
                        "mode": mode, "status": "provider_unhealthy",
                        "attempt": attempt_number,
                        "failed_stage": "capture",
                        "frame_count": 0,
                        "accepted": False,
                        "gap_reason": str(exc)[:200],
                    },
                    provider_status="failed:capture", error=str(exc)[:500],
                )
            package.evidence_ref = evidence_ref
            role_visual_metadata = _prepare_role_model_image(context, package)
            model_size = _register_package(
                context, package,
                actionable=bool(package.accepted and package.actionable),
            )
            _persist_package(package, artifacts)
            if package.accepted:
                context.state["active_package"] = package
                context.state["visual_evidence_metadata"] = role_visual_metadata
                if not package.actionable:
                    registry = context.state.get("observation_registry")
                    if isinstance(registry, ObservationRegistry):
                        registry.clear_active()
            refs = [ref for ref in (package.som_ref, package.tree_ref) if ref]
            attachments = []
            if package.image_for_llm is not None:
                attachments.append(_attachment(
                    package, label="current/end", kind="image",
                    content=package.image_for_llm,
                    artifact_ref=package.som_ref,
                    actionable=bool(package.accepted and package.actionable),
                ))
            attachments.append(_attachment(
                    package, label="current global tree", kind="text",
                    content=package.text_for_llm, artifact_ref=package.tree_ref,
                    actionable=bool(package.accepted and package.actionable),
                ))
            return AgentToolResult(
                status=(ToolStatus.SUCCEEDED if package.accepted else ToolStatus.UNAVAILABLE),
                summary=(
                    "captured one fresh current frame and aligned numbered targets"
                    if package.accepted and package.index_actionable
                    else "captured one fresh current tree and image; no numbered targets are present"
                    if package.accepted and package.mode == ObservationMode.TREE_PLUS_IMAGE
                    else "captured one fresh current image; accessibility tree is unavailable"
                    if package.accepted
                    else f"fresh current observation was not actionable: {package.acceptance_reason}"
                ),
                data={
                    "mode": "current",
                    "status": (
                        "complete"
                        if package.accepted and package.mode != ObservationMode.IMAGE_ONLY
                        else "image_only" if package.accepted
                        else "degraded_current"
                    ),
                    "attempt": attempt_number,
                    "frame_count": 1,
                    "foreground_app_id": (package.ui.app_id or "").strip(),
                    "evidence_tier": capture_meta.get("evidence_tier"),
                    "tree_available": package.mode != ObservationMode.IMAGE_ONLY,
                    "image_available": bool(package.image_for_llm),
                    "observation_id": package.observation_id,
                    "coordinate_space_id": package.coordinate_space_id,
                    "image_size": list(model_size),
                    "frame_geometry": [package.frame_width, package.frame_height],
                    "transform_id": package.transform_id,
                    "actionable": bool(package.accepted and package.actionable),
                    "index_actionable": package.index_actionable,
                    "accepted": package.accepted,
                    "acceptance_reason": package.acceptance_reason,
                    "gap_reason": (
                        package.gap_reasons[0] if package.gap_reasons else ""
                    ),
                    "coordinate_reference": "current/end",
                },
                attachments=attachments,
                replacement_attachments=(attachments if package.accepted else []),
                evidence=EvidenceRecord(
                    evidence_ref=evidence_ref, observation_id=package.observation_id,
                    artifact_refs=refs,
                ),
                evidence_refs=[evidence_ref], artifact_refs=refs,
                actionable_observation_id=(
                    package.observation_id
                    if package.accepted and package.actionable else None
                ),
                provider_status=str(capture_meta.get("provider") or "current_frame"),
                error=None if package.accepted else package.acceptance_reason,
            )

        deadline = ObservationDeadline(mode, TEMPORAL_DEADLINE_MS)
        try:
            frames = int(args.get("frames", 2))
            duration_ms = int(args.get("duration_ms", OBSERVE_SCREEN_DEFAULT_DURATION_MS))
        except (TypeError, ValueError):
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS, summary="frames/duration_ms must be integers",
                error="invalid_arguments",
            )
        frames = 2 if frames < 3 else 3
        duration_ms = max(
            OBSERVE_SCREEN_MIN_DURATION_MS,
            min(duration_ms, OBSERVE_SCREEN_MAX_DURATION_MS),
        )
        request = TemporalObservationRequest(frames=frames, duration_ms=duration_ms)
        provider = getattr(driver, "observe_temporal", None)
        try:
            if callable(provider):
                provider_started = time.monotonic()
                provider_budget_s = deadline.remaining_seconds("provider")
                try:
                    temporal = await asyncio.wait_for(provider(request), timeout=provider_budget_s)
                except asyncio.TimeoutError as exc:
                    deadline.record(
                        "provider", provider_started, budget_s=provider_budget_s,
                        outcome="timeout", timed_out=True,
                    )
                    raise ObservationStageError(
                        "provider", "temporal provider timed out",
                        budget_ms=provider_budget_s * 1000, timed_out=True,
                    ) from exc
                deadline.record("provider", provider_started, budget_s=provider_budget_s)
            else:
                temporal = await _stream_temporal(
                    driver, builder, request, deadline,
                )
                if temporal is None:
                    temporal = await _fallback_temporal(
                        driver, builder, request, baseline_package, deadline,
                    )
        except ObservationStageError as exc:
            return AgentToolResult(
                status=ToolStatus.TIMEOUT if exc.timed_out else ToolStatus.UNAVAILABLE,
                summary=f"temporal observation failed at {exc.stage}: {exc.reason}",
                data={
                    "mode": mode,
                    "status": "timeout" if exc.timed_out else "temporal_unavailable",
                    "frame_count": 0,
                    "gap_reason": f"{exc.stage}:{exc.reason}",
                },
                provider_status=f"failed:{exc.stage}",
                error="timeout" if exc.timed_out else "temporal_unavailable",
            )
        except Exception as exc:  # noqa: BLE001
            return AgentToolResult(
                status=ToolStatus.FAILED,
                summary=f"temporal observation failed at provider: {exc}",
                data={
                    "mode": mode, "status": "provider_unhealthy",
                    "frame_count": 0, "gap_reason": str(exc)[:200],
                },
                provider_status="failed:provider", error=str(exc)[:500],
            )
        if temporal.ending is None:
            return AgentToolResult(
                status=ToolStatus.UNAVAILABLE,
                summary="temporal observation unavailable",
                data={
                    "mode": "temporal", "status": "temporal_unavailable",
                    "frame_count": 0,
                    "gap_reason": temporal.failed_stage or "provider_unavailable",
                },
                provider_status=temporal.provider_status or temporal.status,
                error="temporal_unavailable",
            )
        # Providers must identify every temporal frame independently. Repair a
        # provider that reused the same package object/ID without changing the
        # captured evidence itself.
        normalized_frames: list[ObservationPackage] = []
        seen_observation_ids: set[str] = set()
        for package in temporal.frames:
            if package.observation_id in seen_observation_ids:
                package = replace(
                    package,
                    observation_id=f"obs_{uuid.uuid4().hex}",
                    coordinate_space_id=f"space_{uuid.uuid4().hex}",
                    transform_id="",
                )
            seen_observation_ids.add(package.observation_id)
            normalized_frames.append(package)
        temporal.frames = normalized_frames
        temporal.ending = normalized_frames[-1] if normalized_frames else None
        temporal.temporal_complete = bool(
            temporal.temporal_complete or temporal.status in {"complete", "ok"}
        )
        ending = temporal.ending
        assert ending is not None
        ending_capture = dict(ending.capture_meta or {})
        ending.evidence_ref = evidence_ref
        temporal_image_tokens = 0
        temporal_visual_metadata: list[dict[str, Any]] = []
        for index, package in enumerate(temporal.frames):
            is_end = package is ending or index == len(temporal.frames) - 1
            frame_visual_metadata = _prepare_role_model_image(context, package)
            temporal_visual_metadata.append(frame_visual_metadata)
            if isinstance(frame_visual_metadata, dict):
                try:
                    temporal_image_tokens += int(
                        frame_visual_metadata.get("estimated_image_tokens") or 0
                    )
                except (TypeError, ValueError):
                    pass
            _register_package(
                context, package, actionable=is_end, make_active=False,
            )
            _persist_package(package, artifacts)
        temporal_images_valid = bool(
            len(temporal.frames) >= 2
            and all(package.image_for_llm is not None for package in temporal.frames)
        )
        ending_valid = bool(ending.accepted)
        if ending_valid and temporal_images_valid:
            context.state["active_package"] = ending
            context.state["visual_evidence_metadata"] = {
                **temporal_visual_metadata[-1],
                "temporal_frame_count": len(temporal.frames),
                "estimated_image_tokens": temporal_image_tokens,
            }
            if ending.actionable:
                _register_package(
                    context, ending, actionable=True, make_active=True,
                )
            else:
                registry = context.state.get("observation_registry")
                if isinstance(registry, ObservationRegistry):
                    registry.clear_active()
        refs = [
            ref for package in temporal.frames for ref in (package.som_ref, package.tree_ref) if ref
        ]
        attachments: list[ToolAttachment] = []
        labels = ["start", "middle", "end"] if len(temporal.frames) == 3 else ["start", "end"]
        for index, package in enumerate(temporal.frames):
            is_end = package is ending or index == len(temporal.frames) - 1
            if package.image_for_llm is not None:
                attachments.append(_attachment(
                    package, label=labels[min(index, len(labels) - 1)], kind="image",
                    content=package.image_for_llm,
                    artifact_ref=package.som_ref,
                    actionable=bool(is_end and package.actionable),
                ))
        attachments.append(_attachment(
            ending, label="ending global tree", kind="text", content=ending.text_for_llm,
            artifact_ref=ending.tree_ref, actionable=ending.actionable,
        ))
        observation_available = bool(temporal_images_valid and ending_valid)
        return AgentToolResult(
            status=(ToolStatus.SUCCEEDED if observation_available else ToolStatus.UNAVAILABLE),
            summary=f"captured {len(temporal.frames)} ordered global temporal frames",
            data={
                "mode": "temporal", "status": temporal.status,
                "frame_count": len(temporal.frames),
                "requested_frames": frames, "duration_ms": duration_ms,
                "foreground_app_id": (ending.ui.app_id or "").strip(),
                "evidence_tier": ending_capture.get("evidence_tier"),
                "gap_reason": (
                    ending.gap_reasons[0] if ending.gap_reasons else ""
                ),
                "observation_id": ending.observation_id,
                "coordinate_space_id": ending.coordinate_space_id,
                "image_size": [ending.model_image_width, ending.model_image_height],
                "frame_geometry": [ending.frame_width, ending.frame_height],
                "frames": [{
                    "label": labels[min(index, len(labels) - 1)],
                    "observation_id": package.observation_id,
                    "image_size": [package.model_image_width, package.model_image_height],
                    "actionable": bool((package is ending or index == len(temporal.frames) - 1) and package.actionable),
                } for index, package in enumerate(temporal.frames)],
                "coordinate_reference": "end",
                "index_actionable": ending.index_actionable,
            },
            attachments=attachments,
            # The ending frame is rebuilt as the sole canonical current O by
            # Decision Context v2. Only ordered, non-actionable prior frames
            # supplement that O; trace attachments retain the full sequence.
            replacement_attachments=(
                [
                    attachment for attachment in attachments
                    if attachment.kind == "image"
                    and attachment.observation_id in {
                        package.observation_id for package in temporal.frames[:-1]
                    }
                ]
                if observation_available else []
            ),
            evidence=EvidenceRecord(
                evidence_ref=evidence_ref, observation_id=ending.observation_id,
                status=temporal.status, provider=temporal.provider, artifact_refs=refs,
            ),
            evidence_refs=[evidence_ref], artifact_refs=refs,
            actionable_observation_id=(
                ending.observation_id
                if observation_available and ending.actionable else None
            ),
            provider_status=temporal.provider_status or temporal.status,
            error=None if observation_available else "temporal_role_evidence_unavailable",
        )

    return observe_screen


def make_search_installed_apps_handler(
    *, driver: Any, ticket_store: ResolverTicketStore,
):
    async def search(args: dict[str, Any], context: ToolExecutionContext) -> AgentToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS, summary="query is required",
                error="missing_query",
            )
        validation = ticket_store.consume(
            str(args.get("resolution_ticket") or ""),
            task_id=str(context.state.get("task_id") or ""),
            device_id=str(context.state.get("device_id") or ""),
            subgoal_id=str(context.state.get("subgoal_id") or ""),
        )
        context.state.setdefault("ticket_lifecycle", []).append(validation.model_dump())
        if not validation.accepted:
            return AgentToolResult(
                status=ToolStatus.PRECONDITION_NOT_MET,
                summary=(
                    "precondition_not_met: installed-app search requires the matching "
                    f"unexpired resolver-miss ticket ({validation.reason})"
                ),
                data={
                    "reason": validation.reason,
                    "ticket_fingerprint": validation.ticket_fingerprint,
                    "enumerated": False,
                },
                error="precondition_not_met",
            )
        method = getattr(driver, "search_installed_apps", None)
        if not callable(method):
            return AgentToolResult(
                status=ToolStatus.UNAVAILABLE, summary="installed-app discovery unavailable",
                provider_status="driver_unsupported",
            )
        candidates = await method(query, limit=INSTALLED_APP_CANDIDATE_LIMIT)
        candidates = list(candidates or [])[:INSTALLED_APP_CANDIDATE_LIMIT]
        context.state["installed_app_query"] = query
        context.state["installed_app_candidates"] = [str(c.get("package") or "") for c in candidates]
        return AgentToolResult(
            summary=f"found {len(candidates)} installed app candidates",
            data={
                "query": query,
                "candidates": candidates,
                "bounded": True,
                "ticket_fingerprint": validation.ticket_fingerprint,
            },
            provider_status="installed_packages",
        )

    return search
