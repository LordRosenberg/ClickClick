"""Inert script authoring uses exact native source data and explicit review."""

import pytest

from agent.revisable.store import TaskStore
from agent.skills import script_learning
from agent.skills.learning import LearningBudget
from agent.skills.script_learning_signals import SCRIPT_SKILL_SIGNAL_KIND, ScriptSkillSignal
from agent.skills.script_schema import GuiScript, script_fingerprint
from agent.traces import TraceWriter
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import TaskStatus


APP = "example.notes"


def present(resource_id):
    return {"type": "target_present", "target": {"resource_id": resource_id}}


def tapped_step(target):
    return {"id": "open", "before": [present("root_panel"), present(target)],
            "action": {"type": "tap", "target": {"resource_id": target}}, "after": [present("details")]}


def script(target="open_details"):
    return {"script_id": "open-details", "app_id": APP, "initial": [present("root_panel")],
            "steps": [tapped_step(target)], "final": [present("details")]}


@pytest.fixture
def history(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, manager_model="author", skill_reviewer_model="reviewer")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    task = db.create_task("Open the details panel for the current note.")
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    yield db.get_task(task.id), settings, TaskStore(db, artifacts, task.id)
    db.close()


def record_source(history, *, native=True, coordinate_fallback=False,
                  effect_reason="action_dispatched_observation_available", before_tree=None):
    task, _, store = history
    store.put("observation", "before", {"app": APP, "accepted": True, "text": (
        f"foreground_package={APP}\n" + (before_tree or
        '[4] Button raw_text="Open note" clickable id=open_details\n'
        '[6] Button raw_text="Close note" clickable id=close_details\n[8] View clickable id=root_panel'))})
    store.put("observation", "after", {"app": APP, "accepted": True, "text": (
        f"foreground_package={APP}\n[1] TextView raw_text=\"Note details\" clickable id=details")})
    receipt = {"transaction_id": "tx", "dispatch_succeeded": True, "observation_accepted": True,
               "observation_id": "after", "effect_outcome": "unknown", "effect_reason": effect_reason}
    action = {"type": "tap", "index": 4}
    outcome = {"success": True, "receipt": receipt}
    store.put("event", "7", {"step": 7, "decision": "act", "observation_id": "before",
              "submitted_action": action, "action_result": outcome,
              **({"origin": "takeover", "script_step_id": "open"} if not native else {})})
    if native:
        dispatched = {"type": "tap_xy", "x": 40, "y": 50} if coordinate_fallback else action
        pipeline = {"basis_observation_id": "before", "stages": [
            {"stage": "submitted", "action": action},
            {"stage": "index_resolved", "action": dispatched, "observation_id": "before"},
        ]} if coordinate_fallback else None
        TraceWriter(store.db, store.artifacts).write(task.id, kind="executor_tick", step_seq=8, payload={
            "decision": "act", "basis_observation_id": "before", "observation_id": "before",
            "observation_accepted": True, "action": dispatched, "action_pipeline": pipeline,
            "action_result": outcome})


class FakeBudget(LearningBudget):
    def __init__(self, draft, *, review=True, stale_hash=False):
        super().__init__(max_actions=0)
        self.requests = []
        self.draft, self.review, self.stale_hash = draft, review, stale_hash

    async def ask(self, model, system, payload, settings):
        self.meter("model_call_started", {})
        self.requests.append((model, system, payload))
        if model == "author":
            return self.draft
        return {"decision": "accept" if self.review else "reject",
                "script_hash": "a" * 64 if self.stale_hash else payload["script_hash"], "reason": "Review."}


def run_inline(coro):
    # The synthetic model responds inline. There is no provider, driver, or socket.
    with pytest.raises(StopIteration) as result:
        coro.send(None)
    return result.value.value


def author(history, budget):
    task, settings, store = history
    return run_inline(script_learning.author_script(task, settings=settings, store=store, budget=budget))


