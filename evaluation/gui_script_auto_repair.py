"""Bounded, deterministic takeover for an explicitly replayed GUI script."""

from __future__ import annotations

from agent.executor import resolve_tap_index
from agent.skills.script_runner import TakeoverDecision, check_conditions, resolve_semantic_target, run_gui_script
from agent.skills.script_schema import AppCondition, GuiScript, ScriptStep, SemanticSelector, TargetCondition
from agent.skills.script_targeting import invariant_target, numbered_description_changed
from perception.normalizer import _descendants
from perception.observation import ObservationBuilder
from shared.schemas import Action


_DISMISS_LABELS = {"cancel", "dismiss", "close", "取消", "关闭"}
_STOCK_DISMISS_LABELS = _DISMISS_LABELS - {"dismiss"}
_STOCK_DIALOG_HANDLES = "script_stock_dialog_negative_handles"
_COLLECTOR_TREE_PROVIDERS = {"accessibility_collector", "accessibility_collector_channel"}


class _GuardChanged(ValueError):
    pass


def _stock_dialog_negative_handle(window):
    ids = ("parentPanel", "alertTitle", "contentPanel", "buttonPanel", "button2")
    nodes = _descendants(window)
    required = {
        resource: [node for node in nodes if node.get("resource_id") == f"android:id/{resource}"]
        for resource in ids
    }
    if any(len(matches) != 1 for matches in required.values()):
        return None
    parent, title, content, buttons, negative = (required[resource][0] for resource in ids)
    contained = {id(node) for node in _descendants(parent)}
    content_nodes = {id(node) for node in _descendants(content)}
    button_nodes = {id(node) for node in _descendants(buttons)}
    if (not {id(title), id(content), id(buttons)} <= contained or content_nodes & button_nodes
            or id(title) in content_nodes | button_nodes or id(negative) not in button_nodes
            or title.get("class") != "android.widget.TextView" or not str(title.get("text") or "").strip()
            or negative.get("class") != "android.widget.Button" or negative.get("clickable") is not True
            or negative.get("enabled") is not True):
        return None
    handle = negative.get("node_handle")
    return handle if isinstance(handle, str) and handle else None


class ScriptRecoveryObservationBuilder(ObservationBuilder):
    """Retain exact framework view identity only for opted-in script recovery."""

    def package(self, prepared, *, attach_image):
        package = super().package(prepared, attach_image=attach_image)
        package.capture_meta.pop(_STOCK_DIALOG_HANDLES, None)
        if (package.ui.platform != "android" or not package.index_actionable
                or package.capture_meta.get("tree_provider") not in _COLLECTOR_TREE_PROVIDERS
                or package.capture_meta.get("tree_ownership_candidates_complete") is not True):
            return package
        handles = {}
        for window in prepared.raw_tree.get("children", ()):
            if (isinstance(window, dict) and window.get("window_wrapper")
                    and type(window.get("window_id")) is int and window.get("window_type") == 1 and window.get("display_id") == 0
                    and window.get("active") and window.get("focused")):
                handle = _stock_dialog_negative_handle(window)
                if handle:
                    handles[window["window_id"]] = handle
        package.capture_meta[_STOCK_DIALOG_HANDLES] = handles
        return package


def is_low_impact_initial(script):
    """Only built-in passive gestures and explicit native tabs enroll without impact review."""
    if script.revision != 1 or script.parameters:
        return False
    for step in script.steps:
        action = step.action
        if action.type in {"wait", "scroll", "back"}:
            continue
        if (action.type != "tap" or action.visual is not None or action.object is not None
                or action.target.role != "Tab" or not action.target.resource_id):
            return False
    return True


def is_low_impact_repair(before, after):
    if (after.revision != before.revision + 1 or len(before.steps) != len(after.steps)
            or before.model_dump(exclude={"steps", "revision"}) != after.model_dump(exclude={"steps", "revision"})):
        return False
    changes = [(old, new) for old, new in zip(before.steps, after.steps, strict=True) if old != new]
    if len(changes) != 1:
        return False
    old, new = changes[0]
    if old.action.type not in {"tap", "long_press"} or new.action.type != old.action.type:
        return False
    if getattr(old.action, "visual", None) is not None or getattr(old.action, "object", None) is not None:
        return False
    original = old.action.target
    stable = invariant_target(original)
    if stable is None or new.action != old.action.model_copy(update={"target": stable}):
        return False
    guards = _replace_target_guard(old.before, original, stable)
    return (sorted(item.model_dump_json() for item in new.before) == sorted(item.model_dump_json() for item in guards)
            and new.model_dump(exclude={"before", "action"}) == old.model_dump(exclude={"before", "action"}))


