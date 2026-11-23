from types import SimpleNamespace

import pytest

from evaluation import gui_skill_auto_plan as planner
from evaluation.gui_skill_case_recording import RecordingCaseRequest
from evaluation.gui_skill_case_registry import CaseIdentity


APP = "com.example.timer"
TARGET = f"apps/{APP}/core/SKILL.md"
GOAL = "Clear the timer draft."


def present(text):
    return {"type": "target_present", "target": {"text": text}}


def intent(**changes):
    return {"setup_goal": "Show a 12-second draft on the timer keypad.", "goal": GOAL,
            "result": [present("0 seconds")], "guards": [present("Timer keypad")],
            "branch": [present("12 seconds")], "branch_after": [present("0 seconds")],
            "branch_action": "long_press", **changes}


def record(key, **payload):
    return {"key": key, "version": 1, "payload": payload}


class Store:
    task_id = "native-timer"

    def __init__(self, *, dispatched=True, wrong_effect=False):
        before = record("before", app=APP, text=f'foreground_package={APP}\n'
                        'TextView raw_text="12 seconds"\nTextView raw_text="Timer keypad"\n'
                        '[2] ImageButton raw_a11y_label="Backspace" id=delete')
        after = record("after", app=APP, text=f'foreground_package={APP}\n'
                       'TextView raw_text="0 seconds"\nTextView raw_text="Timer keypad"')
        receipt = {"transaction_id": "verified", "dispatch_succeeded": dispatched, "observation_accepted": True,
                   "effect_outcome": "confirmed", "effect_observation_id": "after"}
        action = {"type": "long_press", "index": 2, "duration_ms": 800}
        event = record("1", decision="act", submitted_action=action, observation_id="before",
                       action_result={"success": True, "receipt": receipt})
        self.entries = {"observation": [before, after], "event": [event]}
        native = dict(receipt, effect_observation_id="different") if wrong_effect else receipt
        self.db = SimpleNamespace(list_traces=lambda task_id: [SimpleNamespace(kind="executor_tick", payload={
            "decision": "act", "action": action, "observation_accepted": True, "basis_observation_id": "before",
            "observation_id": "before", "action_result": {"success": True, "receipt": native},
        })])

    def records(self, kind):
        return self.entries.get(kind, [])

    def get(self, kind, key, version):
        return next(row for row in self.records(kind) if (key, version) == (row["key"], row["version"]))


class Registry:
    def __init__(self, *cases):
        self.cases = cases

    def find_for_skill(self, *, skill_target, app_id, build):
        assert (skill_target, app_id, build) == (TARGET, APP, "7")
        return [(CaseIdentity(TARGET, key, APP, "7"), SimpleNamespace(case=case)) for key, case in self.cases]


def input_for(registry=None, store=None):
    return dict(task=SimpleNamespace(id=Store.task_id, instruction=GOAL), store=store or Store(),
                candidate=SimpleNamespace(target=TARGET, gist="Prefer the learned long-press action", new_text="new\n",
                                          evidence=["source/event:1@1"]), old_text="old\n", app_id=APP, build="7",
                registry=registry or Registry(), settings=object(), budget=object())


def mock_planning(monkeypatch, response):
    inputs = []

    async def ask(settings, budget, model, role, system, packet):
        assert model == "learner" and role == "skill_learner"
        inputs.append(packet)
        return response

    monkeypatch.setattr(planner, "_ask", ask)
    monkeypatch.setattr(planner, "resolve_learner_model", lambda _: "learner")
    return inputs


@pytest.mark.asyncio
async def test_reuse_offers_only_native_witnessed_registered_case(monkeypatch):
    right = RecordingCaseRequest.model_validate({**intent(), "app_id": APP, "build": "7", "branch_expected": True})
    wrong = right.model_copy(update={"branch_action": "tap"})
    inputs = mock_planning(monkeypatch, {"decision": "reuse", "case_key": "right", "event": "event:1@1"})
    result = await planner.plan_skill_case(**input_for(Registry(("wrong", wrong), ("right", right))))
    assert result.case_key == "right" and result.recording is None
    assert result.evidence == ("event:1@1", "observation:before@1", "observation:after@1")
    assert set(inputs[0]["witnessed_existing_cases"]) == {"right"}
    assert "-old\n+new" in inputs[0]["diff"]


