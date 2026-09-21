"""Task-scoped records. Storage assigns identity; models supply meaning."""

import json
import uuid
from typing import Any

from agent.revisable.history import note_directory, page, stage_directory
from agent.revisable.recall import source_id
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState


class TaskStore:
    def __init__(self, db: Database, artifacts: ArtifactStore, task_id: str):
        self.db, self.artifacts, self.task_id = db, artifacts, task_id
        self.budget_snapshot = None

    def put(self, kind: str, key: str, payload: dict) -> dict:
        return self.db.append_agent_record(self.task_id, kind, key, payload)

    def get(self, kind: str, key: str, version: int | None = None) -> dict:
        record = self.db.get_agent_record(self.task_id, kind, key, version)
        if record is None:
            raise ValueError(f"Unknown {kind}: {key}")
        return record

    def records(self, kind: str) -> list[dict]:
        return self.db.list_agent_records(self.task_id, kind)

    def resolve_observation(self, reference: str) -> dict:
        exact = self.db.get_agent_record(self.task_id, "observation", reference)
        if exact is not None:
            return exact
        candidates = [
            row for row in self.records("observation") if row["key"].startswith(reference)
        ]
        if len(reference) >= 8 and len(candidates) == 1:
            return candidates[0]
        raise ValueError("Unknown or ambiguous observation reference; use a listed observation ID")

    def observe(self, package: ObservationPackage, state: AgentState) -> None:
        self.record_stage(state)
        visit = {"step": state.step_number, "stage_id": state.revisable.stage_id}
        existing = self.db.get_agent_record(self.task_id, "observation", package.observation_id)
        if existing:
            payload = existing["payload"]
            if visit not in payload["visits"]:
                payload["visits"].append(visit)
                if visit["stage_id"] not in payload["stage_ids"]:
                    payload["stage_ids"].append(visit["stage_id"])
                self.put("observation", package.observation_id, payload)
            return
        image = package.image_for_llm or package.clean_png or package.annotated_png
        self.put(
            "observation",
            package.observation_id,
            {
                "step": state.step_number,
                "stage_id": state.revisable.stage_id,
                "stage_ids": [state.revisable.stage_id],
                "visits": [visit],
                "app": package.ui.app_id,
                "text": package.text_for_llm,
                "image_ref": self.artifacts.save_bytes("history_images", image) if image else None,
            },
        )

    def record_stage(self, state: AgentState) -> None:
        runtime = state.revisable
        if runtime.stage is None:
            return
        existing = self.db.get_agent_record(self.task_id, "stage", runtime.stage_id)
        payload = {
            "goal": runtime.stage.goal,
            "target_app": runtime.stage.target_app,
            "first_seen_step": existing["payload"]["first_seen_step"]
            if existing
            else state.step_number,
            "last_seen_step": state.step_number,
        }
        if existing is None or existing["payload"] != payload:
            self.put("stage", runtime.stage_id, payload)

    def save_measurement(self, data: dict, state: AgentState) -> str:
        row = self.put("measurement", uuid.uuid4().hex, {**data, "step": state.step_number})
        return source_id("measurement", row)

    def measurements(self, character_budget: int = 6000) -> list[dict]:
        items = []
        for row in reversed(self.records("measurement")):
            item = {"source": source_id("measurement", row), **row["payload"]}
            size = len(json.dumps(item, ensure_ascii=False))
            if size > character_budget or len(items) == 4:
                break
            items.append(item)
            character_budget -= size
        return list(reversed(items))

    def unresolved_notes(self) -> list[dict]:
        return [{"note_key": row["key"], "source": source_id("note", row),
                 "question": row["payload"]["unresolved"],
                 "observation_ids": row["payload"].get("observation_ids", []),
                 "source_refs": row["payload"].get("source_refs", [])}
                for row in self.records("note") if row["payload"].get("unresolved")]

    def note_index(self) -> list[dict]:
        return note_directory(self)

    def context(
        self, state: AgentState, observation_id: str, role: str = "executor"
    ) -> dict[str, Any]:
        runtime = state.revisable
        notes = page(self.note_index(), "notes", character_budget=2400)
        context = {
            "original_instruction": state.instruction,
            "current_device_date": state.current_device_date or None,
            "temporal_conventions": list(state.temporal_conventions),
            "current_stage": runtime.stage.model_dump() if runtime.stage else None,
            "latest_plan": runtime.plan.model_dump() if runtime.plan else None,
            "plan_reason": runtime.plan_reason,
            "plan_revision": runtime.revision,
            "stage_id": runtime.stage_id,
            "stage_start_step": runtime.stage_start_step,
            "current_step": state.step_number,
            "current_observation_id": observation_id,
            "feedback": runtime.feedback,
            "unresolved_questions": self.unresolved_notes(),
            "tool_measurements": self.measurements(),
            "notes": notes["notes"],
            "omitted_note_count": notes["total"] - len(notes["notes"]),
            "note_next_offset": notes["next_offset"],
        }
        if self.budget_snapshot is not None:
            context["remaining_budget"] = self.budget_snapshot()
        if role != "executor":
            context["tool_measurements"] = self.measurements()
            context["evidence_policy"] = "Measurements establish only their recorded source/target/property. Notes, summaries and plans are model interpretations, not independent verification. Historical indices never ground current actions."
            context["handoff"] = runtime.handoff
            context["operation_summary"] = self.operation_summary(state)
            context["note_contents"] = self.recent_notes()
            context["stage_directory"] = page(
                stage_directory(self), "stages", limit=3, character_budget=2400
            )
        return context

    def operation_summary(self, state: AgentState, character_budget: int = 12000) -> dict:
        """Project a chronological event tail without interpreting or merging events."""

        def compact(value):
            if isinstance(value, dict):
                return {key: compact(item) for key, item in value.items() if item is not None}
            if isinstance(value, list):
                return [compact(item) for item in value]
            return value

        earlier_summary = state.revisable.summary
        summary_included = len(earlier_summary) <= character_budget
        if summary_included:
            character_budget -= len(earlier_summary)
        records = sorted(self.records("event"), key=lambda row: row["payload"]["step"])
        events = []
        for row in reversed(records):
            event = compact(row["payload"])
            event["source"] = source_id("event", row)
            size = len(json.dumps(event, ensure_ascii=False))
            if size > character_budget:
                break
            events.append(event)
            character_budget -= size
        return {
            "earlier_executor_summary": earlier_summary if summary_included else None,
            "earlier_summary_omitted": not summary_included,
            "events": list(reversed(events)),
            "omitted_event_count": len(records) - len(events),
        }

    def recent_notes(self, character_budget: int = 12000) -> list[dict]:
        """Include whole recent notes; the directory exposes every omitted note."""
        notes = []
        for row in reversed(self.records("note")):
            note = {"note_key": row["key"], "version": row["version"], "source": source_id("note", row), **row["payload"]}
            size = len(json.dumps(note, ensure_ascii=False))
            if size <= character_budget:
                notes.append(note)
                character_budget -= size
        return notes

    def execution_context(self, state: AgentState, observation_id: str) -> dict:
        """Append changes without rewriting cached dialogue between compactions."""
        current = self.context(state, observation_id)
        current.pop("original_instruction")
        previous = state.revisable.delivered_context
        update = {
            key: value
            for key, value in current.items()
            if key not in previous or previous[key] != value
        }
        if previous.get("plan_revision") != current["plan_revision"]:
            update["latest_plan"] = current["latest_plan"]
        for key in ("stage_id", "current_observation_id", "current_step"):
            update[key] = current[key]
        state.revisable.delivered_context = current
        return {"runtime_update": update}

    def save_dialogue(self, messages: list[dict], state: AgentState) -> None:
        ref = self.artifacts.save_json("dialogue", messages)
        block_id = uuid.uuid4().hex
        self.put("dialogue", block_id, {"step": state.step_number, "ref": ref})
        state.revisable.dialogue_refs.append(block_id)

    def read_dialogue(self, refs: list[str]) -> list[dict]:
        return [
            message
            for block in refs
            for message in json.loads(
                self.artifacts.read_text(self.get("dialogue", block)["payload"]["ref"])
            )
        ]

    def split_dialogue(self, refs: list[str]) -> tuple[list[str], list[str]]:
        """Retain every block of the two newest execution steps, including receipts."""
        steps = [self.get("dialogue", ref)["payload"]["step"] for ref in refs]
        retained_steps = set(sorted(set(steps))[-2:])
        boundary = next((index for index, step in enumerate(steps) if step in retained_steps), 0)
        return refs[:boundary], refs[boundary:]
