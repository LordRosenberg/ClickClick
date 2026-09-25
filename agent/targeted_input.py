"""A bounded focus-and-replace operation for structurally identifiable fields."""

import asyncio
from collections.abc import Callable

from perception.input_evidence import editability_evidence, is_editable
from perception.observation import ObservationPackage
from shared.schemas import Action, ActionResult, ActionTargetSnapshot, CanonicalUI, UIElement, EffectOutcome


def _key(ui: CanonicalUI, element: UIElement) -> tuple:
    return (ui.app_id, element.display_id, element.window_id, element.resource_id, element.role)


def _ancestors(ui: CanonicalUI, element: UIElement) -> tuple:
    tree = ui.semantic_tree
    position = next((i for i, node in enumerate(tree) if node.index == element.index and node.interactable), None)
    if position is None:
        return ()
    parents = {child: i for i, node in enumerate(tree) for child in node.children}
    path, visited = [], set()
    while position in parents and position not in visited:
        visited.add(position)
        position = parents[position]
        node = tree[position]
        path.append((node.role, node.resource_id))
    return tuple(reversed(path))


def supported_target(package: ObservationPackage, index: int) -> UIElement | None:
    if not package.accepted or not package.index_actionable or not package.ui.capture_complete:
        return None
    target = next((node for node in package.ui.elements if node.index == index), None)
    if target is None or not is_editable(target) or not target.resource_id or target.window_id is None:
        return None
    if target.states.get("password") or target.states.get("enabled") is False or len(target.bounds) != 4:
        return None
    x1, y1, x2, y2 = target.bounds
    if x1 >= x2 or y1 >= y2:
        return None
    matches = [node for node in package.ui.elements if _key(package.ui, node) == _key(package.ui, target)]
    return target if len(matches) == 1 else None


def focused_same_target(before: ObservationPackage, target: UIElement, after: ObservationPackage) -> UIElement | None:
    candidates = [node for node in after.ui.elements if _key(after.ui, node) == _key(before.ui, target)]
    if len(candidates) != 1:
        return None
    candidate = supported_target(after, candidates[0].index)
    if candidate is None or not candidate.states.get("focused"):
        return None
    # Input-method controls have their own window-local focus (e.g. the IME
    # Back button). They do not compete with the app's focused text field.
    # Keep IME editors, unknown windows and other competing focus fail-closed.
    focused = [node for node in after.ui.elements
               if node.states.get("focused") and not node.window_wrapper
               and not (editability_evidence(node) == "not_editable"
                        and node.window_type == 2 and node.window_id is not None
                        and node.window_id != candidate.window_id)]
    if len(focused) != 1 or _ancestors(before.ui, target) != _ancestors(after.ui, candidate):
        return None
    return candidate


def input_method_visible(package: ObservationPackage) -> bool:
    """Return whether the current frame exposes an Android IME window.

    Some editors mark themselves focused before Android has created an active
    input connection.  ADBKeyboard broadcasts are then acknowledged by Android
    but silently change no text.  TYPE_INPUT_METHOD is window type 2.
    """
    interaction = getattr(package, "interaction_state", None)
    if interaction is not None and interaction.keyboard_visible is True:
        return True
    nodes = [*package.ui.semantic_tree, *package.ui.elements]
    return any(node.window_wrapper and node.window_type == 2 for node in nodes)


def required_actions(package: ObservationPackage | None, action: Action) -> int:
    # AndroidWorld budgets one submitted agent action as one episode step.
    # Its own input_text(index, text) primitive also bundles target focus and
    # text entry inside that single step.
    return 1


async def replace_target_text(transaction, action: Action, before: ObservationPackage, *,
                              target_snapshot: ActionTargetSnapshot | None,
                              cancel_requested: Callable[[], bool], remaining_actions: int | None):
    """Never infer a business result or replay an uncertain write."""
    stages = {"focus": "not_attempted", "clear": "not_attempted", "input": "not_attempted", "readback": "unknown"}
    target = supported_target(before, action.index)

    def stopped(reason, package, *, result=None, count=1):
        if result is None:
            result = ActionResult(success=False, message=reason, detail={"device_dispatch": "not_dispatched"})
        elif result.receipt is not None:
            result.detail["focus_receipt"] = result.receipt.model_dump(mode="json")
            result.receipt.effect_outcome = EffectOutcome.UNKNOWN
            result.receipt.effect_reason = reason
        result.success = False
        result.message = reason
        result.detail.update(input_steps=dict(stages), device_action_units=count)
        return result, package

    if target is None:
        return stopped("targeted_input_unsupported: focus the intended field separately, confirm focus, then use replace_text without index", before)
    if remaining_actions is not None and remaining_actions < required_actions(before, action):
        return stopped("insufficient_budget_for_focus_and_input", before)
    if cancel_requested():
        raise asyncio.CancelledError
    current = before
    focus_receipt = None
    focus = None
    if focused_same_target(before, target, current) is None or not input_method_visible(current):
        x1, y1, x2, y2 = target.bounds
        focus, current = await transaction.act_and_observe(
            Action(type="tap_xy", x=(x1+x2)/2, y=(y1+y2)/2), before, target=target_snapshot,
        )
        focus_receipt = focus.receipt.model_dump(mode="json") if focus.receipt else None
        stages["focus"] = "dispatched" if focus.success else "failed"
        if not focus.success or focused_same_target(before, target, current) is None:
            return stopped("focus_unverified: text was not entered", current, result=focus)
    else:
        current = await transaction.observe_current(source="input_focus_check")
        if focused_same_target(before, target, current) is None:
            return stopped("focus_unverified: text was not entered", current)
    stages["focus"] = "confirmed"
    if cancel_requested():
        return stopped("cancelled_after_focus: text was not entered", current, result=focus)
    result, after = await transaction.act_and_observe(
        action.model_copy(update={"index": None}), current, target=target_snapshot,
    )
    detail = result.detail
    if result.success:
        stages.update(clear="dispatched", input="dispatched")
    elif detail.get("stage") == "input":
        stages.update(clear="dispatched", input="failed")
    elif detail.get("stage") == "clear":
        stages["clear"] = "failed"
    actual = focused_same_target(before, target, after)
    if actual is not None:
        stages["readback"] = "exact_match" if actual.text == action.text else "different"
    result.detail.update(input_steps=stages, device_action_units=1)
    if focus_receipt is not None:
        result.detail["focus_receipt"] = focus_receipt
    if result.success and stages["readback"] == "different":
        result.success = False
        result.message = "replace_text: readback_mismatch"
        if result.receipt is not None:
            result.receipt.effect_outcome = EffectOutcome.UNKNOWN
            result.receipt.effect_reason = "replace_text_readback_mismatch"
    return result, after
