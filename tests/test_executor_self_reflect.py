"""Executor v2 timeline review and current-observation contracts."""

from __future__ import annotations

from agent.executor import build_executor_prompt
from perception.observation import ObservationPackage
from shared.schemas import AgentState, CanonicalUI, ObservationMode


def _package() -> ObservationPackage:
    return ObservationPackage(
        ui=CanonicalUI(app_id="demo", activity=".A"),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="(root)", image_for_llm=None, annotated_png=None,
        gap_reasons=[], observation_id="obs-current",
    )


def test_executor_history_uses_one_active_timeline_without_legacy_reflect():
    state = AgentState(instruction="打开目标", current_subgoal="打开目标")
    _, _, history, observation = build_executor_prompt(
        "打开目标", _package(), state=state,
    )
    import json

    obs = json.loads(observation.removeprefix("CURRENT OBSERVATION:\n"))
    assert history == ""
    assert "last_tick_reflect" not in history + observation
    assert obs["foreground_package"] == "demo"
    assert "action_observation_id" not in obs


def test_executor_prompt_requests_tools_not_next_tick_image_field():
    from agent.prompts import render_executor_system

    prompt = " ".join(render_executor_system().split())
    assert "Use `observe_screen`" in prompt
    assert "basis_observation_id" not in prompt
    assert "need_image" not in prompt
    assert "rather than observing again for confidence" in prompt
