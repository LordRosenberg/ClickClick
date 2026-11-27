from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.skills.script_checkpoint import ScriptCheckpoint
from agent.skills.script_runner import TakeoverDecision, check_conditions, run_gui_script
from agent.skills.script_schema import GuiScript, script_fingerprint
from driver.fixture import DEFAULT_TREE, FixtureDriver
from perception.observation import ObservationPackage
from shared.db import Database
from shared.recovery_context import ExecutionContext
from shared.recovery_runtime import InfrastructureInterrupted
from shared.recovery_store import RecoveryStore
from shared.schemas import (
    Action, ActionReceipt, ActionResult, AgentState, CanonicalUI, FocusedElementEvidence, InteractionStateEvidence,
    ObservationMode, UIElement,
)

APP = "com.example.demo"
APP_IS = {"type": "app_is", "app_id": APP}
READY = {"type": "target_present", "target": {"text": "Ready"}}
DONE = {"type": "target_present", "target": {"text": "Done"}}


def document(*, action=None, after=None, before=None):
    return {
        "app_id": APP, "script_id": "play",
        "initial": [APP_IS], "final": [DONE],
        "steps": [{
            "id": "start", "before": [before or READY],
            "action": action or {"type": "tap", "target": {"text": "Play", "role": "Button"}},
            "after": [after or DONE], "timeout_seconds": 0.04,
        }],
    }


def element(index, text, bounds, **kwargs):
    return UIElement(index=index, text=text, role="android.widget.Button", bounds=bounds, **kwargs)


def snapshot(*items, app=APP, complete=True, accepted=True, metadata=None):
    ui = CanonicalUI(app_id=app, activity=".Main", elements=[item for item in items if item.interactable],
                     semantic_tree=list(items), capture_complete=complete)
    return ObservationPackage(ui, ObservationMode.TREE_ONLY, "", None, None, [],
                              accepted=accepted, capture_meta=metadata or {})


PLAY = element(7, "Play", [10, 10, 30, 30])
READY_LABEL = element(-1, "Ready", [0, 0, 1, 1], interactable=False)
DONE_LABEL = element(-1, "Done", [0, 0, 1, 1], interactable=False)


class FakeTransaction:
    def __init__(self, elements=(READY_LABEL, PLAY), *, perform=None, app=APP):
        self.elements = elements
        self.app = app
        self.actions = []
        self.deadlines = []
        self.perform = perform

    async def observe_current(self, *, deadline_ms, source):
        self.deadlines.append(deadline_ms)
        return snapshot(*self.elements, app=self.app)

    async def act_and_observe(self, action, before, *, deadline_ms, target):
        self.deadlines.append(deadline_ms)
        self.actions.append(action)
        if self.perform is not None:
            self.perform(self)
        else:
            self.elements = (DONE_LABEL,)
        after = snapshot(*self.elements, app=self.app)
        return ActionResult(success=True, receipt=ActionReceipt(
            transaction_id="action", dispatch_succeeded=True, observation_accepted=True,
            observation_id=after.observation_id, effect_observation_id=after.observation_id,
        )), after


async def run(raw, transaction, **options):
    return await run_gui_script(GuiScript.model_validate(raw), driver=object(),
                                transaction=transaction, poll_seconds=0.002, **options)


@pytest.fixture
def owned_runner():
    db = Database(Path(":memory:"))
    try:
        state = AgentState(instruction="Submit the form once")
        task_id = db.create_task(state.instruction, state, device_serial="fixture").id
        store = RecoveryStore(db)
        store.enable(task_id)
        yield ExecutionContext(store, store.claim(task_id, "replay"), state)
    finally:
        db.close()


async def test_initial_condition_waits_for_a_late_semantic_tree_without_input():
    class LateTree(FakeTransaction):
        async def observe_current(self, *, deadline_ms, source):
            if not self.deadlines:
                self.deadlines.append(deadline_ms)
                return snapshot(complete=False)
            return await super().observe_current(deadline_ms=deadline_ms, source=source)

    transaction = LateTree()
    raw = document()
    raw["initial"].append(READY)
    observations = []
    result = await run(raw, transaction, on_observation=observations.append)

    assert not observations[0].ui.capture_complete
    assert result.status == "succeeded" and result.final_asserted
    assert result.initial_observation_id == observations[1].observation_id
    assert len(transaction.actions) == 1


