"""Official MobileWorld user-simulator transport for the generic HITL broker.

This transport has no task-answer knowledge and never opens a Console question.
The existing native-GUI runner remains a separately disclosed evaluation cohort.
"""

from __future__ import annotations

import time
from typing import Callable

from shared.hitl import Answer, HumanInputBroker, InputConflict, Reply
from shared.schemas import TaskStatus


class HumanSimulationError(RuntimeError):
    """An unscored environment/protocol fault, not an agent's task failure."""


def validate_category(metadata: dict, enabled: bool) -> None:
    tags = set(metadata.get("tags") or [])
    if "agent-mcp" in tags:
        raise ValueError("MCP tasks require the separate delegated MCP adapter")
    if ("agent-user-interaction" in tags) != enabled:
        raise ValueError("task category contradicts frozen user-interaction flag")


class MobileWorldHumanTransport:
    version = "native-gui-delegated-hitl-v1"

    def __init__(self, broker: HumanInputBroker, request: Callable, backend: str, device: str):
        self.broker = broker
        self.request = request
        self.backend = backend.rstrip("/")
        self.device = device

    def dispatch(self, task_id: str) -> None:
        """Send once; persisted uncertain dispatches require operator reconciliation."""
        db = self.broker.db
        item = self.broker.current(task_id)
        if not item or item["status"] != "pending" or item["owner"] != "delegated":
            raise InputConflict("no_delegated_pending_interaction")
        if self.broker.config(task_id)["mode"] != "delegated":
            raise InputConflict("wrong_human_transport")
        if item["purpose"] != "clarification" or len(item["questions"]) != 1:
            raise InputConflict("official_host_requires_single_clarification")
        remaining = item["deadline_at"] - time.time()
        if remaining <= 0:
            raise HumanSimulationError("human_interaction_deadline_exhausted")
        question = item["questions"][0]
        text = question["question"]
        if question.get("options"):
            text += "\nOptions: " + "; ".join(o["label"] for o in question["options"])
        action = {"action_type": "ask_user", "text": text}
        identity = item["interaction_id"]
        row = db.get_agent_record(task_id, "mobileworld_human_dispatch", identity)
        receipt = row["payload"] if row else None
        if receipt and receipt["status"] != "returned":
            raise HumanSimulationError("human_dispatch_outcome_unknown_no_automatic_replay")
        if receipt is None:
            with db.transaction():
                # The task has one episode worker. Reserve the official ask round
                # and durable identity before contacting the user simulator.
                if db.get_agent_record(task_id, "mobileworld_human_dispatch", identity):
                    raise InputConflict("human_dispatch_already_reserved")
                task = db.get_task(task_id)
                runtime = task.state.revisable
                if task.status != TaskStatus.WAITING_FOR_INPUT:
                    raise InputConflict("task_not_waiting")
                if (runtime.limits.prediction_rounds is not None and
                        runtime.prediction_round_count >= runtime.limits.prediction_rounds):
                    raise HumanSimulationError("human_query_round_budget_exhausted")
                runtime.prediction_round_count += 1
                db.update_task(task_id, state=task.state)
                receipt = {
                    "status": "dispatched", "interaction_id": identity,
                    "call_id": item["call_id"], "invocation_id": item["invocation_id"],
                    "owner_version": item["owner_version"], "action": action,
                    "device": self.device, "round": runtime.prediction_round_count,
                    "dispatched_at": time.time(), "transport_version": self.version,
                }
                db.append_agent_record(task_id, "mobileworld_human_dispatch", identity, receipt)
            try:
                response = self.request("POST", self.backend + "/step",
                                        timeout=min(300, remaining),
                                        json={"device": self.device, "action": action})
            except Exception as exc:
                # HTTP errors may contain provider credentials. Persist only type.
                receipt.update(status="unknown", error_type=type(exc).__name__)
                db.append_agent_record(task_id, "mobileworld_human_dispatch", identity, receipt)
                raise HumanSimulationError("official_user_simulator_request_failed") from None
            echoed = response.get("action") if isinstance(response, dict) else None
            answer = response.get("result") if isinstance(response, dict) else None
            if (not isinstance(echoed, dict) or echoed.get("action_type") != "ask_user"
                    or echoed.get("text") != text or response.get("device") != self.device
                    or not isinstance(answer, str) or not answer.strip()
                    or len(answer.strip()) > self.broker.config(task_id)["max_answer_chars"]):
                receipt.update(status="unknown", error_type="InvalidOfficialHumanResponse")
                db.append_agent_record(task_id, "mobileworld_human_dispatch", identity, receipt)
                raise HumanSimulationError("invalid_official_user_simulator_response")
            receipt.update(status="returned", answer=self.broker.sanitize(answer.strip()),
                           returned_at=time.time(), provenance="official_simulated_user")
            db.append_agent_record(task_id, "mobileworld_human_dispatch", identity, receipt)
        if (receipt["call_id"] != item["call_id"] or
                receipt["invocation_id"] != item["invocation_id"] or
                receipt["owner_version"] != item["owner_version"]):
            raise InputConflict("human_result_identity_mismatch")
        self.broker.reply(task_id, Reply(
            interaction_id=identity, owner_version=item["owner_version"],
            response_id="mobileworld:" + identity, outcome="answered",
            answers=[Answer(question_id=question["id"], text=receipt["answer"])],
        ), owner="delegated")

    def report(self, task_id: str) -> dict:
        interactions = self.broker.db.list_agent_records(task_id, "human_input")
        return {
            "enabled": True, "mode": "delegated", "transport_version": self.version,
            "query_count": len(interactions),
            "clarification_queries": sum(r["payload"]["purpose"] == "clarification" for r in interactions),
            "confirmation_queries": sum(r["payload"]["purpose"] == "confirmation" for r in interactions),
            "dispatches": [r["payload"] for r in self.broker.db.list_agent_records(task_id, "mobileworld_human_dispatch")],
            "simulated_user_not_real_human": True,
        }