def test_successful_native_tape_yields_an_inert_reviewed_candidate(history):
    record_source(history)
    budget = FakeBudget({"decision": "propose", "script": script()})
    result = author(history, budget)
    candidate = result["candidate"]
    assert result["status"] == "candidate" and candidate["replay_required"] is True
    assert candidate["script_hash"] == script_fingerprint(GuiScript.model_validate(script()))
    assert candidate["provenance"]["events"] == [{"event": "event:7@1", "before": "observation:before@1",
                                                  "after": "observation:after@1"}]
    assert [(model, system) for model, system, _ in budget.requests] == [
        ("author", script_learning.AUTHOR_SYSTEM), ("reviewer", script_learning.REVIEW_SYSTEM)]
    assert history[2].records("gui_script_run") == []
    assert result["budget"]["actions"] == 0


REPEATED_CONTROLS = (
    "Window id=1 type=1 layer=0 display=0\n"
    "  [8] View clickable id=root_panel\n"
    '    View raw_text="Current note"\n'
    '      [4] Button raw_text="Open note" clickable id=open_details\n'
    '    View raw_text="Archived note"\n'
    '      [6] Button raw_text="Open note" clickable id=open_details'
)


def scoped_open_script(**scope):
    candidate = script()
    target = {"resource_id": "open_details", **scope}
    candidate["steps"][0]["action"]["target"] = target
    candidate["steps"][0]["before"][1]["target"] = target
    return candidate


def test_native_ancestor_disambiguates_repeated_controls(history):
    record_source(history, before_tree=REPEATED_CONTROLS)
    candidate = scoped_open_script(ancestor={"text": "Current note"}, expected_count=1)
    budget = FakeBudget({"decision": "propose", "script": candidate})
    result = author(history, budget)

    assert result["status"] == "candidate"
    assert result["candidate"]["script_hash"] == script_fingerprint(GuiScript.model_validate(candidate))
    frame = budget.requests[0][2]["events"][0]["before"]
    target, = (node for node in frame["nodes"] if node.get("index") == 4)
    assert frame["nodes"][target["parent"]]["text"] == "Current note"
    compact = script_learning._compact_native_packet(budget.requests[0][2])
    frame_ids = compact["observations"][frame["source"]]["node_ids"]
    assert compact["nodes"][frame_ids[target["parent"]]]["text"] == "Current note"


@pytest.mark.parametrize("text,expected", [
    ("Archived note", "script_action_target_differs_from_trace"),
    ("Invented note", "script_action_target_differs_from_trace"),
])
def test_ancestor_cannot_redirect_a_linked_action(history, text, expected):
    record_source(history, before_tree=REPEATED_CONTROLS)
    candidate = scoped_open_script(ancestor={"text": text})
    budget = FakeBudget({"decision": "propose", "script": candidate})
    assert author(history, budget)["reason"] == expected
    assert len(budget.requests) == 1


def test_malformed_native_indentation_cannot_ground_an_ancestor(history):
    record_source(history, before_tree=REPEATED_CONTROLS.replace("      [4]", "     [4]"))
    candidate = scoped_open_script(ancestor={"text": "Current note"})
    budget = FakeBudget({"decision": "propose", "script": candidate})
    assert author(history, budget)["reason"] == "selector_ancestor_not_grounded"


def test_native_window_boundary_breaks_ancestor_chain(history):
    tree = REPEATED_CONTROLS.replace('      [4]', '      Window id=2 type=1 layer=0 display=0\n        [4]')
    record_source(history, before_tree=tree)
    candidate = scoped_open_script(ancestor={"text": "Current note"})
    budget = FakeBudget({"decision": "propose", "script": candidate})
    assert author(history, budget)["reason"] == "script_action_target_differs_from_trace"


