from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from agent.revisable.store import TaskStore
from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget, READONLY_SYSTEM, SCRIPT_RECOVERY_POLICY, run_task_learning
from agent.skills.library import SkillLibrary
from agent.skills.script_learning import _Evidence, _frame, _trace
from agent.skills.script_learning_signals import record_script_skill_signal
from agent.skills.script_schema import GuiScript, script_fingerprint
from agent.skills.snapshot import LibrarySnapshot
from evaluation import gui_skill_auto_plan, gui_skill_loop
from evaluation.gui_skill_replay_evidence import _observed_target, script_repair_histories
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.llm_gateway import GatewayResponse
from shared.schemas import TaskStatus


APP = "example.notes"
GOAL = "Open the details panel for the current note."


def present(label):
    return {"type": "target_present", "target": {"text": label}}


@pytest.mark.parametrize(("target_index", "native_target", "expected"), [
    (None, '[4] Button raw_text="A control" id=control', None),
    (5, '[4] Button raw_text="A control" id=control', None),
    (4, '[4] EditText raw_text="Private value" raw_a11y_label="Private description" '
        'raw_hint="Private hint" editable password id=secret', None),
    (4, '[4] Button raw_text="Private label" raw_fields_redacted=true id=secret', None),
    (4, '[4] EditText raw_text="Typed note title" raw_a11y_label="Title editor" '
        'raw_hint="A title" editable id=title',
        {"role": "EditText", "resource_id": "title", "desc": "Title editor", "hint": "A title"}),
    (4, '[4] Button raw_text="Details" raw_a11y_label="' + "a" * 121 + '" id=details',
        {"role": "Button", "resource_id": "details", "text": "Details"}),
])
def test_observed_replay_target_requires_native_binding_and_limits_private_text(target_index, native_target, expected):
    before = _frame({"key": "before", "version": 1, "payload": {
        "app": APP, "text": f"foreground_package={APP}\n{native_target}",
    }})
    event = _Evidence(source="event:1@1", before=before, after=before, action="tap", target_index=target_index,
                      direction=None, input_value=None, duration_ms=None)
    assert _observed_target(event) == expected


