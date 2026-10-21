"""Synchronous, revisable plans. Routing contains no task interpretation."""

import asyncio
import time
from functools import partial

from agent.orchestrator import Orchestrator, DEFAULT_MAX_STEPS
from agent.pause import TaskPauseRequested
from agent.revisable.store import TaskStore
from agent.tool_registry import stable_hash
from driver.observation_deadline import ObservationStageError
from perception.observation import ObservationPackage
from shared.llm_gateway import GatewayError
from shared.schemas import ExecutorDecisionKind, TaskStatus
from shared.hitl import HumanInputBroker, HumanInputSuspended
from shared.task_budgets import TaskBudgetExhausted
from shared.task_control import SteeringControl, TaskSteered, current_lease
from shared.steering import SteeringInbox


class PlanOrchestrator(Orchestrator):
    def decision_limit(self, state):
        task_limit = state.revisable.limits.executor_decisions
        if self.max_steps != DEFAULT_MAX_STEPS:
            limits = [value for value in (self.max_steps, task_limit) if value is not None]
            return min(limits) if limits else None
        return task_limit if task_limit is not None else self.max_steps

    def exhausted_budget(self, state):
        runtime = state.revisable
        if runtime.limits.expired():
            return "task_time"
        limit = self.decision_limit(state)
        if limit is not None and state.step_number >= limit:
            return "executor_decisions"
        limit = self._model_call_limit(state)
        if limit is not None and state.role_invocation_count >= limit:
            return "model_calls"
        if (runtime.budget_kind == "device_actions" and runtime.limits.device_actions is not None
                and runtime.execution_count + runtime.budget_required > runtime.limits.device_actions):
            return "device_actions"
        return None

    def _budget_fail(self, task_id, state, dimension, required=1):
        state.revisable.stop_reason = "budget_exhausted"
        state.revisable.budget_kind = dimension
        state.revisable.budget_required = required
        try:
            self._prepare_checkpoint(task_id, state)
        except Exception as exc:
            return self._fail(task_id, state, f"budget_checkpoint_invalid:{exc}")
        self.traces.write(task_id, kind="system", message="Task budget exhausted",
                          payload={"event": "budget_exhausted", "budget_kind": dimension})
        reason = {
            "executor_decisions": "step_limit_exhausted",
            "model_calls": "role_invocation_limit_exhausted",
            "device_actions": "task_device_action_limit_exhausted",
            "task_time": "task_deadline_exhausted",
            "prediction_rounds": "mobileworld_round_limit_exhausted",
        }[dimension]
        return self._fail(task_id, state, reason, resumable=dimension != "prediction_rounds")

    async def _invoke(self, task_id, state, role, make_call, control):
        if control is None:
            return await self._call_role(task_id, state, role, make_call(state, self._model_call_meter(task_id, state, role)))
        lease = control.acquire(SteeringInbox(self.db, task_id).revision())
        private = state.model_copy(deep=True)
        record = self._model_call_meter(task_id, state, role)

        def meter(kind, payload):
            lease.check()
            record(kind, payload)
            private.role_invocation_count = state.role_invocation_count

        async def run():
            token = current_lease.set(lease)
            try:
                return await self._call_role(task_id, private, role, make_call(private, meter))
            finally:
                current_lease.reset(token)

        invocation = asyncio.create_task(run())
        changed = asyncio.create_task(control.changed.wait())
        adopted = False
        try:
            await asyncio.wait((invocation, changed), return_when=asyncio.FIRST_COMPLETED)
            if control.changed.is_set() and not lease.dispatching:
                lease.revoked = True
                invocation.cancel()
                # Do not wait for an uncooperative provider. A revoked lease fences
                # its tools/records, and private state cannot overwrite the new run.
                def discarded(future):
                    try:
                        future.result()
                    except BaseException:
                        pass
                    try:
                        self.traces.write(task_id, kind="system", message="Obsolete invocation discarded",
                                          payload={"event": "invocation_superseded", "generation": lease.generation})
                    except Exception:
                        pass  # Backend shutdown may already have closed diagnostics.
                invocation.add_done_callback(discarded)
                raise TaskSteered()
            result = await invocation
            private.role_invocation_count = state.role_invocation_count
            state.__dict__.update(private.__dict__)
            adopted = True
            return result
        finally:
            if not adopted:
                self.db.append_agent_record(task_id, "obsolete_invocation", lease.identity,
                                            {"revision": lease.revision, "generation": lease.generation})
            changed.cancel()
            if not invocation.done() and not lease.revoked:
                lease.revoked = True
                invocation.cancel()
                invocation.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)

    def _cancel(self, task_id, state, *, mode, reason="cancelled_by_operator"):
        status = super()._cancel(task_id, state, mode=mode, reason=reason)
        if status == TaskStatus.CANCELLED and HumanInputBroker(self.db).config(task_id)["enabled"]:
            SteeringInbox(self.db, task_id).cancelled()
        return status

    def _remaining_budget(self, state):
        runtime = state.revisable
        limits = runtime.limits
        remaining = {
            "device_actions": None if limits.device_actions is None else max(0, limits.device_actions - runtime.execution_count),
            "executor_decisions": None if self.decision_limit(state) is None else max(0, self.decision_limit(state) - state.step_number),
            "model_calls": None if self._model_call_limit(state) is None else max(0, self._model_call_limit(state) - state.role_invocation_count),
            "seconds": None if limits.deadline_at is None else max(0, int(limits.deadline_at - time.time())),
        }
        if limits.prediction_rounds is not None:
            remaining["prediction_rounds"] = max(0, limits.prediction_rounds - runtime.prediction_round_count)
            remaining["prediction_round_accounting"] = (
                "Each submitted act consumes one round, including rejected actions. "
                "A final answer or terminal verdict consumes one round if submitted. "
                "It is not mandatory for tasks completed through saved device state: the environment "
                "scores that state when the round limit is reached. Any user-requested answer must "
                "still be submitted within the limit. "
                "Internal planning, observations and tool calls do not consume prediction rounds. "
                "No extra final-answer round is available after the limit."
            )
        return remaining

    def _model_call_meter(self, task_id, state, phase):
        record = super()._model_call_meter(task_id, state, phase)

        def check_and_record(kind, payload):
            if self.is_cancel_requested(task_id):
                raise asyncio.CancelledError
            if state.revisable.limits.expired():
                raise TaskBudgetExhausted("task_time")
            if self.is_pause_requested(task_id):
                raise TaskPauseRequested
            record(kind, payload)

        return check_and_record

    async def _run_loop(
        self,
        task_id,
        state,
        planner,
        reviewer,
        executor,
        *,
        provider_warm_task=None,
        max_device_actions=None,
        max_action_attempts=None,
    ):
        if provider_warm_task is not None:
            await asyncio.gather(provider_warm_task, return_exceptions=True)
        if self.is_pause_requested(task_id):
            return self._pause(task_id, state)
        await self._wake_and_unlock()
        # Resolve system scope before inspecting Planner-selected skill IDs.
        # Executor.act_once binds too late for the handoff below.
        await executor._session.bind_device_skills(executor.driver)
        store = TaskStore(self.db, self.artifacts, task_id)
        store.jev_status_context = self.settings.jev_status_context and self.settings.jev_mode == "enforce"
        store.budget_snapshot = partial(self._remaining_budget, state)
        store.human_provider_budget = lambda: (None if self._model_call_limit(state) is None else
                                               self._model_call_limit(state) - state.role_invocation_count)
        executor.store = store
        executor_driver = executor.driver
        enabled = HumanInputBroker(self.db).config(task_id)["enabled"]
        control = self.steering_controls.setdefault(task_id, SteeringControl()) if enabled else None
        inbox = SteeringInbox(self.db, task_id) if enabled else None
        executor.pause_requested = lambda: self.is_pause_requested(task_id)
        executor.cancel_requested = lambda: (
            self.is_cancel_requested(task_id)
        )
        runtime = state.revisable
        carried = None
        # A resumed Executor also prepares history before its handoff capture.
        refresh_before_executor = runtime.next_role == "executor"
        device_actions = 0
        action_attempts = 0
        while True:
            runtime = state.revisable
            if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                return cancelled
            if self.is_pause_requested(task_id):
                return self._pause(task_id, state)
            if (runtime.limits.prediction_rounds is not None
                    and runtime.prediction_round_count >= runtime.limits.prediction_rounds):
                return TaskStatus.RUNNING
            if max_device_actions is not None and device_actions >= max_device_actions:
                return TaskStatus.RUNNING
            if max_action_attempts is not None and action_attempts >= max_action_attempts:
                return TaskStatus.RUNNING
            if (dimension := self.exhausted_budget(state)) is not None:
                return self._budget_fail(task_id, state, dimension)
            if inbox is not None and inbox.pending():
                runtime.next_role = "planner"
                runtime.handoff = "replan"
                runtime.delivered_context = {}
                carried = None
            try:
                package = carried or await self._observe(will_send_image=True)
                carried = None
                self._persist_observation(package)
                role = runtime.next_role
                if role in {"planner", "reviewer"}:
                    decision_role = planner if role == "planner" else reviewer
                    (decision, refs), _ = await self._invoke(
                        task_id, state, role,
                        lambda call_state, meter: partial(
                            decision_role.decide,
                            call_state,
                            package,
                            store=store,
                            task_id=task_id,
                            model_call_meter=meter,
                        ), control,
                    )
                    runtime = state.revisable
                    if runtime.limits.expired():
                        return self._budget_fail(task_id, state, "task_time")
                    if inbox is not None:
                        if control.lease.revision != inbox.revision():
                            raise TaskSteered()
                        if role == "planner":
                            inbox.applied(control.lease.revision, decision.reason)
                    # A reply may arrive after cancellation/deadline while the
                    HumanInputBroker(self.db).complete(task_id)
                    # provider was in flight. Never commit its terminal verdict.
                    if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                        return cancelled
                    self.traces.write(
                        task_id,
                        kind=f"{role}_decision",
                        message=decision.reason,
                        step_seq=state.step_number,
                        payload={"decision": decision.model_dump(), **refs},
                    )
                    if role == "planner":
                        runtime.plan_reason = decision.reason
                        runtime.feedback = ""
                        if decision.decision == "continue":
                            runtime.next_role = "executor"
                            refresh_before_executor = True
                        elif decision.decision == "execute":
                            runtime.plan = decision.plan
                            runtime.revision += 1
                            runtime.stage_start_step = state.step_number
                            runtime.next_role = "executor"
                            refresh_before_executor = True
                        elif decision.decision == "review":
                            if runtime.last_review_signature == self._review_signature(store, state, package):
                                return self._fail(task_id, state, "review_without_new_evidence_or_plan")
                            runtime.feedback = decision.reason
                            runtime.handoff = "review"
                            runtime.next_role = "reviewer"
                        elif decision.decision == "complete":
                            if self.settings.agent_architecture == "plan_executor":
                                runtime.completion_reason = decision.reason
                                return self._succeed(
                                    task_id, state, decision.reason, terminal_role="planner"
                                )
                            runtime.handoff = "finish"
                            runtime.next_role = "reviewer"
                        else:
                            return self._fail(
                                task_id, state, "planner_inconclusive:" + decision.reason
                            )
                    else:
                        runtime.feedback = decision.reason
                        runtime.last_review_signature = self._review_signature(
                            store, state, package
                        )
                        if decision.decision == "complete":
                            runtime.completion_reason = decision.reason
                            return self._succeed(task_id, state, decision.reason)
                        if decision.decision == "inconclusive":
                            return self._fail(
                                task_id, state, "review_inconclusive:" + decision.reason
                            )
                        runtime.handoff = "replan" if decision.decision == "replan" else "review"
                        runtime.next_role = (
                            "planner"
                            if decision.decision == "replan" or runtime.stage is None
                            else "executor"
                        )
                    carried = package
                    self._persist(task_id, state)
                    continue
                stage = runtime.stage
                state.current_subgoal = (
                    stage.goal if stage else "Verify the original instruction is satisfied."
                )
                state.active_target_app = stage.target_app if stage else ""
                state.active_workflow_ids = [
                    pack.id for pack in executor._session.validate_stage_skills(
                        state.active_target_app, stage.skill_ids if stage else []
                    ) if pack.kind == "workflow"
                ]
                (step, result, ui, mode, refs), _ = await self._invoke(
                    task_id, state, "executor",
                    lambda call_state, meter: partial(
                        executor.act_once,
                        state.current_subgoal,
                        package,
                        task_id=task_id,
                        state=call_state,
                        model_call_meter=meter,
                        observe_after_handoff=(
                            partial(self._observe, will_send_image=True)
                            if refresh_before_executor else None
                        ),
                    ), control,
                )
                runtime = state.revisable
                refresh_before_executor = False
                HumanInputBroker(self.db).complete(task_id)
                package = refs.pop("active_package")
                store.observe(package, state)
                post = refs.pop("post_action_package", None)
                compound_intermediate = refs.pop(
                    "compound_intermediate_package", None,
                )
                compound_evidence = refs.pop(
                    "compound_model_evidence_package", None,
                )
                if (
                    isinstance(compound_intermediate, ObservationPackage)
                    and compound_intermediate.accepted
                ):
                    self._persist_observation(compound_intermediate)
                    store.observe(compound_intermediate, state)
                    refs.update(
                        compound_intermediate_observation_id=(
                            compound_intermediate.observation_id
                        ),
                        compound_intermediate_tree_ref=compound_intermediate.tree_ref,
                        compound_intermediate_annotated_ref=compound_intermediate.som_ref,
                    )
                # A delay returns the pre-action package, not new evidence.
                # Leave carried empty so the next decision acquires a fresh frame.
                if step.action is not None and step.action.type == "sleep":
                    post = None
                if isinstance(post, ObservationPackage) and post.accepted:
                    self._persist_observation(post)
                    store.observe(post, state)
                    refs.update(
                        post_observation_id=post.observation_id,
                        post_tree_ref=post.tree_ref,
                        post_annotated_ref=post.som_ref,
                    )
                    carried = post
                elif step.decision != ExecutorDecisionKind.ACT:
                    carried = package
                executor.record_result(
                    step,
                    result,
                    state,
                    compound_evidence=(
                        compound_evidence
                        if isinstance(compound_evidence, ObservationPackage)
                        else None
                    ),
                )
                if step.decision == ExecutorDecisionKind.ACT:
                    runtime.execution_count += result.detail.get("device_action_units", 1)
                    if runtime.limits.prediction_rounds is not None:
                        runtime.prediction_round_count += 1
                directive = executor.directive
                runtime.feedback = step.summary if directive != "act" else ""
                active_stage_id = runtime.stage_id
                if directive != "act":
                    runtime.handoff = directive
                if directive == "advance":
                    runtime.complete_stage(executor.completed_stage_id)
                    runtime.next_role = "planner"
                elif directive == "replan":
                    runtime.next_role = "planner"
                elif directive in {"finish", "review"}:
                    if directive == "finish" and self.settings.agent_architecture == "plan_executor":
                        runtime.next_role = "planner"
                    elif runtime.last_review_signature == self._review_signature(
                        store, state, package
                    ):
                        return self._fail(task_id, state, "review_without_new_evidence_or_plan")
                    else:
                        runtime.next_role = "reviewer"
                report = self._build_step_report(ui, mode, step, result, refs, package)
                report.subgoal_at_tick = state.current_subgoal
                completed_step = state.step_number
                state.step_number += 1
                self.db.add_step_and_update_state(
                    task_id, active_stage_id, completed_step, report.model_dump(), state
                )
                self._trace_executor_step(task_id, state, step, result, ui, mode, refs, package)
                if step.decision == ExecutorDecisionKind.ACT:
                    device_actions += result.detail.get("device_action_units", 1)
                    action_attempts += 1
            except TaskSteered:
                # No incomplete cognitive state is adopted. New role objects prevent
                # an obsolete invocation's finally block from touching the new one.
                planner, reviewer, executor = self.planner_factory(), self.reviewer_factory(), self.executor_factory()
                driver = decision_role.driver if role in {"planner", "reviewer"} else executor_driver
                for new_role in (planner, reviewer, executor):
                    new_role.driver, new_role.traces = driver, self.traces
                if state.manager_model:
                    planner.set_model(state.manager_model)
                    reviewer.set_model(state.manager_model)
                if state.executor_model:
                    executor.set_model(state.executor_model)
                executor.store = store
                executor.pause_requested = lambda: self.is_pause_requested(task_id)
                executor.cancel_requested = lambda: self.is_cancel_requested(task_id)
                await executor._session.bind_device_skills(executor.driver)
                carried = None
                refresh_before_executor = True
                continue
            except TaskBudgetExhausted as exc:
                return self._budget_fail(task_id, state, exc.dimension, exc.required)
            except asyncio.CancelledError:
                raise
            except HumanInputSuspended as exc:
                self.db.update_task(task_id, status=TaskStatus.WAITING_FOR_INPUT, state=state)
                self.traces.write(task_id, kind="system", message="Waiting for user input",
                                  payload={"event": "human_input_pending", "interaction_id": exc.interaction_id,
                                           "role": role})
                return TaskStatus.WAITING_FOR_INPUT
            except GatewayError as exc:
                signature = stable_hash((package.observation_id, runtime.execution_count))
                if (
                    role != "executor"
                    or exc.category != "budget"
                    or runtime.last_executor_stall == signature
                ):
                    return self._fail(task_id, state, f"plan_runtime:{type(exc).__name__}:{exc}")
                runtime.last_executor_stall = signature
                runtime.delivered_context = {}
                runtime.next_role = "planner"
                runtime.handoff = "replan"
                runtime.feedback = (
                    "Executor exhausted its decision protocol without an accepted decision. "
                    "No action was dispatched by that invocation. Inspect saved facts and the "
                    "current stage for an unsupported premise or a concrete recovery step. "
                    f"Protocol detail: {exc}"
                )
                self.traces.write(
                    task_id,
                    kind="system",
                    message=runtime.feedback,
                    payload={"event": "executor_protocol_handoff"},
                )
                carried = package
                self._persist(task_id, state)
            except Exception as exc:  # noqa: BLE001 — task boundary persists failures for the console
                if isinstance(exc, ObservationStageError):
                    self.traces.write(
                        task_id, kind="system", message="Observation capture failed",
                        payload={"event": "observation_capture_failed", **exc.diagnostics()},
                    )
                return self._fail(task_id, state, f"plan_runtime:{type(exc).__name__}:{exc}")

    @staticmethod
    def _review_signature(store, state, package):
        return stable_hash(
            {
                "observation_id": package.observation_id,
                "plan_revision": state.revisable.revision,
                "execution_count": state.revisable.execution_count,
                "notes": [(row["note_key"], row["version"]) for row in store.note_index()],
            }
        )
