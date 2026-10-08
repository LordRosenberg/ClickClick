"""Continuity, checkpoint validation and bounded reading for one learning job."""
import copy
import json
import pytest
from types import SimpleNamespace
from test_task_skill_learning import context, candidate
from agent.skills.analysis_session import validate_analysis, COMPACTION_SYSTEM
from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget
from shared.llm_gateway import GatewayResponse, ToolCall


def backend(context):
    settings, db, task, store, library = context
    return ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=SimpleNamespace(), settings=settings, library=library)


@pytest.mark.asyncio
async def test_phase_handoff_keeps_reads_decisions_and_analysis_but_new_job_isolated(context, monkeypatch):
    b = backend(context); store = context[3]
    store.put("note", "dependency", {"title": "dependency", "content": "Failed operation established a required setting"})
    before = copy.deepcopy(store.records("note"))
    calls = []
    analysis = {"prerequisites": [{"statement": "Setting remains necessary", "status": "observed", "evidence": ["source/note:dependency@1"]}],
        "unresolved": [{"statement": "Unknown shorter setup", "status": "unknown", "evidence": []}]}
    async def complete(model, messages, **kwargs):
        calls.append(copy.deepcopy(messages)); kwargs["attempt_meter"]("model_call_started", {})
        if len(calls) == 1:
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(id="read", name="read_history",
                arguments=json.dumps({"source": "note:dependency@1", "full": True}))])
        if len(calls) == 2:
            return GatewayResponse(model=model, content=json.dumps({"decision": "explore", "question": "Resolve shorter setup", "trajectory_analysis": analysis}))
        if len(calls) == 3:
            assert messages[0]["content"] == "writing phase"
            assert json.loads(messages[1]["content"])["review_feedback"] == {"changes": ["Check dependency"]}
            assert any(m.get("role") == "tool" and "required setting" in m["content"] for m in messages)
            assert any(m.get("role") == "assistant" and "Resolve shorter setup" in (m.get("content") or "") for m in messages)
            assert any("Unknown shorter setup" in str(m.get("content")) for m in messages)
        else:
            assert not any("Unknown shorter setup" in str(m.get("content")) for m in messages)
            assert not any(m.get("role") == "tool" for m in messages)
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No grounded omission"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget()
    await b.diagnose("test", "initial", {"source": {"instruction": b.task.instruction}, "diagnostic_read_rounds": 1}, budget)
    job = b.learner_conversation.id
    assert b.learner_conversation.checkpoint_message() == []  # Analysis already in the retained decision.
    await b.diagnose("test", "writing phase", {"source": {"instruction": b.task.instruction},
        "review_feedback": {"changes": ["Check dependency"]}, "diagnostic_read_rounds": 0}, budget)
    assert b.learner_conversation.id == job
    saved = json.loads(b.artifacts.read_text(b.learner_conversation.ref))
    assert saved["source_binding"]["task_id"] == b.task.id and saved["previous"]
    assert saved["analysis"]["prerequisites"][0]["status"] == "observed"
    await b.diagnose("test", "new job", {"diagnostic_read_rounds": 0}, LearningBudget())
    assert b.learner_conversation.id != job and store.records("note") == before


