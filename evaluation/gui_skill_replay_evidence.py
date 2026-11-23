"""Resolve a script recovery lead to its exact native read-only history."""

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, replace
import json
from pathlib import Path
from sqlite3 import Error as SQLiteError

from agent.revisable.store import TaskStore
from agent.skills.script_learning import _Evidence, _Frame, _frame, _trace
from agent.skills.script_learning_signals import SCRIPT_SKILL_SIGNAL_KIND, ScriptSkillSignal
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import digest
from shared.schemas import TaskRecord, TaskStatus


def _observed_target(event):
    """Keep the dispatched target visible even when a read returns only part of the tree."""
    node = next((node for node in event.before.nodes
                 if event.target_index is not None and node.get("index") == event.target_index), None)
    if node is None or node.get("password") or node.get("raw_fields_redacted=true"):
        return None
    fields = ("role", "resource_id", "desc", "hint") if node.get("editable") else (
        "role", "resource_id", "text", "desc", "hint")
    return {field: value for field in fields if isinstance(value := node.get(field), str) and 0 < len(value) <= 120}


@dataclass(frozen=True)
class ReplayHistory:
    signal: ScriptSkillSignal
    task: TaskRecord
    store: TaskStore
    events: tuple[_Evidence, ...]
    final: _Frame

    @property
    def namespace(self):
        return "script-repair-" + self.signal.signal_id

    def directory(self):
        return {"namespace": self.namespace, "app_id": self.signal.app_id,
                "app_build": self.signal.app_build,
                "hint": {"condition": self.signal.hint.condition, "rule": self.signal.hint.rule},
                "final": self.namespace + "/" + self.final.source,
                "events": [{"event": self.namespace + "/" + event.source,
                            "before": self.namespace + "/" + event.before.source,
                            "after": self.namespace + "/" + event.after.source, "action": event.action,
                            "observed_target": _observed_target(event)}
                           for event in self.events]}

    def regression_witnesses(self, candidate):
        prefix = self.namespace + "/"
        references = {ref.removeprefix(prefix) for ref in candidate.evidence if ref.startswith(prefix)}
        if not any({event.source, event.before.source, event.after.source}.intersection(references)
                   for event in self.events):
            return []
        # Use the first recovery action so setup never performs the earlier path change.
        event = self.events[0]
        return [(replace(event, source=prefix + event.source,
                         before=replace(event.before, source=prefix + event.before.source),
                         after=replace(event.after, source=prefix + event.after.source)),
                 replace(self.final, source=prefix + self.final.source))]


def _verified_history(signal, db, runtime):
    task = db.get_task(signal.replay_task_id)
    if task is None or task.status is not TaskStatus.SUCCEEDED or task.instruction != signal.goal:
        return None
    store = TaskStore(db, ArtifactStore(runtime / "artifacts", create=False), task.id)
    final = store.get("gui_script_run", "final")["payload"]
    receipt = final["receipt"]
    if (digest(final) != signal.assisted_run_hash or final["fixture"]["app_build"] != signal.app_build
            or receipt["script_hash"] != signal.base_script_hash or receipt["script_id"] != signal.script_id
            or receipt["revision"] != signal.script_revision or receipt["failed_step_id"] != signal.failed_step_id
            or receipt["status"] != "succeeded" or not receipt["assisted"] or not receipt["final_asserted"]):
        return None
    exact = {(item.event, item.before, item.after) for item in signal.evidence}
    recovered = tuple(_trace(task, store, takeover_for=signal.failed_step_id))
    if (not exact or set(signal.hint.evidence) != {item[0] for item in exact}
            or not exact <= {(event.source, event.before.source, event.after.source) for event in recovered}
            or any(event.before.app != signal.app_id or event.after.app != signal.app_id for event in recovered)):
        return None
    final_row = store.get("observation", receipt["final_observation_id"])
    if final_row["payload"].get("accepted") is not True:
        return None
    result = _frame(final_row)
    return ReplayHistory(signal, task, store, recovered, result) if result.app == signal.app_id else None


@contextmanager
def script_repair_histories(db, task, *, selected=None):
    """Expose accepted recovery observations in their own native namespaces."""
    with ExitStack() as stack:
        histories = []
        seen = set()
        remaining_chars = 6000
        for row in reversed(db.list_agent_records(task.id, SCRIPT_SKILL_SIGNAL_KIND)):
            if selected is not None and row["key"] not in selected:
                continue
            try:
                signal = ScriptSkillSignal.model_validate_json(json.dumps(row["payload"]))
                if (signal.signal_id in seen or signal.signal_id != row["key"]
                        or signal.source_task_id != task.id or signal.goal != task.instruction):
                    continue
                runtime = Path(signal.replay_runtime).resolve(strict=True)
                with ExitStack() as opened:
                    native = Database(runtime / "clickclick.db", read_only=True)
                    opened.callback(native.close)
                    history = _verified_history(signal, native, runtime)
                    if history is None:
                        continue
                    size = len(json.dumps(history.directory(), ensure_ascii=False))
                    if size > remaining_chars:
                        continue
                    remaining_chars -= size
                    histories.append(history)
                    seen.add(signal.signal_id)
                    stack.enter_context(opened.pop_all())
            except (OSError, SQLiteError, KeyError, TypeError, ValueError):
                continue
        yield histories
