import time
from unittest.mock import Mock
from argparse import Namespace
from types import SimpleNamespace

import pytest

from evaluation.mobileworld.hitl import HumanSimulationError, MobileWorldHumanTransport, validate_category
from shared.config import Settings
from shared.db import Database
from shared.hitl import HumanInputBroker, HitlConfig, InputConflict
from shared.schemas import AgentState, TaskStatus


@pytest.fixture
def pending(tmp_path):
    db = Database(tmp_path / "clickclick.db")
    state = AgentState(instruction="Ask a missing preference")
    state.revisable.limits.prediction_rounds = 5
    task = db.create_task(state.instruction, state)
    db.update_task(task.id, status=TaskStatus.RUNNING)
    broker = HumanInputBroker(db, settings=Settings(_env_file=None))
    broker.configure(task.id, HitlConfig(enabled=True, mode="delegated"), origin="integration")
    item = broker.begin(task.id, {"questions": [{"id": "time", "question": "What time?"}]},
                        {}, role="planner", invocation_id="invocation", call_id="call")
    yield db, broker, task.id, item
    db.close()


def response(*args, **kwargs):
    return {**kwargs["json"], "result": "At 1:20 PM."}


def test_official_transport_single_result_and_console_exclusion(pending):
    db, broker, task_id, item = pending
    request = Mock(side_effect=response)
    transport = MobileWorldHumanTransport(broker, request, "http://fixture", "emulator")
    assert broker.public(task_id)["questions"] == []
    with pytest.raises(InputConflict):
        broker.takeover(task_id)
    transport.dispatch(task_id)
    assert request.call_count == 1
    assert request.call_args.kwargs["json"] == {
        "device": "emulator", "action": {"action_type": "ask_user", "text": "What time?"}}
    result = broker.continuation(task_id, "planner")
    assert result["call_id"] == "call" and result["invocation_id"] == "invocation"
    assert result["answers"] == [{"question_id": "time", "text": "At 1:20 PM."}]
    assert result["provenance"] == "delegated"
    assert db.get_task(task_id).state.revisable.prediction_round_count == 1
    with pytest.raises(InputConflict):
        transport.dispatch(task_id)
    assert request.call_count == 1
    assert transport.report(task_id)["query_count"] == 1


@pytest.mark.parametrize("fault", ["network", "wrong_device", "wrong_action", "empty", "long", "shape"])
def test_fault_is_unscored_and_never_reissued(pending, fault):
    db, broker, task_id, item = pending
    def send(*args, **kwargs):
        if fault == "network":
            raise RuntimeError("secret provider URL must not be persisted")
        value = response(*args, **kwargs)
        if fault == "wrong_device": value["device"] = "other"
        if fault == "wrong_action": value["action"] = {"action_type": "click"}
        if fault == "empty": value["result"] = " "
        if fault == "long": value["result"] = "x" * 4097
        if fault == "shape": return []
        return value
    request = Mock(side_effect=send)
    transport = MobileWorldHumanTransport(broker, request, "http://fixture", "emulator")
    with pytest.raises(HumanSimulationError): transport.dispatch(task_id)
    with pytest.raises(HumanSimulationError, match="no_automatic_replay"): transport.dispatch(task_id)
    assert request.call_count == 1
    assert broker.current(task_id)["status"] == "pending"
    assert "secret provider URL" not in str(transport.report(task_id))


def test_crash_after_response_reuses_receipt_not_provider(pending, monkeypatch):
    db, broker, task_id, item = pending
    request = Mock(side_effect=response)
    original = broker.reply
    monkeypatch.setattr(broker, "reply", Mock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        MobileWorldHumanTransport(broker, request, "http://fixture", "emulator").dispatch(task_id)
    monkeypatch.setattr(broker, "reply", original)
    restored = HumanInputBroker(db)
    MobileWorldHumanTransport(restored, request, "http://fixture", "emulator").dispatch(task_id)
    assert request.call_count == 1
    assert db.get_task(task_id).state.revisable.prediction_round_count == 1
    assert restored.continuation(task_id, "planner")["outcome"] == "answered"


@pytest.mark.parametrize("expired", [True, False])
def test_exhausted_budget_or_deadline_does_not_dispatch(pending, expired):
    db, broker, task_id, item = pending
    if expired:
        item["deadline_at"] = time.time() - 1
        broker._save(item)
    else:
        task = db.get_task(task_id)
        task.state.revisable.prediction_round_count = 5
        db.update_task(task_id, state=task.state)
    request = Mock()
    with pytest.raises(HumanSimulationError):
        MobileWorldHumanTransport(broker, request, "http://fixture", "emulator").dispatch(task_id)
    request.assert_not_called()


@pytest.mark.parametrize("tags,enabled,valid", [
    ([], False, True), (["agent-user-interaction"], True, True),
    ([], True, False), (["agent-user-interaction"], False, False),
    (["agent-mcp", "agent-user-interaction"], True, False),
])
def test_frozen_category_admission(tags, enabled, valid):
    if valid:
        validate_category({"tags": tags}, enabled)
    else:
        with pytest.raises(ValueError): validate_category({"tags": tags}, enabled)


@pytest.mark.asyncio
async def test_episode_resumes_two_distinct_questions_without_console_or_budget_reset(monkeypatch, tmp_path):
    from evaluation.mobileworld import run_single
    monkeypatch.setattr(run_single, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(Settings, "apply_chatgpt_token_dir", lambda self: None)
    monkeypatch.setattr(run_single, "get_driver", lambda *a, **kw: object())
    request = Mock(side_effect=response)
    monkeypatch.setattr(run_single, "_request", request)
    monkeypatch.setattr(run_single, "_submit_final_answer", lambda *a: {"result": "OK"})
    def create(db, traces, **kwargs):
        calls = []
        async def run(task_id):
            broker = HumanInputBroker(db)
            task = db.get_task(task_id)
            assert broker.config(task_id)["mode"] == "delegated"
            assert task.state.revisable.prediction_round_count == len(calls)
            if calls:
                assert broker.continuation(task_id, "planner")["call_id"] == "call" + str(len(calls))
                broker.complete(task_id)
            if len(calls) == 2:
                task.state.revisable.completion_reason = "Both answers received"
                db.update_task(task_id, status=TaskStatus.SUCCEEDED, state=task.state)
                return TaskStatus.SUCCEEDED
            db.update_task(task_id, status=TaskStatus.RUNNING)
            calls.append(1)
            broker.begin(task_id, {"questions": [{"id": "q" + str(len(calls)),
                            "question": "Missing information " + str(len(calls)) + "?"}]}, {},
                         role="planner", invocation_id="invocation", call_id="call" + str(len(calls)))
            assert not broker.public(task_id)["can_answer"]
            return TaskStatus.WAITING_FOR_INPUT
        return SimpleNamespace(run_task=run)
    monkeypatch.setattr(run_single, "create_orchestrator", create)
    args = Namespace(max_steps=5, max_model_calls=None, max_seconds=60, data_dir=str(tmp_path),
                     model="test/model", collector_apk="a.apk", ime_apk="b.apk", target="fixture",
                     backend="http://fixture", environment_device="fixture", enable_user_interaction=True)
    result = await run_single._run_clickclick(args, "Two missing preferences")
    assert result["clickclick_status"] == "succeeded"
    assert result["mobileworld_rounds"] == 3
    assert result["logical_action_steps"] == 0
    assert result["human_interaction"]["query_count"] == request.call_count == 2
