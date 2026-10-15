"""Keep malformed admission outputs recoverable without relaxing evidence gates."""
import copy
import json

import pytest

from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget, LearningStopped, REVIEW_SYSTEM
from driver.fixture import FixtureDriver
from shared.llm_gateway import GatewayError, GatewayResponse, ToolCall
from test_skill_evidence_admission import supported_review
from test_task_skill_learning import context


def setup(context, monkeypatch, outputs):
    settings, db, task, store, library = context
    driver = FixtureDriver()
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=driver, settings=settings, library=library)
    requests, records = [], []
    native_record = backend._record_research
    def record(role, phase, payload, budget, **kwargs):
        records.append({**kwargs, "payload":copy.deepcopy(payload)})
        return native_record(role, phase, payload, budget, **kwargs)
    monkeypatch.setattr(backend, "_record_research", record)
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        requests.append({"messages":copy.deepcopy(messages), "tools":kwargs["tools"]})
        assert kwargs["max_retries"] in (0, 1)
        output = outputs[len(requests)-1]
        if isinstance(output, Exception):
            raise output
        if isinstance(output, GatewayResponse):
            return output
        return GatewayResponse(model=model, content=json.dumps(output) if isinstance(output, dict) else output)
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    payload = {"candidate":{"target":"apps/com.demo/core/SKILL.md", "new_text":"Frozen rule"},
        "evidence":[{"source":"source/observation:visible@1", "text":"Observed local fact"}],
        "validation":{"validation_stage":"infrastructure_incomplete", "trials":[]}}
    return backend, driver, payload, requests, records


def contradictory():
    return {**supported_review(), "tests":[{"claim":"task improvement", "gap":"unscored trial",
        "check":"independent valid trial", "decision":"Keep utility unknown until scored"}]}


@pytest.mark.asyncio
async def test_same_review_corrects_contradiction_and_keeps_real_blocker(context, monkeypatch):
    invalid = contradictory()
    repaired = copy.deepcopy(invalid)
    repaired["verdict"] = "insufficient"
    repaired["criteria"]["root_cause"]["status"] = "insufficient"
    backend, driver, payload, requests, records = setup(context, monkeypatch, [invalid, repaired])
    budget = LearningBudget(max_calls=3)
    before = copy.deepcopy(payload)
    try:
        result = await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert result["verdict"] == "insufficient" and result["tests"] == invalid["tests"]
        assert requests[1]["messages"][:2] == requests[0]["messages"][:2]
        assert json.loads(requests[1]["messages"][-2]["content"]) == invalid
        assert "passing review cannot retain acceptance blockers" in requests[1]["messages"][-1]["content"]
        assert requests[1]["tools"] is None
        assert budget.calls == 2 and budget.actions == 0 and not driver.actions and payload == before
        assert len(budget.usage) == 2 and budget.snapshot()["provider_usage"]["unknown_responses"] == 2
        assert any(r.get("status") == "invalid_contract" and
            json.loads(r["payload"]["messages"][-1]["content"]) == invalid for r in records)
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_second_invalid_review_stops_without_third_call(context, monkeypatch):
    backend, driver, payload, requests, records = setup(context, monkeypatch, [contradictory(), contradictory()])
    budget = LearningBudget(max_calls=6)
    try:
        with pytest.raises(ValueError, match="passing review cannot retain acceptance blockers"):
            await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert len(requests) == budget.calls == 2 and not driver.actions
        assert sum(r.get("status") == "invalid_contract" for r in records) == 2
    finally:
        backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("preflight", [False, True])
async def test_repair_does_not_spend_reserved_final_review_or_renew_calls(context, monkeypatch, preflight):
    backend, driver, payload, requests, records = setup(context, monkeypatch, [contradictory()])
    budget = LearningBudget(max_calls=2 if preflight else 1)
    if preflight:
        payload.update(phase="draft_preflight", verification_checks=["source"])
    try:
        with pytest.raises(LearningStopped, match="review_format_repair_budget"):
            await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert budget.calls == len(requests) == 1 and not driver.actions
        assert budget.remaining_calls() == int(preflight)
        assert any(r.get("status") == "invalid_contract" for r in records)
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_transport_failure_is_not_a_review_format_retry(context, monkeypatch):
    fault = GatewayError("provider overloaded", category="transport")
    backend, driver, payload, requests, records = setup(context, monkeypatch, [fault])
    budget = LearningBudget(max_calls=6)
    try:
        with pytest.raises(GatewayError) as caught:
            await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert caught.value is fault and len(requests) == budget.calls == 1 and not driver.actions
        assert not any(r.get("status") == "invalid_contract" for r in records)
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_repair_cannot_dispatch_device_tool(context, monkeypatch):
    tool = GatewayResponse(model="test", content="", tool_calls=[
        ToolCall(id="forbidden", name="tap", arguments='{"index":0}')])
    backend, driver, payload, requests, records = setup(context, monkeypatch, [contradictory(), tool])
    budget = LearningBudget(max_calls=6)
    try:
        with pytest.raises(ValueError, match="correction permits no tools"):
            await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert budget.calls == 2 and budget.actions == 0 and not driver.actions
        assert records[-1]["status"] == "invalid_contract"
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_plain_invalid_json_has_the_same_single_correction_limit(context, monkeypatch):
    backend, driver, payload, requests, records = setup(context, monkeypatch, ["not JSON", supported_review()])
    budget = LearningBudget(max_calls=2)
    try:
        result = await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert result["verdict"] == "pass" and budget.calls == 2 and not driver.actions
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_group_repairs_one_patch_without_discarding_independent_review(context, monkeypatch):
    from test_task_skill_learning import candidate
    first = candidate().model_dump()
    second = {**first, "target":"apps/com.other/core/SKILL.md"}
    effect = {"status":"unassessed", "reason":"Unscored execution proves no local adoption", "evidence":[]}
    invalid = {"patch_reviews":{
        first["target"]:{**contradictory(), "effect":effect},
        second["target"]:{**supported_review(), "effect":effect}}}
    repaired = copy.deepcopy(invalid)
    repaired["patch_reviews"][first["target"]]["verdict"] = "insufficient"
    backend, driver, payload, requests, records = setup(context, monkeypatch, [invalid, repaired])
    payload["candidate"] = {"patches":[first, second], "dependencies":{}}
    budget = LearningBudget(max_calls=3)
    try:
        result = await backend.review("test", REVIEW_SYSTEM, payload, budget)
        assert result["patch_reviews"][first["target"]]["tests"]
        assert result["patch_reviews"][second["target"]] == invalid["patch_reviews"][second["target"]]
        assert budget.calls == 2 and not driver.actions
    finally:
        backend.close()