def test_historical_index_and_tree_order_do_not_prove_screen_occurrence(history):
    record_source(history, before_tree=REPEATED_CONTROLS)
    candidate = scoped_open_script(occurrence=0, expected_count=2)
    budget = FakeBudget({"decision": "propose", "script": candidate})
    assert author(history, budget)["reason"] == "selector_order_not_grounded"
    assert len(budget.requests) == 1


@pytest.mark.parametrize("launch_target", [APP, "example.unrelated"])
def test_launch_foreground_receipt_may_precede_the_first_semantic_tree(history, launch_target):
    task, _, store = history
    store.put("observation", "launcher", {"app": "example.launcher", "text": "app=example.launcher\n(empty tree)"})
    store.put("observation", "starting", {"app": APP, "text": f"app={APP}\n(empty tree)"})
    action = {"type": "launch", "app": launch_target}
    receipt = {"transaction_id": "launch", "dispatch_succeeded": True, "observation_accepted": True,
               "observation_id": "starting", "effect_outcome": "confirmed"}
    outcome = {"success": True, "receipt": receipt}
    store.put("event", "0", {"step": 0, "decision": "act", "observation_id": "launcher",
                             "submitted_action": action, "action_result": outcome})
    TraceWriter(store.db, store.artifacts).write(task.id, kind="executor_tick", step_seq=1, payload={
        "decision": "act", "basis_observation_id": "launcher", "observation_id": "launcher",
        "observation_accepted": True, "action": action, "action_result": outcome})
    record_source(history)
    budget = FakeBudget({"decision": "propose", "script": script()})
    result = author(history, budget)
    if launch_target == APP:
        assert result["status"] == "candidate"
        assert len(result["candidate"]["provenance"]["events"]) == 1
    else:
        assert result["reason"] == "source_launch_target_unconfirmed"
        assert budget.requests == []


def test_true_dispatch_unknown_is_rejected_before_model_call(history):
    record_source(history, effect_reason="dispatch_outcome_unknown_observe_before_retry")
    budget = FakeBudget({"decision": "propose", "script": script()})
    assert author(history, budget)["reason"] == "native_dispatch_outcome_unknown"
    assert budget.requests == []


def test_native_index_resolution_preserves_semantic_source(history):
    record_source(history, coordinate_fallback=True)
    budget = FakeBudget({"decision": "propose", "script": script()})
    assert author(history, budget)["status"] == "candidate"
    modeled_event = budget.requests[0][2]["events"][0]
    assert modeled_event["action"] == "tap" and modeled_event["target_index"] == 4
    assert not ({"x", "y"} & modeled_event.keys())


@pytest.mark.parametrize("index", [4, 5])
def test_replace_text_requires_the_exact_already_focused_editable(history, index):
    task, _, store = history
    store.put("observation", "before", {"app": APP, "text": (
        f"foreground_package={APP}\n[4] EditText editable focused id=title\n[5] EditText editable id=body")})
    store.put("observation", "after", {"app": APP, "text": (
        f'foreground_package={APP}\n[4] EditText raw_text="Fixture revised" editable id=title')})
    action = {"type": "replace_text", "index": index, "text": "Fixture revised"}
    receipt = {"transaction_id": "text", "dispatch_succeeded": True, "observation_accepted": True,
               "observation_id": "after", "effect_outcome": "unknown",
               "effect_reason": "action_dispatched_observation_available"}
    outcome = {"success": True, "receipt": receipt}
    store.put("event", "1", {"step": 1, "decision": "act", "observation_id": "before",
                             "submitted_action": action, "action_result": outcome})
    TraceWriter(store.db, store.artifacts).write(task.id, kind="executor_tick", step_seq=2, payload={
        "decision": "act", "basis_observation_id": "before", "observation_id": "before",
        "observation_accepted": True, "action": action, "action_result": outcome})
    before = [present("title")]
    after = [{"type": "target_present", "target": {"resource_id": "title", "text_parameter": "new_title"}}]
    proposal = {"app_id": APP, "script_id": "replace-title", "parameters": {"new_title": {}},
                "initial": before, "final": after, "steps": [{"id": "write", "before": before, "after": after,
                    "action": {"type": "replace_text", "target": {"resource_id": "title"}, "parameter": "new_title"}}]}
    budget = FakeBudget({"decision": "propose", "script": proposal})
    result = author(history, budget)
    if index == 4:
        assert result["status"] == "candidate"
        assert "Fixture revised" not in str(result["candidate"]["script"])
    else:
        assert result["reason"] == "source_input_target_not_already_focused"
        assert budget.requests == []


