"""Stable semantic-policy tests for the compact Executor prompt."""

from pathlib import Path


_PROMPT_PATH = Path(__file__).resolve().parents[1] / "agent" / "prompts" / "executor_system.md"


def _prompt() -> str:
    return " ".join(_PROMPT_PATH.read_text(encoding="utf-8").lower().split())


def test_prompt_defers_transport_contract_to_registered_tool():
    prompt = _prompt()
    assert "`submit_executor_step` exactly once" in prompt
    assert "basis_observation_id" not in prompt
    assert "followed_skill" not in prompt
    assert "subgoal_done" not in prompt


def test_prompt_makes_visual_semantics_and_action_consistency_model_owned():
    prompt = _prompt()
    assert "reconcile the missing effect" in prompt
    assert "raw text, accessibility label, hint, or pixels" in prompt
    assert "submitted target" in prompt
    assert "harness validates the target" not in prompt


def test_prompt_uses_compact_semantic_history_for_root_cause_recovery():
    prompt = _prompt()
    assert "semantic timeline is memory" in prompt
    assert "dispatch and capture do not prove the intended effect" in prompt
    assert "repeated same-purpose actions without relevant progress" in prompt
    assert "reconsider target, grounding, feasibility, or method" in prompt


def test_prompt_separates_current_state_from_task_local_history():
    prompt = _prompt()
    assert "current ui proves state, not that a task-local operation occurred" in prompt
    assert "inherited result" in prompt
    assert "perform a missing required search, submit, refresh" in prompt


def test_prompt_distinguishes_observation_waiting_and_text_entry():
    prompt = _prompt()
    assert "observe_screen(current|temporal)" in prompt
    assert "`sleep` only delays page loading and proves nothing" in prompt
    assert "`type` inserts at the cursor" in prompt
    assert "`replace_text` clears the focused editable first" in prompt
    assert "focused editable value" in prompt


def test_prompt_keeps_completion_and_skill_authority_concise():
    prompt = _prompt()
    assert "`request_review`" in prompt
    assert "`request_replan`" in prompt
    assert "all its workflows are already supplied" in prompt
    assert "apply only relevant guidance" in prompt
