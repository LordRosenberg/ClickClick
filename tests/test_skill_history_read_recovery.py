"""Invalid history arguments stay untrusted but do not discard a learning job."""
import copy
import json
from types import SimpleNamespace

import pytest
from test_task_skill_learning import context
from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget
from shared.llm_gateway import GatewayResponse, ToolCall


@pytest.mark.parametrize("arguments", [
    '{"namespace":"other","source":"source/note:fact@1"}',
    '{"namespace":"unknown","source":"note:fact@1"}',
    '{"view":"image"}',
    '{bad json',
    '[]',
])
@pytest.mark.asyncio
async def test_failed_read_then_valid_read_preserves_scope_and_dialogue(context, monkeypatch, arguments):
    settings, db, task, store, library = context
    store.put("note", "fact", {"title": "fact", "content": "Literal retained evidence"})
    b = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=SimpleNamespace(), settings=settings, library=library)
    b.add_history("other", store, task.state)
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append(copy.deepcopy(messages))
        kwargs["attempt_meter"]("model_call_started", {})
        if len(calls) == 1:
            return GatewayResponse(model=model, content="", tool_calls=[
                ToolCall(id="invalid-read", name="read_history", arguments=arguments)])
        if len(calls) == 2:
            output = next(m for m in messages if m.get("tool_call_id") == "invalid-read")
            failed = json.loads(output["content"])
            assert failed["status"] == "failed" and "data" not in failed
            assert "No evidence supplied" in failed["policy"]
            assert not any("Literal retained evidence" in m.get("content", "")
                for m in messages if isinstance(m.get("content"), str))
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(
                id="correct-read", name="read_history", arguments=json.dumps({
                    "namespace": "other", "source": "note:fact@1", "full": True}))])
        assert len(calls) == 3
        output = next(m for m in messages if m.get("tool_call_id") == "correct-read")
        assert "other/note:fact@1" in output["content"]
        assert "Literal retained evidence" in output["content"]
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No new supported rule"}')

    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget(max_calls=12, max_actions=0)
    result = await b.diagnose("test", "Read-only assessment", {
        "source": {"instruction": task.instruction}, "diagnostic_read_rounds": 3}, budget)
    assert result["decision"] == "skip"
    assert budget.calls == 3 and budget.actions == 0
    archive = json.loads(store.artifacts.read_text(b.learner_conversation.ref))
    outputs = [m for m in archive["history"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in outputs] == ["invalid-read", "correct-read"]
    assert json.loads(outputs[0]["content"])["status"] == "failed"
    b.close()
