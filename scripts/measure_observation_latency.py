"""Measure real-device current/temporal observation latency as JSON.

Run from the repository root with an authorized ADB serial. The command does
not mutate device state; it only warms the configured provider and observes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from io import BytesIO
import json
import statistics
import time
from typing import Any

from agent.read_tools import OBSERVE_SCREEN_DEFAULT_DURATION_MS, make_observe_screen_handler
from agent.tool_registry import AgentRole, ToolExecutionContext
from driver.factory import get_driver
from driver.scrcpy_mirror import REGISTRY
from driver.observation_deadline import CURRENT_DEADLINE_MS, TEMPORAL_DEADLINE_MS
from perception.observation import ObservationBuilder
from shared.config import get_settings
from shared.schemas import Action


def _difference_hash(data: bytes) -> str | None:
    if not data:
        return None
    from PIL import Image

    image = Image.open(BytesIO(data)).convert("L").resize((9, 8))
    pixels = image.load()
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | int(
                pixels[column, row] > pixels[column + 1, row]
            )
    return f"{value:016x}"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return round(ordered[index], 2)


async def measure(
    serial: str,
    repetitions: int,
    mode: str = "both",
    before_action: str | None = None,
    shared_consumer: bool = False,
    force_scrcpy_failure: bool = False,
) -> dict[str, Any]:
    settings = get_settings()
    driver = get_driver(settings, serial=serial)
    warmer = getattr(driver, "warm_observation_provider", None)
    closer = getattr(driver, "close_observation_provider", None)
    warm_started = time.monotonic()
    warm_status = "unsupported"
    warm_error = ""
    if callable(warmer):
        try:
            await warmer()
            warm_status = "ok"
        except Exception as exc:  # noqa: BLE001
            warm_status = "error"
            warm_error = str(exc)[:200]
    warm_elapsed_ms = round((time.monotonic() - warm_started) * 1000.0, 3)
    shared_session = None
    shared_lease = None
    shared_task = None
    shared_units = 0
    if shared_consumer:
        shared_session, shared_lease = await REGISTRY.acquire(serial, "benchmark-console")

        async def drain_shared_stream() -> None:
            nonlocal shared_units
            assert shared_session is not None
            async for _unit in shared_session.frames():
                shared_units += 1

        shared_task = asyncio.create_task(drain_shared_stream())
    tree, shot = await driver.get_frame()
    builder = ObservationBuilder()
    baseline = builder.build((tree, shot), will_send_image=True)
    if force_scrcpy_failure:
        baseline.captured_monotonic_ms = 0.0
        stream_provider = getattr(driver, "_stream_provider", None)
        if stream_provider is None:
            raise RuntimeError("cannot inject scrcpy failure: provider is unavailable")

        async def fail_stream_start() -> None:
            raise RuntimeError("injected_scrcpy_failure")

        stream_provider.start = fail_stream_start
    handler = make_observe_screen_handler(
        driver=driver,
        builder=builder,
        artifacts=None,
        baseline_package=baseline,
    )
    samples: dict[str, list[dict[str, Any]]] = {"current": [], "temporal": []}
    try:
        selected_modes = ("current", "temporal") if mode == "both" else (mode,)
        for selected_mode in selected_modes:
            for iteration in range(max(1, repetitions)):
                action_label = None
                if before_action:
                    if before_action == "alternate":
                        action = (
                            Action(type="launch", app="com.android.settings")
                            if iteration % 2 == 0
                            else Action(type="home")
                        )
                    else:
                        action = Action(type=before_action)
                    action_label = (
                        f"launch:{action.app}" if action.type == "launch" else action.type
                    )
                    await driver.act(action)
                context = ToolExecutionContext(
                    role=AgentRole.EXECUTOR, invocation_id="latency-probe"
                )
                args = {"mode": selected_mode}
                if selected_mode == "temporal":
                    args.update({"frames": 2, "duration_ms": OBSERVE_SCREEN_DEFAULT_DURATION_MS})
                observe_started = time.monotonic()
                result = await handler(args, context)
                observe_elapsed_ms = round(
                    (time.monotonic() - observe_started) * 1000.0, 3
                )
                package = context.state.get("active_package")
                capture_meta = (
                    dict(getattr(package, "capture_meta", {}) or {})
                    if package is not None else {}
                )
                image = (
                    (package.annotated_png or package.image_for_llm or b"")
                    if package is not None else b""
                )
                provider_state = result.data.get("provider_state") or {}
                ring_state = provider_state.get("ring") or {}
                samples[selected_mode].append({
                    "status": result.data.get("status") or result.status.value,
                    "failed_stage": result.data.get("failed_stage"),
                    "acceptance_reason": result.data.get("acceptance_reason"),
                    "gap_reasons": result.data.get("gap_reasons") or [],
                    "evidence_tier": capture_meta.get("evidence_tier"),
                    "capture_attempt_count": capture_meta.get(
                        "observation_capture_attempt_count"
                    ),
                    "resample_trigger": capture_meta.get("resample_trigger"),
                    "tree_complete": capture_meta.get("complete"),
                    "tree_ownership_status": capture_meta.get(
                        "tree_ownership_status"
                    ),
                    "coordinate_compatible": capture_meta.get(
                        "coordinate_compatible"
                    ),
                    "action": action_label,
                    "app_id": getattr(getattr(package, "ui", None), "app_id", ""),
                    "image_digest": hashlib.sha256(image).hexdigest()[:16] if image else None,
                    "image_dhash": _difference_hash(image),
                    "provider": capture_meta.get("provider"),
                    "generation": result.data.get("provider_generation"),
                    "tree_provider": capture_meta.get("tree_provider"),
                    "tree_generation": result.data.get("tree_generation"),
                    "pixel_provider": capture_meta.get("pixel_provider"),
                    "image_geometry": result.data.get("image_size"),
                    "frame_geometry": result.data.get("frame_geometry"),
                    "rotation": result.data.get("rotation_degrees"),
                    "crop": result.data.get("crop_box"),
                    "provider_state": provider_state,
                    "validation": provider_state.get("validation"),
                    "visual_age_ms": ring_state.get("visual_age_ms"),
                    "elapsed_ms": observe_elapsed_ms,
                    "stage_timings": result.data.get("stage_timings") or [],
                    "fallback_edges": capture_meta.get("fallback_edges") or [],
                    "provider_attempts": capture_meta.get("provider_attempts") or [],
                    "cancelled_capture_tasks": capture_meta.get(
                        "cancelled_capture_tasks"
                    ) or [],
                    "capture_boundary_generation": result.data.get(
                        "capture_boundary_generation"
                    ),
                    "capture_frame_boundary_id": result.data.get(
                        "capture_frame_boundary_id"
                    ),
                })
    finally:
        if callable(closer):
            await closer()
        if shared_task is not None:
            shared_task.cancel()
            try:
                await shared_task
            except asyncio.CancelledError:
                pass
        if shared_lease is not None:
            await REGISTRY.release(serial, shared_lease)
        # This is a short-lived standalone probe. Do not leave the registry's
        # idle lease/server alive while asyncio waits for decoder executors.
        await REGISTRY.shutdown()
    summary: dict[str, Any] = {}
    for mode, rows in samples.items():
        elapsed = [float(row["elapsed_ms"]) for row in rows if row["elapsed_ms"] is not None]
        summary[mode] = {
            "count": len(rows),
            "p50_ms": round(statistics.median(elapsed), 2) if elapsed else None,
            "p95_ms": _percentile(elapsed, 0.95),
            "scrcpy_selected": sum(row["provider"] == "scrcpy" for row in rows),
            "adb_fallbacks": sum(row["provider"] == "adb_fallback" for row in rows),
            "validations": sorted({
                str(row["validation"]) for row in rows if row["validation"]
            }),
            "samples": rows,
        }
    return {"serial": serial, "before_action": before_action,
        "force_scrcpy_failure": force_scrcpy_failure, "shared_consumer": {
        "enabled": shared_consumer,
        "units_received": shared_units,
    }, "provider_warm": {
        "status": warm_status,
        "elapsed_ms": warm_elapsed_ms,
        "error": warm_error,
    }, "defaults": {
        "current_deadline_ms": CURRENT_DEADLINE_MS,
        "temporal_deadline_ms": TEMPORAL_DEADLINE_MS,
    }, "results": summary}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial", required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--mode", choices=("current", "temporal", "both"), default="both")
    parser.add_argument("--before-action", choices=("back", "home", "alternate"))
    parser.add_argument("--shared-consumer", action="store_true")
    parser.add_argument("--force-scrcpy-failure", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(measure(
        args.serial, args.repetitions, args.mode, args.before_action,
        args.shared_consumer, args.force_scrcpy_failure,
    )), indent=2))


if __name__ == "__main__":
    main()
