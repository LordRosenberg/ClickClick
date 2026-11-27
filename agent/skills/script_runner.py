"""Execute a versioned GUI script with current observations and native transactions."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent.action_observation import ActionObservationTransaction, ObservationStageError
from agent.executor import _target_snapshot, resolve_long_press_index, resolve_tap_index
from agent.skills.script_checkpoint import ScriptDispatchBlocked
from agent.skills.script_schema import (
    ActivityCondition, AppCondition, GuiScript, SemanticSelector, ScriptStep, SelectorFields, TapAction,
    VisualCondition, VisualObjectCondition, VisualTapAction, script_fingerprint,
)
from agent.skills.script_targeting import choose_candidate
from agent.skills.script_visual import VisualMatchResult, match_visual_template
from agent.skills.script_visual_object import VisualObject, match_visual_object
from perception.observation import ObservationBuilder, ObservationPackage, decoded_image_size
from shared.recovery_context import current_execution
from shared.recovery_runtime import InfrastructureInterrupted
from shared.schemas import Action, ActionReceipt, EffectOutcome, ObservationMode, UIElement
from shared.task_budgets import TaskBudgetExhausted
from shared.task_control import begin_dispatch


class ScriptStepReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    attempt: int
    succeeded: bool
    execution: Literal["not_dispatched", "known", "unknown"]
    reason: str
    before_observation_id: str | None = None
    after_observation_id: str | None = None
    receipt: ActionReceipt | None = None


class ScriptRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    script_hash: str
    script_id: str
    revision: int
    status: Literal["succeeded", "failed", "cancelled", "paused"]
    assisted: bool = False
    steps: tuple[ScriptStepReceipt, ...] = ()
    failed_step_id: str | None = None
    reason: str
    initial_observation_id: str | None = None
    final_observation_id: str | None = None
    final_asserted: bool = False


class TakeoverDecision(StrEnum):
    STOP = "stop"
    RETRY_CURRENT = "retry_current"
    CONTINUE_NEXT = "continue_next"
    FINISH = "finish"


@dataclass(frozen=True)
class TakeoverContext:
    script: GuiScript
    step: ScriptStep
    next_step: ScriptStep | None
    parameters: Mapping[str, str]
    failure: ScriptStepReceipt
    observation: ObservationPackage | None
    observe: Callable[[], Awaitable[ObservationPackage | None]]
    resume_status: Callable[[], Awaitable[frozenset[TakeoverDecision]]]
    deadline: float | None = None
    resolve_limited: Callable[[ScriptRunResult], None] | None = None
    bind_observation: Callable[[ObservationPackage | None], None] | None = None


class _ReplayCancelled(Exception):
    pass


def _matches_fields(element: UIElement, fields: SelectorFields, values: Mapping[str, str]) -> bool:
    if element.states.get("password") and (fields.text is not None or fields.text_parameter is not None):
        return False
    expected_text = fields.text
    if fields.text_parameter is not None:
        expected_text = values.get(fields.text_parameter)
        if expected_text is None:
            return False
    # The trace uses the short role label, while a CanonicalUI element can keep the original class.
    role_matches = fields.role is None or element.role == fields.role or element.role.rsplit(".", 1)[-1] == fields.role
    return role_matches and all(expected is None or actual == expected for actual, expected in (
        (element.resource_id, fields.resource_id),
        (element.text, expected_text), (element.desc, fields.desc),
        (element.hint, fields.hint), (element.ctx, fields.context),
        (element.states.get("checked"), fields.checked), (element.states.get("enabled"), fields.enabled),
        (element.states.get("selected"), fields.selected),
    ))


def _app_window_ids(package: ObservationPackage) -> set[int | None]:
    ui = package.ui
    if not any(element.window_id is not None for element in ui.semantic_tree or ui.elements):
        return {None}
    candidates = package.capture_meta.get("tree_ownership_candidates") or []
    return {row["window_id"] for row in candidates if row.get("window_id") is not None
            and row.get("packages") == [ui.app_id]}


def _matches(package: ObservationPackage, selector, values, *, actionable=False) -> list[UIElement]:
    ui = package.ui
    windows = _app_window_ids(package)
    ancestor = getattr(selector, "ancestor", None)
    if not ui.semantic_tree or ancestor is None:
        elements = ui.elements if actionable else (ui.semantic_tree or ui.elements)
        if ancestor is not None:
            return []
        return [element for element in elements
                if element.window_id in windows and _matches_fields(element, selector, values)]
    parents = {child: index for index, element in enumerate(ui.semantic_tree) for child in element.children}
    matches = []
    for index, element in enumerate(ui.semantic_tree):
        if element.window_id not in windows or (actionable and (element.index < 0 or not element.interactable)):
            continue
        if not _matches_fields(element, selector, values):
            continue
        seen = {index}
        parent = parents.get(index)
        while parent is not None and parent not in seen and 0 <= parent < len(ui.semantic_tree):
            if ui.semantic_tree[parent].window_id in windows and _matches_fields(ui.semantic_tree[parent], ancestor, values):
                matches.append(element)
                break
            seen.add(parent)
            parent = parents.get(parent)
    return matches


def _tree_ready(package: ObservationPackage) -> bool:
    return bool(package.accepted and package.mode != ObservationMode.IMAGE_ONLY
                and package.ui.capture_complete)


def _observation_id(package: ObservationPackage | None) -> str | None:
    return package.observation_id if package is not None and package.accepted else None


def _dispatch_unknown(detail):
    return (detail.get("execution_result") == "unknown" or detail.get("dispatch_unknown") is True
            or detail.get("device_dispatch") == "unknown" or detail.get("node_click_status") == "outcome_unknown")


def _disambiguate(package, selector: SemanticSelector, values, matches):
    anchor, relation = None, None
    if selector.relative_to is not None:
        anchors = _matches(package, selector.relative_to.anchor, values)
        if len(anchors) != 1:
            return None
        anchor, = anchors
        relation = selector.relative_to.relation
    return choose_candidate(matches, relative_anchor=anchor, relation=relation,
                            occurrence=selector.occurrence, expected_count=selector.expected_count)


def _unique_action_target(package, selector, values):
    if not _tree_ready(package) or not package.index_actionable:
        raise ValueError("semantic_tree_unavailable")
    matches = _matches(package, selector, values, actionable=True)
    element = _disambiguate(package, selector, values, matches)
    if element is None:
        raise ValueError("target_unavailable" if not matches else "target_ambiguous")
    if element.index < 0 or not element.interactable or element.states.get("enabled") is False:
        raise ValueError("target_not_actionable")
    return element


def resolve_semantic_target(package, selector, values: Mapping[str, str]) -> UIElement | None:
    """Return the unique actionable candidate in the accepted foreground app."""
    try:
        return _unique_action_target(package, selector, values)
    except ValueError:
        return None


def _match_visual(package, template, asset_root, *, parameters=None, ocr=None):
    if (asset_root is None or _observation_id(package) is None or not package.clean_png or package.crop_box is not None
            or package.frame_width <= 0 or package.frame_height <= 0
            or decoded_image_size(package.clean_png) != (package.frame_width, package.frame_height)):
        return VisualMatchResult("unavailable", reason="visual_coordinate_space_unavailable")
    if isinstance(template, VisualObject):
        return match_visual_object(template, package.clean_png, asset_root=asset_root,
                                   parameters=parameters, ocr=ocr)
    return match_visual_template(template, package.clean_png, asset_root=asset_root)


def _node_fact(node):
    return {"role": node.role, "id": node.resource_id, "context": node.ctx,
            "label": node.text if not node.states.get("password") else "",
            "checked": node.states.get("checked"), "enabled": node.states.get("enabled"),
            "selected": node.states.get("selected")}


def _condition_fact(script, condition, package, values, visual):
    if _observation_id(package) is None or package.ui.platform != script.platform or not package.ui.app_id:
        return None, None
    if isinstance(condition, AppCondition):
        return package.ui.app_id == condition.app_id, {"app": package.ui.app_id}
    if package.ui.app_id != script.app_id:
        return None, None
    if isinstance(condition, ActivityCondition):
        if not package.ui.activity:
            return None, None
        return package.ui.activity == condition.activity, {"activity": package.ui.activity}
    is_visual = isinstance(condition, (VisualCondition, VisualObjectCondition))
    if is_visual or not (_tree_ready(package) and _app_window_ids(package)):
        template = condition.target if is_visual else condition.visual
        if template is None:
            return None, None
        result = visual(package, template)
        if result.status == "unavailable":
            return None, None
        found = result.status == "matched"
        if found and isinstance(template, VisualObject) and len(result.matches) != 1:
            return None, None
        asset = template.layout if isinstance(template, VisualObject) else template
        return (found if condition.type.endswith("present") else not found,
                {"template": asset.sha256, "match_count": len(result.matches)})
    selector = condition.target
    matches = _matches(package, selector, values)
    if condition.type == "target_absent":
        return not matches, None if not matches else sorted(map(_node_fact, matches), key=repr)
    if selector.relative_to is not None or selector.occurrence is not None:
        exact = _disambiguate(package, selector, values, matches)
        if exact is None and matches:
            return None, None
        return exact is not None, _node_fact(exact) if exact is not None else None
    if selector.expected_count is not None and len(matches) != selector.expected_count:
        return False, sorted(map(_node_fact, matches), key=repr)
    if len(matches) == 1:
        return True, _node_fact(matches[0])
    return bool(matches), sorted(map(_node_fact, matches), key=repr) if matches else None


def check_conditions(script, conditions, package, *, parameters=None, asset_root: Path | None = None,
                     visual_lookup=None, ocr=None) -> bool:
    """Evaluate conditions on one accepted observation. Presence and click identity are separate checks."""
    visual = visual_lookup or (lambda frame, template: _match_visual(
        frame, template, asset_root, parameters=parameters, ocr=ocr))
    return all(_condition_fact(script, condition, package, parameters or {}, visual)[0] is True
               for condition in conditions)


def observed_condition_facts(script, conditions, package, *, parameters=None, asset_root: Path | None = None, ocr=None):
    """Return comparable App-local witness values or None when they are unavailable."""
    facts = []
    for condition in conditions:
        verdict, fact = _condition_fact(script, condition, package, parameters or {},
                                       lambda frame, template: _match_visual(
                                           frame, template, asset_root, parameters=parameters, ocr=ocr))
        if verdict is None:
            return None
        facts.append(fact)
    return facts


class _Replay:
    def __init__(self, script, values, driver, transaction, *, cancelled, takeover, takeover_seconds,
                 formal_takeover, checkpoint, poll_seconds, on_observation, on_action, before_dispatch, final_oracle,
                 asset_root, ocr=None):
        self.script = script
        self.script_hash = script_fingerprint(script)
        self.values = MappingProxyType(values)
        self.driver = driver
        self.transaction = transaction
        self.cancelled = cancelled
        self.takeover = takeover
        self.formal_takeover = formal_takeover
        self.checkpoint = checkpoint
        self.takeover_seconds = takeover_seconds
        self.poll_seconds = poll_seconds
        self.on_observation = on_observation
        self.on_action = on_action
        self.before_dispatch = before_dispatch
        self.final_oracle = final_oracle
        self.asset_root = asset_root
        self.ocr = ocr
        self.visual_frame_id = None
        self.visual_matches = {}
        self.receipts = ([ScriptStepReceipt.model_validate(receipt) for receipt in checkpoint.receipts]
                         if checkpoint is not None else [])
        self.same_turn_operations: set[str] = set()
        self.assisted = False
        self.takeover_used = checkpoint.takeover_used if checkpoint is not None else 0.0
        self.initial_id = None
        self.failed_step_id = None

    def _check_cancel(self):
        if self.cancelled is not None and self.cancelled():
            raise _ReplayCancelled

    def _app_matches(self, package):
        return package.ui.app_id == self.script.app_id and package.ui.platform == self.script.platform

    def _holds(self, conditions, package):
        return check_conditions(self.script, conditions, package, parameters=self.values, visual_lookup=self._visual)

    def _business_ids(self, step, package):
        confirmation = step.confirmation
        if (confirmation is None or _observation_id(package) is None or not _tree_ready(package)
                or package.ui.platform != self.script.platform or package.ui.app_id != self.script.app_id):
            return None
        tree = package.ui.semantic_tree
        windows = _app_window_ids(package)
        scopes = [node for node in tree if node.window_id in windows
                  and _matches_fields(node, confirmation.scope, self.values)]
        if len(scopes) != 1:
            return None
        scope, = scopes
        remaining = list(scope.children)
        visited = set()
        identifiers = set()
        while remaining:
            index = remaining.pop()
            if index in visited or not 0 <= index < len(tree):
                return None
            visited.add(index)
            node = tree[index]
            if node.window_id != scope.window_id:
                return None
            remaining.extend(node.children)
            if not _matches_fields(node, confirmation.target, self.values):
                continue
            if node.states.get("password") or not node.text.startswith(confirmation.identifier_prefix):
                return None
            identifier = node.text[len(confirmation.identifier_prefix):].strip()
            if not identifier or len(identifier) > 128 or any(character.isspace() for character in identifier):
                return None
            identifiers.add(identifier)
        return sorted(identifiers)

    def _pending(self, index):
        return self.checkpoint.pending(index) if self.checkpoint is not None else None

    def _accept_same_turn(self, index):
        pending = self._pending(index)
        if pending is None or pending["result_state"] != "known":
            return
        result = pending["result"] or {}
        detail = result.get("detail") or {}
        native = result.get("receipt")
        if (not result.get("success") or _dispatch_unknown(detail) or detail.get("device_action_units") == 0
                or detail.get("device_dispatch") == "not_dispatched" or native and not native.get("dispatch_succeeded")):
            return
        self.same_turn_operations.add(pending["operation_id"])

    def _step_confirmed(self, index, step, package, *, before_id=None, baseline=None):
        observation_id = _observation_id(package)
        if observation_id is None or observation_id == before_id:
            return False
        pending = self._pending(index)
        if pending is not None and observation_id == pending["before_observation_id"]:
            return False
        if step.confirmation is None:
            return (pending is None or pending["operation_id"] in self.same_turn_operations) and self._holds(
                step.after, package,
            )
        identifiers = self._business_ids(step, package)
        if pending is None:
            return baseline is not None and identifiers is not None and len(set(identifiers) - set(baseline)) == 1
        return self.checkpoint.correlates(index, identifiers)

    def _complete(self, index, step, receipt, package):
        if self.checkpoint is not None:
            pending = self._pending(index)
            if step.confirmation is not None:
                evidence = self._business_ids(step, package)
            else:
                evidence = observed_condition_facts(self.script, step.after, package, parameters=self.values,
                                                    asset_root=self.asset_root, ocr=self.ocr)
            self.checkpoint.confirm(
                index, observation_id=_observation_id(package), evidence=evidence,
                receipt=receipt.model_dump(mode="json"),
                same_turn_known=(step.confirmation is None and pending is not None
                                 and pending["operation_id"] in self.same_turn_operations),
            )

    def _visual(self, package, template):
        identity = _observation_id(package)
        if self.visual_frame_id != identity:
            self.visual_frame_id = identity
            self.visual_matches.clear()
        if template not in self.visual_matches:
            self.visual_matches[template] = _match_visual(
                package, template, self.asset_root, parameters=self.values, ocr=self.ocr)
        return self.visual_matches[template]

    async def _notify(self, callback, *args):
        if callback is None:
            return None
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    async def _observe(self, deadline):
        self._check_cancel()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            async with asyncio.timeout(remaining):
                package = await self.transaction.observe_current(
                    deadline_ms=max(1, int(remaining * 1000)), source="script_current",
                )
        except (ObservationStageError, TimeoutError):
            return None
        self._check_cancel()
        await self._notify(self.on_observation, package)
        return package

    async def _wait_after(self, satisfied, deadline, initial):
        package = initial
        while True:
            self._check_cancel()
            if satisfied(package):
                return package, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return package, False
            await asyncio.sleep(min(self.poll_seconds, remaining))
            sample = await self._observe(deadline)
            if sample is not None:
                package = sample

    def _resolve_visual(self, before, template, semantic_candidates=()):
        result = self._visual(before, template)
        appearance = template.layout if isinstance(template, VisualObject) else template
        if result.status == "unavailable":
            raise ValueError(result.reason or "visual_observation_unavailable")
        if isinstance(template, VisualObject) and len(result.matches) > 1:
            raise ValueError("visual_object_ambiguous")
        candidates = []
        for match in (*result.matches, *result.near_matches):
            containing = [node for node in semantic_candidates if len(node.bounds) == 4
                          and node.bounds[0] <= match.x < node.bounds[2] and node.bounds[1] <= match.y < node.bounds[3]]
            if not semantic_candidates or (len(containing) == 1 and containing[0].states.get("enabled") is not False):
                candidates.append((match, containing[0] if containing else None))
        if not candidates or all(match.confidence < appearance.threshold for match, _ in candidates):
            raise ValueError("visual_target_unavailable" if not result.matches else "visual_target_conflicts_with_tree")
        candidates.sort(key=lambda item: item[0].confidence, reverse=True)
        match, node = candidates[0]
        if len(candidates) > 1:
            gap = match.confidence - candidates[1][0].confidence
            if gap <= 0 or gap < appearance.min_score_gap:
                raise ValueError("visual_target_ambiguous")
        target = _target_snapshot(node, package=before.ui.app_id) if node is not None else None
        return Action(type="tap_xy", x=match.x, y=match.y), target, True

    def _resolve_action(self, step, before):
        proposal = step.action
        if proposal.type == "launch":
            return Action(type="launch", app=self.script.app_id), None, False
        if not self._app_matches(before):
            raise ValueError("script_app_not_in_foreground")
        if not before.actionable:
            raise ValueError("before_observation_not_actionable")
        if proposal.type == "scroll":
            return Action(type="scroll", direction=proposal.direction), None, False
        if proposal.type == "back":
            return Action(type="back"), None, False
        if isinstance(proposal, VisualTapAction):
            if proposal.object is None:
                raise ValueError("visual_object_evidence_required")
            return self._resolve_visual(before, proposal.object)
        try:
            element = _unique_action_target(before, proposal.target, self.values)
        except ValueError as exc:
            if (not isinstance(proposal, TapAction) or (proposal.object is None and proposal.visual is None)
                    or str(exc) == "target_not_actionable"):
                raise
            candidates = ()
            if _tree_ready(before) and before.index_actionable:
                selector = proposal.target
                candidates = _matches(before, selector, self.values, actionable=True)
                if (not candidates or selector.relative_to is not None or selector.occurrence is not None
                        or (selector.expected_count is not None and len(candidates) != selector.expected_count)):
                    raise
            if proposal.object is None and not candidates:
                raise ValueError("visual_object_evidence_required") from exc
            return self._resolve_visual(before, proposal.object or proposal.visual, candidates)
        if isinstance(proposal, TapAction) and proposal.object is not None:
            return self._resolve_visual(before, proposal.object, (element,))
        target = _target_snapshot(element, package=before.ui.app_id)
        if proposal.type in {"type", "replace_text"}:
            evidence = before.interaction_state
            focused = evidence.focused_editable if evidence is not None else None
            if (target.password or focused is None or focused.password
                    or focused.index != element.index or focused.editability != "editable"):
                raise ValueError("target_not_current_focused_editable")
            return Action(type=proposal.type, text=self.values[proposal.parameter]), target, False
        if proposal.type == "long_press":
            unbound = Action(type="long_press", index=element.index, duration_ms=proposal.duration_ms)
            bound = resolve_long_press_index(unbound, before.ui)
        else:
            unbound = Action(type="tap", index=element.index)
            bound = resolve_tap_index(
                unbound, before.ui, native_node_click=getattr(self.driver, "supports_native_node_click", False) is True,
            )
        if bound == unbound and not bound._node_handle:
            raise ValueError("target_could_not_be_bound")
        return bound, target, False

    async def _run_step(self, index, step, attempt, *, retry_failure=None):
        deadline = time.monotonic() + step.timeout
        before = await self._observe(deadline)
        before_id = _observation_id(before)
        last_observation = before
        reason = "before_observation_unavailable"

        def receipt(reason, *, succeeded=False, execution="not_dispatched", after=None, native=None):
            return ScriptStepReceipt(step_id=step.id, attempt=attempt, reason=reason, succeeded=succeeded,
                                     execution=execution, before_observation_id=before_id,
                                     after_observation_id=_observation_id(after), receipt=native)

        if isinstance(step.action, VisualTapAction) and step.action.object is None:
            return receipt("visual_object_evidence_required"), before

        while True:
            if before_id is not None:
                last_observation = before
                reason = "before_condition_unsatisfied"
                if step.blocked_when and self._holds(step.blocked_when, before):
                    return receipt("blocked_condition_observed"), before
                if self._holds(step.before, before):
                    if retry_failure is not None and not self._can_retry(index, step, retry_failure, before):
                        return receipt("takeover_retry_condition_changed"), before
                    if step.action.type == "wait":
                        break
                    try:
                        action, target, visual_binding = self._resolve_action(step, before)
                        break
                    except ValueError as exc:
                        reason = str(exc)
                        if reason == "script_app_not_in_foreground":
                            return receipt(reason), before
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                before_id = _observation_id(last_observation)
                return receipt(reason), last_observation
            await asyncio.sleep(min(self.poll_seconds, remaining))
            before = await self._observe(deadline)
            before_id = _observation_id(before)

        deadline = time.monotonic() + step.timeout
        if step.action.type == "wait":
            after, satisfied = await self._wait_after(lambda package: self._step_confirmed(index, step, package),
                                                      deadline, before)
            return receipt("postcondition_met" if satisfied else "postcondition_timeout",
                           succeeded=satisfied, after=after), after

        self._check_cancel()
        await self._notify(self.before_dispatch)
        if visual_binding:
            before = await self._observe(deadline)
            before_id = _observation_id(before)
            if (before_id is None or not self._holds(step.before, before)
                    or (step.blocked_when and self._holds(step.blocked_when, before))
                    or (retry_failure is not None and not self._can_retry(index, step, retry_failure, before))):
                return receipt("visual_precondition_changed"), before
            try:
                action, target, _ = self._resolve_action(step, before)
            except ValueError as exc:
                return receipt(str(exc)), before
        self._check_cancel()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return receipt("step_deadline_before_dispatch"), before
        durable = current_execution.get()
        if durable is not None:
            durable.basis = before_id
        baseline = self._business_ids(step, before)
        if self.checkpoint is not None:
            self.checkpoint.bind_attempt(index, attempt, before_observation_id=before_id,
                                         baseline=baseline)
        try:
            begin_dispatch()
            async with asyncio.timeout(remaining):
                result, after = await self.transaction.act_and_observe(
                    action, before, deadline_ms=max(1, int(remaining * 1000)), target=target,
                )
        except (OSError, TimeoutError, InfrastructureInterrupted, ObservationStageError, ScriptDispatchBlocked):
            return receipt("dispatch_outcome_unknown", execution="unknown"), before
        finally:
            if self.checkpoint is not None:
                self.checkpoint.clear_attempt()

        if _observation_id(after) != before_id:
            await self._notify(self.on_observation, after)
        await self._notify(self.on_action, step, before, action, result, after)
        self._check_cancel()
        native = result.receipt
        if native is not None:
            native = native.model_copy(update={"effect_reason": ""})
        if _dispatch_unknown(result.detail):
            return receipt("dispatch_outcome_unknown", execution="unknown", after=after, native=native), after
        if (result.detail.get("device_dispatch") == "not_dispatched"
                or native is not None and native.effect_outcome == EffectOutcome.SUPPRESSED):
            return receipt("action_not_dispatched", after=after, native=native), after
        if native is None:
            return receipt("dispatch_receipt_missing", execution="unknown", after=after), after
        if not native.dispatch_succeeded or not result.success:
            return receipt("action_failed", execution="known", after=after, native=native), after
        post = after if native.observation_accepted and native.observation_id == _observation_id(after) else None
        if post is not None:
            self._accept_same_turn(index)
        after, satisfied = await self._wait_after(
            lambda package: self._step_confirmed(index, step, package, before_id=before_id, baseline=baseline),
            deadline, post,
        )
        return receipt("postcondition_met" if satisfied else "postcondition_timeout",
                       succeeded=satisfied, execution="known", after=after, native=native), after

    async def _final_holds(self, package):
        if package is None or not self._app_matches(package):
            return False
        if not self._holds(self.script.final, package):
            return False
        accepted = self.final_oracle is None or await self._notify(self.final_oracle, package) is True
        self._check_cancel()
        return accepted

    def _can_retry(self, index, step, failure, package):
        return (failure.attempt < 2 and failure.execution == "not_dispatched" and self._pending(index) is None
                and not (step.blocked_when and self._holds(step.blocked_when, package))
                and not self._holds(step.after, package) and self._holds(step.before, package))

    async def _allowed_takeover(self, index, step, next_step, failure, observation):
        allowed = {TakeoverDecision.STOP}
        if (_observation_id(observation) is None
                or (failure.execution == "unknown" and self._pending(index) is None)):
            return frozenset(allowed)
        if self._can_retry(index, step, failure, observation):
            allowed.add(TakeoverDecision.RETRY_CURRENT)
        if self._step_confirmed(index, step, observation, before_id=failure.before_observation_id):
            allowed.add(TakeoverDecision.CONTINUE_NEXT)
            if next_step is None and await self._final_holds(observation):
                allowed.add(TakeoverDecision.FINISH)
        return frozenset(allowed)

    async def _verify_pending(self, index, step, next_step, failure, deadline):
        if self._pending(index) is not None:
            sample = await self._observe(deadline)
            allowed = await self._allowed_takeover(index, step, next_step, failure, sample)
            for choice in (TakeoverDecision.FINISH, TakeoverDecision.CONTINUE_NEXT):
                if choice in allowed:
                    return choice, sample, ""
        return None

    async def _run_takeover(self, index, step, next_step, failure, observation):
        remaining = self.takeover_seconds - self.takeover_used
        if remaining <= 0:
            # An explicit resume still permits a fresh read-only check.
            verified = await self._verify_pending(index, step, next_step, failure,
                                                  time.monotonic() + step.timeout)
            if verified is not None:
                return verified
            return TakeoverDecision.STOP, None, "takeover_deadline_exceeded"
        self.assisted = True
        started = time.monotonic()
        deadline = started + remaining
        attempt_can_dispatch = False

        async def observe():
            sample = await self._observe(deadline)
            if attempt_can_dispatch:
                self._accept_same_turn(index)
            return sample

        async def resume_status():
            return await self._allowed_takeover(index, step, next_step, failure, await observe())

        def resolve_limited(proof):
            if (proof.status != "succeeded" or not proof.final_asserted or not proof.final_observation_id
                    or len(proof.steps) != 1 or not proof.steps[0].succeeded or proof.steps[0].receipt is None):
                raise ValueError("limited_repair_evidence_unavailable")
            self.checkpoint.resolve_limited(
                index, observation_id=proof.final_observation_id,
                evidence={"script_hash": proof.script_hash, "receipt": proof.steps[0].model_dump(mode="json")},
            )

        if self.checkpoint is not None:
            self.checkpoint.start_takeover()
        reason = "takeover_stopped"
        try:
            async with asyncio.timeout(remaining):
                verified = await self._verify_pending(index, step, next_step, failure, deadline)
                if verified is not None:
                    return verified
                for phase, callback in (("limited", self.takeover), ("formal", self.formal_takeover)):
                    if callback is None:
                        continue
                    if phase == "limited" and (failure.execution != "not_dispatched"
                                                or self._pending(index) is not None or failure.attempt >= 2):
                        continue
                    if phase == "formal" and failure.execution != "not_dispatched" and self.checkpoint is None:
                        continue
                    observation = await observe()
                    attempt_can_dispatch = self._pending(index) is None

                    def bind_observation(package, *, phase=phase):
                        if self._pending(index) is None:
                            self.checkpoint.bind_attempt(
                                index, failure.attempt, before_observation_id=_observation_id(package),
                                baseline=self._business_ids(step, package), phase=phase,
                            )

                    context = TakeoverContext(
                        self.script, step, next_step, self.values, failure, observation, observe, resume_status,
                        deadline=deadline,
                        resolve_limited=resolve_limited if self.checkpoint is not None and phase == "limited" else None,
                        bind_observation=bind_observation if self.checkpoint is not None else None,
                    )
                    if self.checkpoint is not None:
                        bind_observation(observation)
                    choice = TakeoverDecision.STOP
                    try:
                        choice = TakeoverDecision(await callback(context))
                    except ValueError:
                        reason = "invalid_takeover_decision"
                    except ScriptDispatchBlocked:
                        reason = "script_step_requires_result_verification"
                    finally:
                        if attempt_can_dispatch:
                            if phase == "formal" or choice in (TakeoverDecision.FINISH, TakeoverDecision.CONTINUE_NEXT):
                                self._accept_same_turn(index)
                            elif pending := self._pending(index):
                                self.same_turn_operations.discard(pending["operation_id"])
                        attempt_can_dispatch = False
                        if self.checkpoint is not None:
                            self.checkpoint.clear_attempt()
                    if choice is TakeoverDecision.STOP:
                        continue
                    observation = await observe()
                    allowed = await self._allowed_takeover(index, step, next_step, failure, observation)
                    if choice in allowed:
                        return choice, observation, ""
                    reason = "takeover_resume_condition_unsatisfied"
                # A delayed confirmation can still settle this attempt. Poll without device input.
                while self._pending(index) is not None:
                    verified = await self._verify_pending(index, step, next_step, failure, deadline)
                    if verified is not None:
                        return verified
                    await asyncio.sleep(self.poll_seconds)
                return TakeoverDecision.STOP, observation, reason
        except TimeoutError:
            return TakeoverDecision.STOP, None, "takeover_deadline_exceeded"
        finally:
            if self.checkpoint is None:
                self.takeover_used += time.monotonic() - started
            else:
                self.checkpoint.finish_takeover()
                self.takeover_used = self.checkpoint.takeover_used

    def _result(self, status, reason, *, final=None, final_asserted=False):
        return ScriptRunResult(
            script_hash=self.script_hash, script_id=self.script.script_id, revision=self.script.revision,
            status=status, reason=reason, assisted=self.assisted, steps=tuple(self.receipts),
            failed_step_id=self.failed_step_id, initial_observation_id=self.initial_id,
            final_observation_id=_observation_id(final), final_asserted=final_asserted,
        )

    async def run(self):
        try:
            start = self.checkpoint.cursor if self.checkpoint is not None else 0
            if start == 0 and self._pending(0) is None:
                initial_deadline = time.monotonic() + self.script.steps[0].timeout
                initial, ready = await self._wait_after(
                    lambda package: _observation_id(package) is not None and self._holds(self.script.initial, package),
                    initial_deadline, await self._observe(initial_deadline),
                )
                self.initial_id = _observation_id(initial)
                if not ready:
                    return self._result("failed", "initial_condition_unsatisfied")
            elif self.receipts:
                self.initial_id = self.receipts[0].before_observation_id
            for index in range(start, len(self.script.steps)):
                step = self.script.steps[index]
                next_step = self.script.steps[index + 1] if index + 1 < len(self.script.steps) else None
                pending = self._pending(index)
                if pending is None:
                    result, observation = await self._run_step(index, step, 1)
                else:
                    raw = (pending["result"] or {}).get("receipt")
                    result = ScriptStepReceipt(
                        step_id=step.id, attempt=pending["attempt"], succeeded=False, execution="unknown",
                        reason="pending_step_confirmation", before_observation_id=pending["before_observation_id"],
                        receipt=ActionReceipt.model_validate(raw) if raw else None,
                    )
                    observation = None
                while True:
                    self.receipts.append(result)
                    if result.succeeded:
                        self._complete(index, step, result, observation)
                        break
                    self.failed_step_id = step.id
                    pending = self._pending(index)
                    if pending is None and (result.execution != "not_dispatched" or
                                             (self.formal_takeover is None and
                                              (self.takeover is None or result.attempt >= 2))):
                        return self._result("failed", result.reason)
                    choice, verified, reason = await self._run_takeover(index, step, next_step, result, observation)
                    if choice == TakeoverDecision.STOP:
                        if self._pending(index) is not None:
                            return self._result("paused", "step_confirmation_required")
                        return self._result("failed", reason)
                    if choice == TakeoverDecision.RETRY_CURRENT:
                        result, observation = await self._run_step(index, step, result.attempt + 1, retry_failure=result)
                        continue
                    confirmed = result.model_copy(update={
                        "succeeded": True, "reason": "takeover_postcondition_met",
                        "after_observation_id": _observation_id(verified),
                    })
                    self._complete(index, step, confirmed, verified)
                    self.receipts.append(confirmed)
                    if choice == TakeoverDecision.FINISH:
                        return self._result("succeeded", "final_condition_met", final=verified, final_asserted=True)
                    break
            final = await self._observe(time.monotonic() + self.script.steps[-1].timeout)
            if not await self._final_holds(final):
                return self._result("failed", "final_condition_unsatisfied", final=final)
            return self._result("succeeded", "final_condition_met", final=final, final_asserted=True)
        except _ReplayCancelled:
            return self._result("cancelled", "cancel_requested")
        except TaskBudgetExhausted:
            if self.checkpoint is None:
                raise
            cursor = self.checkpoint.cursor
            if cursor < len(self.script.steps):
                self.failed_step_id = self.script.steps[cursor].id
                if self._pending(cursor) is not None:
                    return self._result("paused", "step_confirmation_required")
            return self._result("paused", "script_replay_budget_exhausted")


async def run_gui_script(
    script: GuiScript,
    *,
    driver,
    transaction=None,
    parameters: dict[str, str] | None = None,
    takeover: Callable[[TakeoverContext], Awaitable[TakeoverDecision]] | None = None,
    formal_takeover: Callable[[TakeoverContext], Awaitable[TakeoverDecision]] | None = None,
    checkpoint=None,
    cancelled: Callable[[], bool] | None = None,
    takeover_seconds: float = 30.0,
    poll_seconds: float = 0.15,
    on_observation=None,
    on_action=None,
    before_dispatch=None,
    final_oracle=None,
    asset_root: Path | None = None,
    ocr=None,
) -> ScriptRunResult:
    """Run under a caller-owned device lease. A takeover callback is an explicit opt-in."""
    script = GuiScript.model_validate(script.model_dump())
    values = script.bind_parameters(parameters or {})
    if not 0 < poll_seconds <= 2 or not 0 < takeover_seconds <= 600:
        raise ValueError("invalid script polling or takeover time limit")
    if transaction is None:
        transaction = ActionObservationTransaction(driver, ObservationBuilder())
    replay = _Replay(
        script, values, driver, transaction, cancelled=cancelled, takeover=takeover, takeover_seconds=takeover_seconds,
        formal_takeover=formal_takeover, checkpoint=checkpoint, poll_seconds=poll_seconds,
        on_observation=on_observation, on_action=on_action, before_dispatch=before_dispatch,
        final_oracle=final_oracle, asset_root=Path(asset_root) if asset_root is not None else None,
        ocr=ocr,
    )
    return await replay.run()
