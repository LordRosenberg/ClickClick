"""Source-bound Console telemetry; never part of learning evidence or prompts."""
from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

from agent.tool_registry import redact_value


def safe_payload(value):
    """Redact structured credentials even when embedded in JSON message text."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return redact_value(value, max_string=None, max_items=None)
        if isinstance(parsed, (dict, list)):
            return json.dumps(safe_payload(parsed), ensure_ascii=False)
        return value
    if isinstance(value, list):
        return [safe_payload(item) for item in value]
    if isinstance(value, dict):
        return redact_value({key: safe_payload(item) for key, item in value.items()},
                            max_string=None, max_items=None)
    return redact_value(value, max_string=None, max_items=None)


class ResearchTelemetry:
    def __init__(self, artifacts, source_task_id):
        self.artifacts = artifacts
        self.source_task_id = source_task_id
        self.id = uuid4().hex
        self.entries = []
        self.started_at = time.time()

    def record(self, role, phase, payload, *, cost, histories, status="recorded", model=None):
        # Observability failure must not alter admission or device dispatch.
        try:
            now = time.time()
            order = len(self.entries) + 1
            namespace = "skill-learning/runs/" + self.source_task_id + "/" + self.id
            ref = self.artifacts.save_json(namespace + "/records", {
                "schema_version": 1, "source_task_id": self.source_task_id,
                "job_id": self.id, "order": order, "role": role, "phase": phase,
                "created_at": now, "model": model, "status": status,
                "cost": cost, "payload": safe_payload(payload),
                "policy": "Model analysis and verdicts are fallible. Execution reports are not evaluator truth. Telemetry is never supplied as evidence."})
            self.entries.append({"ref": ref, "order": order, "role": role,
                "phase": phase, "created_at": now, "status": status, "model": model})
            self.artifacts.save_json(namespace + "/catalog", {
                "schema_version": 1, "source_task_id": self.source_task_id,
                "job_id": self.id, "version": order, "started_at": self.started_at,
                "updated_at": now, "entries": self.entries, "cost": cost,
                "histories": [{"namespace": key, "task_id": store.task_id}
                    for key, store in histories.items() if key != "source"]})
        except Exception:  # Console cannot turn an accepted model result into failure.
            logging.getLogger(__name__).exception("Could not persist research Console telemetry")
