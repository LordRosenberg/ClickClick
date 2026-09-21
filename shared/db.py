"""SQLite persistence for tasks, loop state, steps, and traces."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from shared.schemas import (
    AgentState,
    LogLevel,
    TaskRecord,
    TaskStatus,
    TraceEvent,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  instruction TEXT NOT NULL,
  status TEXT NOT NULL,
  current_node_id TEXT,
  failure_reason TEXT,
  state_json TEXT,
  device_serial TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_records (
  task_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  record_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (task_id, kind, record_key, version)
);

CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  node_id TEXT,
  seq INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE (task_id, node_id, seq)
);

CREATE TABLE IF NOT EXISTS traces (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  node_id TEXT,
  step_seq INTEGER,
  kind TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_ref TEXT,
  payload_json TEXT,
  ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traces_task_level ON traces(task_id, level);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_device_serial ON tasks(device_serial);
"""


class Database:
    """Thin SQLite wrapper used by Control API and Agent persistence."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._transaction_depth = 0
        if not read_only:
            self.migrate()

    def migrate(self) -> None:
        """Create the current schema idempotently; historical schemas are unsupported."""
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self):
        """Commit related task/trace writes together on this connection."""
        outermost = self._transaction_depth == 0
        if outermost:
            self._conn.execute("BEGIN")
        self._transaction_depth += 1
        try:
            yield
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                self._conn.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if outermost:
                self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def create_task(
        self,
        instruction: str,
        state: AgentState | None = None,
        *,
        device_serial: str | None = None,
    ) -> TaskRecord:
        """Insert a new queued task with an initial AgentState (plan empty by default)."""
        now = time.time()
        task_id = str(uuid.uuid4())
        agent_state = state or AgentState(instruction=instruction)
        state_json = agent_state.model_dump_json()
        self._conn.execute(
            """
            INSERT INTO tasks(id, instruction, status, current_node_id, failure_reason,
                              state_json, created_at, updated_at, device_serial)
            VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                task_id,
                instruction,
                TaskStatus.QUEUED.value,
                state_json,
                now,
                now,
                device_serial,
            ),
        )
        self._commit()
        return self.get_task(task_id)  # type: ignore[return-value]

    def _row_to_task(self, row: sqlite3.Row, *, include_state: bool = True) -> TaskRecord:
        state: AgentState | None = None
        if row["state_json"]:
            try:
                raw = json.loads(row["state_json"])
                # Earlier plan-driven checkpoints also serialized unused fields
                # from the removed runtime. Project their existing plan state;
                # this does not reconstruct or execute a legacy contract task.
                if "revisable" in raw:
                    raw = {key: value for key, value in raw.items()
                           if key in AgentState.model_fields}
                if not include_state:
                    raw = {key: raw[key] for key in (
                        "instruction", "revisable", "current_subgoal", "step_number", "skill_learn"
                    ) if key in raw}
                state = AgentState.model_validate(raw)
            except Exception:  # noqa: BLE001
                state = None
        plan = [state.revisable.stage.goal] if state and state.revisable.stage else []
        if state and state.current_subgoal and (
            not plan or plan[0] != state.current_subgoal
        ):
            plan.insert(0, state.current_subgoal)
        current_subgoal = state.current_subgoal if state else ""
        step_number = state.step_number if state else 0
        device_serial = row["device_serial"]
        skill_learn = False
        if state is not None:
            skill_learn = bool(getattr(state, "skill_learn", False))
        return TaskRecord(
            id=row["id"],
            instruction=row["instruction"],
            status=TaskStatus(row["status"]),
            current_node_id=row["current_node_id"] or current_subgoal or None,
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            plan=plan,
            current_subgoal=current_subgoal,
            step_number=step_number,
            state=state if include_state else None,
            device_serial=device_serial,
            skill_learn=skill_learn,
        )

    def get_task(self, task_id: str) -> TaskRecord | None:
        """Fetch a task by id."""
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def list_tasks(
        self, status: TaskStatus | None = None, *, include_state: bool = True
    ) -> list[TaskRecord]:
        """List tasks, optionally filtered by status."""
        if status:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [self._row_to_task(r, include_state=include_state) for r in rows]

    def busy_serials(self) -> dict[str, str]:
        """Map device_serial → task_id for non-terminal tasks with a binding."""
        rows = self._conn.execute(
            """
            SELECT id, device_serial FROM tasks
            WHERE device_serial IS NOT NULL AND device_serial != ''
              AND status IN (?, ?)
            """,
            (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
        ).fetchall()
        return {r["device_serial"]: r["id"] for r in rows}

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus | None = None,
        current_node_id: str | None = None,
        failure_reason: str | None = None,
        state: AgentState | None = None,
    ) -> None:
        """Update mutable task fields and/or the persisted AgentState."""
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        new_status = status or task.status
        new_node = current_node_id if current_node_id is not None else task.current_node_id
        new_fail = failure_reason if failure_reason is not None else task.failure_reason
        if state is not None:
            state_json = state.model_dump_json()
            new_node = state.current_subgoal or None
        else:
            state_json = task.state.model_dump_json() if task.state else None
        self._conn.execute(
            """
            UPDATE tasks SET status=?, current_node_id=?, failure_reason=?, state_json=?, updated_at=?
            WHERE id=?
            """,
            (new_status.value, new_node, new_fail, state_json, time.time(), task_id),
        )
        self._commit()

    def add_step(self, task_id: str, node_id: str | None, seq: int, payload: dict[str, Any]) -> None:
        """Persist a step report payload (node_id now carries the current_subgoal)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO steps(task_id, node_id, seq, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, node_id, seq, json.dumps(payload)),
        )
        self._commit()

    def add_step_and_update_state(
        self,
        task_id: str,
        node_id: str | None,
        seq: int,
        payload: dict[str, Any],
        state: AgentState,
    ) -> None:
        """Atomically persist one completed step and its routed agent state."""
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE tasks SET current_node_id=?, state_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    state.current_subgoal or None,
                    state.model_dump_json(),
                    time.time(),
                    task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
            self._conn.execute(
                """
                INSERT OR REPLACE INTO steps(task_id, node_id, seq, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, node_id, seq, json.dumps(payload)),
            )

    def list_steps(self, task_id: str) -> list[dict[str, Any]]:
        """Return ordered steps for replay."""
        rows = self._conn.execute(
            "SELECT node_id, seq, payload_json FROM steps WHERE task_id=? ORDER BY id ASC",
            (task_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = json.loads(r["payload_json"])
            item["node_id"] = r["node_id"]
            item["seq"] = r["seq"]
            out.append(item)
        return out

    def add_trace(self, event: TraceEvent) -> None:
        """Append a trace event."""
        self._conn.execute(
            """
            INSERT INTO traces(task_id, node_id, step_seq, kind, level, message, payload_ref, payload_json, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.task_id,
                event.node_id,
                event.step_seq,
                event.kind,
                event.level.value,
                event.message,
                event.payload_ref,
                json.dumps(event.payload),
                event.ts or time.time(),
            ),
        )
        self._commit()

    def list_traces(
        self, task_id: str, level: LogLevel | None = None
    ) -> list[TraceEvent]:
        """List traces for a task, optionally filtered by minimum/exact level."""
        if level:
            rows = self._conn.execute(
                "SELECT * FROM traces WHERE task_id=? AND level=? ORDER BY id ASC",
                (task_id, level.value),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM traces WHERE task_id=? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [
            TraceEvent(
                task_id=r["task_id"],
                node_id=r["node_id"],
                step_seq=r["step_seq"],
                kind=r["kind"],  # type: ignore[arg-type]
                level=LogLevel(r["level"]),
                message=r["message"],
                payload_ref=r["payload_ref"],
                payload=json.loads(r["payload_json"] or "{}"),
                ts=r["ts"],
            )
            for r in rows
        ]

    def append_agent_record(self, task_id: str, kind: str, key: str, payload: dict) -> dict:
        with self.transaction():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM agent_records "
                "WHERE task_id=? AND kind=? AND record_key=?", (task_id, kind, key),
            ).fetchone()
            version = row[0]
            self._conn.execute(
                "INSERT INTO agent_records VALUES (?, ?, ?, ?, ?)",
                (task_id, kind, key, version, json.dumps(payload, ensure_ascii=False)),
            )
        return {"key": key, "version": version, "payload": payload}

    def get_agent_record(self, task_id: str, kind: str, key: str,
                         version: int | None = None) -> dict | None:
        rows = self._conn.execute(
            "SELECT record_key, version, payload_json FROM agent_records "
            "WHERE task_id=? AND kind=? AND record_key=? "
            "AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1",
            (task_id, kind, key, version, version),
        ).fetchone()
        return self._agent_record(rows) if rows else None

    def list_agent_records(self, task_id: str, kind: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT r.record_key, r.version, r.payload_json FROM agent_records r "
            "JOIN (SELECT record_key, MAX(version) AS version FROM agent_records "
            "WHERE task_id=? AND kind=? GROUP BY record_key) latest "
            "ON r.record_key=latest.record_key AND r.version=latest.version "
            "WHERE r.task_id=? AND r.kind=? ORDER BY r.rowid",
            (task_id, kind, task_id, kind),
        ).fetchall()
        return [self._agent_record(row) for row in rows]

    @staticmethod
    def _agent_record(row) -> dict:
        return {"key": row[0], "version": row[1], "payload": json.loads(row[2])}
