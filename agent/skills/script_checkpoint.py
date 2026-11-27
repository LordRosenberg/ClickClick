"""Durable business progress for an owned GUI script replay."""

from __future__ import annotations

import json
import time

from agent.skills.script_schema import script_fingerprint
from shared.recovery_store import RecoveryConflict, digest


CHECKPOINT_KIND = "gui_script_checkpoint"
CHECKPOINT_KEY = "current"
COMPLETION_KIND = "gui_script_step_completion"


class ScriptDispatchBlocked(RecoveryConflict):
    """The current replay step permits verification only."""


def load_script_checkpoint(store, task_id):
    record = store.db.get_agent_record(task_id, CHECKPOINT_KIND, CHECKPOINT_KEY)
    return record["payload"] if record else None


def _not_dispatched(row, result):
    if row["result_state"] != "known" or not isinstance(result, dict):
        return False
    detail = result.get("detail") or {}
    receipt = result.get("receipt") or {}
    if (detail.get("execution_result") == "unknown" or detail.get("dispatch_unknown") is True
            or detail.get("device_dispatch") in {"attempted", "unknown"}
            or detail.get("node_click_status") == "outcome_unknown"
            or detail.get("native_action_performed") is True or receipt.get("dispatch_succeeded")):
        return False
    return (detail.get("device_dispatch") == "not_dispatched"
            or detail.get("device_action_units") == 0
            or (receipt.get("effect_outcome") == "suppressed" and not receipt.get("dispatch_succeeded")))


def _confirmation_identifier(operation, evidence):
    if operation is None:
        return None
    baseline = operation["baseline"]
    if not isinstance(baseline, (list, tuple)) or not isinstance(evidence, (list, tuple)):
        return None
    if any(not isinstance(value, str) or not value.strip() for value in (*baseline, *evidence)):
        return None
    fresh = set(evidence) - set(baseline)
    return next(iter(fresh)) if len(fresh) == 1 else None


def pending_script_operation(store, task_id, checkpoint=None):
    """Read T1/T2 even after T3 has settled the low-level action and its budget."""
    if checkpoint is None:
        checkpoint = load_script_checkpoint(store, task_id)
    if checkpoint is None:
        return None
    rows = store.db._conn.execute(
        "SELECT * FROM task_operations WHERE task_id=? AND kind='device_action' "
        "AND json_extract(intent_json,'$.script_attempt.script_hash')=? "
        "AND json_extract(intent_json,'$.script_attempt.index')=? ORDER BY created_at,ordinal",
        (task_id, checkpoint["script_hash"], checkpoint["cursor"]),
    )
    completed = set(checkpoint.get("resolved_limited", ()))
    for row in rows:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if row["operation_id"] in completed or _not_dispatched(row, result):
            continue
        attempt = json.loads(row["intent_json"])["script_attempt"]
        return {**attempt, "operation_id": row["operation_id"], "step_seq": row["step_seq"], "result": result,
                "result_state": row["result_state"], "action": json.loads(row["args_json"]),
                "operation_status": row["status"]}
    return None


def check_script_dispatch(store, task_id, attempt):
    """Apply the same guard before T1 for scripted and model-driven input."""
    checkpoint = load_script_checkpoint(store, task_id)
    if checkpoint is None and attempt is None:
        return
    if checkpoint is None or attempt is None:
        raise ScriptDispatchBlocked("script_step_binding_required")
    cursor, step_ids = checkpoint["cursor"], checkpoint["step_ids"]
    if (attempt["script_hash"] != checkpoint["script_hash"] or attempt["index"] != cursor
            or cursor >= len(step_ids) or step_ids[cursor] != attempt["step_id"]):
        raise ScriptDispatchBlocked("script_step_binding_changed")
    if pending_script_operation(store, task_id, checkpoint) is not None:
        raise ScriptDispatchBlocked("script_step_requires_result_verification")