def test_schema_rejects_coordinates_unbound_inputs_and_duplicate_steps():
    raw = document()
    expected = script_fingerprint(GuiScript.model_validate(raw))
    assert expected == script_fingerprint(GuiScript.model_validate(deepcopy(raw)))
    raw["steps"][0]["action"]["x"] = 25
    with pytest.raises(ValidationError, match="Extra inputs"):
        GuiScript.model_validate(raw)
    raw = document(action={"type": "type", "target": {"hint": "Name"}, "parameter": "name"})
    with pytest.raises(ValidationError, match="undeclared parameter"):
        GuiScript.model_validate(raw)
    raw["parameters"] = {"name": {"max_length": 5}}
    with pytest.raises(ValueError, match="invalid string argument"):
        GuiScript.model_validate(raw).bind_parameters({"name": "too long"})
    raw["steps"].append(raw["steps"][0])
    with pytest.raises(ValidationError, match="unique"):
        GuiScript.model_validate(raw)


async def test_native_transaction_resolves_current_target_and_reports_observations():
    driver = FixtureDriver(tree=deepcopy(DEFAULT_TREE))
    label = {"class": "android.widget.TextView", "text": "Ready", "bounds": [100, 600, 450, 680]}
    driver.tree["tree"]["children"].insert(0, {
        "class": "android.widget.Button", "text": "Cancel", "clickable": True, "bounds": [500, 200, 800, 300],
    })
    driver.tree["tree"]["children"].append(label)
    original = driver.act

    async def act(action):
        result = await original(action)
        if (action.x, action.y) == (250, 250):
            label["text"] = "Done"
        return result

    driver.act = act
    received = []
    raw = document()
    raw["steps"][0].pop("timeout_seconds")
    script = GuiScript.model_validate(raw)
    result = await run_gui_script(script, driver=driver, on_action=lambda *args: received.append(args))
    assert result.status == "succeeded" and result.final_asserted and not result.assisted
    assert result.script_hash == script_fingerprint(script)
    assert [(action.x, action.y) for action in driver.actions] == [(250, 250)]
    step, before, _, native, after = received[0]
    assert step.id == "start" and native.receipt.dispatch_succeeded
    assert result.steps[0].before_observation_id == before.observation_id
    assert result.steps[0].after_observation_id == after.observation_id
    assert result.final_observation_id != after.observation_id