@pytest.mark.parametrize("target,reason", [
    ("close_details", "script_action_target_differs_from_trace"),
    ("invented", "script_action_target_differs_from_trace"),
])
def test_draft_cannot_change_executed_target(history, target, reason):
    record_source(history)
    budget = FakeBudget({"decision": "propose", "script": script(target)})
    assert author(history, budget)["reason"] == reason
    assert len(budget.requests) == 1


def test_review_hash_must_bind_the_exact_candidate(history):
    record_source(history)
    budget = FakeBudget({"decision": "propose", "script": script()}, stale_hash=True)
    result = author(history, budget)
    assert result["status"] == "rejected"
    assert "candidate" not in result


def test_model_context_limit_keeps_required_evidence_intact(history):
    record_source(history)
    history[1].models_json = '{"author":{"context":{"skill_learner":{"max_input_tokens":32}}}}'
    budget = FakeBudget({"decision": "propose", "script": script()})
    assert author(history, budget)["reason"] == "script_source_exceeds_context_budget"
    assert budget.requests == []


def repair_fixture(history, target="old_open_details"):
    task, _, store = history
    record_source(history, native=False)
    base = GuiScript.model_validate(script(target))
    receipt = {"status": "succeeded", "script_hash": script_fingerprint(base),
               "revision": base.revision, "script_id": base.script_id, "assisted": True, "final_asserted": True,
               "failed_step_id": "open", "steps": [{"step_id": "open", "succeeded": False,
                                                     "execution": "not_dispatched"}]}
    store.put("gui_script_run", "final", {"receipt": receipt})
    return base, {"task_id": task.id, "receipt": receipt}


def repair(history, budget, base, run, **kwargs):
    task, settings, store = history
    return run_inline(script_learning.repair_script(task, settings=settings, store=store, base_script=base,
                                                    run_evidence=run, budget=budget, **kwargs))


def test_repair_binds_assisted_run_updates_only_failed_step_and_preserves_contract(history):
    from shared.recovery_store import digest

    base, run = repair_fixture(history)
    run = {**run, "runtime": "test/runtime", "app_build": "1", "snapshot": "frozen"}
    budget = FakeBudget({"decision": "propose", "replacement_steps": [tapped_step("open_details")]})
    candidate = repair(history, budget, base, run)["candidate"]
    revised = GuiScript.model_validate(candidate["script"])
    assert revised.revision == base.revision + 1 and revised.script_id == base.script_id
    assert (revised.initial, revised.final, revised.parameters, revised.app_id) == (
        base.initial, base.final, base.parameters, base.app_id)
    assert candidate["provenance"]["base_hash"] == script_fingerprint(base)
    assert candidate["provenance"]["assisted_run_hash"] == digest(run)
    assert budget.requests[-1][2]["assisted_run"] == run["receipt"]
    assert [system for _, system, _ in budget.requests] == [script_learning.REPAIR_SYSTEM,
                                                           script_learning.REVIEW_SYSTEM]


def test_repair_rejects_unbound_receipt_and_weakened_checkpoint(history):
    base, run = repair_fixture(history)
    replacement = {**tapped_step("open_details"), "after": [present("root_panel")]}
    budget = FakeBudget({"decision": "propose", "replacement_steps": [replacement]})
    result = repair(history, budget, base, {**run, "receipt": {**run["receipt"], "revision": 2}})
    assert result["reason"] == "assisted_run_or_base_unconfirmed" and budget.requests == []
    result = repair(history, budget, base, run)
    assert result["reason"] == "script_repair_changes_frozen_contract_or_has_no_change"
    assert len(budget.requests) == 1


