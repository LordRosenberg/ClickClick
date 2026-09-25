"""Short planning/review sessions and one task-long Executor dialogue."""

import asyncio
import json
from pathlib import Path

from agent.decision_role import DecisionRoleRunner, build_observation_messages
from agent.executor import Executor, _compound_history_messages, _model_submission_pipeline
from agent.revisable.dialogue import restore_dialogue
from agent.revisable.scope import skill_app
from agent.revisable.session import ExecutionSession, terminal_registry
from agent.revisable.tools import ReadResult, register_memory_tools
from agent.session import AgentSession
from agent.tool_registry import AgentToolResult
from shared.revisable import PlannerDecision, Review

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def prompt(role):
    return "\n\n".join(
        (PROMPTS / name).read_text(encoding="utf-8")
        for name in (f"revisable_{role}.md", "revisable_common.md")
    )


class PlanDecisionRole(DecisionRoleRunner):
    async def decide(self, state, package, *, store, task_id, model_call_meter):
        # Skill bodies are restored from current app/plan; no previous dialogue.
        self.session = AgentSession(
            self.role,
            self.model,
            settings=self.settings,
            max_total_rounds=5 if self.role == "reviewer" else 8,
        )
        await self.session.bind_device_skills(self.driver)
        self.prepare_lifecycle(state, package, task_id=task_id)
        if state.revisable.stage is not None:
            stage = state.revisable.stage
            target = await skill_app(stage.target_app, self.driver, strict=False)
            self.session.set_stage_skills(target, stage.skill_ids)
        image, _ = self.prepare_observation(package)
        store.observe(package, state)
        schema = PlannerDecision if self.role == "planner" else Review

        async def submit(args, ctx):
            decision = schema.model_validate(args)
            from agent.revisable.recall import resolve_source
            for source in decision.source_refs:
                resolve_source(store, source)
            if isinstance(decision, PlannerDecision) and decision.plan:
                stage = decision.plan.current_stage
                stage.target_app = await skill_app(stage.target_app, self.driver)
                packs = self.session.validate_stage_skills(stage.target_app, stage.skill_ids)
                core = self.session.library.app_core(stage.target_app)
                required = ([core] if core else []) + packs
                delivered = ctx.state.get("delivered_skill_text", "")
                missing = []
                for pack in required:
                    content = self.session.skill_message(pack, label="stage_guidance")["content"]
                    # Labels vary by source; ID, version and exact role body must agree.
                    signature = content[content.index("skill:"):]
                    if signature not in delivered:
                        missing.append(content)
                if missing:
                    return ReadResult(
                        summary="Read the supplied stage guidance, then resubmit your decision. Adjust the goal only if this guidance requires it.",
                        data={"stage_guidance": missing},
                    )
            questions = {row["note_key"] for row in store.unresolved_notes()}
            resolved = set(decision.resolved_questions)
            known_notes = {row["key"] for row in store.records("note")}
            if resolved and (decision.decision != "complete" or not decision.source_refs or not resolved <= known_notes):
                raise ValueError("resolved_questions is for complete only: copy known note_keys, cite source_refs and explain the resolution in reason")
            if decision.decision == "complete" and questions - resolved:
                choices = "execute, review or inconclusive" if self.role == "planner" else "execute, replan or inconclusive"
                raise ValueError("Recorded material questions remain unresolved. If cited evidence settles them, list their note_keys in resolved_questions and explain why in reason. Otherwise choose " + choices + ".")
            return AgentToolResult(terminal_value=decision)

        registry = terminal_registry(
            self.session,
            schema,
            f"submit_{self.role}_decision",
            "Choose a current goal, request independent review, or judge the original task complete/inconclusive."
            if self.role == "planner"
            else "Submit whether the original task is complete, needs execution/replanning, or is inconclusive.",
            submit,
        )
        register_memory_tools(registry, self.role, store, state)
        self.session.set_stable_system(prompt(self.role))

        def event_sink(kind, payload):
            if self.traces:
                self.traces.write(
                    task_id, kind=kind, step_seq=state.step_number, message=kind, payload=payload
                )

        result = await self.session.run(
            [],
            final_messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        store.context(state, package.observation_id, self.role), ensure_ascii=False
                    ),
                },
                *build_observation_messages(package.text_for_llm, image),
            ],
            tool_registry=registry,
            model_call_meter=model_call_meter,
            reserve_terminal_round=True,
            event_sink=event_sink,
            artifacts=self.artifacts,
        )
        refs = self._invocation_refs(result, result.decision)
        store.put(
            self.role,
            str(len(store.records(self.role))),
            {
                "decision": result.decision.model_dump(),
                "step": state.step_number,
                "observation_id": package.observation_id,
                **refs,
            },
        )
        return result.decision, refs