async def test_waits_for_precondition_without_asking_for_takeover():
    transaction = FakeTransaction((), perform=lambda device: setattr(device, "elements", (DONE_LABEL,)))
    observe = transaction.observe_current
    reads = 0

    async def delayed(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 3:
            transaction.elements = (READY_LABEL, PLAY)
        return await observe(**kwargs)

    transaction.observe_current = delayed
    script = GuiScript.model_validate(document())
    result = await run_gui_script(script, driver=object(), transaction=transaction, poll_seconds=0.001)
    assert result.status == "succeeded" and len(transaction.actions) == 1 and reads >= 4


async def test_targets_are_unique_and_confined_to_app_owned_windows():
    duplicate = element(3, "Play", [70, 70, 90, 90])
    transaction = FakeTransaction((READY_LABEL, PLAY, duplicate))
    outcome = await run(document(), transaction)
    assert outcome.reason == "target_ambiguous"
    assert not outcome.assisted and not transaction.actions

    script = GuiScript.model_validate(document())
    scoped = snapshot(
        READY_LABEL.model_copy(update={"window_id": 1}),
        duplicate.model_copy(update={"window_id": 2, "text": "Done"}),
        metadata={"tree_ownership_candidates": [
            {"window_id": 1, "packages": [APP]}, {"window_id": 2, "packages": ["com.elsewhere.overlay"]},
        ]},
    )
    assert not check_conditions(script, script.final, scoped)
    assert check_conditions(script, script.steps[0].before, scoped)


async def test_transient_duplicate_target_resolves_within_step_wait():
    duplicate = element(3, "Play", [70, 70, 90, 90])
    transaction = FakeTransaction((READY_LABEL, PLAY, duplicate),
                                  perform=lambda device: setattr(device, "elements", (DONE_LABEL,)))
    observe = transaction.observe_current
    reads = 0

    async def delayed(**kwargs):
        nonlocal reads
        reads += 1
        if reads == 4:
            transaction.elements = (READY_LABEL, PLAY)
        return await observe(**kwargs)

    transaction.observe_current = delayed
    raw = document()
    raw["steps"][0]["timeout_seconds"] = 0.3
    result = await run_gui_script(GuiScript.model_validate(raw), driver=object(),
                                  transaction=transaction, poll_seconds=0.001)
    assert result.status == "succeeded" and len(transaction.actions) == 1


def test_checked_and_absent_conditions_require_exact_complete_evidence():
    raw = document()
    raw["final"] = [{"type": "target_present", "target": {"text": "Settings", "checked": False}},
                    {"type": "target_absent", "target": {"text": "Disabled"}}]
    script = GuiScript.model_validate(raw)
    setting = element(9, "Settings", [10, 10, 20, 20], states={"checked": False})
    foreign = replace(snapshot(setting), accepted=False)
    assert check_conditions(script, script.final, snapshot(setting))
    assert not check_conditions(script, script.final, snapshot(setting, complete=False))
    assert not check_conditions(script, script.final, foreign)
    assert not check_conditions(script, script.final, snapshot(setting.model_copy(update={"states": {}})))


async def test_wait_polls_without_device_action_and_uses_bounded_step_time():
    txn = FakeTransaction((READY_LABEL,))
    raw = document(action={"type": "wait"})
    assert GuiScript.model_validate(raw).steps[0].timeout == 0.04
    launch = document(action={"type": "launch"})
    launch["steps"][0].pop("timeout_seconds")
    assert GuiScript.model_validate(launch).steps[0].timeout == 15.5
    launch["steps"][0]["timeout_seconds"] = 18
    assert GuiScript.model_validate(launch).steps[0].timeout == 18

    async def show_done():
        await asyncio.sleep(0.006)
        txn.elements = (DONE_LABEL,)

    update = asyncio.create_task(show_done())
    outcome = await run(raw, txn)
    await update
    assert outcome.status == "succeeded"
    assert outcome.steps[0].execution == "not_dispatched" and txn.actions == []
    assert len(txn.deadlines) >= 4 and max(txn.deadlines) <= 40


async def test_parameterized_input_requires_the_selected_focused_editor():
    raw = document(action={"type": "type", "target": {"resource_id": "query"}, "parameter": "value"},
                   after={"type": "target_present", "target": {"resource_id": "query", "text_parameter": "value"}})
    raw["parameters"] = {"value": {"max_length": 8}}
    raw["final"] = raw["steps"][0]["after"]
    editor = element(12, "", [10, 10, 30, 30], resource_id="query")
    txn = FakeTransaction((READY_LABEL, editor),
                          perform=lambda transaction: setattr(transaction, "elements", (
                              editor.model_copy(update={"text": "orchid"}),
                          )))
    missing_focus = await run(raw, txn, parameters={"value": "orchid"})
    assert missing_focus.reason == "target_not_current_focused_editable" and txn.actions == []

    observe = txn.observe_current

    async def focused(**kwargs):
        package = await observe(**kwargs)
        package.interaction_state = InteractionStateEvidence(
            focused_editable=FocusedElementEvidence(index=editor.index, editability="editable"),
        )
        return package

    txn.observe_current = focused
    result = await run(raw, txn, parameters={"value": "orchid"})
    assert result.status == "succeeded" and txn.actions[0].text == "orchid"
    assert "orchid" not in result.model_dump_json()


async def test_postaction_wait_has_same_deadline_and_does_not_retry():
    transaction = FakeTransaction(perform=lambda transaction: None)
    result = await run(document(), transaction)
    assert result.status == "failed" and result.reason == "postcondition_timeout"
    assert len(transaction.actions) == 1
    assert result.steps[0].execution == "known"
    assert len(transaction.deadlines) >= 4 and transaction.deadlines[-1] < transaction.deadlines[2]


@pytest.mark.parametrize("detail", [None, {"dispatch_unknown": True}, {"node_click_status": "outcome_unknown"}])
async def test_unknown_dispatch_does_not_call_takeover_or_repeat(detail):
    async def unwanted(_):
        pytest.fail("takeover received unresolved input")

    transaction = FakeTransaction()
    act = transaction.act_and_observe

    async def unknown(*args, **kwargs):
        result, after = await act(*args, **kwargs)
        if detail is None:
            raise TimeoutError
        result.detail.update(detail)
        return result, after

    transaction.act_and_observe = unknown
    result = await run(document(), transaction, takeover=unwanted)
    assert result.reason == "dispatch_outcome_unknown" and not result.assisted
    assert len(transaction.actions) == 1 and result.steps[0].execution == "unknown"


@pytest.mark.parametrize(("before_ids", "after_ids", "outside_ids", "expected"), [
    (("old",), ("old", "new"), (), "succeeded"),
    (("old",), ("old",), ("new",), "paused"),
    (("old",), ("old", "new", "ambiguous"), (), "paused"),
    (None, ("old", "new"), (), "paused"),
])
async def test_interrupted_submit_uses_new_id_from_the_same_ledger(
    owned_runner, before_ids, after_ids, outside_ids, expected,
):
    ledger = {"type": "target_present", "target": {"resource_id": "ledger"}}
    raw = document(after={"type": "target_present", "target": {"text": "Submission complete"}})
    raw["final"] = [ledger]
    raw["steps"][0]["confirmation"] = {
        "scope": {"resource_id": "ledger"},
        "target": {"resource_id": "receipt_id"},
        "identifier_prefix": "Submission #",
    }
    script = GuiScript.model_validate(raw)

    def records(ids, outside=()):
        if ids is None:
            return ()
        scope = element(-1, "History", [1, 1, 40, 40], resource_id="ledger", interactable=False,
                        children=list(range(1, len(ids) + 1)))
        rows = tuple(element(-1, f"Submission #{identifier}", [2, 2, 4, 4],
                             resource_id="receipt_id", interactable=False) for identifier in (*ids, *outside))
        return (scope, *rows)

    checkpoint = ScriptCheckpoint(owned_runner, script, {})

    class InterruptedSubmit(FakeTransaction):
        async def act_and_observe(self, action, before, *, deadline_ms, target):
            owned_runner.prepare_action(action)
            self.actions.append(action)
            # No response or confirmation page returned after the device applied the input.
            self.elements = records(after_ids, outside_ids)
            raise InfrastructureInterrupted("lost driver response")

    transaction = InterruptedSubmit((*records(before_ids), READY_LABEL, PLAY))
    result = await run(raw, transaction, checkpoint=checkpoint, takeover_seconds=0.5)
    assert result.status == expected, result.model_dump()
    assert len(transaction.actions) == 1
    store, task_id = owned_runner.store, owned_runner.owner.task_id
    assert len(store.operations(task_id)) == 1
    if expected == "succeeded":
        completed = store.db.get_agent_record(task_id, "gui_script_step_completion", "start")["payload"]
        assert completed["confirmation_id"] == "new"
        assert completed["next_index"] == checkpoint.cursor == 1
    else:
        assert result.failed_step_id == "start"
        assert checkpoint.cursor == 0 and checkpoint.pending(0) is not None


async def test_task_action_budget_pauses_at_current_step_without_dispatch(owned_runner):
    raw = document()
    raw["steps"].insert(0, {"id": "ready", "before": [READY], "action": {"type": "wait"}, "after": [READY]})
    owned_runner.state.revisable.limits.device_actions = 1
    owned_runner.state.revisable.execution_count = 1
    checkpoint = ScriptCheckpoint(owned_runner, GuiScript.model_validate(raw), {})

    class BudgetedTransaction(FakeTransaction):
        async def act_and_observe(self, action, before, *, deadline_ms, target):
            owned_runner.prepare_action(action)
            return await super().act_and_observe(action, before, deadline_ms=deadline_ms, target=target)

    transaction = BudgetedTransaction()
    result = await run(raw, transaction, checkpoint=checkpoint)
    assert (result.status, result.reason, result.failed_step_id) == ("paused", "script_replay_budget_exhausted", "start")
    assert checkpoint.cursor == 1 and checkpoint.pending(1) is None
    assert [receipt["step_id"] for receipt in checkpoint.receipts] == ["ready"]
    assert transaction.actions == [] and owned_runner.script_attempt is None


async def test_takeover_advance_requires_current_after():
    raw = document(after={"type": "target_present", "target": {"text": "Middle"}})
    second = document(before={"type": "target_present", "target": {"text": "Middle"}})["steps"][0]
    second.update(id="next", action={"type": "tap", "target": {"text": "Proceed"}})
    raw["steps"].append(second)
    transaction = FakeTransaction((READY_LABEL,))

    async def take_over(ctx):
        assert ctx.failure.reason == "target_unavailable" and ctx.next_step.id == "next"
        assert TakeoverDecision.CONTINUE_NEXT not in await ctx.resume_status()
        transaction.elements = (element(4, "Middle", [0, 0, 1, 1]),
                                element(5, "Proceed", [20, 20, 40, 40]))
        assert TakeoverDecision.CONTINUE_NEXT in await ctx.resume_status()
        return TakeoverDecision.CONTINUE_NEXT

    result = await run(raw, transaction, takeover=take_over)
    assert result.status == "succeeded" and result.assisted and result.final_asserted
    assert result.failed_step_id == "start" and len(transaction.actions) == 1


@pytest.mark.parametrize("second_confirms", [True, False])
async def test_each_takeover_hands_back_only_its_failed_step(owned_runner, second_confirms):
    second_ready = {"type": "target_present", "target": {"text": "Second ready"}}
    second_after = {"type": "target_present", "target": {"text": "Second result"}}
    third_ready = {"type": "target_present", "target": {"text": "Third ready"}}
    second_marker = element(2, "Second ready", [0, 0, 1, 1])
    second_result = element(3, "Second result", [0, 0, 1, 1])
    third_marker = element(4, "Third ready", [0, 0, 1, 1])
    proceed = element(8, "Proceed", [40, 40, 50, 50])
    raw = document(after=second_ready)
    second = document(before=second_ready, after=second_after)["steps"][0]
    second.update(id="second", action={"type": "tap", "target": {"text": "Old control"}})
    third = document(before=third_ready)["steps"][0]
    third.update(id="third", action={"type": "tap", "target": {"text": "Proceed"}})
    raw["steps"].extend((second, third))
    incidents = []
    checkpoint = ScriptCheckpoint(owned_runner, GuiScript.model_validate(raw), {})

    def perform(device):
        if len(device.actions) == 1:
            device.elements = (second_marker,)
        elif len(device.actions) == 2:
            device.elements = (second_result,)
        else:
            device.elements = (DONE_LABEL,)

    class OwnedTransaction(FakeTransaction):
        async def act_and_observe(self, action, before, *, deadline_ms, target):
            operation_id = owned_runner.prepare_action(action)
            result, after = await super().act_and_observe(action, before, deadline_ms=deadline_ms, target=target)
            owned_runner.store.receipt(operation_id, result.model_dump(mode="json"))
            return result, after

    transaction = OwnedTransaction(perform=perform)

    async def takeover(ctx):
        incidents.append(ctx.step.id)
        if ctx.step.id == "second":
            await transaction.act_and_observe(Action(type="tap_xy", x=15, y=15), ctx.observation,
                                              deadline_ms=20, target=None)
            assert TakeoverDecision.CONTINUE_NEXT in await ctx.resume_status()
            if not second_confirms:
                # Earlier advice cannot authorize handback after current evidence changes.
                transaction.elements = (third_marker, proceed)
            return TakeoverDecision.CONTINUE_NEXT
        assert ctx.step.id == "third" and ctx.failure.reason == "before_condition_unsatisfied"
        assert checkpoint.cursor == 2
        assert [receipt["step_id"] for receipt in checkpoint.receipts] == ["start", "second"]
        transaction.elements = (third_marker, proceed)
        assert TakeoverDecision.RETRY_CURRENT in await ctx.resume_status()
        return TakeoverDecision.RETRY_CURRENT

    outcome = await run(raw, transaction, takeover=takeover, checkpoint=checkpoint, takeover_seconds=0.5)
    assert [(action.x, action.y) for action in transaction.actions].count((15, 15)) == 1
    records = owned_runner.store.db.list_agent_records(owned_runner.owner.task_id, "gui_script_step_completion")
    if second_confirms:
        assert incidents == ["second", "third"]
        assert outcome.status == "succeeded"
        assert [(step.step_id, step.reason) for step in outcome.steps if step.succeeded] == [
            ("start", "postcondition_met"), ("second", "takeover_postcondition_met"),
            ("third", "postcondition_met"),
        ]
        assert [(row["payload"]["step_id"], row["payload"]["next_index"]) for row in records] == [
            ("start", 1), ("second", 2), ("third", 3),
        ]
    else:
        assert incidents == ["second"]
        assert outcome.status == "paused" and outcome.failed_step_id == "second"
        assert outcome.reason == "step_confirmation_required"
        assert not any(step.succeeded for step in outcome.steps if step.step_id == "second")
        assert checkpoint.cursor == 1 and checkpoint.pending(1) is not None
        assert [row["payload"]["step_id"] for row in records] == ["start"]
        assert len(transaction.actions) == 2


async def test_limited_stop_routes_to_formal_recovery_under_same_deadline():
    transaction = FakeTransaction((READY_LABEL,))
    deadlines = []

    async def local(ctx):
        deadlines.append(ctx.deadline)
        return TakeoverDecision.STOP

    async def formal(ctx):
        deadlines.append(ctx.deadline)
        transaction.elements = (DONE_LABEL,)
        assert TakeoverDecision.FINISH in await ctx.resume_status()
        return TakeoverDecision.FINISH

    result = await run(document(), transaction, takeover=local, formal_takeover=formal)
    assert len(deadlines) == 2 and deadlines[0] == deadlines[1]
    assert result.status == "succeeded" and result.steps[-1].reason == "takeover_postcondition_met"


async def test_formal_receipt_with_unknown_dispatch_cannot_confirm_generic_after(owned_runner):
    raw = document()
    checkpoint = ScriptCheckpoint(owned_runner, GuiScript.model_validate(raw), {})
    transaction = FakeTransaction((READY_LABEL,))

    async def formal(ctx):
        operation_id = owned_runner.prepare_action(Action(type="tap_xy", x=15, y=15))
        owned_runner.store.receipt(operation_id, {"success": True, "detail": {"dispatch_unknown": True}})
        transaction.elements = (DONE_LABEL,)
        assert await ctx.resume_status() == {TakeoverDecision.STOP}
        return TakeoverDecision.STOP

    outcome = await run(raw, transaction, formal_takeover=formal, checkpoint=checkpoint, takeover_seconds=0.15)
    assert outcome.status == "paused" and outcome.reason == "step_confirmation_required"
    assert checkpoint.cursor == 0 and checkpoint.pending(0) is not None


async def test_takeover_finish_has_to_pass_fresh_final_oracle():
    transaction = FakeTransaction((READY_LABEL,))

    async def claim_finish(ctx):
        transaction.elements = (DONE_LABEL,)
        return TakeoverDecision.FINISH

    denied = await run(document(), transaction, takeover=claim_finish, final_oracle=lambda _: False)
    assert denied.status == "failed" and denied.assisted and not denied.final_asserted
    assert denied.reason == "takeover_resume_condition_unsatisfied" and transaction.actions == []

    transaction.elements = (READY_LABEL,)
    accepted = await run(document(), transaction, takeover=claim_finish, final_oracle=lambda _: True)
    assert accepted.status == "succeeded" and accepted.assisted and accepted.final_asserted


async def test_retry_requires_no_effect_and_can_only_redispatch_current_step_once():
    txn = FakeTransaction(perform=lambda _: None)
    raw = document()
    raw["steps"][0]["retry_when"] = [READY]

    async def request_retry(ctx):
        pytest.fail("a possibly dispatched action was offered to writable takeover")

    result = await run(raw, txn, takeover=request_retry)
    assert result.reason == "postcondition_timeout" and len(txn.actions) == 1

    txn = FakeTransaction((READY_LABEL,), perform=lambda _: None)
    seen = []

    async def verify_retry(ctx):
        seen.append(ctx.step.id)
        txn.elements = (READY_LABEL, PLAY)
        assert TakeoverDecision.RETRY_CURRENT in await ctx.resume_status()
        return TakeoverDecision.RETRY_CURRENT

    result = await run(raw, txn, takeover=verify_retry)
    assert result.status == "failed" and result.assisted
    assert [receipt.attempt for receipt in result.steps] == [1, 2]
    assert seen == ["start"] and len(txn.actions) == 1


async def test_two_distinct_takeover_incidents_share_the_same_time_limit():
    first_after = {"type": "target_present", "target": {"text": "Middle"}}
    raw = document(after=first_after)
    second = document(before=first_after)["steps"][0]
    second["id"] = "next"
    raw["steps"].append(second)
    transaction = FakeTransaction((READY_LABEL,))
    handed_off = []

    async def assist(ctx):
        handed_off.append(ctx.step.id)
        if ctx.next_step is not None:
            await asyncio.sleep(0.055)
            transaction.elements = (element(5, "Middle", [10, 10, 30, 30]),)
            return TakeoverDecision.CONTINUE_NEXT
        await asyncio.sleep(0.06)
        pytest.fail("second takeover received a fresh time allowance")

    result = await run(raw, transaction, takeover=assist, takeover_seconds=0.1)
    assert result.status == "failed" and result.reason == "takeover_deadline_exceeded"
    assert handed_off == ["start", "next"] and not transaction.actions


async def test_cooperative_cancel_before_dispatch_does_not_touch_device():
    transaction = FakeTransaction()
    stop = False

    def stop_at_boundary():
        nonlocal stop
        stop = True

    result = await run(document(), transaction, cancelled=lambda: stop, before_dispatch=stop_at_boundary)
    assert result.status == "cancelled"
    assert transaction.actions == []