def test_repair_does_not_absorb_a_possibly_partial_prior_mutation(history):
    base, run = repair_fixture(history)
    run["receipt"]["steps"][0]["execution"] = "known"
    history[2].put("gui_script_run", "final", {"receipt": run["receipt"]})
    budget = FakeBudget({"decision": "propose", "replacement_steps": [tapped_step("open_details")]})
    assert repair(history, budget, base, run)["reason"] == "assisted_run_or_base_unconfirmed"
    assert budget.requests == []


def known_timeout_fixture(history, *, invalid=None):
    task, _, store = history
    base = GuiScript.model_validate(script("old_open_details"))
    source_before = ('[2] Button raw_text="Open editor" clickable id=old_open_details\n'
                     '[3] Button clickable id=other\n[8] View clickable id=root_panel')
    editor = '[4] Button raw_text="Open note" clickable id=open_details\n[8] View clickable id=root_panel'
    receipt_after = "missing" if invalid == "missing_action_after" else "opened"
    original = {"transaction_id": "script-tx", "dispatch_succeeded": True, "observation_accepted": True,
                "observation_id": receipt_after, "effect_observation_id": receipt_after,
                "effect_outcome": "unknown", "effect_reason": "action_dispatched_observation_available"}
    for key, text, timestamp in (("pre-open", source_before, 100),
                                 ("opened", source_before if invalid == "unchanged" else editor, 200),
                                 ("timed-out", editor, 300)):
        if key == "timed-out" and invalid == "missing_last_poll":
            continue
        frame_app = "different.notes" if key == "opened" and invalid == "foreign_app" else APP
        store.put("observation", key, {"app": frame_app, "text": f"foreground_package={frame_app}\n{text}",
                                      "accepted": not (key == "opened" and invalid == "unaccepted"),
                                      "captured_monotonic_ms": timestamp})
    recorded_receipt = {**original, "transaction_id": "other-tx"} if invalid == "wrong_transaction" else original
    original_index = 3 if invalid == "wrong_target" else 2
    store.put("event", "1", {"step": 8 if invalid == "wrong_order" else 1, "script_step_id": "open", "origin": "script",
                              "decision": "act", "observation_id": "pre-open",
                              "submitted_action": {"type": "tap", "index": original_index},
                              "target_snapshot": {"index": original_index, "role": "Button",
                                                  "resource_id": "other" if invalid == "wrong_target" else "old_open_details"},
                              "action_result": {"success": True, "receipt": recorded_receipt}})
    record_source(history, native=False, before_tree=editor)
    for key, timestamp in (("before", 400), ("after", 500)):
        record = store.get("observation", key)["payload"]
        store.put("observation", key, {**record, "captured_monotonic_ms": timestamp})
    store.put("observation", "final", {**store.get("observation", "after")["payload"], "captured_monotonic_ms": 600})
    failed = {"step_id": "open", "attempt": 1, "succeeded": False,
              "execution": "unknown" if invalid == "unknown" else "known",
              "reason": "action_failed" if invalid == "action_failed" else "postcondition_timeout",
              "before_observation_id": "pre-open", "after_observation_id": "timed-out",
              "receipt": {**original, "effect_reason": ""}}
    receipt = {"status": "succeeded", "script_hash": script_fingerprint(base), "revision": base.revision,
               "script_id": base.script_id, "assisted": True, "final_asserted": True,
               "failed_step_id": "open", "steps": [failed], "initial_observation_id": "pre-open",
               "final_observation_id": "final"}
    store.put("gui_script_run", "final", {"receipt": receipt, "fixture": {"app_build": "123"}})
    return base, {"task_id": task.id, "receipt": receipt}


