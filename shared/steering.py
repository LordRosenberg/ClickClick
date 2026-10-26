"""Task-local ordered user instructions, separate from optional ask_user replies."""

import time

from pydantic import BaseModel, ConfigDict, Field

from shared.hitl import HumanInputBroker, InputConflict


class SteeringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    instruction: str = Field(min_length=1, max_length=20000)
    request_key: str = Field(min_length=1, max_length=160)


class SteeringInbox:
    def __init__(self, db, task_id):
        self.db, self.task_id = db, task_id

    def records(self):
        return sorted(self.db.list_agent_records(self.task_id, "user_adjustment"), key=lambda row: row["payload"]["revision"])

    def revision(self):
        return max((r["payload"]["revision"] for r in self.records()), default=0)

    def pending(self):
        return [r for r in self.records() if r["payload"]["status"] == "accepted"]

    def previous(self, request):
        row = self.db.get_agent_record(self.task_id, "user_adjustment", request.request_key)
        if row and row["payload"]["instruction"] != request.instruction:
            raise InputConflict("request_key_conflict")
        return row

    def accept(self, request, **extra):
        with self.db.transaction():
            row = self.previous(request)
            if row:
                return row["payload"]
            payload = {"revision": self.revision() + 1, "instruction": request.instruction,
                       "status": "accepted", "created_at": time.time(), **extra}
            self.db.append_agent_record(self.task_id, "user_adjustment", request.request_key, payload)
            self.db._conn.execute("UPDATE tasks SET control_revision=control_revision+1 WHERE id=?",(self.task_id,))
            return payload

    def applied(self, revision, reason):
        with self.db.transaction():
            for row in self.pending():
                if row["payload"]["revision"] <= revision:
                    self.db.append_agent_record(self.task_id, "user_adjustment", row["key"], {
                        **row["payload"], "status": "applied", "feedback": reason,
                        "applied_at": time.time()})

    def cancelled(self):
        with self.db.transaction():
            for row in self.pending():
                self.db.append_agent_record(self.task_id, "user_adjustment", row["key"], {
                    **row["payload"], "status": "cancelled", "feedback": "Task cancelled before applying adjustment"})

    def projection(self):
        from agent.revisable.recall import source_id
        return [{"source": source_id("user_adjustment", r), **r["payload"]}
                for r in self.records()]

    def supersede_question(self):
        broker = HumanInputBroker(self.db)
        item = broker.current(self.task_id)
        if item and item["status"] in {"pending", "resolved"}:
            broker._save({**item, "status": "superseded", "checkpoint": None})
            return {"purpose": item["purpose"], "questions": item["questions"],
                    "answers": item.get("answers", []), "outcome": item.get("outcome")}
        return None
