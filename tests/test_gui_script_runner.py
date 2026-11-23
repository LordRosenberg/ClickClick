from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from pydantic import ValidationError

from agent.skills.script_runner import TakeoverDecision, check_conditions, run_gui_script
from agent.skills.script_schema import GuiScript, script_fingerprint
from driver.fixture import DEFAULT_TREE, FixtureDriver
from perception.observation import ObservationPackage
from shared.schemas import (
    ActionReceipt, ActionResult, CanonicalUI, FocusedElementEvidence, InteractionStateEvidence, ObservationMode, UIElement,
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


async def test_takeover_advance_requires_current_after_and_next_before():
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

    async def request_retry(ctx):
        return TakeoverDecision.RETRY_CURRENT

    result = await run(raw, txn, takeover=request_retry)
    assert result.reason == "takeover_resume_condition_unsatisfied" and len(txn.actions) == 1

    txn = FakeTransaction(perform=lambda _: None)
    raw["steps"][0]["retry_when"] = [READY]
    seen = []

    async def verify_retry(ctx):
        seen.append(ctx.step.id)
        assert TakeoverDecision.RETRY_CURRENT in await ctx.resume_status()
        return TakeoverDecision.RETRY_CURRENT

    result = await run(raw, txn, takeover=verify_retry)
    assert result.status == "failed" and result.assisted
    assert [receipt.attempt for receipt in result.steps] == [1, 2]
    assert seen == ["start"] and len(txn.actions) == 2


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