@pytest.mark.asyncio
async def test_verified_recovery_can_supply_a_real_regression_branch(monkeypatch, tmp_path):
    script = GuiScript.model_validate({
        "script_id": "details", "app_id": APP, "initial": [present("Current note")],
        "steps": [{"id": "details", "before": [present("Current note")], "after": [present("Details")],
                   "action": {"type": "tap", "target": {"text": "Open details"}}}],
        "final": [present("Details")],
    })
    source_runtime = tmp_path / "source"
    replay_runtime = tmp_path / "replay"
    settings = Settings(_env_file=None, data_dir=source_runtime, manager_model="test-model")
    options = SimpleNamespace(env_file=None, model="test-model", serial="unused")
    library = LibrarySnapshot.freeze(tmp_path / "skills", tmp_path / "frozen")
    requests = []

    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        payload, = [value for message in messages if message["role"] == "user"
                    if (value := json.loads(message["content"])).get("initial_diagnosis")]
        requests.append((messages[0]["content"], payload))
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No useful draft"}')

    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    with closing(Database(settings.db_path)) as source_db, closing(Database(replay_runtime / "clickclick.db")) as replay_db:
        source_task = source_db.create_task(GOAL)
        replay_task = replay_db.create_task(GOAL)
        source_db.update_task(source_task.id, status=TaskStatus.SUCCEEDED)
        replay_db.update_task(replay_task.id, status=TaskStatus.SUCCEEDED)
        source_task, replay_task = source_db.get_task(source_task.id), replay_db.get_task(replay_task.id)
        assets = ArtifactStore(settings.artifacts_dir)
        source = TaskStore(source_db, assets, source_task.id)
        replay = TaskStore(replay_db, ArtifactStore(replay_runtime / "artifacts"), replay_task.id)
        replay.put("observation", "before", {"app": APP, "accepted": True, "text":
                   f'foreground_package={APP}\nTextView raw_text="Current note"\n'
                   'TextView raw_text="List"\n[4] Button raw_text="Details panel" clickable id=details'})
        replay.put("observation", "after", {"app": APP, "accepted": True, "text":
                   f'foreground_package={APP}\nTextView raw_text="Current note"\nTextView raw_text="Details"'})
        leading_controls = 'TextView raw_text="Unrelated editor content"\n' * 140
        replay.put("observation", "tools", {"app": APP, "accepted": True, "text":
                   f'foreground_package={APP}\nTextView raw_text="Current note"\n'
                   f'{leading_controls}[3] Button raw_text="Different action" clickable id=other_action\n'
                   '[4] Button raw_text="Advanced toolbar" raw_a11y_label="Open note details" '
                   'raw_hint="Shows details" clickable id=advanced_toolbar'})
        replay.put("event", "1", {"step": 1, "script_step_id": "details", "origin": "takeover",
                   "decision": "act", "observation_id": "before", "submitted_action": {"type": "scroll", "direction": "left"},
                   "action_result": {"success": True, "receipt": {"transaction_id": "first", "dispatch_succeeded": True,
                   "observation_accepted": True, "observation_id": "tools", "effect_outcome": "confirmed"}}})
        replay.put("event", "2", {"step": 2, "script_step_id": "details", "origin": "takeover",
                   "decision": "act", "observation_id": "tools", "submitted_action": {"type": "tap", "index": 4},
                   "action_result": {"success": True, "receipt": {"transaction_id": "second", "dispatch_succeeded": True,
                   "observation_accepted": True, "observation_id": "after", "effect_outcome": "confirmed"}}})
        final = {"receipt": {"status": "succeeded", "assisted": True, "final_asserted": True, "script_id": "details",
                 "script_hash": script_fingerprint(script), "revision": 1, "failed_step_id": "details",
                 "final_observation_id": "after"},
                 "fixture": {"app_build": "22"}}
        replay.put("gui_script_run", "final", final)
        signal, status = record_script_skill_signal(raw_hint={"condition": "App build 22 in the note list",
            "rule": "Scroll the toolbar to open the Details panel", "evidence": ["event:2@1"]}, task=replay_task,
            replay_store=replay, signal_store=source, source_task_id=source_task.id, base=script,
            evidence=_trace(replay_task, replay, takeover_for="details"), saved=final)
        assert status == "recorded"
        with script_repair_histories(source_db, source_task) as histories:
            history, = histories
            namespace = history.namespace
            reference = namespace + "/event:1@1"
            candidate = SimpleNamespace(evidence=[namespace + "/event:2@1"], target=f"apps/{APP}/core/SKILL.md",
                                        new_text="new", gist="Open Details panel")
            assert history.signal == signal and history.directory()["events"][0]["event"] == reference
            assert not history.regression_witnesses(SimpleNamespace(evidence=["source/event:1@1"]))

            async def choose(settings, budget, model, role, system, packet):
                assert packet["events"][0]["source"] == reference
                return {"decision": "record", "event": reference, "intent": {
                    "goal": GOAL, "setup_goal": "Open the current note list and leave details closed.",
                    "result": [present("Details")], "guards": [present("Current note")],
                    "branch": [present("List")], "branch_after": [present("Advanced toolbar")], "branch_action": "scroll",
                }}

            monkeypatch.setattr(gui_skill_auto_plan, "_ask", choose)
            monkeypatch.setattr(gui_skill_auto_plan, "resolve_learner_model", lambda _: "planner")
            plan = await gui_skill_auto_plan.plan_skill_case(task=source_task, store=None, candidate=candidate,
                old_text="old", app_id=APP, build="22", registry=SimpleNamespace(find_for_skill=lambda **_: []),
                settings=None, budget=None, replay_witnesses=history.regression_witnesses(candidate))
            assert plan.recording.build == "22" and plan.recording.branch_action == "scroll"
            assert plan.evidence == (reference, namespace + "/observation:before@1",
                                     namespace + "/observation:tools@1", namespace + "/observation:after@1")
            verified_directory = history.directory()

        _, learned = await gui_skill_loop._learn(source_runtime, source_task.id, library,
                                                 tmp_path / "verified-manual", options)
        system, packet = requests.pop()
        assert learned["script_repair_signal_ids"] == [signal.signal_id]
        assert system == READONLY_SYSTEM + SCRIPT_RECOVERY_POLICY
        assert packet["live_exploration_disabled"] is True
        assert "witnessed_script_recovery" in packet["source"]["signals"]
        wire_evidence, = packet["source"]["script_repair_evidence"]["items"]
        assert wire_evidence == verified_directory
        scroll, tap = wire_evidence["events"]
        assert scroll.get("observed_target") is None
        assert tap.get("observed_target") == {"role": "Button", "resource_id": "advanced_toolbar",
                                               "text": "Advanced toolbar", "desc": "Open note details",
                                               "hint": "Shows details"}
        assert "Unrelated editor content" not in json.dumps(wire_evidence)

        backend = ExplorationBackend(source_task, db=source_db, artifacts=assets, driver=None, settings=settings,
                                     library=SkillLibrary(library.root))
        try:
            await run_task_learning(source_task, settings=settings, library=backend.library, store=source,
                                    backend=backend, budget=LearningBudget(max_actions=0),
                                    outcome_evidence={"success": False}, script_repair_evidence=[verified_directory])
        finally:
            backend.close()
        ordinary_system, ordinary_packet = requests.pop()
        assert ordinary_system == READONLY_SYSTEM
        assert "witnessed_script_recovery" not in ordinary_packet["source"]["signals"]
        assert "script_repair_evidence" not in ordinary_packet["source"]

        replay.put("gui_script_run", "final", {**final, "fixture": {"app_build": "23"}})
        with script_repair_histories(source_db, source_task) as histories:
            assert histories == []
        _, learned = await gui_skill_loop._learn(source_runtime, source_task.id, library,
                                                 tmp_path / "unverified-manual", options)
        manual_system, manual_packet = requests.pop()
        assert "script_repair_signal_ids" not in learned
        assert manual_system == READONLY_SYSTEM
        assert "witnessed_script_recovery" not in manual_packet["source"]["signals"]
        assert "script_repair_evidence" not in manual_packet["source"]
        assert requests == []