@pytest.mark.asyncio
async def test_exact_case_takes_priority_when_model_redundantly_drafts(monkeypatch):
    existing = RecordingCaseRequest.model_validate({**intent(), "app_id": APP, "build": "7", "branch_expected": True})
    mock_planning(monkeypatch, {"decision": "record", "intent": intent(), "event": "event:1@1"})
    result = await planner.plan_skill_case(**input_for(Registry(("owned", existing))))
    assert result.case_key == "owned" and result.recording is None


@pytest.mark.asyncio
async def test_missing_case_uses_stable_identity_and_host_device_scope(monkeypatch):
    mock_planning(monkeypatch, {"decision": "record", "intent": intent(), "event": "event:1@1"})
    first = await planner.plan_skill_case(**input_for())
    second = await planner.plan_skill_case(**input_for())
    assert first == second and first.case_key.startswith("auto-")
    assert first.recording.app_id == APP and first.recording.build == "7" and first.recording.branch_expected is True
    assert first.recording.parameters == {}


@pytest.mark.asyncio
async def test_planner_selects_only_engine_verified_facts(monkeypatch):
    first = input_for()
    first["store"].entries["observation"][0]["payload"]["text"] += (
        '\nTextView raw_text="Duplicate"\nTextView raw_text="Duplicate"'
    )
    monkeypatch.setattr(planner, "resolve_learner_model", lambda _: "learner")

    async def ask(settings, budget, model, role, system, packet):
        event = packet["events"][0]
        facts = event["condition_facts"]
        assert not any(target.get("text") == "Duplicate" for group in facts.values() for target in group.values())
        before = next(key for key, target in facts["before_only"].items() if target.get("text") == "12 seconds")
        after = next(key for key, target in facts["after_only"].items() if target.get("text") == "0 seconds")
        guard = next(key for key, target in facts["common"].items() if target.get("text") == "Timer keypad")
        return {"decision": "record", "event": event["source"], "intent": {
            "goal": GOAL, "setup_goal": "Leave the timer keypad at 12 seconds.",
            "branch": [before], "branch_after": [after], "result": [after], "guards": [guard],
        }}

    monkeypatch.setattr(planner, "_ask", ask)
    result = await planner.plan_skill_case(**first)
    assert result.recording.branch_action == "long_press"
    assert result.recording.branch[0].target.text == "12 seconds"
    assert result.recording.result[0].target.text == "0 seconds"
    assert result.recording.guards[0].target.text == "Timer keypad"


@pytest.mark.asyncio
async def test_fact_cannot_be_reallocated_to_unverified_phase(monkeypatch):
    mock_planning(monkeypatch, {"decision": "record", "event": "event:1@1", "intent": {
        "goal": GOAL, "setup_goal": "Leave the timer keypad at 12 seconds.",
        "branch": ["b1"], "branch_after": ["a1"], "result": ["g1"], "guards": ["g1"],
    }})
    result = await planner.plan_skill_case(**input_for())
    assert result.case_key is None and result.reason == "case_plan_fact_not_offered"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"decision": "reuse", "case_key": "invented", "event": "event:1@1"},
    {"decision": "record", "event": "event:1@1", "intent": intent(result=[present("invented")])},
    {"decision": "record", "event": "event:1@1", "intent": intent(branch_after=[present("Timer keypad")])},
    {"decision": "record", "event": "event:1@1", "intent": intent(app_id="com.offscope.app")},
    {"decision": "record", "event": "event:1@1", "intent": intent(setup_goal=GOAL)},
    {"decision": "record", "event": "event:9@1", "intent": intent()},
])
async def test_unbound_choice_can_never_reach_device(monkeypatch, response):
    mock_planning(monkeypatch, response)
    result = await planner.plan_skill_case(**input_for())
    assert result.case_key is None and result.recording is None and result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("store", [Store(dispatched=False), Store(wrong_effect=True)])
async def test_declines_without_matching_native_dispatch_and_effect(monkeypatch, store):
    inputs = mock_planning(monkeypatch, {"decision": "record", "intent": intent(), "event": "event:1@1"})
    result = await planner.plan_skill_case(**input_for(store=store))
    assert result.reason == "no_witnessed_case_branch" and not inputs