class PlanExecutor(Executor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session = ExecutionSession("executor", self.model, settings=self.settings)
        self.store = None
        self.directive = ""
        self.completed_stage_id = None
        self._restored_history = None

    async def act_once(self, subgoal, package, prior_result="", *, task_id="", state=None,
                       model_call_meter=None, observe_after_handoff=None):
        if state is not None:
            stage = state.revisable.stage
            target = await skill_app(stage.target_app, self.driver, strict=False) if stage else ""
            if stage and target:
                stage.target_app = target
            # Normalize before the parent captures this value in its observation
            # refresh closure, including when resuming a historical display name.
            state.active_target_app = target
        def event_sink(kind, payload):
            if self.traces is not None and task_id:
                self.traces.write(task_id, kind=kind, step_seq=state.step_number,
                                  message=kind, payload=payload)

        # Compaction can itself take a model round. Finish it before acquiring
        # the handoff frame and before the parent binds pixels/tree/coordinates.
        self._restored_history = await restore_dialogue(
            self.store, state, model=self.model, settings=self.settings,
            meter=model_call_meter, event_sink=event_sink,
        )
        try:
            if self.cancel_requested():
                raise asyncio.CancelledError
            if observe_after_handoff is not None:
                previous_id = package.observation_id
                package = await observe_after_handoff()
                event_sink("system", {
                    "event": "executor_handoff_observation",
                    "previous_observation_id": previous_id,
                    "observation_id": package.observation_id,
                    "captured_monotonic_ms": package.captured_monotonic_ms,
                })
            return await super().act_once(subgoal, package, prior_result, task_id=task_id,
                                          state=state, model_call_meter=model_call_meter)
        finally:
            self._restored_history = None

    async def _call_with_session(self, obs_messages, history_messages=None, **kwargs):
        context = kwargs["context_state"]
        state = context["agent_state"]
        self._session.store, self._session.state = self.store, state
        stage = state.revisable.stage
        self._session.set_stage_skills(state.active_target_app, stage.skill_ids if stage else [])
        self._session.set_stable_system(
            prompt("executor") + "\n\nOriginal instruction:\n" + state.instruction
        )
        old_render = context["render_observation_bucket"]

        def render(package):
            self.store.observe(package, state)
            messages, names = old_render(package)
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        self.store.execution_context(state, package.observation_id),
                        ensure_ascii=False,
                    ),
                }
            )
            return messages, [*names, "runtime"]

        context["render_observation_bucket"] = render
        context["allow_note_before_submit"] = True
        # TaskStore history is append-only between compaction boundaries. Any
        # compound evidence was already committed with its action result.
        restored_history = list(self._restored_history or [])
        supplied_history = list(history_messages or [])
        history = [*restored_history, *supplied_history]
        supplied_names = list(kwargs.get("history_message_names") or [])
        history_names = [
            *[f"restored_history[{index}]" for index in range(len(restored_history))],
            *supplied_names,
        ]
        obs_messages, names = render(context["active_package"])
        handlers = dict(kwargs.get("handlers") or {})
        inspect = handlers.get("inspect_image_regions")
        if inspect:
            async def measured(args, ctx):
                result = await inspect(args, ctx)
                if result.status.value == "succeeded":
                    result.data["source"] = self.store.save_measurement(result.data, state)
                return result
            handlers["inspect_image_regions"] = measured
        result = await self._session.run(
            obs_messages,
            observation_message_names=names,
            history_messages=history,
            history_message_names=history_names,
            handlers=handlers,
            context_state=context,
            event_sink=kwargs.get("event_sink"),
            model_call_meter=kwargs.get("model_call_meter"),
            artifacts=self.artifacts,
            retain_observations=True,
            reserve_terminal_round=True,
        )
        if self.cancel_requested():
            raise asyncio.CancelledError
        self.directive = result.context_state["directive"]
        self.completed_stage_id = result.context_state.get("completed_stage_id")
        self.store.save_dialogue(result.dialogue_messages, state)
        step = result.decision
        step.action_pipeline = _model_submission_pipeline(
            step,
            validation_attempts=sum(
                call.name == "submit_executor_step" for call in result.tool_calls
            ),
        )
        return step, result

    def record_result(self, step, result, state, *, compound_evidence=None):
        # Device-space coordinates and raw driver messages remain diagnostic.
        receipt = result.receipt
        action_result = {
            "success": result.success,
            "receipt": receipt.model_dump(
                mode="json",
                include={
                    "transaction_id",
                    "dispatch_succeeded",
                    "effect_outcome",
                    "observation_id",
                    "effect_observation_id",
                    "observation_accepted",
                    "visible_change",
                },
            )
            if receipt
            else None,
        }
        if result.detail.get("input_steps"):
            action_result["input_steps"] = result.detail["input_steps"]
            if not result.success:
                action_result["input_status"] = result.message
        if receipt and receipt.node_click_status:
            action_result["receipt"].update({
                "node_click_status": receipt.node_click_status,
                "native_action_performed": receipt.native_action_performed,
            })
        if receipt and receipt.interaction_ack == "confirmed":
            action_result["interaction_ack"] = "confirmed"
        event = {
            "step": state.step_number,
            "stage_id": state.revisable.stage_id,
            "decision": self.directive,
            "completed_stage_id": getattr(self, "completed_stage_id", None),
            "executor_report": step.summary,
            "observation_id": step.basis_observation_id,
            "submitted_action": step.submitted_action_snapshot.model_dump(mode="json")
            if step.submitted_action_snapshot
            else None,
            "action_result": action_result,
        }
        self.store.put("event", str(state.step_number), event)
        outcome_json = json.dumps(
            {"action_result": {**action_result, "receipt": {
                key: value for key, value in (action_result["receipt"] or {}).items()
                if key != "transaction_id" and value not in (None, "")
            } if action_result["receipt"] else None}, "step": state.step_number},
            ensure_ascii=False,
        )
        outcome = {"role": "user", "content": outcome_json}
        if result.success and compound_evidence is not None:
            historical = _compound_history_messages(compound_evidence)
            if historical:
                content = historical[0].get("content")
                heading = "ACTION OUTCOME\n" + outcome_json + "\n\n"
                if isinstance(content, list):
                    blocks = [dict(block) for block in content]
                    text_block = next(
                        (
                            block for block in blocks
                            if block.get("type") == "text"
                        ),
                        None,
                    )
                    if text_block is None:
                        blocks.insert(0, {"type": "text", "text": heading.rstrip()})
                    else:
                        text_block["text"] = heading + str(text_block.get("text") or "")
                    outcome["content"] = blocks
                else:
                    outcome["content"] = heading + str(content or "")
        # One block is the durable semantic action outcome. The next Executor
        # step restores it normally before rendering the latest observation.
        self.store.save_dialogue([outcome], state)
