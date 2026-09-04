"""Reviewer -> Planner -> Executor closed-loop orchestration.

The Harness owns deterministic observation, routing, persistence, counts,
exact evidence bindings, device dispatch, cancellation, and safety limits.
Planner and Reviewer own every semantic planning and adjudication decision.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from agent.action_observation import ActionObservationTransaction
from agent.executor import Executor, _normalize_act
from agent.planner import Planner
from agent.reviewer import Reviewer
from agent.task_memory import (
    apply_reviewer_progress,
    ensure_memory,
    record_attempt,
)
from agent.traces import TraceWriter
from driver.observation_deadline import ObservationStageError
from perception.normalizer import normalize_a11y_tree
from perception.observation import ObservationBuilder, ObservationPackage
from shared.artifacts import ArtifactStore
from shared.cancel_registry import CancelRegistry
from shared.db import Database
from shared.llm_gateway import GatewayError
from shared.protocol import DeviceDriver
from shared.schemas import (
    ActiveCompletionContract,
    ActiveTaskCompletionContract,
    ActionResultUnsuccessful,
    AgentState,
    DriverDispatchFailed,
    ExecutorDecisionKind,
    ExecutorReplanRequested,
    ExecutorStep,
    LogLevel,
    ObservationMode,
    PlannerDecision,
    PlannerMode,
    PostActionObservationMissing,
    ReviewerDecision,
    ReviewerVerdict,
    RuntimeBudgetSnapshot,
    StepReport,
    TaskStatus,
    VisualGroundingRejected,
)

if TYPE_CHECKING:
    from driver.pool import DriverPool


_current_driver: contextvars.ContextVar[DeviceDriver | None] = contextvars.ContextVar(
    "orch_current_driver", default=None,
)

DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_ROLE_INVOCATIONS = 200
DEFAULT_ROLE_CALL_RETRY_N = 1
DEFAULT_EXECUTOR_GROUNDING_RETRY_N = 1

PlannerFactory = Callable[[], Planner]
ReviewerFactory = Callable[[], Reviewer]
ExecutorFactory = Callable[[], Executor]
RoleResult = TypeVar("RoleResult")


class RoleInvocationLimitExceeded(RuntimeError):
    """The task-wide model-call runaway guard was exhausted."""


_EXECUTOR_BOUNDARY_CAUSES = frozenset({
    "executor_review_requested",
    "executor_replan_requested",
    "executor_grounding_rejected",
    "driver_action_failed",
    "post_action_observation_missing",
    "action_result_unsuccessful",
})


def _recovered_executor_report(state: AgentState) -> str:
    """Recover the latest canonical Executor report after a process restart."""
    if state.recovery_state.boundary_cause not in _EXECUTOR_BOUNDARY_CAUSES:
        return ""
    lineage_id = state.recovery_state.lineage_id
    for event in reversed(ensure_memory(state).events):
        if event.kind == "attempt" and event.lineage_id == lineage_id:
            return (event.model_intent or event.summary or "").strip()
    return ""


class Orchestrator:
    """Drive the focused three-role loop with minimal deterministic Harness."""

    def __init__(
        self,
        db: Database,
        traces: TraceWriter,
        planner_factory: PlannerFactory,
        reviewer_factory: ReviewerFactory,
        executor_factory: ExecutorFactory,
        driver: DeviceDriver | None = None,
        *,
        driver_pool: "DriverPool | None" = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_role_invocations: int = DEFAULT_MAX_ROLE_INVOCATIONS,
        role_call_retry_n: int = DEFAULT_ROLE_CALL_RETRY_N,
        executor_grounding_retry_n: int = DEFAULT_EXECUTOR_GROUNDING_RETRY_N,
        artifacts: ArtifactStore | None = None,
        settings: Any = None,
    ) -> None:
        self.db = db
        self.traces = traces
        self.planner_factory = planner_factory
        self.reviewer_factory = reviewer_factory
        self.executor_factory = executor_factory
        self.driver = driver
        self.driver_pool = driver_pool
        self.max_steps = max(1, int(max_steps))
        self.max_role_invocations = max(1, int(max_role_invocations))
        self.role_call_retry_n = max(0, int(role_call_retry_n))
        self.executor_grounding_retry_n = max(
            0, int(executor_grounding_retry_n),
        )
        self.artifacts = artifacts
        self.settings = settings
        self.observation_builder = ObservationBuilder()
        self.cancel_registry = CancelRegistry()

    def request_cancel(self, task_id: str) -> None:
        self.cancel_registry.request(task_id)

    def is_cancel_requested(self, task_id: str) -> bool:
        return self.cancel_registry.is_requested(task_id)

    def _resolve_driver(self, serial: str | None) -> DeviceDriver | None:
        if self.driver_pool is None:
            return self.driver
        if not serial:
            raise ValueError("task missing device_serial for driver pool routing")
        return self.driver_pool.get(serial)

    async def start_task(
        self,
        instruction: str,
        *,
        device_serial: str | None = None,
    ) -> str:
        state = AgentState(instruction=instruction)
        record = self.db.create_task(
            instruction,
            state,
            device_serial=device_serial,
        )
        await self.run_task(record.id)
        return record.id

    async def run_task(self, task_id: str) -> TaskStatus:
        task = self.db.get_task(task_id)
        if task is None or task.state is None:
            raise KeyError(task_id)
        state = task.state
        serial = task.device_serial
        lock = None
        if self.driver_pool is not None and serial:
            owner = self.db.busy_serials().get(serial)
            if owner is not None and owner != task_id:
                return self._fail(
                    task_id, state, f"device_busy:{serial}:owned_by:{owner}",
                )
            lock = self.driver_pool.lock_for(serial)

        async def body() -> TaskStatus:
            return await self._run_task_body(task_id, state, serial)

        try:
            if lock is None:
                return await body()
            async with lock:
                return await body()
        except asyncio.CancelledError:
            return self._cancel(task_id, state, mode="hard")

    async def _run_task_body(
        self,
        task_id: str,
        state: AgentState,
        serial: str | None,
    ) -> TaskStatus:
        try:
            driver = self._resolve_driver(serial)
        except Exception as exc:  # noqa: BLE001
            self.db.update_task(task_id, status=TaskStatus.RUNNING)
            return self._fail(task_id, state, f"device_unavailable:{exc}")

        token = _current_driver.set(driver)
        self.db.update_task(task_id, status=TaskStatus.RUNNING)
        self.traces.write(
            task_id,
            kind="loop_tick",
            message=f"task started serial={serial or '—'}",
        )
        planner = self.planner_factory()
        reviewer = self.reviewer_factory()
        executor = self.executor_factory()
        if state.manager_model:
            planner.set_model(state.manager_model)
            reviewer.set_model(state.manager_model)
        if state.executor_model:
            executor.set_model(state.executor_model)
        for role in (planner, reviewer, executor):
            role.traces = self.traces
            if driver is not None:
                role.driver = driver

        warmer = getattr(driver, "warm_observation_provider", None)
        warm_task = (
            asyncio.create_task(warmer(), name=f"provider-warm-{task_id}")
            if callable(warmer)
            else None
        )
        try:
            scope_failure = await self._ensure_task_scope(
                task_id, state, reviewer,
            )
            if scope_failure is not None:
                return scope_failure
            if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                return cancelled
            return await self._run_loop(
                task_id,
                state,
                planner,
                reviewer,
                executor,
                provider_warm_task=warm_task,
            )
        except asyncio.CancelledError:
            return self._cancel(task_id, state, mode="hard")
        finally:
            if warm_task is not None:
                if not warm_task.done():
                    warm_task.cancel()
                await asyncio.gather(warm_task, return_exceptions=True)
            closer = getattr(driver, "close_observation_provider", None)
            if callable(closer):
                try:
                    await closer()
                except Exception as exc:  # noqa: BLE001
                    self.traces.write(
                        task_id,
                        kind="system",
                        level=LogLevel.WARN,
                        message=f"observation provider close warning: {exc}",
                    )
            for role in (planner, reviewer, executor):
                try:
                    await role.aclose()
                except Exception:  # noqa: BLE001
                    pass
            _current_driver.reset(token)
            self.cancel_registry.clear(task_id)

    async def _ensure_task_scope(
        self,
        task_id: str,
        state: AgentState,
        reviewer: Reviewer,
    ) -> TaskStatus | None:
        if state.task_completion_contract is not None:
            return None
        if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
            return cancelled
        try:
            (body, refs), role_elapsed_ms = await self._call_role(
                task_id,
                state,
                "reviewer_scope",
                lambda: reviewer.author_task_scope(
                    state,
                    task_id=task_id,
                    model_call_meter=self._model_call_meter(
                        task_id, state, "reviewer_scope",
                    ),
                ),
            )
            refs["role_elapsed_ms"] = role_elapsed_ms
        except RoleInvocationLimitExceeded as exc:
            return self._fail(task_id, state, str(exc))
        except GatewayError as exc:
            return self._fail(
                task_id, state, f"reviewer_scope_gateway_{exc.category}:{exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(
                task_id,
                state,
                f"reviewer_scope_internal_error:{type(exc).__name__}:{exc}",
            )
        state.task_completion_contract = ActiveTaskCompletionContract(
            contract_id=uuid.uuid4().hex,
            revision=1,
            created_step=state.step_number,
            body=body,
        )
        state.next_role = "planner"
        self._persist(task_id, state)
        self.traces.write(
            task_id,
            kind="task_scope",
            step_seq=state.step_number,
            message="Reviewer authored immutable task scope",
            payload={
                "role": "reviewer",
                "phase": "task_scope",
                "contract": body.model_dump(mode="json"),
                **self._trace_refs(refs),
            },
        )
        return None

    async def _call_role(
        self,
        task_id: str,
        state: AgentState,
        role: str,
        call: Callable[[], Awaitable[RoleResult]],
    ) -> tuple[RoleResult, int]:
        started = time.monotonic()
        for attempt in range(self.role_call_retry_n + 1):
            try:
                result = await call()
                return result, int((time.monotonic() - started) * 1000)
            except GatewayError as exc:
                retry = (
                    exc.category in {"transient", "malformed"}
                    and attempt < self.role_call_retry_n
                )
                self.traces.write(
                    task_id,
                    kind="system",
                    level=LogLevel.WARN if retry else LogLevel.ERROR,
                    step_seq=state.step_number,
                    message=f"{role} gateway error [{exc.category}]: {exc}",
                    payload={"role": role, "retry": attempt + 1, "will_retry": retry},
                )
                if not retry:
                    raise
        raise AssertionError("unreachable role retry state")

    def _model_call_meter(
        self,
        task_id: str,
        state: AgentState,
        phase: str,
    ) -> Callable[[str, dict[str, Any]], None]:
        """Persist one count immediately before each actual provider request."""
        metric_role = "reviewer" if phase == "reviewer_scope" else phase

        def record(_kind: str, payload: dict[str, Any]) -> None:
            if state.role_invocation_count >= self.max_role_invocations:
                raise RoleInvocationLimitExceeded(
                    "role_invocation_limit_exhausted:"
                    f"{state.role_invocation_count}/{self.max_role_invocations}"
                )
            state.role_invocation_count += 1
            self._persist(task_id, state)
            self.traces.write(
                task_id,
                kind="system",
                step_seq=state.step_number,
                message="role_invocation_started",
                payload={
                    "phase": phase,
                    "role": metric_role,
                    "task_role_invocation_ordinal": state.role_invocation_count,
                    "round": payload.get("round"),
                    "request_kind": payload.get("kind"),
                },
            )

        return record

    async def _run_loop(
        self,
        task_id: str,
        state: AgentState,
        planner: Planner,
        reviewer: Reviewer,
        executor: Executor,
        *,
        provider_warm_task: asyncio.Task[Any] | None = None,
    ) -> TaskStatus:
        if state.task_completion_contract is None:
            return self._fail(task_id, state, "missing_task_scope_invariant")
        if provider_warm_task is not None:
            try:
                await provider_warm_task
            except Exception as exc:  # noqa: BLE001
                self.traces.write(
                    task_id,
                    kind="system",
                    level=LogLevel.WARN,
                    message=f"scrcpy warm degraded to fallback: {exc}",
                )
        await self._wake_and_unlock()

        carried_package: ObservationPackage | None = None
        boundary_reason = state.recovery_state.boundary_cause
        executor_report = _recovered_executor_report(state)
        terminal_review = bool(state.recovery_state.awaiting_review)
        grounding_rejections = 0

        while True:
            if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                return cancelled
            try:
                package = carried_package or await self._observe(will_send_image=True)
                carried_package = None
            except (ObservationStageError, TimeoutError) as exc:
                return self._fail(task_id, state, f"observation_unavailable:{exc}")
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    task_id,
                    state,
                    f"observation_internal_error:{type(exc).__name__}:{exc}",
                )

            if state.next_role == "reviewer":
                reviewer_input_package = package
                reviewer_boundary_reason = boundary_reason
                try:
                    (decision, refs, observation_refs), role_elapsed_ms = await self._call_role(
                        task_id,
                        state,
                        "reviewer",
                        lambda: reviewer.decide(
                            state,
                            package,
                            executor_report=executor_report,
                            boundary_reason=boundary_reason,
                            terminal_review=terminal_review,
                            review_requirement_ref=(
                                state.recovery_state.review_requirement_ref
                            ),
                            task_id=task_id,
                            model_call_meter=self._model_call_meter(
                                task_id, state, "reviewer",
                            ),
                        ),
                    )
                    refs["role_elapsed_ms"] = role_elapsed_ms
                except RoleInvocationLimitExceeded as exc:
                    return self._fail(task_id, state, str(exc))
                except GatewayError as exc:
                    return self._fail(
                        task_id, state, f"reviewer_gateway_{exc.category}:{exc}",
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._fail(
                        task_id,
                        state,
                        f"reviewer_internal_error:{type(exc).__name__}:{exc}",
                    )
                try:
                    package = self._take_active_package(
                        refs,
                        expected_observation_id=str(
                            observation_refs.get("observation_id") or ""
                        ),
                    )
                    self._validate_reviewer_binding(decision, refs)
                except ValueError as exc:
                    return self._fail(task_id, state, f"reviewer_binding_error:{exc}")
                reviewed_state = state.model_copy(deep=True)
                try:
                    with self.db.transaction():
                        progress_delta = apply_reviewer_progress(
                            reviewed_state, decision, reviewed_state.step_number,
                        )
                        if decision.verdict == ReviewerVerdict.DONE:
                            self._finish_review_boundary(reviewed_state, decision)
                            reviewed_state.current_subgoal = ""
                            reviewed_state.plan = []
                            reviewed_state.active_completion_contract = None
                            reviewed_state.active_timeline_lineage_ids = []
                        elif decision.verdict == ReviewerVerdict.BLOCKED:
                            self._finish_review_boundary(reviewed_state, decision)
                        else:
                            self._finish_review_boundary(reviewed_state, decision)
                            reviewed_state.next_role = (
                                "executor"
                                if decision.verdict == ReviewerVerdict.RETRY
                                else "planner"
                            )
                            boundary_reason = decision.reason
                            reviewed_state.recovery_state.boundary_cause = boundary_reason
                            reviewed_state.recovery_state.last_reviewer_verdict = decision.verdict
                            reviewed_state.recovery_state.last_reviewer_reason = decision.reason
                            executor_report = ""
                            terminal_review = False
                            carried_package = (
                                None
                                if (
                                    reviewer_boundary_reason
                                    == "post_action_observation_missing"
                                    and package.observation_id
                                    == reviewer_input_package.observation_id
                                )
                                else package
                            )
                        self._persist(task_id, reviewed_state)
                        self._trace_decision(
                            task_id,
                            reviewed_state,
                            "reviewer_decision",
                            decision,
                            refs,
                            observation_refs,
                            extra=progress_delta,
                        )
                        if decision.verdict == ReviewerVerdict.DONE:
                            return self._succeed(
                                task_id, reviewed_state, decision.user_facing_answer(),
                            )
                        if decision.verdict == ReviewerVerdict.BLOCKED:
                            return self._fail(task_id, reviewed_state, decision.reason)
                except ValueError as exc:
                    return self._fail(
                        task_id, state, f"reviewer_progress_error:{exc}",
                    )
                state = reviewed_state
                continue

            if state.next_role == "planner":
                try:
                    (decision, refs, observation_refs), role_elapsed_ms = await self._call_role(
                        task_id,
                        state,
                        "planner",
                        lambda: planner.decide(
                            state,
                            package,
                            deviation=boundary_reason,
                            task_id=task_id,
                            model_call_meter=self._model_call_meter(
                                task_id, state, "planner",
                            ),
                        ),
                    )
                    refs["role_elapsed_ms"] = role_elapsed_ms
                except RoleInvocationLimitExceeded as exc:
                    return self._fail(task_id, state, str(exc))
                except GatewayError as exc:
                    return self._fail(
                        task_id, state, f"planner_gateway_{exc.category}:{exc}",
                    )
                except Exception as exc:  # noqa: BLE001
                    return self._fail(
                        task_id,
                        state,
                        f"planner_internal_error:{type(exc).__name__}:{exc}",
                    )
                try:
                    package = self._take_active_package(
                        refs,
                        expected_observation_id=str(
                            observation_refs.get("observation_id") or ""
                        ),
                    )
                except ValueError as exc:
                    return self._fail(task_id, state, f"planner_binding_error:{exc}")
                self._trace_decision(
                    task_id,
                    state,
                    "planner_decision",
                    decision,
                    refs,
                    observation_refs,
                )
                if decision.mode == PlannerMode.REVIEW:
                    # A Planner review is task-level adjudication, not a retry of
                    # the previously attempted subgoal. Keep canonical events,
                    # but close the stale active binding mechanically.
                    state.current_subgoal = ""
                    state.active_completion_contract = None
                    state.active_timeline_lineage_ids = []
                    state.next_role = "reviewer"
                    boundary_reason = "planner_review_requested"
                    terminal_review = True
                    state.recovery_state.boundary_cause = boundary_reason
                    state.recovery_state.awaiting_review = True
                    state.recovery_state.review_requirement_ref = (
                        decision.review_requirement_ref
                    )
                    carried_package = package
                    self._persist(task_id, state)
                    continue

                self._install_planner_decision(state, decision)
                state.recovery_state.review_requirement_ref = ""
                boundary_reason = ""
                executor_report = ""
                terminal_review = False
                state.next_role = "executor"
                carried_package = package
                self._persist(task_id, state)
                continue

            if state.next_role != "executor":
                return self._fail(task_id, state, f"invalid_next_role:{state.next_role}")
            if state.step_number >= self.max_steps:
                return self._fail(task_id, state, "max_steps_exhausted")
            if not state.current_subgoal or state.active_completion_contract is None:
                return self._fail(task_id, state, "executor_without_active_subgoal")

            if hasattr(executor, "configure_skills"):
                executor.configure_skills(frozen_skill_dirs=state.frozen_skill_dirs)
            state.runtime_budget = self._budget_snapshot(state)
            try:
                (step, result, ui, mode, refs), role_elapsed_ms = await self._call_role(
                    task_id,
                    state,
                    "executor",
                    lambda: executor.act_once(
                        state.current_subgoal,
                        package,
                        prior_result="",
                        task_id=task_id,
                        state=state,
                        model_call_meter=self._model_call_meter(
                            task_id, state, "executor",
                        ),
                    ),
                )
                refs["role_elapsed_ms"] = role_elapsed_ms
            except RoleInvocationLimitExceeded as exc:
                return self._fail(task_id, state, str(exc))
            except ObservationStageError as exc:
                return self._fail(
                    task_id,
                    state,
                    f"observation_unavailable:{exc.stage}:{exc.reason}",
                )
            except GatewayError as exc:
                return self._fail(
                    task_id, state, f"executor_gateway_{exc.category}:{exc}",
                )
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    task_id,
                    state,
                    f"executor_internal_error:{type(exc).__name__}:{exc}",
                )

            try:
                package = self._take_active_package(
                    refs,
                    expected_observation_id=str(refs.get("observation_id") or ""),
                )
            except ValueError as exc:
                return self._fail(task_id, state, f"executor_binding_error:{exc}")
            post_package = refs.pop("post_action_package", None)
            receipt = getattr(result, "receipt", None)
            if (
                isinstance(post_package, ObservationPackage)
                and bool(getattr(receipt, "observation_accepted", False))
            ):
                carried_package = post_package
                self._persist_observation(post_package)
                refs["post_observation_id"] = post_package.observation_id
                refs["post_tree_ref"] = post_package.tree_ref
                refs["post_annotated_ref"] = post_package.som_ref
            elif isinstance(post_package, ObservationPackage):
                carried_package = package
            elif step.decision in {
                ExecutorDecisionKind.REQUEST_REVIEW,
                ExecutorDecisionKind.REQUEST_REPLAN,
            }:
                carried_package = package
            step.summary = _normalize_act(step.summary)
            step.subgoal_at_tick = state.current_subgoal
            record_attempt(
                ensure_memory(state),
                step,
                subgoal=state.current_subgoal,
                step=state.step_number,
                lineage_id=state.recovery_state.lineage_id,
                refs=[
                    value for value in refs.values()
                    if isinstance(value, str) and value.strip()
                ],
            )
            dispatch_succeeded = bool(
                getattr(receipt, "dispatch_succeeded", False)
            )
            accepted_post_evidence = (
                dispatch_succeeded
                and isinstance(post_package, ObservationPackage)
                and bool(getattr(receipt, "observation_accepted", False))
            )
            if accepted_post_evidence:
                state.recovery_state.last_reviewer_verdict = None
                state.recovery_state.last_reviewer_reason = ""
            completed_step_number = state.step_number
            completed_subgoal = state.current_subgoal
            step_report = self._build_step_report(
                ui, mode, step, result, refs, package,
            )
            state.step_number += 1

            boundary_reason, terminal_review = self._executor_boundary(
                state,
                step,
                result,
                grounding_rejections=grounding_rejections,
            )
            if boundary_reason == "grounding_retry":
                grounding_rejections += 1
                boundary_reason = ""
                state.next_role = "executor"
            elif boundary_reason:
                grounding_rejections = 0
                executor_report = step.summary or step.result
                state.recovery_state.awaiting_review = terminal_review
                state.recovery_state.boundary_cause = boundary_reason
                state.next_role = "reviewer"
            else:
                grounding_rejections = 0
                state.next_role = "executor"
            self.db.add_step_and_update_state(
                task_id,
                completed_subgoal or f"step-{completed_step_number}",
                completed_step_number,
                step_report.model_dump(),
                state,
            )
            self._trace_executor_step(
                task_id, state, step, result, ui, mode, refs, package,
            )

    def _install_planner_decision(
        self,
        state: AgentState,
        decision: PlannerDecision,
    ) -> None:
        if (
            decision.mode != PlannerMode.EXECUTE
            or decision.completion_contract is None
            or not decision.next_subgoal.strip()
        ):
            raise ValueError("Planner execute decision is incomplete")
        prior_lineages = list(state.active_timeline_lineage_ids)
        prior_lineage = state.recovery_state.lineage_id
        if prior_lineages and prior_lineage not in prior_lineages:
            prior_lineages.append(prior_lineage)
        lineage_id = uuid.uuid4().hex
        generation = max(1, int(state.recovery_state.boundary_generation or 0) + 1)
        last_verdict = state.recovery_state.last_reviewer_verdict
        last_reason = state.recovery_state.last_reviewer_reason
        state.current_subgoal = decision.next_subgoal.strip()
        state.plan = list(decision.plan)
        state.active_completion_contract = ActiveCompletionContract(
            contract_id=uuid.uuid4().hex,
            lineage_id=lineage_id,
            boundary_generation=generation,
            created_step=state.step_number,
            target_requirement_ref=decision.target_requirement_ref,
            body=decision.completion_contract,
        )
        state.recovery_state = state.recovery_state.__class__(
            lineage_id=lineage_id,
            boundary_generation=generation,
            last_reviewer_verdict=last_verdict,
            last_reviewer_reason=last_reason,
        )
        state.active_timeline_lineage_ids = [*prior_lineages, lineage_id]

    @staticmethod
    def _finish_review_boundary(
        state: AgentState,
        decision: ReviewerDecision,
    ) -> None:
        if decision.verdict == ReviewerVerdict.ACCEPT:
            state.active_timeline_lineage_ids = []
            state.active_completion_contract = None
            state.current_subgoal = ""
        state.recovery_state.awaiting_review = False
        state.recovery_state.boundary_cause = ""
        state.recovery_state.boundary_review = None
        state.recovery_state.review_requirement_ref = ""
        state.recovery_state.last_reviewer_verdict = None
        state.recovery_state.last_reviewer_reason = ""

    def _executor_boundary(
        self,
        state: AgentState,
        step: ExecutorStep,
        result: Any,
        *,
        grounding_rejections: int,
    ) -> tuple[str, bool]:
        action = step.action
        if step.decision == ExecutorDecisionKind.REQUEST_REVIEW:
            return "executor_review_requested", False
        if step.decision == ExecutorDecisionKind.REQUEST_REPLAN:
            state.recovery_state.boundary_review = ExecutorReplanRequested(
                detail=step.summary or "executor requested replanning",
            )
            return "executor_replan_requested", False
        receipt = getattr(result, "receipt", None)
        outcome = str(getattr(getattr(receipt, "effect_outcome", None), "value", ""))
        if receipt is not None:
            if outcome == "suppressed":
                if grounding_rejections < self.executor_grounding_retry_n:
                    return "grounding_retry", False
                state.recovery_state.boundary_review = VisualGroundingRejected(
                    detail=str(result.message or ""),
                )
                return "executor_grounding_rejected", False
            if not receipt.dispatch_succeeded or outcome == "failed":
                state.recovery_state.boundary_review = DriverDispatchFailed(
                    detail=str(result.message or ""),
                )
                return "driver_action_failed", False
            if not receipt.observation_accepted:
                state.recovery_state.boundary_review = PostActionObservationMissing(
                    detail=str(receipt.effect_reason or result.message or ""),
                )
                return "post_action_observation_missing", False
            if outcome == "timeout":
                state.recovery_state.boundary_review = ActionResultUnsuccessful(
                    detail=str(receipt.effect_reason or result.message or ""),
                )
                return "action_result_unsuccessful", False
        if not result.success:
            state.recovery_state.boundary_review = ActionResultUnsuccessful(
                detail=str(
                    getattr(receipt, "effect_reason", "") if receipt else result.message
                ),
            )
            return "action_result_unsuccessful", False
        return "", False
    def _budget_snapshot(
        self,
        state: AgentState,
    ) -> RuntimeBudgetSnapshot:
        return RuntimeBudgetSnapshot(
            remaining_steps=max(0, self.max_steps - state.step_number),
        )

    @staticmethod
    def _take_active_package(
        refs: dict[str, Any],
        *,
        expected_observation_id: str,
    ) -> ObservationPackage:
        package = refs.pop("active_package", None)
        if not isinstance(package, ObservationPackage):
            raise ValueError("active observation package is missing")
        if not expected_observation_id:
            raise ValueError("active observation id is missing")
        if package.observation_id != expected_observation_id:
            raise ValueError(
                "active observation id mismatch:"
                f"{package.observation_id}!={expected_observation_id}"
            )
        return package

    @staticmethod
    def _validate_reviewer_binding(
        decision: ReviewerDecision,
        refs: dict[str, Any],
    ) -> None:
        expected = str(refs.get("reviewer_packet_digest") or "")
        if not expected:
            raise ValueError("Reviewer packet digest is missing")
        if decision.packet_digest != expected:
            raise ValueError("Reviewer packet digest mismatch")

    async def _wake_and_unlock(self) -> None:
        driver = _current_driver.get() or self.driver
        fn = getattr(driver, "wake_and_unlock", None)
        if callable(fn):
            try:
                await fn()
            except Exception:  # noqa: BLE001
                pass

    async def _observe(self, *, will_send_image: bool = True) -> ObservationPackage:
        driver = _current_driver.get() or self.driver
        if driver is None:
            ui = normalize_a11y_tree({"class": "Root", "children": []})
            return ObservationPackage(
                ui=ui,
                mode=ObservationMode.TREE_ONLY,
                text_for_llm="",
                image_for_llm=None,
                annotated_png=None,
                gap_reasons=[],
            )
        transaction = ActionObservationTransaction(driver, self.observation_builder)
        package = await transaction.observe_current(
            source="orchestrator_current",
            attach_image=will_send_image,
        )
        self._persist_observation(package)
        if not package.accepted:
            capture = package.capture_meta or {}
            raise ObservationStageError(
                str(capture.get("error_stage") or "acceptance"),
                package.acceptance_reason or "observation_not_accepted",
                timed_out=bool(capture.get("timed_out")),
                fallback_edges=list(capture.get("fallback_edges") or []),
                provider_attempts=list(capture.get("provider_attempts") or []),
                cancelled_tasks=list(capture.get("cancelled_capture_tasks") or []),
            )
        return package

    def _persist_observation(self, package: ObservationPackage) -> None:
        if self.artifacts is None:
            return
        if package.annotated_png is not None and package.som_ref is None:
            package.som_ref = self.artifacts.save_bytes(
                "som", package.annotated_png, suffix=".png",
            )
        if package.tree_ref is None:
            package.tree_ref = self.artifacts.save_json("trees", {
                "app_id": package.ui.app_id,
                "activity": package.ui.activity,
                "filter_tier": package.ui.filter_tier,
                "text_for_llm": package.text_for_llm,
                "elements_count": len(package.ui.elements),
                "capture": package.capture_meta,
                "transaction_id": package.transaction_id,
                "accepted": package.accepted,
                "acceptance_reason": package.acceptance_reason,
                "coordinate_actionable": package.actionable,
                "index_actionable": package.index_actionable,
            })

    def _trace_decision(
        self,
        task_id: str,
        state: AgentState,
        kind: str,
        decision: PlannerDecision | ReviewerDecision,
        refs: dict[str, Any],
        observation_refs: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.traces.write(
            task_id,
            kind=kind,
            step_seq=state.step_number,
            message=(
                f"mode={decision.mode.value}"
                if isinstance(decision, PlannerDecision)
                else f"verdict={decision.verdict.value}"
            ),
            payload={
                "role": (
                    "planner" if isinstance(decision, PlannerDecision) else "reviewer"
                ),
                "decision": decision.model_dump(mode="json"),
                **self._trace_refs(refs),
                **observation_refs,
                **(extra or {}),
            },
        )

    @staticmethod
    def _trace_refs(refs: dict[str, Any]) -> dict[str, Any]:
        return {
            key: refs.get(key)
            for key in (
                "llm_input_ref",
                "llm_output_ref",
                "tool_calls",
                "agent_rounds",
                "active_skills",
                "delivered_evidence_digest",
                "reviewer_packet_digest",
                "role_elapsed_ms",
            )
            if refs.get(key) not in (None, "", [])
        }

    def _trace_executor_step(
        self,
        task_id: str,
        state: AgentState,
        step: ExecutorStep,
        result: Any,
        ui: Any,
        mode: ObservationMode,
        refs: dict[str, Any],
        package: ObservationPackage,
    ) -> None:
        self.traces.write(
            task_id,
            kind="executor_tick",
            step_seq=state.step_number,
            message=f"{step.decision.value} -> {result.message}",
            payload={
                "schema_version": 3,
                "role": "executor",
                "decision": step.decision.value,
                "action": (
                    step.action.model_dump(mode="json") if step.action else None
                ),
                "action_pipeline": (
                    step.action_pipeline.model_dump(mode="json")
                    if step.action_pipeline else None
                ),
                "action_result": {
                    "success": bool(result.success),
                    "message": result.message,
                    "detail": result.detail,
                    "receipt": (
                        result.receipt.model_dump(mode="json")
                        if getattr(result, "receipt", None) is not None
                        else None
                    ),
                },
                "summary": step.summary,
                "subgoal_at_tick": step.subgoal_at_tick,
                "basis_observation_id": step.basis_observation_id,
                "evidence_refs": list(step.evidence_refs),
                "observation_id": package.observation_id,
                "observation_mode": mode.value,
                "app_id": getattr(ui, "app_id", ""),
                "som_ref": package.som_ref,
                "tree_ref": package.tree_ref,
                "gap_reasons": list(package.gap_reasons),
                "runtime_budget": state.runtime_budget.model_dump(mode="json"),
                **self._trace_refs(refs),
            },
        )

    @staticmethod
    def _build_step_report(
        ui: Any,
        mode: ObservationMode,
        step: ExecutorStep,
        result: Any,
        refs: dict[str, Any],
        package: ObservationPackage,
    ) -> StepReport:
        return StepReport(
            action=step.action,
            executor_decision=step.decision,
            action_pipeline=step.action_pipeline,
            action_receipt=step.action_receipt,
            summary=step.summary,
            observation_digest=ui.page_summary or ui.app_id,
            observation_mode=mode,
            subgoal_at_tick=step.subgoal_at_tick,
            success=bool(result.success),
            reason=result.message,
            tree_ref=package.tree_ref,
            annotated_ref=package.som_ref,
            llm_input_ref=refs.get("llm_input_ref"),
            llm_output_ref=refs.get("llm_output_ref"),
            estimated_tokens=ui.estimated_tokens,
            observation_id=package.observation_id,
            post_observation_id=str(refs.get("post_observation_id") or ""),
            post_tree_ref=refs.get("post_tree_ref"),
            post_annotated_ref=refs.get("post_annotated_ref"),
            basis_observation_id=step.basis_observation_id,
            evidence_refs=list(step.evidence_refs),
        )

    def _persist(self, task_id: str, state: AgentState) -> None:
        self.db.update_task(
            task_id,
            state=state,
            current_node_id=state.current_subgoal or None,
        )

    def _succeed(self, task_id: str, state: AgentState, reason: str) -> TaskStatus:
        self.db.update_task(
            task_id,
            status=TaskStatus.SUCCEEDED,
            failure_reason=None,
            state=state,
            current_node_id=state.current_subgoal or None,
        )
        self.traces.write(
            task_id,
            kind="loop_tick",
            message=f"task succeeded: {reason}",
            payload={"terminal_role": "reviewer"},
        )
        self.cancel_registry.clear(task_id)
        self._schedule_skill_learner(task_id)
        return TaskStatus.SUCCEEDED

    def _fail(self, task_id: str, state: AgentState, reason: str) -> TaskStatus:
        self.db.update_task(
            task_id,
            status=TaskStatus.FAILED,
            failure_reason=reason,
            state=state,
            current_node_id=state.current_subgoal or None,
        )
        self.traces.write(
            task_id,
            kind="loop_tick",
            level=LogLevel.ERROR,
            message=f"task failed: {reason}",
        )
        self.cancel_registry.clear(task_id)
        self._schedule_skill_learner(task_id)
        return TaskStatus.FAILED

    def _schedule_skill_learner(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if task is None or not getattr(task, "skill_learn", False):
            return
        from agent.skills.learner import run_skill_learner

        async def run() -> None:
            try:
                result = await run_skill_learner(task, settings=self.settings)
                self.traces.write(
                    task_id,
                    kind="skill",
                    message="skill_learner",
                    payload=(
                        result if isinstance(result, dict) else {"result": str(result)}
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self.traces.write(
                    task_id,
                    kind="skill",
                    level=LogLevel.WARN,
                    message=f"skill_learner_error: {exc}",
                )

        asyncio.get_running_loop().create_task(run())

    def _soft_cancel_if_requested(
        self,
        task_id: str,
        state: AgentState,
    ) -> TaskStatus | None:
        if not self.cancel_registry.is_requested(task_id):
            return None
        return self._cancel(task_id, state, mode="soft")

    def _cancel(
        self,
        task_id: str,
        state: AgentState,
        *,
        mode: str,
        reason: str = "cancelled_by_operator",
    ) -> TaskStatus:
        task = self.db.get_task(task_id)
        if task is not None and task.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            self.cancel_registry.clear(task_id)
            return task.status
        self.db.update_task(
            task_id,
            status=TaskStatus.CANCELLED,
            failure_reason=reason,
            state=state,
            current_node_id=state.current_subgoal or None,
        )
        self.traces.write(
            task_id,
            kind="loop_tick",
            level=LogLevel.WARN,
            message=f"task_cancelled mode={mode}",
            payload={"mode": mode, "reason": reason},
        )
        self.cancel_registry.clear(task_id)
        return TaskStatus.CANCELLED
