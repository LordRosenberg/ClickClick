"""One readable action -> effect -> observation lifecycle.

The coordinator deliberately owns only lifecycle and acceptance. Android
transport/provider fallback stays in the driver, while model and app business
rules stay in prompts/skills.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import os
import time
import uuid
from typing import Any

from perception.normalizer import (
    tree_ownership_facts,
    top_window_identity,
)
from perception.filters import DetailedFilter
from perception.observation import (
    ObservationBuilder,
    ObservationPackage,
    decoded_image_size,
    mechanical_tree_usable,
)
from driver.observation_deadline import (
    CURRENT_DEADLINE_MS,
    ObservationDeadline,
    ObservationStageError,
)
from shared.schemas import (
    Action,
    ActionReceipt,
    ActionResult,
    ActionTargetSnapshot,
    CanonicalUI,
    EffectClass,
    EffectOutcome,
    ObservationMode,
)


DEFAULT_CURRENT_DEADLINE_MS = CURRENT_DEADLINE_MS
DEFAULT_EFFECT_DEADLINE_MS = CURRENT_DEADLINE_MS
MAX_CAPTURE_ATTEMPTS = 2
DEFAULT_RESAMPLE_SETTLE_MS = 1_000
RESAMPLE_SETTLE_ENV = "CLICKCLICK_OBSERVATION_RESAMPLE_SETTLE_MS"
INTERACTION_ACK_ACTION_EVENTS = {
    "tap_xy": frozenset({1}),
    "long_press": frozenset({2}),
    "scroll": frozenset({4096}),
    "swipe": frozenset({4096}),
}
INTERACTION_ACK_BOUNDS_TOLERANCE_PX = 4
INTERACTION_EVENT_READS_ENV = "CLICKCLICK_INTERACTION_EVENT_READS_ENABLED"


def _interaction_event_reads_enabled() -> bool:
    return os.getenv(INTERACTION_EVENT_READS_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def _bounded_identity_equal(left: str, right: str) -> bool:
    left, right = left.strip(), right.strip()
    if not left or not right:
        return True
    return left == right or left.endswith(right) or right.endswith(left)


def _event_matches_target(event: Any, target: ActionTargetSnapshot) -> bool:
    if target.package and str(getattr(event, "package", "")) != target.package:
        return False
    event_window = int(getattr(event, "window_id", -1))
    if target.window_id is not None and event_window >= 0 and event_window != target.window_id:
        return False
    if not _bounded_identity_equal(
        str(getattr(event, "resource_id", "")), target.resource_id,
    ):
        return False
    if not _bounded_identity_equal(
        str(getattr(event, "source_class", "")), target.source_class,
    ):
        return False
    event_bounds = getattr(event, "bounds", None)
    if len(target.bounds) == 4 and event_bounds is not None:
        if len(event_bounds) != 4 or any(
            abs(int(observed) - int(expected)) > INTERACTION_ACK_BOUNDS_TOLERANCE_PX
            for observed, expected in zip(event_bounds, target.bounds, strict=True)
        ):
            return False
    return True


def interaction_ack_for_batch(
    action: Action,
    target: ActionTargetSnapshot | None,
    batch: Any | None,
) -> tuple[str, str | None]:
    """Return a non-semantic acknowledgement from one complete bounded read."""
    eligible_types = INTERACTION_ACK_ACTION_EVENTS.get(action.type)
    if eligible_types is None or target is None or batch is None:
        return "unavailable", None
    if not bool(getattr(batch, "complete_coverage", False)):
        return "unavailable", None
    for event in tuple(getattr(batch, "events", ())):
        if (
            int(getattr(event, "event_type", -1)) in eligible_types
            and _event_matches_target(event, target)
        ):
            return "confirmed", "accessibility_event"
    return "unobserved", None
def unchanged_post_action_observation(
    before: ObservationPackage,
    after: ObservationPackage,
) -> bool:
    """True only when accepted tree text and source pixels are byte-identical.

    Any missing, degraded, or differing tree/image is treated as uncertain.
    Callers must not report a UI change or success from a false result.
    """
    if not before.accepted or not after.accepted:
        return False
    before_tree = (before.text_for_llm or "").strip()
    after_tree = (after.text_for_llm or "").strip()
    if not before_tree or before_tree != after_tree:
        return False
    if (before.ui.app_id or "").strip() != (after.ui.app_id or "").strip():
        return False
    before_kind, before_pixels = _source_pixels(before)
    after_kind, after_pixels = _source_pixels(after)
    return (
        before_kind is not None
        and before_kind == after_kind
        and bool(before_pixels)
        and before_pixels == after_pixels
    )


def _source_pixels(package: ObservationPackage) -> tuple[str | None, bytes]:
    if package.clean_png:
        return "clean", package.clean_png
    if package.annotated_png:
        return "annotated", package.annotated_png
    return None, b""


# Two 50/50-valid project-device probes observed a 788.431 ms worst tail.
# A 50% scheduling margin gives 1,182.647 ms, rounded upward. Keep this bound
# independent of tree/pixel acquisition; the outer observation fuse remains
# authoritative and empty/conflicting identity still fails closed.
FOREGROUND_IDENTITY_ATTEMPT_TIMEOUT_MS = 1_200


@dataclass(frozen=True)
class MechanicalComponentFacts:
    """Typed, model-free evidence facts shared by retry and acceptance."""

    tree_usable: bool
    pixels_usable: bool

    def evidence_tier(self, package: ObservationPackage) -> str:
        if self.tree_usable and self.pixels_usable:
            return (
                "indexed_tree_image"
                if package.index_actionable else "nonindexed_tree_image"
            )
        if self.tree_usable:
            return "tree_only"
        if self.pixels_usable:
            return "image_only"
        return "unavailable"


def _mechanical_component_facts(
    package: ObservationPackage,
) -> MechanicalComponentFacts:
    """Normalize component flags once so all lifecycle decisions agree."""
    meta = package.capture_meta or {}
    tree_usable = mechanical_tree_usable(meta, package.gap_reasons)
    pixel_width, pixel_height = decoded_image_size(package.clean_png)
    pixels_usable = bool(
        pixel_width > 0
        and pixel_height > 0
        and package.frame_width > 0
        and package.frame_height > 0
        and str(meta.get("pixel_provider") or "")
        in {"scrcpy", "adb_screencap", "driver_get_frame"}
    )
    return MechanicalComponentFacts(
        tree_usable=tree_usable,
        pixels_usable=pixels_usable,
    )


def _closed_world_identity_timeout(
    error: ObservationStageError,
    attempt: dict[str, Any],
) -> bool:
    """True when an identity phase timed out without any completed identity facts."""
    return bool(
        error.stage == "foreground_identity"
        and error.reason == "identity_request_timeout"
        and attempt.get("timed_out") is True
        and not attempt.get("package")
        and not attempt.get("component")
        and not attempt.get("conflict")
        and not attempt.get("sources")
    )


def _retryable_stage_failure(error: ObservationStageError) -> bool:
    """Return whether one fresh full capture can replace unstable evidence."""
    return error.stage in {
        "accessibility_tree",
        "alignment",
        "foreground_identity",
        "grounding_barrier",
        "pixel_capture",
        "screencap",
    }


def configured_resample_settle_ms() -> int:
    """Read one externally bindable cross-App settle candidate."""
    raw = os.getenv(RESAMPLE_SETTLE_ENV, "").strip()
    if not raw:
        return DEFAULT_RESAMPLE_SETTLE_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RESAMPLE_SETTLE_MS
    return value if 0 <= value <= 5_000 else DEFAULT_RESAMPLE_SETTLE_MS


def _mechanical_resample_reason(package: ObservationPackage) -> str:
    """Name the finite first-capture fact that merits one full resample."""
    meta = package.capture_meta or {}
    if not (package.ui.app_id or "").strip():
        return "foreground_identity_unavailable"
    if bool(meta.get("foreground_identity_conflict")):
        return "foreground_identity_conflict"
    ownership = str(meta.get("tree_ownership_status") or "")
    if ownership in {"conflict", "ambiguous"}:
        return f"tree_ownership_{ownership}"
    components = _mechanical_component_facts(package)
    if not components.tree_usable and not components.pixels_usable:
        return "tree_unavailable"
    if not components.pixels_usable:
        return "pixels_unavailable"
    return ""


def _positive_pair(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    width, height = value
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or min(width, height) <= 0
    ):
        return None
    return width, height


def _evidence_tier(package: ObservationPackage) -> str:
    return _mechanical_component_facts(package).evidence_tier(package)


def effect_class_for(action: Action) -> EffectClass:
    """Map actions to a small app-independent completion policy."""
    if action.type in {"type", "replace_text"}:
        return EffectClass.TEXT_INPUT
    if action.type in {"swipe", "scroll", "drag", "long_press"}:
        return EffectClass.GESTURE
    if action.type in {"tap", "tap_xy", "key", "launch", "back", "home"}:
        return EffectClass.UI_TRANSITION
    return EffectClass.NONE


def effect_deadline_ms_for(action: Action) -> int:
    """Bound the shared post-dispatch settle and observation transaction."""
    if action.type == "launch":
        return CURRENT_DEADLINE_MS + 3_500
    # Dispatch completes before this fuse starts. The generic transition
    # settle and one current observation share it; no semantic effect-class
    # timing policy is added here.
    return DEFAULT_EFFECT_DEADLINE_MS


def suppressed_action_result(action: Action, reason: str) -> ActionResult:
    """Represent a rejected dispatch explicitly; never manufacture success."""
    now_ms = time.monotonic() * 1000.0
    receipt = ActionReceipt(
        transaction_id=f"txn_{uuid.uuid4().hex}",
        effect_class=effect_class_for(action),
        dispatch_succeeded=False,
        effect_outcome=EffectOutcome.SUPPRESSED,
        effect_reason=reason,
        started_monotonic_ms=now_ms,
        dispatch_completed_monotonic_ms=now_ms,
        completed_monotonic_ms=now_ms,
    )
    return ActionResult(
        success=False,
        message=reason,
        detail={
            "dispatch_suppressed": True,
            "recoverable": True,
            "effect_outcome": EffectOutcome.SUPPRESSED.value,
            "effect_reason": reason,
            "transaction_id": receipt.transaction_id,
        },
        receipt=receipt,
    )


def observation_acceptance_reason(package: ObservationPackage) -> str:
    """Return empty when at least one mechanically safe evidence tier exists."""
    components = _mechanical_component_facts(package)
    if not (package.ui.app_id or "").strip():
        return "foreground_identity_unavailable"
    if not components.tree_usable and not components.pixels_usable:
        return "grounding_evidence_unavailable"
    if package.mode == ObservationMode.IMAGE_ONLY:
        return "" if components.pixels_usable else "image_only_pixels_unavailable"
    if not components.tree_usable:
        return "capture_incomplete"
    return ""


def _launch_target_package(action: Action, result: ActionResult) -> str:
    resolved = str((result.detail or {}).get("resolved_package") or "").strip()
    if resolved:
        return resolved
    requested = (action.app or "").strip()
    return requested if "." in requested else ""


def _pre_sanitization_facts(
    package: ObservationPackage, *, reason: str,
) -> dict[str, Any]:
    """Retain bounded mechanical facts without copying raw tree or pixels."""
    meta = package.capture_meta or {}
    return {
        "rejection_reason": reason,
        "foreground_app_id": (package.ui.app_id or "").strip(),
        "evidence_tier": _evidence_tier(package),
        "tree_complete": meta.get("complete"),
        "tree_ownership_status": meta.get("tree_ownership_status"),
        "coordinate_compatible": meta.get("coordinate_compatible"),
        "pixel_provider": meta.get("pixel_provider"),
        "decoded_image_geometry": list(decoded_image_size(package.clean_png)),
        "frame_geometry": list(_positive_pair(meta.get("frame_geometry")) or ()),
        "capture_ordinal": meta.get("active_capture_ordinal"),
        "resample_trigger": meta.get("resample_trigger"),
    }


def _sanitize_rejected_observation(
    package: ObservationPackage, *, reason: str,
) -> ObservationPackage:
    """Retain capture diagnostics without exposing rejected evidence as a basis."""
    gap_reasons = list(package.gap_reasons)
    if reason not in gap_reasons:
        gap_reasons.append(reason)
    capture_meta = dict(package.capture_meta or {})
    capture_meta["pre_sanitization_facts"] = _pre_sanitization_facts(
        package, reason=reason,
    )
    return replace(
        package,
        ui=CanonicalUI(platform=package.ui.platform),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=f"[observation rejected: {reason}]",
        image_for_llm=None,
        annotated_png=None,
        clean_png=None,
        gap_reasons=gap_reasons,
        som_ref=None,
        tree_ref=None,
        actionable=False,
        index_actionable=False,
        frame_width=0,
        frame_height=0,
        model_image_width=0,
        model_image_height=0,
        transform_id="",
        evidence_ref=None,
        capture_meta=capture_meta,
        accepted=False,
        acceptance_reason=reason,
    )


def degraded_post_observation(
    before: ObservationPackage,
    *,
    transaction_id: str,
    source: str,
    error: Exception,
) -> ObservationPackage:
    """Return explicit evidence-only state when post-action capture yields no package."""
    if isinstance(error, ObservationStageError):
        reason = f"{error.stage}:{error.reason}"
        capture_meta: dict[str, Any] = {
            "complete": False,
            "error_stage": error.stage,
            "error_reason": error.reason,
            "timed_out": error.timed_out,
            "fallback_edges": list(error.fallback_edges),
            "provider_attempts": list(error.provider_attempts),
            "cancelled_capture_tasks": list(error.cancelled_tasks),
            "observation_capture_attempt_count": max(
                0, int(error.capture_attempt_count)
            ),
        }
    else:
        reason = str(error) or "capture_timeout"
        capture_meta = {
            "complete": False,
            "error_stage": "capture",
            "error_reason": reason,
            "timed_out": isinstance(error, TimeoutError),
        }
    return replace(
        before,
        ui=CanonicalUI(platform=before.ui.platform),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=f"[post-action observation unavailable: {reason}]",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[reason],
        clean_png=None,
        som_ref=None,
        tree_ref=None,
        observation_id=f"obs_{uuid.uuid4().hex}",
        coordinate_space_id=f"space_{uuid.uuid4().hex}",
        actionable=False,
        index_actionable=False,
        captured_monotonic_ms=time.monotonic() * 1000.0,
        model_image_width=0,
        model_image_height=0,
        transform_id="",
        evidence_ref=None,
        capture_meta=capture_meta,
        transaction_id=transaction_id,
        accepted=False,
        acceptance_reason=reason,
        observation_source=source,
    )


class ActionObservationTransaction:
    """Coordinate fresh capture and bounded observable action completion."""

    def __init__(
        self,
        driver: Any,
        builder: ObservationBuilder,
        *,
        current_deadline_ms: int = DEFAULT_CURRENT_DEADLINE_MS,
        effect_deadline_ms: int | None = None,
        resample_settle_ms: int | None = None,
    ) -> None:
        self.driver = driver
        self.builder = builder
        self.current_deadline_ms = max(1, int(current_deadline_ms))
        self.effect_deadline_ms = (
            max(1, int(effect_deadline_ms)) if effect_deadline_ms is not None else None
        )
        self.resample_settle_ms = max(
            0,
            int(
                configured_resample_settle_ms()
                if resample_settle_ms is None else resample_settle_ms
            ),
        )

    async def _read_exact_identity(
        self,
        *,
        deadline: ObservationDeadline,
        phase: str,
        capture_ordinal: int,
        attempt_log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Read one exact foreground identity within the shared outer fuse."""
        started = time.monotonic()
        budget_ms = min(
            FOREGROUND_IDENTITY_ATTEMPT_TIMEOUT_MS,
            deadline.remaining_ms,
        )
        if budget_ms <= 0:
            raise ObservationStageError(
                "foreground_identity",
                "outer_deadline_exhausted",
                timed_out=True,
                provider_attempts=list(deadline.provider_attempts),
            )

        identity: dict[str, Any] = {}
        try:
            candidate = await asyncio.wait_for(
                self.driver.current_foreground_identity(
                    timeout_s=budget_ms / 1000.0,
                ),
                timeout=budget_ms / 1000.0,
            )
            identity = candidate if isinstance(candidate, dict) else {}
            timed_out = bool(identity.get("timed_out"))
            error = (
                "identity_request_timeout"
                if timed_out else str(identity.get("error") or "")
            )
        except asyncio.CancelledError:
            deadline.record_attempt({
                "provider": "foreground_identity",
                "status": "cancelled",
                "budget_ms": round(budget_ms, 3),
                "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
                "error": "request_cancelled",
                "phase": phase,
                "attempt": 1,
                "timed_out": False,
                "capture_ordinal": capture_ordinal,
            })
            deadline.cancelled_tasks.append(
                f"foreground_identity:{phase}:1"
            )
            raise
        except asyncio.TimeoutError:
            timed_out = True
            error = "identity_request_timeout"
        except Exception as exc:  # noqa: BLE001
            timed_out = False
            error = str(exc)[:200]

        detail = {
            "phase": phase,
            "attempt": 1,
            "capture_ordinal": capture_ordinal,
            "package": str(identity.get("package") or ""),
            "component": str(identity.get("component") or ""),
            "conflict": bool(identity.get("conflict")),
            "sources": list(identity.get("sources") or []),
            "error": error,
            "timed_out": timed_out,
        }
        attempt_log.append(detail)
        resolved = bool(
            detail["package"]
            and not detail["conflict"]
            and not timed_out
            and not error
        )
        deadline.record_attempt({
            **detail,
            "provider": "foreground_identity",
            "status": "ok" if resolved else "error",
            "budget_ms": round(budget_ms, 3),
            "elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "error": "" if resolved else error or "unresolved_or_conflicting",
        })
        if resolved:
            return identity
        raise ObservationStageError(
            "foreground_identity",
            "identity_request_timeout" if timed_out else "unresolved_or_conflicting",
            timed_out=timed_out,
            provider_attempts=list(deadline.provider_attempts),
        )

    async def _capture_once(
        self,
        transaction_id: str,
        source: str,
        *,
        attach_image: bool = False,
        capture_budget_ms: int | None = None,
        capture_deadline: ObservationDeadline | None = None,
        capture_ordinal: int = 1,
    ) -> ObservationPackage:
        requested_ms = time.monotonic() * 1000.0
        capture_deadline = capture_deadline or ObservationDeadline(
            source, capture_budget_ms or self.current_deadline_ms,
        )
        identity_attempts: list[dict[str, Any]] = []

        capture = getattr(self.driver, "capture_deadline_frame", None)

        async def capture_frame() -> tuple[dict[str, Any], bytes | None, dict[str, Any]]:
            metadata: dict[str, Any] = {}
            if callable(capture):
                tree, shot, metadata = await capture(capture_deadline)
                raw_capture = tree.setdefault("_capture", {})
                if isinstance(raw_capture, dict):
                    raw_capture.update(metadata)
                return tree, shot, metadata

            tree, shot = await self.driver.get_frame()
            captured_ms = time.monotonic() * 1000.0
            raw_capture = tree.setdefault("_capture", {})
            if isinstance(raw_capture, dict):
                raw_capture.setdefault("pixel_provider", "driver_get_frame")
                raw_capture.setdefault("pixel_monotonic_ms", captured_ms)
                raw_capture.setdefault("coherence_status", "driver_current")
            return tree, shot, metadata

        anterior_identity_timeout: ObservationStageError | None = None
        try:
            identity_before = await self._read_exact_identity(
                deadline=capture_deadline,
                phase="before_capture",
                capture_ordinal=capture_ordinal,
                attempt_log=identity_attempts,
            )
        except ObservationStageError as exc:
            anterior_attempt = identity_attempts[-1] if identity_attempts else {}
            if _closed_world_identity_timeout(exc, anterior_attempt):
                anterior_identity_timeout = exc
                identity_before = {}
            else:
                raise
        before_package = str(identity_before.get("package") or "").strip()
        before_component = str(identity_before.get("component") or "").strip()
        tree, shot, _metadata = await capture_frame()
        posterior_identity_timeout: ObservationStageError | None = None
        try:
            identity_after = await self._read_exact_identity(
                deadline=capture_deadline,
                phase="after_capture",
                capture_ordinal=capture_ordinal,
                attempt_log=identity_attempts,
            )
        except ObservationStageError as exc:
            posterior_attempt = (
                identity_attempts[-1] if identity_attempts else {}
            )
            if _closed_world_identity_timeout(exc, posterior_attempt):
                posterior_identity_timeout = exc
                identity_after = identity_before
            else:
                exc.provider_attempts = list(capture_deadline.provider_attempts)
                raise
        app_id = str(identity_after.get("package") or "").strip()
        activity = str(identity_after.get("activity") or "").strip()
        after_component = str(identity_after.get("component") or "").strip()
        posterior_only = bool(
            anterior_identity_timeout is not None
            and not before_package
            and app_id
            and posterior_identity_timeout is None
        )
        if (
            posterior_identity_timeout is None
            and before_package != app_id
            and not posterior_only
        ):
            capture_deadline.record_attempt({
                "provider": "grounding_barrier",
                "status": "error",
                "budget_ms": 0.0,
                "elapsed_ms": 0.0,
                "error": "foreground_changed_during_capture",
                "before_package": before_package,
                "after_package": app_id,
                "capture_ordinal": capture_ordinal,
            })
            raise ObservationStageError(
                "grounding_barrier",
                "foreground_changed_during_capture",
                provider_attempts=list(capture_deadline.provider_attempts),
            )
        if (
            posterior_identity_timeout is None
            and before_component
            and after_component
            and before_component != after_component
            and not posterior_only
        ):
            # A package can reuse the same application window while switching
            # activities. During that interval the collector tree, rendered
            # pixels, and post-capture activity can describe different pages.
            # Treat the exact component change as a mechanical transition so
            # observe_current performs its one bounded full resample.
            capture_deadline.record_attempt({
                "provider": "grounding_barrier",
                "status": "error",
                "budget_ms": 0.0,
                "elapsed_ms": 0.0,
                "error": "foreground_component_changed_during_capture",
                "before_package": before_package,
                "after_package": app_id,
                "before_component": before_component,
                "after_component": after_component,
                "capture_ordinal": capture_ordinal,
            })
            raise ObservationStageError(
                "grounding_barrier",
                "foreground_component_changed_during_capture",
                provider_attempts=list(capture_deadline.provider_attempts),
            )

        provider_attempts = list(capture_deadline.provider_attempts)
        top_window = top_window_identity(tree)
        top_window_package = str(top_window.get("package") or "")
        top_window_type = top_window.get("window_type")
        source_capture = tree.get("_capture") if isinstance(tree, dict) else {}
        source_capture = source_capture if isinstance(source_capture, dict) else {}
        geometry = source_capture.get("frame_geometry")
        screen = (
            (int(geometry[0]), int(geometry[1]))
            if isinstance(geometry, (list, tuple))
            and len(geometry) == 2
            and all(int(value) > 0 for value in geometry)
            else None
        )
        ownership = tree_ownership_facts(
            tree,
            foreground_package=app_id,
            tree_filter=(
                self.builder.detailed_filter
                or DetailedFilter(screen=screen)
            ),
        )
        tree_application_package = str(
            ownership["tree_application_package"] or ""
        )
        ownership_status = str(ownership["tree_ownership_status"])
        tree_providers_exhausted = bool(
            source_capture.get("tree_providers_exhausted")
        )
        tree_declares_usable = bool(
            mechanical_tree_usable(source_capture, ())
            and not tree_providers_exhausted
        )
        if posterior_only:
            capture_deadline.fallback_edges.append({
                "from": "foreground_identity:before_capture",
                "to": "posterior_identity",
                "reason": "identity_request_timeout",
                "capture_ordinal": str(capture_ordinal),
                "pre_package": before_package,
                "posterior_package": app_id,
            })
        if posterior_identity_timeout is not None:
            posterior_corroborated = bool(
                before_package
                and tree_declares_usable
                and ownership_status == "exact"
                and tree_application_package == before_package
            )
            both_timed_out = bool(
                anterior_identity_timeout is not None
                and not before_package
                and not app_id
            )
            tree_owner_ok = bool(
                both_timed_out
                and tree_declares_usable
                and tree_application_package
                and ownership_status in {"exact", "conflict"}
            )
            if posterior_corroborated:
                capture_deadline.fallback_edges.append({
                    "from": "foreground_identity:after_capture",
                    "to": "pre_identity+exact_tree_owner",
                    "reason": "identity_request_timeout",
                    "capture_ordinal": str(capture_ordinal),
                    "pre_package": before_package,
                    "tree_package": tree_application_package,
                    "tree_ownership_status": ownership_status,
                })
            elif tree_owner_ok:
                app_id = tree_application_package
                ownership = tree_ownership_facts(
                    tree,
                    foreground_package=app_id,
                    tree_filter=(
                        self.builder.detailed_filter
                        or DetailedFilter(screen=screen)
                    ),
                )
                tree_application_package = str(
                    ownership["tree_application_package"] or ""
                )
                ownership_status = str(ownership["tree_ownership_status"])
                capture_deadline.fallback_edges.append({
                    "from": "foreground_identity:before_capture",
                    "to": "exact_tree_owner",
                    "reason": "identity_request_timeout",
                    "capture_ordinal": str(capture_ordinal),
                    "tree_package": tree_application_package,
                    "tree_ownership_status": ownership_status,
                })
            else:
                timeout_error = posterior_identity_timeout or anterior_identity_timeout
                timeout_error.provider_attempts = list(
                    capture_deadline.provider_attempts
                )
                raise timeout_error
        if tree_declares_usable and ownership_status in {"conflict", "ambiguous"}:
            reason = (
                "foreground_tree_identity_ambiguous"
                if ownership_status == "ambiguous"
                else "foreground_tree_identity_conflict"
            )
            capture_deadline.record_attempt({
                "provider": "grounding_barrier",
                "status": "error",
                "budget_ms": 0.0,
                "elapsed_ms": 0.0,
                "error": reason,
                "foreground_package": app_id,
                "tree_application_package": tree_application_package,
                "tree_ownership_status": ownership_status,
                "tree_ownership_candidates": list(
                    ownership["tree_ownership_candidates"]
                ),
                "tree_ownership_candidate_count": ownership[
                    "tree_ownership_candidate_count"
                ],
                "tree_ownership_candidates_truncated": ownership[
                    "tree_ownership_candidates_truncated"
                ],
                "tree_ownership_candidates_complete": ownership[
                    "tree_ownership_candidates_complete"
                ],
            })
            source_capture["tree_index_eligible"] = False
            source_capture.update({
                "complete": False,
                "tree_removed_reason": reason,
                "coordinate_compatible": False,
            })
            tree = {
                "class": "hierarchy",
                "children": [],
                "_capture": source_capture,
            }
        if tree_declares_usable and ownership_status == "missing":
            capture_deadline.record_attempt({
                "provider": "grounding_barrier",
                "status": "error",
                "budget_ms": 0.0,
                "elapsed_ms": 0.0,
                "error": "foreground_tree_identity_missing",
                "foreground_package": app_id,
                "tree_application_package": "",
                "tree_ownership_status": "missing",
                "tree_ownership_candidates": list(
                    ownership["tree_ownership_candidates"]
                ),
                "tree_ownership_candidate_count": ownership[
                    "tree_ownership_candidate_count"
                ],
                "tree_ownership_candidates_truncated": ownership[
                    "tree_ownership_candidates_truncated"
                ],
                "tree_ownership_candidates_complete": ownership[
                    "tree_ownership_candidates_complete"
                ],
            })
            source_capture["tree_index_eligible"] = False
        owned_candidates = [
            row for row in ownership["tree_ownership_candidates"]
            if row.get("packages") == [tree_application_package]
            and int(row.get("retained_model_visible_node_count") or 0) > 0
        ]
        selected_candidate = max(
            owned_candidates,
            key=lambda row: (
                bool(row.get("active") or row.get("focused")),
                int(row.get("window_layer") or 0),
            ),
            default={},
        )
        raw_capture = tree.setdefault("_capture", {})
        if isinstance(raw_capture, dict):
            capture_fallback_edges = list(
                raw_capture.get("fallback_edges") or []
            )
            for edge in capture_deadline.fallback_edges:
                if edge not in capture_fallback_edges:
                    capture_fallback_edges.append(edge)
            raw_capture.update({
                "foreground_app_id": app_id,
                "foreground_activity": activity,
                "foreground_identity_required": True,
                "foreground_identity_attempts": identity_attempts,
                "foreground_identity_conflict": False,
                "foreground_identity_after_status": (
                    "timeout_corroborated_by_exact_tree_owner"
                    if posterior_identity_timeout is not None else "exact"
                ),
                "top_window_package": top_window_package,
                "top_window_type": top_window_type,
                "top_window_layer": top_window.get("window_layer"),
                "top_window_display_id": top_window.get("display_id"),
                "tree_application_package": tree_application_package,
                "tree_application_window_layer": selected_candidate.get(
                    "window_layer"
                ),
                "tree_ownership_status": ownership_status,
                "tree_ownership_candidates": list(
                    ownership["tree_ownership_candidates"]
                ),
                "tree_ownership_candidate_count": ownership[
                    "tree_ownership_candidate_count"
                ],
                "tree_ownership_candidates_truncated": ownership[
                    "tree_ownership_candidates_truncated"
                ],
                "tree_ownership_candidates_complete": ownership[
                    "tree_ownership_candidates_complete"
                ],
                "tree_index_eligible": ownership_status == "exact",
                "active_capture_ordinal": capture_ordinal,
                "source_pixel_byte_count": len(shot or b""),
                "source_pixel_sha256": (
                    hashlib.sha256(shot).hexdigest()
                    if shot is not None else None
                ),
                "grounding_barrier": (
                    "pre_identity_tree_exact"
                    if posterior_identity_timeout is not None
                    else (
                        "aligned" if ownership_status == "exact" else "degraded"
                    )
                ),
                "provider_attempts": provider_attempts,
                "fallback_edges": capture_fallback_edges,
            })
        prepared = self.builder.prepare(
            (tree, shot), app_id=app_id, activity=activity,
        )
        package = self.builder.package(
            prepared,
            attach_image=bool(prepared.gap_reasons) or attach_image,
        )
        capture = dict(package.capture_meta or {})
        capture.setdefault("request_started_monotonic_ms", requested_ms)
        capture.setdefault("capture_completed_monotonic_ms", time.monotonic() * 1000.0)
        package.capture_meta = capture
        package.transaction_id = transaction_id
        package.observation_source = source
        set_ui = getattr(self.driver, "set_last_ui", None)
        if callable(set_ui):
            set_ui(package.ui)
        return package

    async def observe_current(
        self,
        *,
        deadline_ms: int | None = None,
        source: str = "fresh_current",
        transaction_id: str | None = None,
        attach_image: bool = False,
        expected_foreground_app_id: str = "",
    ) -> ObservationPackage:
        """Capture once, optionally settle, then fully recapture exactly once."""
        transaction_id = transaction_id or f"txn_{uuid.uuid4().hex}"
        budget_ms = max(1, int(deadline_ms or self.current_deadline_ms))
        deadline = ObservationDeadline(source, budget_ms)
        latest: ObservationPackage | None = None
        resample_trigger = ""
        settle_elapsed_ms = 0.0

        async def settle_once() -> bool:
            nonlocal settle_elapsed_ms
            settle_s = self.resample_settle_ms / 1000.0
            if settle_s <= 0:
                return deadline.remaining_ms > 0
            if deadline.remaining_ms < self.resample_settle_ms:
                return False
            started = time.monotonic()
            settled = await deadline.wait(self.resample_settle_ms)
            settle_elapsed_ms = (time.monotonic() - started) * 1000.0
            return settled

        for ordinal in range(1, MAX_CAPTURE_ATTEMPTS + 1):
            remaining_s = deadline.remaining_ms / 1000.0
            if remaining_s <= 0:
                break
            deadline.capture_ordinal = ordinal
            try:
                package = await asyncio.wait_for(
                    self._capture_once(
                        transaction_id,
                        source,
                        attach_image=attach_image,
                        capture_deadline=deadline,
                        capture_ordinal=ordinal,
                    ),
                    timeout=remaining_s,
                )
            except asyncio.TimeoutError as exc:
                raise ObservationStageError(
                    "observation_capture",
                    "outer_deadline_exhausted",
                    elapsed_ms=deadline.elapsed_ms,
                    budget_ms=deadline.budget_ms,
                    timed_out=True,
                    fallback_edges=list(deadline.fallback_edges),
                    provider_attempts=list(deadline.provider_attempts),
                    cancelled_tasks=list(deadline.cancelled_tasks),
                    capture_attempt_count=ordinal,
                    stage_timings=list(deadline.stage_timings),
                ) from exc
            except ObservationStageError as exc:
                exc.stage_timings = list(deadline.stage_timings)
                exc.provider_attempts = list(deadline.provider_attempts)
                exc.fallback_edges = list(deadline.fallback_edges)
                exc.cancelled_tasks = list(deadline.cancelled_tasks)
                exc.capture_attempt_count = ordinal
                if ordinal == 1 and _retryable_stage_failure(exc):
                    resample_trigger = f"{exc.stage}:{exc.reason}"
                    if await settle_once():
                        continue
                    deadline.record_attempt({
                        "provider": "bounded_resample",
                        "status": "not_attempted",
                        "capture_ordinal": ordinal,
                        "trigger": resample_trigger,
                        "settle_configured_ms": self.resample_settle_ms,
                        "settle_elapsed_ms": round(settle_elapsed_ms, 3),
                        "reason": "insufficient_outer_deadline",
                    })
                    exc.provider_attempts = list(deadline.provider_attempts)
                raise

            latest = package
            latest.capture_meta.update({
                "provider_attempts": list(deadline.provider_attempts),
                "fallback_edges": list(deadline.fallback_edges),
                "cancelled_capture_tasks": list(deadline.cancelled_tasks),
                "observation_capture_attempt_count": ordinal,
                "active_capture_ordinal": ordinal,
                "evidence_tier": _evidence_tier(latest),
            })
            if resample_trigger:
                latest.capture_meta.update({
                    "resample_trigger": resample_trigger,
                    "settle_configured_ms": self.resample_settle_ms,
                    "settle_elapsed_ms": round(settle_elapsed_ms, 3),
                })

            reason = observation_acceptance_reason(latest)
            mechanical_reason = _mechanical_resample_reason(latest)
            if (
                ordinal == 1
                and expected_foreground_app_id
                and (latest.ui.app_id or "").strip()
                != expected_foreground_app_id
            ):
                mechanical_reason = "expected_foreground_not_observed"

            if ordinal == 1 and mechanical_reason:
                resample_trigger = mechanical_reason
                latest.capture_meta["resample_trigger"] = resample_trigger
                latest.capture_meta["settle_configured_ms"] = (
                    self.resample_settle_ms
                )
                latest.capture_meta["settle_elapsed_ms"] = 0.0
                if await settle_once():
                    continue
                latest.capture_meta["settle_elapsed_ms"] = round(
                    settle_elapsed_ms, 3
                )
                latest.capture_meta["resample_not_attempted"] = (
                    "insufficient_outer_deadline"
                )

            if reason:
                latest.capture_meta["terminal_failure_class"] = reason
                return _sanitize_rejected_observation(latest, reason=reason)

            latest.accepted = True
            latest.acceptance_reason = "accepted"
            latest.capture_meta["evidence_tier"] = _evidence_tier(latest)
            return latest

        if latest is not None:
            reason = observation_acceptance_reason(latest)
            if not reason:
                latest.accepted = True
                latest.acceptance_reason = "accepted"
                return latest
            return _sanitize_rejected_observation(latest, reason=reason)
        raise ObservationStageError(
            "observation_capture",
            "outer_deadline_exhausted",
            elapsed_ms=deadline.elapsed_ms,
            budget_ms=deadline.budget_ms,
            timed_out=True,
            fallback_edges=list(deadline.fallback_edges),
            provider_attempts=list(deadline.provider_attempts),
            cancelled_tasks=list(deadline.cancelled_tasks),
            capture_attempt_count=MAX_CAPTURE_ATTEMPTS,
        )

    async def act_and_observe(
        self,
        action: Action,
        before: ObservationPackage,
        *,
        deadline_ms: int | None = None,
        target: ActionTargetSnapshot | None = None,
    ) -> tuple[ActionResult, ObservationPackage]:
        """Dispatch once; capture a postcondition unless the action is delay-only."""
        transaction_id = f"txn_{uuid.uuid4().hex}"
        budget_ms = max(
            1,
            int(
                deadline_ms
                if deadline_ms is not None
                else (self.effect_deadline_ms or effect_deadline_ms_for(action))
            ),
        )
        started_ms = time.monotonic() * 1000.0
        event_cursor: int | None = None
        cursor_reader = getattr(self.driver, "interaction_event_cursor", None)
        if (
            _interaction_event_reads_enabled()
            and action.type in INTERACTION_ACK_ACTION_EVENTS
            and target is not None and callable(cursor_reader)
        ):
            try:
                event_cursor = await cursor_reader(timeout_s=0.35)
            except Exception:  # noqa: BLE001 - optional evidence cannot block dispatch
                event_cursor = None
        result = await self.driver.act(action)
        dispatch_completed_ms = time.monotonic() * 1000.0
        post_action_settle_elapsed_ms = 0.0
        receipt = ActionReceipt(
            transaction_id=transaction_id,
            effect_class=effect_class_for(action),
            dispatch_succeeded=bool(result.success),
            effect_outcome=(
                EffectOutcome.UNKNOWN if result.success else EffectOutcome.FAILED
            ),
            effect_reason=(
                "dispatch_succeeded"
                if result.success else result.message or "dispatch_failed"
            ),
            started_monotonic_ms=started_ms,
            dispatch_completed_monotonic_ms=dispatch_completed_ms,
            deadline_ms=budget_ms,
            interaction_ack="unavailable",
            node_click_status=result.detail.get("node_click_status"),
            native_action_performed=result.detail.get("native_action_performed"),
        )

        if action.type == "sleep":
            receipt.effect_reason = (
                "delay_completed"
                if result.success else result.message or "dispatch_failed"
            )
            receipt.completed_monotonic_ms = time.monotonic() * 1000.0
            result.receipt = receipt
            return result, before

        post_action_deadline = ObservationDeadline(
            "action_postcondition",
            budget_ms,
        )
        launch_target = (
            _launch_target_package(action, result)
            if result.success and action.type == "launch" else ""
        )
        event_batch = None
        try:
            if (
                result.success and self.resample_settle_ms > 0
            ):
                settle_started = time.monotonic()
                await post_action_deadline.wait(self.resample_settle_ms)
                post_action_settle_elapsed_ms = (
                    time.monotonic() - settle_started
                ) * 1000.0
            event_reader = getattr(self.driver, "interaction_events_after", None)
            if (
                result.success and event_cursor is not None and callable(event_reader)
                and post_action_deadline.remaining_ms > 0
            ):
                try:
                    event_batch = await event_reader(
                        event_cursor,
                        limit=64,
                        timeout_s=min(0.35, post_action_deadline.remaining_ms / 1000.0),
                    )
                except Exception:  # noqa: BLE001 - optional evidence cannot block capture
                    event_batch = None
            remaining_ms = int(post_action_deadline.remaining_ms)
            if remaining_ms <= 0:
                raise ObservationStageError(
                    "observation_capture",
                    "outer_deadline_exhausted_during_post_action_settle",
                    elapsed_ms=post_action_deadline.elapsed_ms,
                    budget_ms=post_action_deadline.budget_ms,
                    timed_out=True,
                )
            after = await self.observe_current(
                deadline_ms=remaining_ms,
                source=(
                    "action_postcondition"
                    if result.success else "failed_action_postcondition"
                ),
                transaction_id=transaction_id,
                attach_image=bool(
                    (result.success and action.type != "launch")
                    or result.detail.get("node_click_status")
                ),
                expected_foreground_app_id=launch_target,
            )
        except (ObservationStageError, TimeoutError) as exc:
            if isinstance(exc, ObservationStageError):
                result.detail["observation_failure"] = exc.diagnostics()
            after = degraded_post_observation(
                before,
                transaction_id=transaction_id,
                source="action_postcondition",
                error=exc,
            )
        after.capture_meta.update({
            "post_action_settle_configured_ms": self.resample_settle_ms,
            "post_action_settle_elapsed_ms": round(
                post_action_settle_elapsed_ms, 3,
            ),
        })

        ack, ack_source = interaction_ack_for_batch(action, target, event_batch)
        receipt.interaction_ack = ack
        receipt.interaction_ack_source = ack_source  # type: ignore[assignment]
        result.detail.setdefault("interaction_ack", {
            "status": ack,
            "source": ack_source,
            "event_count": len(tuple(getattr(event_batch, "events", ()))),
            "complete_coverage": (
                bool(getattr(event_batch, "complete_coverage", False))
                if event_batch is not None else None
            ),
        })

        if result.detail.get("node_click_status") == "outcome_unknown":
            receipt.effect_outcome = EffectOutcome.UNKNOWN
            effect_reason = "node_click_outcome_unknown_observe_before_retry"
        elif not result.success:
            receipt.effect_outcome = EffectOutcome.FAILED
            effect_reason = result.message or "dispatch_failed"
        elif action.type == "launch":
            foreground = (after.ui.app_id or "").strip()
            if launch_target and after.accepted and foreground == launch_target:
                receipt.effect_outcome = EffectOutcome.CONFIRMED
                receipt.effect_observation_id = after.observation_id
                effect_reason = "launch_target_foreground_observed"
            elif not launch_target:
                receipt.effect_outcome = EffectOutcome.UNKNOWN
                effect_reason = "launch_target_unresolved"
            else:
                receipt.effect_outcome = EffectOutcome.TIMEOUT
                effect_reason = (
                    after.acceptance_reason
                    if not after.accepted else "launch_target_not_foreground"
                )
        elif after.accepted:
            receipt.effect_outcome = EffectOutcome.UNKNOWN
            effect_reason = "action_dispatched_observation_available"
        else:
            receipt.effect_outcome = EffectOutcome.TIMEOUT
            effect_reason = after.acceptance_reason

        raw_attempt_count = (after.capture_meta or {}).get(
            "observation_capture_attempt_count",
        )
        attempt_count = (
            1
            if raw_attempt_count is None
            else max(0, int(raw_attempt_count))
        )
        receipt.effect_reason = effect_reason
        receipt.completed_monotonic_ms = time.monotonic() * 1000.0
        receipt.observation_capture_count = attempt_count
        receipt.observation_id = after.observation_id
        receipt.observation_accepted = after.accepted
        if (
            result.success
            and action.type != "sleep"
            and unchanged_post_action_observation(before, after)
        ):
            receipt.visible_change = "none"
        result.receipt = receipt
        return result, after
