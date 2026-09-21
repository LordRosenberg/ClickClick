"""Synchronous, revisable plans. Routing contains no task interpretation."""

import asyncio
import time
from functools import partial

from agent.orchestrator import Orchestrator
from agent.revisable.store import TaskStore
from agent.tool_registry import stable_hash
from driver.observation_deadline import ObservationStageError
from perception.observation import ObservationPackage
from shared.llm_gateway import GatewayError
from shared.schemas import ExecutorDecisionKind, TaskStatus


class PlanOrchestrator(Orchestrator):
    def _cancel(self, task_id, state, *, mode, reason="cancelled_by_operator"):
        if state.revisable.limits.expired():
            reason = "task_deadline_exhausted"
        return super()._cancel(task_id, state, mode=mode, reason=reason)

    def _soft_cancel_if_requested(self, task_id, state):
        if state.revisable.limits.expired():
            return self._cancel(task_id, state, mode="soft")
        return super()._soft_cancel_if_requested(task_id, state)

    def _remaining_budget(self, state):
        runtime = state.revisable
        limits = runtime.limits
        return {
            "device_actions": None if limits.device_actions is None else max(0, limits.device_actions - runtime.execution_count),
            "executor_decisions": max(0, self.max_steps - state.step_number),
            "model_calls": max(0, self.max_role_invocations - state.role_invocation_count),
            "seconds": None if limits.deadline_at is None else max(0, int(limits.deadline_at - time.time())),
        }

    def _model_call_meter(self, task_id, state, phase):
        record = super()._model_call_meter(task_id, state, phase)

        def check_and_record(kind, payload):
            if self.is_cancel_requested(task_id) or state.revisable.limits.expired():
                raise asyncio.CancelledError
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
    ):
        if provider_warm_task is not None:
            await asyncio.gather(provider_warm_task, return_exceptions=True)
        await self._wake_and_unlock()
        # Resolve system scope before inspecting Planner-selected skill IDs.
        # Executor.act_once binds too late for the handoff below.
        await executor._session.bind_device_skills(executor.driver)
        store = TaskStore(self.db, self.artifacts, task_id)
        store.budget_snapshot = partial(self._remaining_budget, state)
        executor.store = store
        executor.cancel_requested = lambda: (
            self.is_cancel_requested(task_id) or state.revisable.limits.expired()
        )
        runtime = state.revisable
        carried = None
        # A resumed Executor also prepares history before its handoff capture.
        refresh_before_executor = runtime.next_role == "executor"
        device_actions = 0
        while True:
            if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                return cancelled
            if max_device_actions is not None and device_actions >= max_device_actions:
                return TaskStatus.RUNNING
            if state.step_number >= self.max_steps:
                return self._fail(task_id, state, "step_limit_exhausted")
            try:
                package = carried or await self._observe(will_send_image=True)
                carried = None
                self._persist_observation(package)
                role = runtime.next_role
                meter = self._model_call_meter(task_id, state, role)
                if role in {"planner", "reviewer"}:
                    decision_role = planner if role == "planner" else reviewer
                    (decision, refs), _ = await self._call_role(
                        task_id,
                        state,
                        role,
                        partial(
                            decision_role.decide,
                            state,
                            package,
                            store=store,
                            task_id=task_id,
                            model_call_meter=meter,
                        ),
                    )
                    # A reply may arrive after cancellation/deadline while the
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
                        if decision.decision == "execute":
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
                (step, result, ui, mode, refs), _ = await self._call_role(
                    task_id,
                    state,
                    "executor",
                    partial(
                        executor.act_once,
                        state.current_subgoal,
                        package,
                        task_id=task_id,
                        state=state,
                        model_call_meter=meter,
                        observe_after_handoff=(
                            partial(self._observe, will_send_image=True)
                            if refresh_before_executor else None
                        ),
                    ),
                )
                refresh_before_executor = False
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
            except asyncio.CancelledError:
                raise
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