@pytest.mark.parametrize("decision", ["skip", "propose"])
def test_acknowledged_postcondition_timeout_can_save_only_a_skill_hint(history, decision):
    base, run = known_timeout_fixture(history)
    store = history[2]
    hint = {"condition": "The editor exposes the direct Open note control",
            "rule": "Use the visible direct control to open note details", "evidence": ["event:7@1"]}
    draft = {"decision": decision, "skill_hint": hint}
    if decision == "propose":
        draft["replacement_steps"] = [tapped_step("open_details")]
    learner = FakeBudget(draft)
    result = repair(history, learner, base, run, signal_store=store)
    assert result["status"] == "skipped" and result["reason"] == "original_action_already_dispatched"
    assert "candidate" not in result
    assert result["skill_signal_status"] == "recorded"
    assert [model for model, _, _ in learner.requests] == ["author"]
    assert learner.requests[0][2]["preceding_dispatch"] == {
        "event": "event:1@1", "before": "observation:pre-open@1", "observed_after": "observation:opened@1",
        "failed_postcondition_frame": "observation:timed-out@1",
    }
    signal = ScriptSkillSignal.model_validate(result["skill_signal"])
    assert signal.replay_task_id == history[0].id and signal.app_build == "123"
    proof, = signal.evidence
    assert (proof.event, proof.before, proof.after) == ("event:7@1", "observation:before@2", "observation:after@2")
    assert store.get(SCRIPT_SKILL_SIGNAL_KIND, signal.signal_id)["payload"] == result["skill_signal"]


@pytest.mark.parametrize("invalid", ["unknown", "action_failed", "wrong_transaction", "wrong_target", "wrong_order",
                                      "missing_action_after", "missing_last_poll", "unchanged", "unaccepted", "foreign_app"])
def test_unverified_dispatched_postcondition_timeout_cannot_trigger_learning(history, invalid):
    base, run = known_timeout_fixture(history, invalid=invalid)
    store = history[2]
    hint = {"condition": "Alternate control visible", "rule": "Use the alternate control", "evidence": ["event:7@1"]}
    learner = FakeBudget({"decision": "skip", "skill_hint": hint})
    result = repair(history, learner, base, run, signal_store=store)
    assert result["status"] == "skipped"
    assert "candidate" not in result and "skill_signal" not in result
    assert learner.requests == [] and store.records(SCRIPT_SKILL_SIGNAL_KIND) == []


@pytest.mark.parametrize("verified", [True, False])
def test_original_step_retry_uses_exact_script_event_and_skips_learning(history, verified):
    base, run = repair_fixture(history, target="open_details")
    store = history[2]
    action = store.get("event", "7")["payload"]
    native = {**action["action_result"]["receipt"], "transaction_id": "retry-tx"}
    run["receipt"]["steps"].append({"step_id": "open", "attempt": 2, "succeeded": True,
                                    "reason": "postcondition_met", "execution": "known",
                                    "before_observation_id": "before", "after_observation_id": "after",
                                    "receipt": native})
    store.put("event", "8", {**action, "origin": "script" if verified else "takeover",
                             "action_result": {"success": True, "receipt": native}})
    store.put("gui_script_run", "final", {"receipt": run["receipt"], "fixture": {"app_build": "123"}})
    budget = FakeBudget({"decision": "propose", "replacement_steps": [tapped_step("open_details")]})
    result = repair(history, budget, base, run, signal_store=store)
    assert result["status"] == "skipped"
    assert result["resume_evidence"]["verified"] is verified
    if verified:
        assert result["reason"] == "original_step_succeeded_after_takeover"
        assert result["repair_judgment"] == "original_step_resumed"
        assert result["resume_evidence"]["event"] == "event:8@1"
        assert result["resume_evidence"]["before"] == "observation:before@1"
        assert result["resume_evidence"]["after"] == "observation:after@1"
    else:
        assert result["reason"] == "original_step_retry_unverified"
        assert result["repair_judgment"] == "evidence_insufficient"
    assert budget.requests == []
    assert "candidate" not in result and "skill_signal" not in result
    assert store.records(SCRIPT_SKILL_SIGNAL_KIND) == []


