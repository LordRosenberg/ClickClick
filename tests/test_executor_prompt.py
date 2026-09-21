"""Semantic safeguards for the plan-driven Executor policy."""
import pytest
from agent.prompts import render_executor_system

@pytest.mark.parametrize("rules", [
    ("End with one accepted `submit_executor_step`", "echoing the current `observation_id`"),
    ("actual text, accessibility label, hint or pixels", "does not establish identity"),
    ("repeated attempts without relevant progress", "supported change of target or method"),
    ("a request for a new item still requires creation", "Do not create new duties"),
    ("Use `sleep` when elapsed time itself is required", "the next decision receives a fresh observation"),
    ("`type` inserts at the cursor", "replaces its full value", "not that the record was saved"),
    ("A skill is guidance", "current evidence controls", "before any later-stage action"),
    ("use `launch(app=name_or_package)` directly", "A resolver miss dispatches nothing", "`resolution_ticket`"),
    ("`index_actions_available=true`", "Coordinates use the attached image's `image_size`", "Historical frames and indices cannot ground actions"),
    ("`interaction_ack=confirmed` proves only", "`visible_change=none`", "Match the relevant resulting state to the goal"),
])
def test_executor_semantic_safeguards(rules):
    prompt = " ".join(render_executor_system().split())
    for rule in rules:
        assert rule in prompt

def test_decision_gates_precede_routes_and_grounding():
    prompt = render_executor_system()
    sections = ["## Role", "## Decision process", "## Open a named app",
                "## Ground one action", "## Evidence and working memory", "## Tool protocol"]
    offsets = [prompt.index(section) for section in sections]
    assert offsets == sorted(offsets)
    gates = [prompt.index(gate) for gate in ("1. **Conflict.**", "2. **Stop.**", "3. **Recover.**", "4. **Act.**")]
    assert gates == sorted(gates)