def test_analysis_rejects_fictional_evidence_and_requires_support(context):
    b = backend(context)
    with pytest.raises(ValueError):
        validate_analysis({"working_path": [{"statement": "fiction", "status": "observed", "evidence": ["event:FICTIONAL_A@1"]}]}, b.resolve_evidence)
    with pytest.raises(ValueError, match="require native evidence"):
        validate_analysis({"prerequisites": [{"statement": "guessed", "status": "observed"}]}, b.resolve_evidence)
    result = validate_analysis({"unresolved": [{"statement": "Could an unknown prerequisite matter?", "status": "unknown"}]}, b.resolve_evidence)
    assert result["unresolved"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_checkpoint_cannot_erase_history_on_invalid_evidence_or_spend_final_reserve(context, monkeypatch):
    b = backend(context); budget = LearningBudget(max_calls=8, reserve_calls=3)
    async def initial(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Dependency unresolved"}')
    monkeypatch.setattr("agent.skills.exploration.complete", initial)
    await b.diagnose("test", "initial", {"diagnostic_read_rounds": 0}, budget)
    session = b.learner_conversation
    messages = session.messages("next", {"source": {"instruction": b.task.instruction}})
    before = copy.deepcopy(session.history)
    async def bad(model, messages, **kwargs):
        assert messages[0]["content"] == COMPACTION_SYSTEM
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(model=model, content=json.dumps({"working_path": [{"statement": "invented", "status": "observed", "evidence": ["event:fiction@1"]}]}))
    monkeypatch.setattr("agent.skills.exploration.complete", bad)
    with pytest.raises(ValueError):
        await b._compact_diagnosis("test", messages, session, budget, 3)
    assert session.history[:len(before)] == before and session.compactions == 0
    budget.max_calls = budget.calls + 4
    with pytest.raises(Exception, match="analysis_checkpoint_budget"):
        await b._compact_diagnosis("test", messages, session, budget, 3)
    assert session.history[:len(before)] == before


@pytest.mark.asyncio
async def test_duplicate_reads_stop_without_consuming_all_budget(context, monkeypatch):
    b = backend(context); context[3].put("note", "same", {"title": "same", "content": "No new fact"})
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages); kwargs["attempt_meter"]("model_call_started", {})
        if kwargs.get("tools"):
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(id=str(len(calls)), name="read_history",
                arguments=json.dumps({"source": "note:same@1", "full": True}))])
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Reads made no progress"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    budget = LearningBudget(max_calls=20, reserve_calls=3)
    result = await b.diagnose("test", "analyze", {}, budget)
    assert result["decision"] == "skip" and budget.calls == 4 and budget.actions == 0
    assert b.learner_conversation.stalled_rounds == 2