def test_original_wait_rechecks_the_existing_postcondition_without_dispatch(history):
    _, run = repair_fixture(history)
    original = script()
    original["steps"][0]["action"] = {"type": "wait"}
    base = GuiScript.model_validate(original)
    run["receipt"]["script_hash"] = script_fingerprint(base)
    run["receipt"]["steps"].append({"step_id": "open", "attempt": 2, "succeeded": True,
                                    "reason": "postcondition_met", "execution": "not_dispatched",
                                    "before_observation_id": "before", "after_observation_id": "after",
                                    "receipt": None})
    replay = history[2]
    replay.put("gui_script_run", "final", {"receipt": run["receipt"], "fixture": {"app_build": "123"}})
    budget = FakeBudget({"decision": "skip"})
    result = repair(history, budget, base, run, signal_store=replay)
    assert result["reason"] == "original_step_succeeded_after_takeover"
    assert result["resume_evidence"]["verified"] is True
    assert "transaction_id" not in result["resume_evidence"]
    assert budget.requests == [] and replay.records(SCRIPT_SKILL_SIGNAL_KIND) == []


def test_repair_can_skip_script_and_record_one_source_bound_skill_hint(history, tmp_path):
    from pathlib import Path

    from shared.recovery_store import digest

    base, run = repair_fixture(history)
    replay = history[2]
    saved = {"receipt": run["receipt"], "fixture": {"app_build": "123"}}
    replay.put("gui_script_run", "final", saved)
    source_path = tmp_path / "console"
    source_path.mkdir()
    source_db = Database(source_path / "clickclick.db")
    original = source_db.create_task(history[0].instruction)
    source_db.update_task(original.id, status=TaskStatus.SUCCEEDED)
    unrelated = source_db.create_task("Open a different screen.")
    source_db.update_task(unrelated.id, status=TaskStatus.SUCCEEDED)
    source = TaskStore(source_db, ArtifactStore(source_path / "artifacts"), original.id)
    mismatch = TaskStore(source_db, source.artifacts, unrelated.id)
    hint = {"condition": "Details visible from the note page", "rule": "Use Open note to show details",
            "evidence": ["event:7@1"]}
    try:
        rejected = repair(history, FakeBudget({"decision": "skip", "skill_hint": hint}), base, run,
                          signal_store=mismatch, source_task_id=unrelated.id)
        assert rejected["skill_signal_status"] == "source_task_unconfirmed"
        assert mismatch.records(SCRIPT_SKILL_SIGNAL_KIND) == []

        result = repair(history, FakeBudget({"decision": "skip", "skill_hint": hint}), base, run,
                        signal_store=source, source_task_id=original.id)
        assert result["status"] == "skipped" and result["skill_signal_status"] == "recorded"
        assert "candidate" not in result
        signal = ScriptSkillSignal.model_validate(result["skill_signal"])
        assert signal.source_task_id == original.id and signal.replay_task_id == history[0].id
        assert Path(signal.replay_runtime) == Path(replay.db.path).resolve().parent
        assert signal.goal == history[0].instruction and (signal.app_id, signal.app_build) == (APP, "123")
        assert signal.base_script_hash == script_fingerprint(base) and signal.assisted_run_hash == digest(saved)
        assert signal.hint.evidence == ("event:7@1",)
        assert (signal.evidence[0].before, signal.evidence[0].after) == ("observation:before@1", "observation:after@1")
        assert replay.records(SCRIPT_SKILL_SIGNAL_KIND) == []

        revised_hint = {**hint, "rule": "Another wording of the same finding"}
        repeated = repair(history, FakeBudget({"decision": "skip", "skill_hint": revised_hint}), base, run,
                          signal_store=source, source_task_id=original.id)
        assert repeated["skill_signal_status"] == "already_recorded"
        assert repeated["skill_signal"] == result["skill_signal"]
        assert source.get(SCRIPT_SKILL_SIGNAL_KIND, signal.signal_id)["version"] == 1
    finally:
        source_db.close()