class ScriptCheckpoint:
    """Tie a script cursor and completion evidence to the existing action history."""

    def __init__(self, execution, script, values):
        self.execution = execution
        self.store = execution.store
        self.task_id = execution.owner.task_id
        self.script = script
        self.signature = {
            "script_hash": script_fingerprint(script), "script_id": script.script_id,
            "revision": script.revision, "parameters_hash": digest(dict(values)),
        }
        with self.store.db.transaction():
            self.store.check(execution.owner)
            record = load_script_checkpoint(self.store, self.task_id)
            if record is None:
                self._save({**self.signature, "cursor": 0, "receipts": [],
                            "step_ids": [step.id for step in script.steps],
                            "resolved_limited": [], "takeover_seconds": 0.0, "takeover_started_at": None})
            elif any(record.get(key) != value for key, value in self.signature.items()):
                raise RecoveryConflict("script_checkpoint_binding_changed")

    def _load(self):
        record = load_script_checkpoint(self.store, self.task_id)
        if record is None or any(record.get(key) != value for key, value in self.signature.items()):
            raise RecoveryConflict("script_checkpoint_binding_changed")
        return record

    def _save(self, record, *, state=None):
        self.store.db.append_agent_record(self.task_id, CHECKPOINT_KIND, CHECKPOINT_KEY, record)
        self.store.publish_manifest(self.task_id, state=state if state is not None else self.execution.state)

    @property
    def cursor(self):
        return self._load()["cursor"]

    @property
    def receipts(self):
        return tuple(self._load()["receipts"])

    @property
    def takeover_used(self):
        record = self._load()
        started = record["takeover_started_at"]
        return record["takeover_seconds"] + (max(0.0, time.time() - started) if started is not None else 0.0)

    def start_takeover(self):
        with self.store.db.transaction():
            self.store.check(self.execution.owner)
            record = self._load()
            if record["takeover_started_at"] is None:
                self._save({**record, "takeover_started_at": time.time()})

    def finish_takeover(self):
        with self.store.db.transaction():
            self.store.check(self.execution.owner, allow_stopped=True)
            record = self._load()
            started = record["takeover_started_at"]
            if started is not None:
                self._save({**record, "takeover_started_at": None,
                            "takeover_seconds": record["takeover_seconds"] + max(0.0, time.time() - started)})

    def pending(self, index):
        record = self._load()
        if record["cursor"] != index:
            return None
        return pending_script_operation(self.store, self.task_id, record)

    def bind_attempt(self, index, attempt, *, before_observation_id, baseline, phase="action"):
        if (self.cursor != index or not 0 <= index < len(self.script.steps)
                or phase not in {"action", "limited", "formal"}):
            raise RecoveryConflict("script_attempt_binding_changed")
        self.execution.script_attempt = {
            "script_hash": self.signature["script_hash"], "script_id": self.script.script_id,
            "revision": self.script.revision, "index": index, "step_id": self.script.steps[index].id,
            "attempt": attempt, "before_observation_id": before_observation_id,
            "baseline": baseline, "phase": phase,
        }

    def clear_attempt(self):
        self.execution.script_attempt = None

    def correlates(self, index, evidence):
        """Require a fresh, single business identifier emitted by the verifier."""
        return _confirmation_identifier(self.pending(index), evidence) is not None

    def _check_observation(self, index, observation_id, operation):
        if (not observation_id or (operation is not None and observation_id == operation["before_observation_id"])
                or self.cursor != index):
            raise RecoveryConflict("script_completion_requires_fresh_observation")

    def resolve_limited(self, index, *, observation_id, evidence):
        """Settle a checked, host-authorized recovery input without advancing the script."""
        with self.store.db.transaction():
            self.store.check(self.execution.owner)
            record = self._load()
            operation = self.pending(index)
            if operation is None:
                raise RecoveryConflict("script_limited_input_receipt_missing")
            self._check_observation(index, observation_id, operation)
            if (operation["phase"] != "limited" or operation["result_state"] != "known"
                    or not (operation["result"] or {}).get("success")):
                raise RecoveryConflict("script_limited_input_result_uncertain")
            self.store.db.append_agent_record(self.task_id, "gui_script_limited_confirmation", operation["operation_id"], {
                "step_id": self.script.steps[index].id, "index": index, "observation_id": observation_id,
                "operation_id": operation["operation_id"], "evidence": evidence,
            })
            self._save({**record, "resolved_limited": [*record["resolved_limited"], operation["operation_id"]]})

    def _close_verified_transport(self, operation, step_id):
        """Close an interrupted T1/T2 using ordinary T3 without changing its receipt."""
        if operation is None or operation["operation_status"] == "committed":
            return None
        seq = operation["step_seq"]
        db = self.store.db
        operations = db._conn.execute(
            "SELECT o.*, b.reserved FROM task_operations o JOIN budget_ledger b USING (task_id,operation_id) "
            "WHERE o.task_id=? AND o.step_seq=? AND o.status!='committed' ORDER BY o.created_at,o.ordinal",
            (self.task_id, seq),
        )
        units = 0
        outcomes = []
        for row in operations:
            result = (self.store.record_unknown(row["operation_id"]) if row["result_state"] is None
                      else json.loads(row["result_json"]))
            units += self.store.charge_units(row, row["reserved"])
            outcomes.append({"operation_id": row["operation_id"], "result": result})
        state = self.execution.state.model_copy(deep=True)
        state.step_number = max(state.step_number, seq + 1)
        state.revisable.execution_count += units
        db.add_step_and_update_state(self.task_id, state.revisable.stage_id, seq, {
            "script_step_id": step_id, "action": operation["action"], "reason": "script_business_result_verified",
            "recovery_operations": outcomes,
        }, state)
        return state

    def confirm(self, index, *, observation_id, evidence, receipt, same_turn_known=False):
        """Publish business evidence and the next cursor in one transaction."""
        with self.store.db.transaction():
            self.store.check(self.execution.owner)
            record = self._load()
            operation = self.pending(index)
            self._check_observation(index, observation_id, operation)
            step_id = self.script.steps[index].id
            if receipt.get("step_id") != step_id or not receipt.get("succeeded"):
                raise RecoveryConflict("script_completion_receipt_invalid")
            identifier = _confirmation_identifier(operation, evidence)
            if operation and identifier is None:
                if not (same_turn_known and operation["result_state"] == "known"
                        and (operation["result"] or {}).get("success")):
                    raise RecoveryConflict("script_completion_requires_correlated_evidence")
            self.store.db.append_agent_record(self.task_id, COMPLETION_KIND, step_id, {
                **self.signature, "step_id": step_id, "index": index, "next_index": index + 1,
                "attempt": operation["attempt"] if operation else receipt.get("attempt"),
                "operation_id": operation["operation_id"] if operation else None,
                "before_observation_id": operation["before_observation_id"] if operation else None,
                "observation_id": observation_id, "evidence": evidence, "confirmation_id": identifier,
            })
            state = self._close_verified_transport(operation, step_id)
            self._save({**record, "cursor": index + 1, "resolved_limited": [],
                        "receipts": [*record["receipts"], receipt]}, state=state)
        if state is not None:
            self.execution.state.step_number = state.step_number
            self.execution.state.revisable.execution_count = state.revisable.execution_count
