"""Shared lifecycle for the plan-driven runtime.

The Harness owns deterministic observation, routing, persistence, counts,
exact evidence bindings, device dispatch, cancellation, and safety limits.
Planner and Reviewer own every semantic planning and adjudication decision.
"""

from __future__ import annotations

import asyncio
import contextvars
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeVar

from agent.action_observation import ActionObservationTransaction
from agent.executor import Executor
from agent.traces import TraceWriter
from driver.observation_deadline import ObservationStageError
from perception.normalizer import normalize_a11y_tree
from perception.observation import ObservationBuilder, ObservationPackage
from shared.artifacts import ArtifactStore
from shared.cancel_registry import CancelRegistry
from shared.db import Database
from shared.llm_gateway import GatewayError
from shared.protocol import DeviceDriver
from shared.schemas import AgentState, ExecutorStep, LogLevel, ObservationMode, StepReport, TaskStatus

if TYPE_CHECKING:
    from driver.pool import DriverPool


_current_driver: contextvars.ContextVar[DeviceDriver | None] = contextvars.ContextVar(
    "orch_current_driver", default=None,
)

DEFAULT_MAX_STEPS = 50
DEFAULT_MAX_ROLE_INVOCATIONS = 200
DEFAULT_ROLE_CALL_RETRY_N = 1
DEFAULT_EXECUTOR_GROUNDING_RETRY_N = 1

PlannerFactory = Callable[[], Any]
ReviewerFactory = Callable[[], Any]
ExecutorFactory = Callable[[], Executor]
RoleResult = TypeVar("RoleResult")


class RoleInvocationLimitExceeded(RuntimeError):
    """The task-wide model-call runaway guard was exhausted."""