@pytest.mark.parametrize("reference,build,expected", [
    ("event:999@1", "123", "hint_evidence_unconfirmed"),
    ("event:7@1", None, "app_build_unconfirmed"),
    ("event:7@1", "123", "recorded"),
])
def test_optional_skill_hint_does_not_change_script_review(history, reference, build, expected):
    base, run = repair_fixture(history)
    replay = history[2]
    replay.put("gui_script_run", "final", {"receipt": run["receipt"], "fixture": {"app_build": build}})
    budget = FakeBudget({"decision": "propose", "replacement_steps": [tapped_step("open_details")],
                         "skill_hint": {"condition": "Before opening details", "rule": "Use Open note",
                                        "evidence": [reference]}})
    result = repair(history, budget, base, run, signal_store=replay)
    assert result["status"] == "candidate" and result["skill_signal_status"] == expected
    assert len(budget.requests) == 2
    if expected == "recorded":
        signal = ScriptSkillSignal.model_validate(result["skill_signal"])
        assert signal.source_task_id is None and signal.replay_task_id == history[0].id
        assert len(replay.records(SCRIPT_SKILL_SIGNAL_KIND)) == 1
    else:
        assert "skill_signal" not in result and replay.records(SCRIPT_SKILL_SIGNAL_KIND) == []


def test_locator_only_takeover_cannot_publish_a_skill_hint(history):
    _, run = repair_fixture(history)
    replay = history[2]
    prior = script()
    prior["steps"][0]["action"]["target"]["desc"] = "Details 12"
    prior["steps"][0]["before"][1]["target"]["desc"] = "Details 12"
    base = GuiScript.model_validate(prior)
    before = replay.get("observation", "before")["payload"]
    replay.put("observation", "before", {**before, "text": before["text"].replace(
        'raw_text="Open note"', 'raw_text="Open note" raw_a11y_label="Details 13"')})
    run["receipt"]["script_hash"] = script_fingerprint(base)
    replay.put("gui_script_run", "final", {"receipt": run["receipt"], "fixture": {"app_build": "123"}})
    budget = FakeBudget({"decision": "propose", "replacement_steps": [tapped_step("open_details")],
                         "skill_hint": {"condition": "Opening details", "rule": "Ignore the dynamic description",
                                        "evidence": ["event:7@1"]}})
    result = repair(history, budget, base, run, signal_store=replay)
    assert budget.requests[0][2]["takeover_kind"] == "locator_only"
    assert result["status"] == "candidate" and result["skill_signal_status"] == "locator_only"
    assert "skill_signal" not in result and replay.records(SCRIPT_SKILL_SIGNAL_KIND) == []


def test_compacted_packet_keeps_exact_native_evidence():
    from agent.skills.script_learning import _compact_native_packet

    first = {"source": "observation:start@1", "app": "com.demo", "nodes": [
        {"index": 0, "text": "Start", "role": "Button"}, {"text": "Identity"}]}
    second = {"source": "observation:end@1", "app": "com.demo", "nodes": [
        {"index": 0, "text": "Done", "role": "Button"}, {"text": "Identity"}]}
    packet = {"goal": "Finish", "events": [{"source": "event:1@1", "action": "tap", "before": first, "after": second}]}
    compact = _compact_native_packet(packet)
    assert len(compact["nodes"]) == 3
    for side, original in (("before", first), ("after", second)):
        ref = compact["events"][0][side]
        entry = compact["observations"][ref]
        assert ref == original["source"] and entry["app"] == original["app"]
        assert [compact["nodes"][i] for i in entry["node_ids"]] == original["nodes"]
