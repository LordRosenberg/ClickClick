"""Executor pre-validation + nudge-degrade tests (no real LLM, no real driver)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as _PILImage

from agent.executor import (
    Executor,
    _is_valid_step,
    _missing_required_params,
    _required_params_satisfied,
    resolve_tap_index,
    rescale_action_xy,
)
from agent.observation_space import validate_action_bounds
from agent.tool_registry import AgentRole, ToolExecutionContext, ToolStatus
from driver.observation_deadline import ObservationStageError
from shared.schemas import (
    Action,
    ActionPipeline,
    ActionPipelineStage,
    AgentState,
    CanonicalUI,
    ExecutorStep,
    ObservationMode,
    UIElement,
)


def _png_bytes(w: int, h: int) -> bytes:
    """Solid-color PNG bytes for fixture-only size manipulation."""
    buf = io.BytesIO()
    _PILImage.new("RGB", (w, h), (128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# _required_params_satisfied unit tests
# ---------------------------------------------------------------------------


def _a(type: str, **kw) -> Action:
    return Action(type=type, **kw)


def test_required_params_swipe_missing_y2():
    assert not _required_params_satisfied(_a("swipe", x=100, y=200, x2=300))


def test_required_params_swipe_complete():
    assert _required_params_satisfied(_a("swipe", x=100, y=200, x2=300, y2=400))


def test_required_params_swipe_duration_optional():
    assert _required_params_satisfied(_a("swipe", x=100, y=200, x2=300, y2=400))


def test_required_params_scroll_missing_direction():
    assert not _required_params_satisfied(_a("scroll"))


def test_required_params_scroll_with_direction():
    assert _required_params_satisfied(_a("scroll", direction="down"))


def test_required_params_tap_missing_index():
    assert not _required_params_satisfied(_a("tap"))


def test_required_params_tap_with_index():
    assert _required_params_satisfied(_a("tap", index=3))


def test_required_params_type_missing_text():
    assert not _required_params_satisfied(_a("type"))


def test_required_params_type_with_text():
    assert _required_params_satisfied(_a("type", text="hello"))


def test_required_params_replace_text_requires_text():
    assert not _required_params_satisfied(_a("replace_text"))
    assert _required_params_satisfied(_a("replace_text", text="exact final value"))


def test_required_params_key_missing_key():
    assert not _required_params_satisfied(_a("key"))


def test_required_params_key_with_key():
    assert _required_params_satisfied(_a("key", key="delete"))


def test_required_params_launch_missing_app():
    assert not _required_params_satisfied(_a("launch"))


def test_required_params_launch_with_app():
    assert _required_params_satisfied(_a("launch", app="com.x"))


def test_required_params_drag_missing_x2():
    assert not _required_params_satisfied(_a("drag", x=1, y=2, y2=4))


def test_required_params_drag_complete():
    assert _required_params_satisfied(_a("drag", x=1, y=2, x2=3, y2=4))


def test_required_params_long_press_index_only():
    assert _required_params_satisfied(_a("long_press", index=5))


def test_required_params_long_press_xy_only():
    assert _required_params_satisfied(_a("long_press", x=100, y=200))


def test_required_params_long_press_neither():
    assert not _required_params_satisfied(_a("long_press"))


def test_required_params_long_press_only_x():
    assert not _required_params_satisfied(_a("long_press", x=100))


def test_required_params_back_home_sleep_no_params():
    assert _required_params_satisfied(_a("back"))
    assert _required_params_satisfied(_a("home"))
    assert _required_params_satisfied(_a("sleep"))


def test_missing_required_params_names_swipe_y2():
    assert _missing_required_params(_a("swipe", x=1, y=2, x2=3)) == ["y2"]


def test_missing_required_params_long_press_neither():
    # long_press with neither index nor (x,y) → returns the combined hint.
    assert _missing_required_params(_a("long_press")) == ["index or (x,y)"]


# ---------------------------------------------------------------------------
# _is_valid_step integration with pre-validation
# ---------------------------------------------------------------------------


def test_is_valid_step_rejects_swipe_missing_y2():
    step = ExecutorStep(action=_a("swipe", x=1, y=2, x2=3))
    assert not _is_valid_step(step)


def test_is_valid_step_accepts_swipe_complete():
    step = ExecutorStep(action=_a("swipe", x=1, y=2, x2=3, y2=4))
    assert _is_valid_step(step)


def test_is_valid_step_rejects_missing_action():
    assert not _is_valid_step(ExecutorStep(action=None))


def test_is_valid_step_rejects_empty_type():
    # Action requires a type; construct via model_construct to bypass validation
    # is not needed — Action.type is a Literal without a default, so an empty
    # type can't be built normally. Test the None-action + no-type path instead.
    step = ExecutorStep(action=_a("sleep"))
    assert _is_valid_step(step)


# ---------------------------------------------------------------------------
# Executor._call_with_retry: nudge + degrade behavior
# ---------------------------------------------------------------------------


class _FakeDriver:
    """Minimal driver stub that records dispatched actions."""

    def __init__(self) -> None:
        self.acts: list[Action] = []

    async def act(self, action: Action):
        self.acts.append(action)
        from shared.schemas import ActionResult
        return ActionResult(success=True, message="ok")

    async def get_frame(self):
        return (
            {
                "class": "Root", "text": "ready", "package": "com.x",
                "bounds": [0, 0, 900, 1600], "children": [],
                "_capture": {"complete": True, "coordinate_compatible": True},
            },
            _png_bytes(900, 1600),
        )

    async def current_foreground_identity(self, *, timeout_s=None):
        return {
            "package": "com.x",
            "activity": "Main",
            "component": "com.x/Main",
            "sources": [],
            "conflict": False,
        }


def _make_executor(monkeypatch, *, first_content: str, second_content: str | None):
    """Build an Executor whose AgentSession `complete()` yields scripted tool submits."""
    from shared.config import Settings
    from shared.llm_gateway import GatewayResponse, ToolCall

    settings = Settings(
        gateway_max_retries=1,
        gateway_retry_base_delay=0.001,
        gateway_request_timeout=1.0,
        use_fixture_driver=True,
    )
    drv = _FakeDriver()
    exe = Executor(drv, model="test-model", settings=settings)

    calls = {"n": 0}

    def _wrap(content: str) -> GatewayResponse:
        return GatewayResponse(
            content="",
            model="test-model",
            stop_reason="tool_calls",
            tool_calls=[
                ToolCall(id=f"call_{calls['n']}", name="submit_executor_step", arguments=content),
            ],
        )

    async def fake_complete(model, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _wrap(first_content)
        return _wrap(second_content or first_content)

    monkeypatch.setattr("agent.session.complete", fake_complete)
    return exe, drv, calls


def _submit_json(
    action: dict[str, Any],
    *,
    thought: str = "test",
    summary: str = "act",
    basis_observation_id: str = "",
    **overrides: Any,
) -> str:
    del thought, basis_observation_id
    payload: dict[str, Any] = {
        "decision": "act",
        "action": action,
        "summary": summary,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("action_type", ["type", "replace_text"])
@pytest.mark.parametrize("editability", ["editable", "conflict", "unknown"])
async def test_password_text_never_reaches_persistent_executor_surfaces(
    monkeypatch,
    tmp_path,
    action_type: str,
    editability: str,
):
    from agent.decision_context import task_action_timeline_rows
    from agent.task_memory import record_attempt
    from agent.orchestrator import Orchestrator
    from perception.input_evidence import build_interaction_state
    from perception.normalizer import render_semantic_tree
    from perception.observation import ObservationPackage
    from shared.artifacts import ArtifactStore
    from shared.llm_gateway import GatewayResponse, ToolCall
    from shared.schemas import ActionResult, ObservationMode

    secret = f"persist-secret-{action_type}-{editability}-🔒"

    class EchoDriver(_FakeDriver):
        async def act(self, action: Action):
            self.acts.append(action)
            return ActionResult(
                success=True,
                message=f"driver accepted {action.text}",
                detail={"echo": action.text},
            )

    field = UIElement(
        index=0,
        role="android.widget.EditText",
        text=f"current-{secret}",
        desc=f"label-{secret}",
        hint=f"hint-{secret}",
        bounds=[10, 20, 410, 100],
        states={
            "focused": True,
            "focusable": True,
            "password": True,
        },
    )
    if editability == "conflict":
        field.states["editable"] = False
    elif editability == "unknown":
        field.role = "android.view.View"
    ui = CanonicalUI(
        app_id="com.x",
        activity="Main",
        elements=[field],
        semantic_tree=[field],
    )
    package = ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=render_semantic_tree(ui),
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        interaction_state=build_interaction_state(ui),
        actionable=True,
        index_actionable=True,
        frame_width=900,
        frame_height=1600,
    )
    state = AgentState(
        instruction="enter a password",
        current_subgoal="enter the password",
        recovery_state={"lineage_id": "password-lineage"},
        active_timeline_lineage_ids=["password-lineage"],
    )
    driver = EchoDriver()
    artifacts = ArtifactStore(tmp_path / f"artifacts-{action_type}-{editability}")
    executor = Executor(driver, artifacts=artifacts, model="test-model")

    async def fake_complete(model, messages, **kwargs):
        del model, messages, kwargs
        return GatewayResponse(
            content=f"response echoed {secret}",
            model="test-model",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="password-submit",
                name="submit_executor_step",
                arguments=_submit_json(
                    {"type": action_type, "text": secret},
                    summary=f"enter {secret}",
                ),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    step, result, _ui, mode, refs = await executor.act_once(
        state.current_subgoal,
        package,
        state=state,
        task_id=f"password-{action_type}-{editability}",
    )

    assert driver.acts[0].text == secret  # one invocation-local dispatch only
    assert step.action is not None
    assert step.action.text is None and step.action.text_redacted is True
    assert secret not in step.model_dump_json()
    assert secret not in result.model_dump_json()
    assert mode == ObservationMode.TREE_ONLY
    persisted_refs = {
        key: refs[key]
        for key in (
            "tool_calls",
            "agent_rounds",
            "observation_registry",
            "interaction_state",
        )
    }
    assert secret not in json.dumps(persisted_refs, ensure_ascii=False)

    record_attempt(
        state.task_memory,
        step,
        subgoal=state.current_subgoal,
        step=0,
        lineage_id="password-lineage",
    )
    timeline_payload = json.dumps(
        [event.model_dump(mode="json") for event in state.task_memory.events],
        ensure_ascii=False,
    )
    assert secret not in timeline_payload
    assert secret not in json.dumps(task_action_timeline_rows(state), ensure_ascii=False)

    class TraceSink:
        payloads: list[dict[str, Any]] = []

        def write(self, *args, **kwargs):
            del args
            self.payloads.append(dict(kwargs.get("payload") or {}))

    sink = TraceSink()
    orchestrator = object.__new__(Orchestrator)
    orchestrator.traces = sink
    Orchestrator._trace_executor_step(
        orchestrator,
        "task",
        state,
        step,
        result,
        ui,
        mode,
        refs,
        package,
    )
    assert secret not in json.dumps(sink.payloads, ensure_ascii=False)
    assert secret not in Orchestrator._build_step_report(
        ui,
        mode,
        step,
        result,
        refs,
        package,
    ).model_dump_json()

    for path in artifacts.root.rglob("*"):
        if path.is_file():
            assert secret.encode("utf-8") not in path.read_bytes(), path


@pytest.mark.asyncio
async def test_swipe_missing_y2_never_becomes_outer_executor_step(monkeypatch):
    """Repeated invalid submit exhausts the role loop without a synthetic step."""
    bad = _submit_json(
        {"type": "swipe", "x": 1, "y": 2, "x2": 3, "y2": None},
        thought="x", summary="d",
    )
    still_bad = bad
    exe, drv, calls = _make_executor(monkeypatch, first_content=bad, second_content=still_bad)

    from perception.observation import ObservationPackage
    from shared.schemas import CanonicalUI, ObservationMode

    pkg = ObservationPackage(
        ui=CanonicalUI(app_id="com.x", activity="Main", elements=[]),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="tree",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        estimated_tokens=0,
    )
    from shared.llm_gateway import GatewayError

    with pytest.raises(GatewayError, match="tool loop exhausted without submit"):
        await exe.act_once("subgoal", pkg)

    # Every role round remains internal; no invalid round becomes an action.
    assert calls["n"] == 8
    # The malformed swipe never escapes the role session or reaches Driver.
    assert drv.acts == []


@pytest.mark.asyncio
async def test_deliberate_model_sleep_is_not_fallback(monkeypatch):
    valid_sleep = _submit_json(
        {"type": "sleep", "duration_ms": 200}, thought="loading", summary="wait",
    )
    exe, drv, calls = _make_executor(monkeypatch, first_content=valid_sleep, second_content=None)
    from perception.observation import ObservationPackage
    from shared.schemas import ObservationMode
    pkg = ObservationPackage(
        ui=CanonicalUI(app_id="com.x", activity="Main"),
        mode=ObservationMode.TREE_ONLY, text_for_llm="tree", image_for_llm=None,
        annotated_png=None, gap_reasons=[], estimated_tokens=0,
    )
    step, *_ = await exe.act_once("subgoal", pkg)
    assert calls["n"] == 1
    assert drv.acts[0].type == "sleep"
    assert step.action_pipeline is not None
    assert step.action_pipeline.origin == "model"
    assert step.action_pipeline.fallback_reason is None


@pytest.mark.asyncio
async def test_compress_then_rerender_som_passes_compressed_frame_to_llm(monkeypatch):
    """When an image is attached on a 2K+ device (1440×3200), the JPEG that
    reaches the LLM MUST be the compressed-frame SoM (re-rendered for legibility),
    NOT `package.image_for_llm`. Assert by capturing the base64 image bytes
    sent to `complete()` and decoding them — the resulting PNG must have the
    compressed-frame dimensions, not the original 1440×3200.
    """
    from shared.config import Settings
    from shared.schemas import ExecutorStep as _ES  # noqa: F811

    settings = Settings(
        gateway_max_retries=0,
        gateway_retry_base_delay=0.001,
        gateway_request_timeout=1.0,
        use_fixture_driver=True,
    )
    drv = _FakeDriver()
    exe = Executor(drv, model="test-model", settings=settings)

    captured: dict[str, Any] = {}

    step_json = _submit_json(
        {"type": "tap_xy", "x": 132, "y": 940},
        thought="tap it", summary="tap；test",
        basis_observation_id="obs-active",
    )

    async def fake_complete(model, messages, **kw):
        from shared.llm_gateway import GatewayResponse, ToolCall
        # Capture the message carrying the image.
        for m in messages:
            if isinstance(m.get("content"), list):
                for blk in m["content"]:
                    if blk.get("type") == "image_url":
                        captured["b64"] = blk["image_url"]["url"].split(",", 1)[1]
                        captured["media_type"] = blk["image_url"]["url"].split(";")[0].split(":")[1]
        return GatewayResponse(
            content="",
            model="test-model",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id="c1", name="submit_executor_step", arguments=step_json)],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)

    # Build a 1440×3200 PNG to feed as `image_for_llm`.
    src_png = _png_bytes(1440, 3200)
    from perception.observation import ObservationPackage
    from shared.schemas import CanonicalUI, ObservationMode, UIElement

    pkg = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x",
            activity="Main",
            elements=[UIElement(index=0, text="OK", bounds=[720, 1600, 900, 1700])],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=src_png,  # original-frame SoM (1440×3200)
        annotated_png=src_png,   # trace artifact
        gap_reasons=["a11y_uncovered:0.5"],
        estimated_tokens=0,
        observation_id="obs-active",
    )
    step, *_ = await exe.act_once("subgoal", pkg)

    # Decoder: the captured base64 should be a SoM PNG whose dimensions are
    # the COMPRESSED frame (max long edge 1080).
    assert "b64" in captured, "complete() never received an image block"
    raw = base64.b64decode(captured["b64"])
    decoded = _PILImage.open(io.BytesIO(raw))
    w, h = decoded.size
    assert max(w, h) == 1080, f"LLM-bound image must be ≤1080 long edge; got {w}x{h}"
    # And it must NOT equal the original 1440×3200 — proving the rerender happened.
    assert (w, h) != (1440, 3200)
    assert step.submitted_action_snapshot is not None
    assert step.submitted_action_snapshot.image_size == (w, h)

    # Bug 1 regression check: the model emitted `tap_xy(132, 940)` against
    # the compressed frame; the driver must receive the rescaled point in
    # the original-frame coordinate space.
    assert drv.acts, "driver should have been called once with the rescaled tap_xy"
    assert drv.acts[0].type == "tap_xy"
    assert drv.acts[0].x is not None and drv.acts[0].y is not None
    # Long-edge scale: 3200/1080 ≈ 2.963; (132, 940) → ~(391, 2785).
    assert abs(drv.acts[0].x - 391.11) < 0.5
    assert abs(drv.acts[0].y - 2785.18) < 0.5


@pytest.mark.asyncio
async def test_index_32_resolves_once_without_parent_retarget(monkeypatch):
    """The reported regression: index 32 stays at its own bounds center."""
    good = _submit_json(
        {"type": "tap", "index": 32}, thought="ok", summary="tap",
        basis_observation_id="obs-active",
    )
    exe, drv, calls = _make_executor(monkeypatch, first_content=good, second_content="")

    from perception.observation import ObservationPackage
    from shared.schemas import CanonicalUI, ObservationMode, UIElement

    pkg = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x", activity="Main",
            elements=[
                # This containing semantic parent previously captured the
                # index-derived point and moved it to (600, 276).
                UIElement(
                    index=22, text="parent", bounds=[350, 191, 850, 361],
                    clickable=True,
                ),
                UIElement(
                    index=32, role="View", bounds=[830, 350, 844, 371],
                    clickable=False,
                ),
                UIElement(index=99, role="View", bounds=[0, 0, 1200, 2670]),
            ],
        ),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="tree",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        estimated_tokens=0,
        observation_id="obs-active",
    )
    resolved = resolve_tap_index(Action(type="tap", index=32), pkg.ui)
    assert (resolved.x, resolved.y) == (837.0, 360.5)

    step, *_ = await exe.act_once("subgoal", pkg)

    assert calls["n"] == 1, "no nudge retry should happen on a valid first call"
    assert drv.acts[0].type == "tap_xy"
    assert (drv.acts[0].x, drv.acts[0].y) == (837.0, 360.5)
    assert (drv.acts[0].x, drv.acts[0].y) != (600.0, 276.0)
    assert step.action_pipeline is not None
    resolution_stages = [
        stage for stage in step.action_pipeline.stages
        if stage.reason == "index_resolved"
    ]
    assert len(resolution_stages) == 1
    assert (resolution_stages[0].action.x, resolution_stages[0].action.y) == (837.0, 360.5)
    dumped = step.action_pipeline.model_dump()
    assert "grounding_target" not in dumped
    assert "snap_outcome" not in dumped
    assert "snap_reason" not in dumped


@pytest.mark.asyncio
async def test_direct_tap_xy_inside_a11y_bounds_dispatches_exact_point(monkeypatch):
    good = _submit_json(
        {"type": "tap_xy", "x": 837, "y": 360.5}, thought="ok", summary="tap",
        basis_observation_id="obs-active",
    )
    exe, drv, _calls = _make_executor(monkeypatch, first_content=good, second_content="")

    from perception.observation import ObservationPackage
    from shared.schemas import ObservationMode

    pkg = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x", activity="Main",
            elements=[
                UIElement(
                    index=22, text="parent", bounds=[350, 191, 850, 361],
                    clickable=True,
                ),
                UIElement(index=32, role="View", bounds=[830, 350, 844, 371]),
            ],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=_png_bytes(900, 600),
        annotated_png=_png_bytes(900, 600),
        gap_reasons=[],
        estimated_tokens=0,
        observation_id="obs-active",
    )

    step, *_ = await exe.act_once("subgoal", pkg)

    assert (drv.acts[0].x, drv.acts[0].y) == (837.0, 360.5)
    assert (drv.acts[0].x, drv.acts[0].y) != (600.0, 276.0)
    assert step.action_pipeline is not None
    assert all(stage.reason != "index_resolved" for stage in step.action_pipeline.stages)
    assert all("snap" not in stage.reason for stage in step.action_pipeline.stages)
    dumped = step.action_pipeline.model_dump()
    assert "grounding_target" not in dumped
    assert "snap_outcome" not in dumped
    assert "snap_reason" not in dumped


@pytest.mark.asyncio
async def test_tap_xy_without_legacy_witness_is_grounded_and_dispatched_once(monkeypatch):
    submitted = {
        "decision": "act",
        "action": {"type": "tap_xy", "x": 837, "y": 360.5},
        "summary": "tap; submit the visible control",
    }
    exe, drv, calls = _make_executor(
        monkeypatch,
        first_content=json.dumps(submitted),
        second_content=None,
    )

    from perception.observation import ObservationPackage

    pkg = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x", activity="Main",
            elements=[UIElement(
                index=32, text="Submit", bounds=[830, 350, 844, 371], clickable=True,
            )],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=_png_bytes(900, 600),
        annotated_png=_png_bytes(900, 600),
        gap_reasons=[],
        estimated_tokens=0,
        observation_id="obs-active",
    )

    step, _result, _ui, _mode, refs = await exe.act_once("submit", pkg)

    assert calls["n"] == 1
    assert len(drv.acts) == 1
    assert drv.acts[0].type == "tap_xy"
    assert (drv.acts[0].x, drv.acts[0].y) == (837.0, 360.5)
    assert step.action and step.action.type == "tap_xy"
    assert step.decision.value == "act"
    assert refs["tool_calls"][0]["arguments"] == submitted
    assert "schema_normalizations" not in refs


@pytest.mark.asyncio
async def test_act_once_places_task_anchor_in_final_bucket(monkeypatch):
    from perception.observation import ObservationPackage
    from shared.config import Settings

    settings = Settings(
        gateway_max_retries=0,
        gateway_request_timeout=1.0,
        use_fixture_driver=True,
    )
    exe = Executor(_FakeDriver(), model="test-model", settings=settings)
    pkg = ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x",
            activity="Main",
            elements=[UIElement(index=1, text="Ready", bounds=[0, 0, 100, 100])],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=_png_bytes(900, 600),
        annotated_png=_png_bytes(900, 600),
        gap_reasons=[],
        observation_id="obs-anchor-order",
    )
    state = AgentState(
        instruction="Keep the exact requested task",
        current_subgoal="Wait for verification",
    )
    captured: dict[str, Any] = {}

    async def fake_call(
        obs_messages,
        history_messages=None,
        *,
        final_messages=None,
        observation_message_names=None,
        history_message_names=None,
        final_message_names=None,
        handlers=None,
        context_state=None,
        event_sink=None,
        model_call_meter=None,
    ):
        del handlers, event_sink, model_call_meter
        captured["obs_messages"] = obs_messages
        captured["history_messages"] = history_messages
        captured["final_messages"] = final_messages
        captured["observation_message_names"] = observation_message_names
        captured["history_message_names"] = history_message_names
        captured["final_message_names"] = final_message_names
        return ExecutorStep(action=_a("sleep", duration_ms=50), summary="pause"), SimpleNamespace(
            context_state={
                "active_package": context_state["active_package"],
                "active_observation_id": context_state["active_observation_id"],
            },
            request_snapshot={},
            tool_calls=[],
            llm_rounds=[],
        )

    monkeypatch.setattr(exe, "_call_with_session", fake_call)

    step, result, _ui, _mode, _refs = await exe.act_once(
        "Wait for verification",
        pkg,
        task_id="anchor-order",
        state=state,
    )

    assert result.success is True
    assert step.action and step.action.type == "sleep"
    assert captured["observation_message_names"] == ["observation"]
    assert captured["history_message_names"] == []
    assert captured["final_message_names"] == ["task_anchor"]
    assert captured["history_messages"] == []
    assert len(captured["final_messages"]) == 1
    assert captured["final_messages"][0]["content"].startswith("TASK ANCHOR:\n")
    assert captured["obs_messages"][0]["content"][0]["text"].startswith("CURRENT OBSERVATION:\n")


# ---------------------------------------------------------------------------
# Prompt rendering assertions
# ---------------------------------------------------------------------------


def test_executor_tool_schema_contains_required_action_parameters():
    from agent.session import tools_for_role

    submit = next(
        tool for tool in tools_for_role("executor")
        if tool["function"]["name"] == "submit_executor_step"
    )
    branches = submit["function"]["parameters"]["properties"]["action"]["oneOf"]
    swipe = next(branch for branch in branches if branch["properties"]["type"]["enum"] == ["swipe"])
    assert swipe["required"] == ["type", "x", "y", "x2", "y2"]
    key = next(branch for branch in branches if branch["properties"]["type"]["enum"] == ["key"])
    assert key["required"] == ["type", "key"]
    assert "duration_ms" in key["properties"]

# ---------------------------------------------------------------------------
# Tap target preservation
# ---------------------------------------------------------------------------


def _ui_with_elements(*els: UIElement) -> CanonicalUI:
    return CanonicalUI(elements=list(els))


def test_direct_tap_xy_inside_overlapping_elements_preserves_point():
    ui = _ui_with_elements(
        UIElement(index=0, text="outer", bounds=[0, 0, 400, 400]),
        UIElement(index=1, text="inner", bounds=[100, 100, 200, 200]),
    )
    submitted = Action(type="tap_xy", x=160, y=170)
    assert any(e.bounds[0] <= submitted.x <= e.bounds[2] for e in ui.elements)
    transformed, normalized, _ = validate_action_bounds(
        submitted, width=400, height=400, epsilon=1e-6,
    )
    assert not normalized
    assert transformed == submitted


def test_rescale_preserves_exact_transformed_visual_point():
    # 2K+ scale: long edge 3200 → 1080 (factor 3200/1080 ≈ 2.963); width 1440 → ~487 (factor ≈ 2.956).
    compressed = Action(type="tap_xy", x=132, y=940)
    sx, sy = 1440 / 487, 3200 / 1080
    rescaled = rescale_action_xy(compressed, sx=sx, sy=sy)
    assert rescaled.x == pytest.approx(132 * sx)
    assert rescaled.y == pytest.approx(940 * sy)


# ---------------------------------------------------------------------------
# rescale_action_xy (Executor coordinate adapter)
# ---------------------------------------------------------------------------


def test_rescale_action_xy_tap_xy_multiplies_xy():
    action = Action(type="tap_xy", x=132, y=940)
    out = rescale_action_xy(action, sx=1440 / 365, sy=3200 / 1080)
    # x ≈ 132 * (1440/365), y ≈ 940 * (3200/1080)
    assert abs(out.x - 132 * (1440 / 365)) < 0.01
    assert abs(out.y - 940 * (3200 / 1080)) < 0.01
    assert out.type == "tap_xy"
    assert out.index is None


def test_rescale_action_xy_swipe_multiplies_all_four_coords():
    action = Action(type="swipe", x=100, y=500, x2=200, y2=800, duration_ms=400)
    out = rescale_action_xy(action, sx=3.0, sy=3.0)
    assert out.x == 300
    assert out.y == 1500
    assert out.x2 == 600
    assert out.y2 == 2400
    assert out.duration_ms == 400


def test_rescale_action_xy_long_press_xy_no_index():
    action = Action(type="long_press", x=50, y=80, duration_ms=1000)
    out = rescale_action_xy(action, sx=2.0, sy=2.5)
    assert out.x == 100
    assert out.y == 200
    assert out.duration_ms == 1000


def test_rescale_action_xy_drag_multiplies_all_four_coords():
    action = Action(type="drag", x=10, y=20, x2=110, y2=220, duration_ms=500)
    out = rescale_action_xy(action, sx=4.0, sy=2.0)
    assert out.x == 40
    assert out.y == 40
    assert out.x2 == 440
    assert out.y2 == 440
    assert out.duration_ms == 500


def test_rescale_action_xy_index_only_unchanged():
    action = Action(type="tap", index=7)
    out = rescale_action_xy(action, sx=3.0, sy=3.0)
    assert out.type == "tap"
    assert out.index == 7
    assert out.x is None and out.y is None


def test_rescale_action_xy_noop_when_scale_is_one():
    action = Action(type="swipe", x=100, y=200, x2=300, y2=400)
    out = rescale_action_xy(action, sx=1.0, sy=1.0)
    assert out.x == 100
    assert out.y == 200
    assert out.x2 == 300
    assert out.y2 == 400


def test_rescale_action_xy_independent_axes():
    """sx and sy may differ (resize only scales the long edge)."""
    action = Action(type="tap_xy", x=100, y=200)
    out = rescale_action_xy(action, sx=2.5, sy=1.5)
    assert out.x == 250
    assert out.y == 300


def test_rescale_action_xy_preserves_other_fields():
    action = Action(type="swipe", x=1, y=2, x2=3, y2=4, duration_ms=300)
    out = rescale_action_xy(action, sx=2.0, sy=2.0)
    assert out.duration_ms == 300
    assert out.text is None
    assert out.key is None
    assert out.app is None
    assert out.direction is None


# ---------------------------------------------------------------------------
# Image-format invariant (data-URL media type MUST match the bytes)
# ---------------------------------------------------------------------------


def _capture_data_url(messages: list[dict[str, Any]]) -> tuple[bytes, str]:
    """Extract (decoded bytes, media type) from the image_url block of `messages`.

    Returns the FIRST image_url block found. Mirrors the capture pattern used
    in `test_compress_then_rerender_som_passes_compressed_frame_to_llm`.
    """
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if blk.get("type") != "image_url":
                continue
            url = blk["image_url"]["url"]
            # data:<media>;base64,<b64>
            media_type = url.split(";", 1)[0].split(":", 1)[1]
            b64 = url.split(",", 1)[1]
            return base64.b64decode(b64), media_type
    raise AssertionError("no image_url block in messages")


def _make_pkg(image_for_llm: bytes | None) -> "ObservationPackage":
    from perception.observation import ObservationPackage
    from shared.schemas import CanonicalUI, ObservationMode, UIElement

    return ObservationPackage(
        ui=CanonicalUI(
            app_id="com.x",
            activity="Main",
            elements=[UIElement(index=0, text="OK", bounds=[0, 0, 100, 100])],
        ),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="tree",
        image_for_llm=image_for_llm,
        annotated_png=image_for_llm,
        gap_reasons=["a11y_uncovered:0.5"],
        estimated_tokens=0,
        observation_id="obs-active",
    )


@pytest.mark.asyncio
async def test_build_messages_media_type_matches_bytes_three_paths(monkeypatch):
    """Regression: `_build_messages` MUST tag the data URL's media type to
    match the actual bytes. Three execution paths produce three different
    byte formats and the tag MUST follow each:

    (a) no-resize path: `compress_for_model` returns JPEG → `image/jpeg`.
    (b) resize path: `render_som` re-encodes to PNG → `image/png`.
    (c) decompression-failure path: invalid bytes fail closed and no image is
        sent to the provider.

    A mismatch triggers Anthropic's content validator (HTTP 400:
    "the image was specified using the image/jpeg media type, but the image
    appears to be a image/png image"). Sniffing the bytes is the fix.
    """
    from shared.config import Settings
    from agent.executor import _build_messages

    settings = Settings(
        gateway_max_retries=0,
        gateway_retry_base_delay=0.001,
        gateway_request_timeout=1.0,
        use_fixture_driver=True,
    )

    captured: dict[str, Any] = {}

    step_json = _submit_json(
        {"type": "sleep", "duration_ms": 1},
        thought="wait", summary="wait；inspect media transport",
    )

    async def fake_complete(model, messages, **kw):
        from shared.llm_gateway import GatewayResponse, ToolCall
        try:
            captured["bytes"], captured["media_type"] = _capture_data_url(messages)
        except AssertionError:
            captured["bytes"], captured["media_type"] = b"", ""
        return GatewayResponse(
            content="",
            model="test-model",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id="c1", name="submit_executor_step", arguments=step_json)],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)

    # ---- (a) no-resize path: 800×600 PNG → 800×600 JPEG, no resize ----------
    drv_a = _FakeDriver()
    exe_a = Executor(drv_a, model="test-model", settings=settings)
    captured.clear()
    await exe_a.act_once("subgoal", _make_pkg(_png_bytes(800, 600)))
    assert captured["bytes"], "(a) no image reached the LLM"
    # Decode must produce a JPEG (SOI marker).
    assert captured["bytes"][:3] == b"\xff\xd8\xff", (
        f"(a) expected JPEG bytes, got magic={captured['bytes'][:8]!r}"
    )
    assert captured["media_type"] == "image/jpeg", (
        f"(a) media type mismatch: {captured['media_type']!r} vs JPEG bytes"
    )

    # ---- (b) resize path: 1440×3200 PNG → 365×1080 JPEG → render_som PNG ---
    drv_b = _FakeDriver()
    exe_b = Executor(drv_b, model="test-model", settings=settings)
    captured.clear()
    await exe_b.act_once("subgoal", _make_pkg(_png_bytes(1440, 3200)))
    assert captured["bytes"], "(b) no image reached the LLM"
    # render_som re-encodes as PNG (signature `\x89PNG\r\n\x1a\n`).
    assert captured["bytes"][:8] == b"\x89PNG\r\n\x1a\n", (
        f"(b) expected PNG bytes (from render_som re-encode), got magic={captured['bytes'][:8]!r}"
    )
    assert captured["media_type"] == "image/png", (
        f"(b) media type mismatch: {captured['media_type']!r} vs PNG bytes"
    )

    # ---- (c) decompression-failure path: invalid bytes fail closed ----------
    drv_c = _FakeDriver()
    exe_c = Executor(drv_c, model="test-model", settings=settings)
    captured.clear()
    garbage = b"\x00\x01\x02 not a valid image"
    package = _make_pkg(garbage)
    await exe_c.act_once("subgoal", package)
    assert captured["bytes"] == b""
    assert captured["media_type"] == ""
    assert package.image_for_llm is None
    assert package.model_image_width == 0
    assert package.model_image_height == 0
    assert package.mode == ObservationMode.TREE_ONLY


@pytest.mark.asyncio
async def test_executor_image_only_invalid_role_image_never_calls_model(monkeypatch):
    package = _make_pkg(b"invalid-image")
    package.mode = ObservationMode.IMAGE_ONLY
    package.clean_png = b"invalid-image"
    package.image_for_llm = b"invalid-image"
    package.index_actionable = False
    calls = 0

    monkeypatch.setattr(
        "agent.executor.compress_for_model",
        lambda *args, **kwargs: (b"invalid-image", (0, 0), (0, 0)),
    )

    async def fake_complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("model must not receive an ungrounded observation")

    monkeypatch.setattr("agent.session.complete", fake_complete)
    with pytest.raises(ObservationStageError, match="role_image: decode_failed"):
        await Executor(_FakeDriver(), model="m").act_once("inspect", package)

    assert calls == 0
    assert package.accepted is False
    assert package.actionable is False
    assert package.acceptance_reason == "role_image_unavailable"


def test_sniff_image_media_type_recognizes_jpeg_and_png():
    """Direct unit test for `_sniff_image_media_type` covering the magic bytes."""
    from agent.executor import _sniff_image_media_type

    # JPEG: SOI marker \xff\xd8\xff (3 bytes minimum).
    assert _sniff_image_media_type(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "image/jpeg"
    # PNG: \x89PNG\r\n\x1a\n (8 bytes).
    assert _sniff_image_media_type(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") == "image/png"
    # GIF: GIF87a / GIF89a (6 bytes).
    assert _sniff_image_media_type(b"GIF87a\x00\x00") == "image/gif"
    assert _sniff_image_media_type(b"GIF89a\x00\x00") == "image/gif"
    # WEBP: RIFF....WEBP (12 bytes).
    assert _sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8") == "image/webp"
    # Unknown format defaults to image/jpeg (legacy tag; validator will reject).
    assert _sniff_image_media_type(b"unknown format") == "image/jpeg"
    # Truncated input is treated as unknown.
    assert _sniff_image_media_type(b"\xff") == "image/jpeg"
    assert _sniff_image_media_type(b"") == "image/jpeg"


def test_build_messages_no_image_does_not_emit_data_url():
    """When `image_bytes is None`, `_build_messages` MUST NOT emit an image_url
    block — the LLM gets a pure-text user message. Defensive check against a
    regression where the format-sniff path accidentally attaches a default
    image when the caller means "no image".
    """
    from agent.executor import _build_messages

    msgs = _build_messages("system", "user", None)
    assert len(msgs) == 2
    assert isinstance(msgs[1]["content"], str)
    assert msgs[1]["content"] == "user"
    assert "image_size" not in msgs[1]["content"]


def _message_texts(messages: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if blk.get("type") == "text":
                    texts.append(blk["text"])
    return texts


def test_build_messages_attaches_image_without_inline_size_annotation():
    """Landscape compressed frame is delivered as an image attachment only."""
    from agent.executor import _build_messages

    jpeg = _png_bytes(1080, 485)  # bytes don't need to be JPEG for this unit test
    msgs = _build_messages(
        "system",
        "user prompt",
        jpeg,
    )
    joined = "\n".join(_message_texts(msgs))
    assert joined == "system\nuser prompt"
    assert isinstance(msgs[1]["content"], list)
    image_blocks = [blk for blk in msgs[1]["content"] if blk.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert "2670x1200" not in joined
    assert "image_size:" not in joined
    assert "coordinate_space:" not in joined


def test_build_messages_omits_image_size_when_comp_size_unknown_and_undecodable():
    from agent.executor import _build_messages

    msgs = _build_messages("system", "user", b"not-an-image")
    joined = "\n".join(_message_texts(msgs))
    assert "image_size:" not in joined


# ---------------------------------------------------------------------------
# bounds validation (original-frame OOB guard)
# ---------------------------------------------------------------------------


def test_bounds_validation_rejects_landscape_oob_tap():
    """Compressed (360, 1200) rescaled onto 2670×1200 is materially invalid."""
    compressed = Action(type="tap_xy", x=360, y=1200)
    rescaled = rescale_action_xy(compressed, sx=2670 / 1080, sy=1200 / 485)
    assert rescaled.y is not None and rescaled.y > 1199
    bounded, normalized, evidence = validate_action_bounds(
        rescaled, width=2670, height=1200, epsilon=1e-6,
    )
    assert bounded is None and normalized is False
    assert evidence["reason"] == "coordinate_out_of_bounds"


def test_bounds_validation_inbounds_unchanged():
    action = Action(type="tap_xy", x=890, y=600)
    out, normalized, _ = validate_action_bounds(
        action, width=2670, height=1200, epsilon=1e-6,
    )
    assert normalized is False and out is not None
    assert out.x == 890
    assert out.y == 600


def test_bounds_validation_index_only_unchanged():
    action = Action(type="tap", index=3)
    out, normalized, _ = validate_action_bounds(
        action, width=2670, height=1200, epsilon=1e-6,
    )
    assert normalized is False and out is not None
    assert out.index == 3
    assert out.x is None and out.y is None


def test_bounds_validation_rejects_material_swipe_and_drag_endpoints():
    swipe = Action(type="swipe", x=-10, y=50, x2=3000, y2=2000)
    out, _, evidence = validate_action_bounds(
        swipe, width=2670, height=1200, epsilon=1e-6,
    )
    assert out is None and evidence["reason"] == "coordinate_out_of_bounds"

    drag = Action(type="drag", x=10, y=-5, x2=100, y2=5000, duration_ms=500)
    out2, _, evidence2 = validate_action_bounds(
        drag, width=1000, height=2000, epsilon=1e-6,
    )
    assert out2 is None and evidence2["reason"] == "coordinate_out_of_bounds"


@pytest.mark.asyncio
async def test_landscape_image_size_and_oob_tap_rejected(monkeypatch):
    """End-to-end: material image-space OOB input is rejected without dispatch."""
    from shared.config import Settings

    settings = Settings(
        gateway_max_retries=0,
        gateway_retry_base_delay=0.001,
        gateway_request_timeout=1.0,
        use_fixture_driver=True,
    )
    drv = _FakeDriver()
    exe = Executor(drv, model="test-model", settings=settings)
    captured: dict[str, Any] = {}

    step_json = _submit_json(
        {"type": "tap_xy", "x": 360, "y": 1200},
        thought="tap bottom", summary="tap；bottom chrome",
        basis_observation_id="obs-active",
    )

    async def fake_complete(model, messages, **kw):
        from shared.llm_gateway import GatewayResponse, ToolCall
        captured["texts"] = _message_texts(messages)
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for blk in content:
                    if blk.get("type") == "image_url":
                        b64 = blk["image_url"]["url"].split(",", 1)[1]
                        captured["img"] = _PILImage.open(io.BytesIO(base64.b64decode(b64)))
        return GatewayResponse(
            content="",
            model="test-model",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id="c1", name="submit_executor_step", arguments=step_json)],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)

    # 2670×1200 landscape → long edge 1080 → comp ≈ 1080×485
    pkg = _make_pkg(_png_bytes(2670, 1200))
    from shared.llm_gateway import GatewayError

    with pytest.raises(GatewayError, match="tool loop exhausted"):
        await exe.act_once("subgoal", pkg)

    joined = "\n".join(captured["texts"])
    img = captured["img"]
    assert img.size == (1080, 485) or (
        max(img.size) == 1080 and img.size[0] > img.size[1]
    )
    assert "image_size:" not in joined
    assert "coordinate_space:" not in joined

    assert drv.acts == []


def test_executor_system_prompt_declares_attached_image_coordinate_space():
    from agent.prompts import render_executor_system

    out = " ".join(render_executor_system().lower().split())
    assert "coordinates use the attached image’s `image_size`" in out
    assert "screen coordinates" not in out.lower()