class Orchestrator:
    """Shared device lifecycle, persistence, cancellation and trace transport."""

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
        max_steps: int | None = DEFAULT_MAX_STEPS,
        max_role_invocations: int | None = DEFAULT_MAX_ROLE_INVOCATIONS,
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
        self.max_steps = None if max_steps is None else max(1, int(max_steps))
        self.max_role_invocations = None if max_role_invocations is None else max(1, int(max_role_invocations))
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

    async def run_task(
        self,
        task_id: str,
        *,
        max_device_actions: int | None = None,
        max_action_attempts: int | None = None,
    ) -> TaskStatus:
        """Run or resume a task.

        ``max_device_actions`` is an integration boundary for evaluators
        whose agent API advances one externally-counted step at a time.  The
        normal ClickClick entrypoints leave it unset and still run to a
        terminal state. A positive value permits any number of cognitive or
        non-action boundary decisions but returns ``RUNNING`` before another
        Executor ``act`` could be dispatched.

        ``max_action_attempts`` instead counts every submitted ``act``, including
        rejected attempts that dispatch zero device actions. MobileWorld uses
        this boundary to implement one external prediction round.
        """
        if max_device_actions is not None:
            max_device_actions = max(1, int(max_device_actions))
        if max_action_attempts is not None:
            max_action_attempts = max(1, int(max_action_attempts))
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
            return await self._run_task_body(
                task_id,
                state,
                serial,
                max_device_actions=max_device_actions,
                max_action_attempts=max_action_attempts,
            )

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
        *,
        max_device_actions: int | None = None,
        max_action_attempts: int | None = None,
    ) -> TaskStatus:
        try:
            # Driver construction may sync-connect ADB; keep it off the loop.
            driver = await asyncio.to_thread(self._resolve_driver, serial)
        except Exception as exc:  # noqa: BLE001
            self.db.update_task(task_id, status=TaskStatus.RUNNING)
            return self._fail(task_id, state, f"device_unavailable:{exc}")

        self.db.update_task(task_id, status=TaskStatus.RUNNING)
        self.traces.write(
            task_id,
            kind="loop_tick",
            message=f"task started serial={serial or '—'}",
        )
        initializer = getattr(driver, "reconcile_environment", None)
        if callable(initializer):
            try:
                environment = await initializer()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return self._fail(
                    task_id, state, f"device_environment_reconciliation_failed:{exc}"
                )
            environment_status = (
                str(environment.get("status") or "unknown")
                if isinstance(environment, dict) else "invalid_response"
            )
            if (
                self.driver_pool is not None
                and serial
                and isinstance(environment, dict)
            ):
                self.driver_pool.record_environment_result(serial, environment)
            self.traces.write(
                task_id,
                kind="system",
                level=(
                    LogLevel.ERROR
                    if environment_status == "failed" else
                    LogLevel.WARN
                    if environment_status in {"degraded", "operator_action_required"} else
                    LogLevel.INFO
                ),
                message=f"automatic device environment reconciliation: {environment_status}",
            )
            if environment_status == "failed":
                return self._fail(
                    task_id,
                    state,
                    "device_environment_reconciliation_failed:initialization_failed",
                )
        date_reader = getattr(driver, "current_device_date", None)
        if callable(date_reader) and not state.current_device_date:
            try:
                state.current_device_date = str(await date_reader())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- optional model context
                self.traces.write(
                    task_id,
                    kind="system",
                    level=LogLevel.WARN,
                    message=f"device date unavailable: {type(exc).__name__}: {exc}",
                )
            else:
                self.db.update_task(task_id, state=state)
        token = _current_driver.set(driver)
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

        warm_task: asyncio.Task[Any] | None = None
        try:
            session_starter = getattr(driver, "begin_task_session", None)
            if callable(session_starter):
                session = await session_starter(task_id)
                session_status = (
                    str(session.get("status") or "")
                    if isinstance(session, dict) else "failed"
                )
                if session_status == "failed":
                    reason = (
                        str(session.get("reason") or "unknown")
                        if isinstance(session, dict) else "invalid response"
                    )
                    return self._fail(
                        task_id, state, f"screen_bright_lease_unavailable:{reason}"
                    )
                self.traces.write(
                    task_id,
                    kind="system",
                    message=f"task screen-bright lease: {session_status or 'active'}",
                )
            warmer = getattr(driver, "warm_observation_provider", None)
            warm_task = (
                asyncio.create_task(warmer(), name=f"provider-warm-{task_id}")
                if callable(warmer)
                else None
            )
            if (cancelled := self._soft_cancel_if_requested(task_id, state)) is not None:
                return cancelled
            return await self._run_loop(
                task_id,
                state,
                planner,
                reviewer,
                executor,
                provider_warm_task=warm_task,
                max_device_actions=max_device_actions,
                max_action_attempts=max_action_attempts,
            )
        except asyncio.CancelledError:
            return self._cancel(task_id, state, mode="hard")
        finally:
            if warm_task is not None:
                if not warm_task.done():
                    warm_task.cancel()
                await asyncio.gather(warm_task, return_exceptions=True)
            session_ender = getattr(driver, "end_task_session", None)
            if callable(session_ender):
                release_task = asyncio.create_task(
                    session_ender(task_id), name=f"screen-bright-release-{task_id}"
                )
                try:
                    release = await asyncio.wait_for(
                        asyncio.shield(release_task), timeout=5.0,
                    )
                    release_status = (
                        str(release.get("status") or "")
                        if isinstance(release, dict) else "invalid_response"
                    )
                    self.traces.write(
                        task_id,
                        kind="system",
                        level=(
                            LogLevel.WARN
                            if release_status == "release_deferred_to_ttl"
                            else LogLevel.INFO
                        ),
                        message=f"task screen-bright lease cleanup: {release_status}",
                    )
                except Exception as exc:  # noqa: BLE001
                    self.traces.write(
                        task_id,
                        kind="system",
                        level=LogLevel.WARN,
                        message=(
                            "task screen-bright lease cleanup deferred to TTL: "
                            f"{str(exc)[:160]}"
                        ),
                    )
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
        def record(_kind: str, payload: dict[str, Any]) -> None:
            if self.max_role_invocations is not None and state.role_invocation_count >= self.max_role_invocations:
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
                    "role": phase,
                    "task_role_invocation_ordinal": state.role_invocation_count,
                    "round": payload.get("round"),
                    "request_kind": payload.get("kind"),
                },
            )

        return record


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
                "runtime_budget": self._remaining_budget(state),
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

    def _succeed(self, task_id: str, state: AgentState, reason: str,
                 *, terminal_role: str = "reviewer") -> TaskStatus:
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
            payload={"terminal_role": terminal_role},
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
        if mode == "hard" and task is not None and task.state is not None:
            # Reviewer reconciliation replaces the loop's state with a copy.
            # Outer cancellation handlers may still hold the pre-review object;
            # only the latest committed checkpoint can preserve completed work.
            state = task.state
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