def _replace_target_guard(conditions, old, new):
    return tuple(condition.model_copy(update={"target": new})
                 if isinstance(condition, TargetCondition) and condition.type == "target_present"
                 and condition.visual is None and condition.target == old else condition
                 for condition in conditions)


def _same_control_step(ctx, sample):
    old = ctx.step.action
    if old.type not in {"tap", "long_press"} or getattr(old, "visual", None) or getattr(old, "object", None):
        return None
    stable = invariant_target(old.target)
    if stable is None or resolve_semantic_target(sample, old.target, ctx.parameters) is not None:
        return None
    element = resolve_semantic_target(sample, stable, ctx.parameters)
    if element is None or element.states.get("password"):
        return None
    if not numbered_description_changed(old.target.desc, element.desc):
        return None
    current = old.target.model_copy(update={"desc": element.desc})
    return ctx.step.model_copy(update={
        "id": "recover-contracted-target", "action": old.model_copy(update={"target": current}),
        "before": _replace_target_guard(ctx.step.before, old.target, current),
    })


def _dismiss_step(ctx, sample):
    original_target = getattr(ctx.step.action, "target", None)
    if (not isinstance(original_target, SemanticSelector) or sample.ui.app_id != ctx.script.app_id
            or sample.ui.platform != "android" or not sample.accepted or not sample.index_actionable):
        return None
    candidates = sample.capture_meta.get("tree_ownership_candidates", ())
    windows = [item for item in candidates
               if item.get("source_kind") == "window" and item.get("window_type") == 1
               and item.get("display_id") == 0 and item.get("packages") == [ctx.script.app_id]
               and type(item.get("window_id")) is int and type(item.get("window_layer")) is int]
    native_handle = None
    if len(windows) == 2:
        lower, modal = sorted(windows, key=lambda row: row["window_layer"])
        if (modal["window_layer"] <= lower["window_layer"] or lower.get("active") or lower.get("focused")
                or not any(element.window_id == modal["window_id"]
                           and element.role.rsplit(".", 1)[-1] in {"Dialog", "AlertDialog"}
                           for element in sample.ui.semantic_tree)):
            return None
    elif len(windows) == len(candidates) == 1:
        modal, = windows
        native_handle = sample.capture_meta.get(_STOCK_DIALOG_HANDLES, {}).get(modal["window_id"])
        if (not native_handle or resolve_semantic_target(sample, original_target, ctx.parameters) is not None):
            return None
    else:
        return None
    if not modal.get("active") or not modal.get("focused"):
        return None
    modal_id = modal["window_id"]
    allowed_labels = _STOCK_DISMISS_LABELS if native_handle else _DISMISS_LABELS
    buttons = [element for element in sample.ui.elements if element.window_id == modal_id
               and element.role.rsplit(".", 1)[-1] == "Button" and element.clickable and element.resource_id
               and (element.text or element.desc).strip().casefold() in allowed_labels
               and (not element.text or not element.desc or element.text.casefold() == element.desc.casefold())]
    if len(buttons) != 1:
        return None
    button = buttons[0]
    if native_handle and (button.node_handle != native_handle or button.resource_id != "button2"):
        return None
    selector = SemanticSelector(resource_id=button.resource_id, role="Button", text=button.text or None,
                                desc=button.desc or None)
    if resolve_semantic_target(sample, selector, ctx.parameters) != button:
        return None
    before = (AppCondition(type="app_is", app_id=ctx.script.app_id), TargetCondition(type="target_present", target=selector))
    after = ctx.step.before
    # The dismissal itself must no longer be present before handing back.
    if len(after) < 24:
        after = (*after, TargetCondition(type="target_absent", target=selector))
    return ScriptStep.model_validate({
        "id": "dismiss-proven-modal", "before": before, "after": after,
        "action": {"type": "tap", "target": selector},
    }), modal_id