@pytest.mark.asyncio
async def test_reviewer_gets_fallible_analysis_without_learner_conversation(context, monkeypatch):
    b = backend(context); budget = LearningBudget()
    calls = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        calls.append(messages)
        if len(calls) == 1:
            return GatewayResponse(model=model, content=json.dumps({"decision": "skip", "trajectory_analysis": {
                "unresolved": [{"statement": "Deletion dependency unverified", "status": "unknown", "evidence": []}]}}))
        payload = json.loads(messages[1]["content"])
        assert payload["learner_analysis"]["analysis"]["unresolved"][0]["status"] == "unknown"
        assert "not independent proof" in payload["learner_analysis"]["policy"]
        assert len(messages) == 2 and not any(m["role"] == "assistant" for m in messages)
        return GatewayResponse(model=model, content='{"verdict":"insufficient"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    await b.diagnose("test", "learner", {"diagnostic_read_rounds": 0}, budget)
    await b.review("test", "independent review", {"candidate": {"gist": "conditional omission"}}, budget)


@pytest.mark.asyncio
async def test_checkpoint_repairs_missing_citation_once_within_same_budget(context, monkeypatch):
    b = backend(context); budget = LearningBudget(max_calls=8, reserve_calls=3)
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages); kwargs["attempt_meter"]("model_call_started", {})
        if len(calls) == 1:
            return GatewayResponse(model=model, content='{"decision":"skip","reason":"Dependency unresolved"}')
        if len(calls) == 2:
            return GatewayResponse(model=model, content='{"working_path":[{"statement":"Task requirement","status":"observed"}]}')
        assert "Repair this checkpoint once" in messages[-1]["content"]
        return GatewayResponse(model=model, content='{"unresolved":[{"statement":"Dependency unresolved","status":"unknown","evidence":[]}]}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    await b.diagnose("test", "initial", {"diagnostic_read_rounds": 0}, budget)
    session = b.learner_conversation
    result = await b._compact_diagnosis("test", session.messages("next", {}), session, budget, 3)
    assert budget.calls == 3 and session.compactions == 1
    assert session.analysis["unresolved"][0]["status"] == "unknown"
    assert any("Dependency unresolved" in m.get("content", "") for m in result)


@pytest.mark.asyncio
async def test_handoff_reuses_latest_analysis_without_another_summary_request(context, monkeypatch):
    b = backend(context); budget = LearningBudget(max_calls=4, reserve_calls=2)
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages); kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(model=model, content='{"decision":"skip","trajectory_analysis":{"unresolved":[{"statement":"Need prerequisite evidence","status":"unknown"}]}}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    await b.diagnose("test", "initial", {"diagnostic_read_rounds": 0}, budget)
    session = b.learner_conversation
    assert session.analysis_current
    messages = session.messages("repair", {"candidate": "exact candidate", "review_feedback": "exact feedback"})
    result = await b._compact_diagnosis("test", messages, session, budget, 2)
    assert len(calls) == 1 and budget.calls == 1 and session.reused_checkpoints == 1
    assert "exact feedback" in result[1]["content"] and "Need prerequisite evidence" in result[2]["content"]
    session.save([*result, {"role": "assistant", "content": "New, not yet analyzed evidence"}])
    assert not session.analysis_current


def test_analysis_accepts_only_exact_frozen_skill_references(context):
    b = backend(context)
    skill = candidate()
    skill_path = b.library.root / skill.target
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(skill.new_text, encoding="utf-8")
    snapshot = b.freeze_library()
    path, content_hash = next(iter(snapshot.manifest["files"].items()))
    reference = "official_skill:" + path + "@" + content_hash
    result = validate_analysis({"prerequisites": [{"statement": "Existing guidance contains a rule, not proof of app behavior",
        "status": "observed", "evidence": [reference]}]}, b.resolve_analysis_evidence)
    assert result["prerequisites"][0]["evidence"] == [reference]
    with pytest.raises(ValueError, match="hash mismatch"):
        b.resolve_analysis_evidence("official_skill:" + path + "@" + "0" * 64)
    with pytest.raises(ValueError, match="frozen official library"):
        b.resolve_analysis_evidence("official_skill:../outside@" + content_hash)
    with pytest.raises(ValueError, match="Text is not an event reference"):
        validate_analysis({"omission_candidates": [{"steps": ["Text is not an event reference"], "condition": "known",
            "dependency_check": "unresolved", "changed_decision": "none", "status": "unknown", "evidence": [reference]}]}, b.resolve_analysis_evidence)

    with pytest.raises(ValueError, match="omitted steps require event references"):
        validate_analysis({"omission_candidates": [{"steps": [reference], "purpose": "Find a duration",
            "condition": "duration is already visible", "cost": "2 observed excess actions; net saving unmeasured",
            "dependency_check": "unresolved", "changed_decision": "inspect current surface", "status": "unknown", "evidence": [reference]}]}, b.resolve_analysis_evidence)


@pytest.mark.asyncio
async def test_diagnose_handoff_marker_does_not_force_redundant_summary(context, monkeypatch):
    b = backend(context); budget = LearningBudget(max_calls=6, reserve_calls=2)
    calls = []
    async def complete(model, messages, **kwargs):
        assert messages[0]["content"] != COMPACTION_SYSTEM
        calls.append(messages); kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(model=model, content=json.dumps({"decision": "skip",
            "reason": "x" * 44000 if len(calls) == 1 else "Still unresolved",
            "trajectory_analysis": {"unresolved": [{"statement": "Net saving unknown", "status": "unknown"}]}}))
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    await b.diagnose("test", "initial", {"diagnostic_read_rounds": 0}, budget)
    await b.diagnose("test", "revision", {"diagnostic_read_rounds": 1, "review_feedback": "Keep scope"}, budget)
    assert len(calls) == budget.calls == 2
    assert b.learner_conversation.reused_checkpoints == 1
    assert "Keep scope" in calls[-1][1]["content"]
    assert any("Net saving unknown" in m.get("content", "") for m in calls[-1])
