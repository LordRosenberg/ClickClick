from copy import deepcopy
import json

from pytest import mark, raises

from agent.revisable.store import TaskStore
from agent.skills.script_schema import GuiScript, script_fingerprint
from driver.fixture import DEFAULT_TREE, FixtureDriver
from evaluation.gui_script_assist import AssistPolicy, recovery_session
from evaluation.gui_script_host import evaluation_settings, replay_owned
from evaluation.gui_script_release import register_script
from evaluation.gui_scripts import _script
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.llm_gateway import GatewayResponse
from shared.recovery_store import RecoveryStore


def text_is(text):
    return {"type": "target_present", "target": {"text": text}}


def app_script(timeout=2.0):
    return GuiScript.model_validate({"app_id": "com.example.demo", "script_id": "play", "initial": [
        {"type": "app_is", "app_id": "com.example.demo"}], "steps": [{
        "id": "open", "before": [text_is("Ready")], "after": [text_is("Done")], "blocked_when": [text_is("Blocked")],
        "action": {"type": "tap", "target": {"text": "Play", "role": "Button"}}, "timeout_seconds": timeout,
    }], "final": [text_is("Done")]})


class ToggleDriver(FixtureDriver):
    def __init__(self, blocked):
        super().__init__(deepcopy(DEFAULT_TREE))
        self.status = {"class": "android.widget.TextView", "text": "Blocked" if blocked else "Ready",
                       "bounds": [100, 600, 450, 680]}
        self.dismiss = {"class": "android.widget.Button", "text": "Dismiss", "clickable": True,
                        "bounds": [600, 200, 800, 300]}
        self.tree["tree"]["children"].append(self.status)
        if blocked:
            self.tree["tree"]["children"].append(self.dismiss)

    async def act(self, action):
        result = await super().act(action)
        if action.x == 700:
            self.tree["tree"]["children"].remove(self.dismiss)
            self.status["text"] = "Ready"
        elif action.x == 250:
            self.status["text"] = "Done"
        return result


@mark.asyncio
@mark.parametrize("blocked, clears_while_model_runs", [(False, False), (True, False), (True, True)])
async def test_explicit_owned_replay_commits_native_events(tmp_path, monkeypatch, blocked, clears_while_model_runs):
    driver = ToggleDriver(blocked)
    settings = evaluation_settings(None, tmp_path)
    policy = AssistPolicy.model_validate({"max_actions": 1, "max_seconds": 10.0, "actions": [
        {"id": "dismiss", "before": [text_is("Blocked")], "after": [text_is("Ready")],
         "action": {"type": "tap", "target": {"text": "Dismiss", "role": "Button"}}},
    ]})
    chosen = []

    async def choose(*args, **kwargs):
        chosen.append(True)
        if clears_while_model_runs:
            driver.status["text"] = "Ready"
            driver.tree["tree"]["children"].remove(driver.dismiss)
        return GatewayResponse(content='{"action_id":"dismiss"}', model="stub")

    monkeypatch.setattr("evaluation.gui_script_assist.complete", choose)

    def assist(owned, transaction, evidence):
        return recovery_session(policy, settings=settings, driver=owned, transaction=transaction, evidence=evidence)

    result = await replay_owned(app_script(12.0), goal="Open the item", settings=settings, driver=driver,
                                serial=driver.serial, parameters={}, max_seconds=40.0, assist=assist)
    assert result["status"] == "succeeded", result
    assert result["receipt"]["final_asserted"] and result["receipt"]["assisted"] is blocked
    assert len(chosen) == int(blocked)
    was_dismissed = blocked and not clears_while_model_runs
    expected = [700, 250] if was_dismissed else [250]
    assert [action.x for action in driver.actions] == expected
    db = Database(settings.db_path, read_only=True)
    try:
        store = TaskStore(db, ArtifactStore(settings.artifacts_dir, create=False), result["task_id"])
        assert store.get("gui_script_run", "final")["payload"]["receipt"] == result["receipt"]
        assert [row["payload"]["origin"] for row in store.records("event")] == (
            ["takeover", "script"] if was_dismissed else ["script"])
        assert all(row["payload"]["submitted_action"]["type"] == "tap" for row in store.records("event"))
        # A receipt and recorded step settle each reserved operation.
        recovery = RecoveryStore(db, store.artifacts)
        assert len(recovery.operations(result["task_id"])) == len(expected)
        assert not recovery.operations(result["task_id"], pending=True)
    finally:
        db.close()


def test_candidate_envelope_rejects_edited_body():
    script = app_script()
    candidate = {"script": script.model_dump(mode="json"), "script_hash": script_fingerprint(script)}
    assert _script({"candidate": candidate}) == script
    candidate = json.loads(json.dumps(candidate))
    candidate["script"]["revision"] += 1
    with raises(ValueError, match="hash"):
        _script({"candidate": candidate})


@mark.asyncio
async def test_registration_requires_two_distinct_persisted_replays(tmp_path):
    script = app_script(12.0)
    goal = "Open the item"
    fixture = {"app_build": "1", "snapshot": "pristine", "avd": "sandbox", "parameters_hash": "inputs"}
    replays = []
    for run in ("first", "second"):
        runtime = tmp_path / run
        driver = ToggleDriver(False)
        result = await replay_owned(script, goal=goal, settings=evaluation_settings(None, runtime), driver=driver,
                                    serial=driver.serial, parameters={}, max_seconds=40.0, fixture=fixture)
        assert result["status"] == "succeeded", result
        replays.append({**result, "runtime": str(runtime)})
    sha = script_fingerprint(script)
    candidate = {"script": script.model_dump(mode="json"), "script_hash": sha,
                 "provenance": {"goal": goal}, "review": {"decision": "accept", "script_hash": sha}}
    report = {"update": "verified_pending_review", "first": replays[0], "second": replays[1]}
    with raises(ValueError, match="distinct replay"):
        register_script(script, candidate, {**report, "second": replays[0]}, tmp_path / "registry")
    target = register_script(script, candidate, report, tmp_path / "registry")
    assert target.is_file() and json.loads(target.read_text(encoding="utf-8"))["replay_required"] is False
    with raises(FileExistsError):
        register_script(script, candidate, report, tmp_path / "registry")