def contracted_recovery(*, driver, transaction, evidence, asset_root=None, ocr=None):
    """Run at most one proven action, then let the replay host verify handoff."""
    used = bool(getattr(evidence, "limited_attempts", 0))

    async def recover(ctx):
        nonlocal used
        if used or ctx.failure.execution != "not_dispatched":
            return TakeoverDecision.STOP
        sample = await ctx.observe()
        if sample is None or not sample.accepted or sample.ui.app_id != ctx.script.app_id:
            return TakeoverDecision.STOP
        options = {"parameters": ctx.parameters, "asset_root": asset_root, "ocr": ocr}
        if check_conditions(ctx.script, ctx.step.after, sample, **options):
            allowed = await ctx.resume_status()
            return next((decision for decision in (TakeoverDecision.CONTINUE_NEXT, TakeoverDecision.FINISH)
                         if decision in allowed), TakeoverDecision.STOP)
        dismissed = _dismiss_step(ctx, sample)
        step = dismissed[0] if dismissed else _same_control_step(ctx, sample)
        if step is None:
            allowed = await ctx.resume_status()
            return next((decision for decision in (TakeoverDecision.FINISH, TakeoverDecision.CONTINUE_NEXT,
                                                    TakeoverDecision.RETRY_CURRENT) if decision in allowed),
                        TakeoverDecision.STOP)
        if ((step.blocked_when and check_conditions(ctx.script, step.blocked_when, sample, **options))
                or not check_conditions(ctx.script, step.before, sample, **options)):
            return TakeoverDecision.STOP
        used = True
        replay = GuiScript(script_id="contracted-recovery", app_id=ctx.script.app_id, platform=ctx.script.platform,
                           parameters=ctx.script.parameters, initial=step.before, steps=(step,), final=step.after)
        last = sample

        def observe(package):
            nonlocal last
            last = package
            evidence.observation(package)

        def before_dispatch():
            # Guard the exact fresh sample the nested runner will use for dispatch.
            current = _dismiss_step(ctx, last) if dismissed else _same_control_step(ctx, last)
            if current != (dismissed if dismissed else step) or check_conditions(ctx.script, ctx.step.after, last, **options):
                raise _GuardChanged("contracted_recovery_guard_changed")
            native_handle = None
            if dismissed and len(last.capture_meta.get("tree_ownership_candidates", ())) == 1:
                native_handle = last.capture_meta.get(_STOCK_DIALOG_HANDLES, {}).get(dismissed[1])
            if native_handle:
                node = resolve_semantic_target(last, step.action.target, ctx.parameters)
                bound = resolve_tap_index(Action(type="tap", index=node.index), last.ui,
                                          native_node_click=getattr(driver, "supports_native_node_click", False) is True)
                if bound._node_handle != native_handle:
                    raise _GuardChanged("stock_dialog_native_dispatch_unavailable")
            if ctx.bind_observation is not None:
                ctx.bind_observation(last)

        def record(recovery_step, before, action, result, after):
            evidence.action(recovery_step, before, action, result, after, origin="takeover", parent_step_id=ctx.step.id)

        try:
            result = await run_gui_script(replay, driver=driver, transaction=transaction, on_observation=observe,
                                          on_action=record, before_dispatch=before_dispatch, **options)
        except _GuardChanged:
            return TakeoverDecision.STOP
        if result.status != "succeeded":
            return TakeoverDecision.STOP
        if dismissed:
            sample = await ctx.observe()
            windows = sample.capture_meta.get("tree_ownership_candidates", ()) if sample is not None else ()
            if (not any(row.get("packages") == [ctx.script.app_id] and row.get("active") and row.get("focused")
                        for row in windows) or any(row.get("window_id") == dismissed[1] for row in windows)
                    or resolve_semantic_target(sample, ctx.step.action.target, ctx.parameters) is None):
                return TakeoverDecision.STOP
            if ctx.resolve_limited is not None:
                ctx.resolve_limited(result)
            return (TakeoverDecision.RETRY_CURRENT if TakeoverDecision.RETRY_CURRENT in await ctx.resume_status()
                    else TakeoverDecision.STOP)
        allowed = await ctx.resume_status()
        return next((decision for decision in (TakeoverDecision.CONTINUE_NEXT, TakeoverDecision.FINISH)
                     if decision in allowed), TakeoverDecision.STOP)

    return recover
