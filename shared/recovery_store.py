"""Durable recovery facts. SQLite transactions never span device execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
TERMINAL = {"succeeded", "failed", "cancelled"}
RECOVERING = {"recovering", "waiting_for_device", "recovery_blocked"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class StaleOwner(BaseException):
    """Retired workers yield without marking the successor cancelled/failed."""


class RecoveryConflict(ValueError):
    pass


@dataclass(frozen=True)
class Ownership:
    task_id: str
    epoch: int
    token: str
    device_fence: int
    device_key: str


def migrate_recovery(db):
    conn = db._conn
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RecoveryConflict("database schema is newer; refusing incompatible writes")
    if version == SCHEMA_VERSION:
        return
    if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
        backup = db.path.with_suffix(".pre-recovery-v1.backup")
        if not backup.exists():
            temporary = backup.with_suffix(".tmp")
            target=sqlite3.connect(temporary)
            try:
                conn.backup(target)
            finally:
                target.close()
            temporary.replace(backup)
    columns = {
        "state_version": "INTEGER NOT NULL DEFAULT 0",
        "control_revision": "INTEGER NOT NULL DEFAULT 0",
        "execution_epoch": "INTEGER NOT NULL DEFAULT 0",
        "worker_token": "TEXT",
        "owner_instance_id": "TEXT",
        "desired_state": "TEXT NOT NULL DEFAULT 'run'",
        "recovery_policy": "TEXT NOT NULL DEFAULT 'manual'",
        "recovery_protocol_version": "INTEGER NOT NULL DEFAULT 0",
        "active_manifest_id": "TEXT",
        "last_committed_seq": "INTEGER NOT NULL DEFAULT -1",
        "recovery_json": "TEXT NOT NULL DEFAULT '{}'",
    }
    with db.transaction():
        existing = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        for name, kind in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {kind}")
        statements = [
            """CREATE TABLE IF NOT EXISTS device_runtime (
                device_key TEXT PRIMARY KEY, last_token INTEGER NOT NULL DEFAULT 0,
                task_id TEXT, epoch INTEGER, quarantine INTEGER NOT NULL DEFAULT 0,
                driver_generation TEXT, detail_json TEXT NOT NULL DEFAULT '{}')""",
            """CREATE TABLE IF NOT EXISTS task_operations (
                operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, step_seq INTEGER NOT NULL,
                invocation_id TEXT NOT NULL, call_id TEXT, ordinal INTEGER NOT NULL DEFAULT 0,
                parent_id TEXT, kind TEXT NOT NULL, args_json TEXT NOT NULL, args_digest TEXT NOT NULL,
                epoch INTEGER NOT NULL, device_fence INTEGER NOT NULL, control_revision INTEGER NOT NULL,
                base_state_version INTEGER NOT NULL, basis_observation_id TEXT,
                intent_json TEXT NOT NULL, status TEXT NOT NULL, result_state TEXT,
                result_json TEXT, operation_version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_operations_task ON task_operations(task_id, step_seq)",
            """CREATE TABLE IF NOT EXISTS operation_receipts (
                operation_id TEXT NOT NULL, source TEXT NOT NULL, source_generation TEXT NOT NULL,
                source_seq INTEGER NOT NULL, result_json TEXT NOT NULL, received_at REAL NOT NULL,
                PRIMARY KEY(operation_id, source, source_generation, source_seq))""",
            """CREATE TABLE IF NOT EXISTS recovery_requests (
                task_id TEXT NOT NULL, request_key TEXT NOT NULL, payload_digest TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(task_id, request_key))""",
            """CREATE TABLE IF NOT EXISTS budget_ledger (
                task_id TEXT NOT NULL, operation_id TEXT NOT NULL, dimension TEXT NOT NULL,
                reserved INTEGER NOT NULL DEFAULT 0, settled INTEGER NOT NULL DEFAULT 0,
                uncertain INTEGER NOT NULL DEFAULT 0, entry_version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(task_id, operation_id, dimension))""",
        ]
        for statement in statements:
            conn.execute(statement)
        for action in ("INSERT","UPDATE","DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS recovery_writer_{action.lower()} BEFORE {action} ON tasks "
                         "WHEN clickclick_schema_writer()!=1 BEGIN SELECT RAISE(ABORT,'incompatible schema writer'); END")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


class RecoveryStore:
    def __init__(self, db, artifacts=None):
        self.db, self.artifacts = db, artifacts
        if artifacts is not None:
            artifacts.protected_refs = self.protected_refs

    def metadata(self, task_id):
        row = self.db._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def enable(self, task_id, policy="manual"):
        if policy not in {"safe_auto", "manual", "disabled"}:
            raise ValueError("invalid recovery policy")
        with self.db.transaction():
            meta=self.metadata(task_id)
            if meta["recovery_protocol_version"]==PROTOCOL_VERSION:
                if meta["recovery_policy"]!=policy:
                    raise RecoveryConflict("recovery policy is frozen for this task")
                return
            conflict=self.db._conn.execute("SELECT seq FROM steps WHERE task_id=? GROUP BY seq HAVING COUNT(*)>1 LIMIT 1",(task_id,)).fetchone()
            if conflict:
                raise RecoveryConflict("legacy_step_identity_conflict")
            self.db._conn.execute(
                "UPDATE tasks SET recovery_policy=?, recovery_protocol_version=? WHERE id=?",
                (policy, PROTOCOL_VERSION, task_id),
            )
            if self.artifacts:
                self.publish_manifest(task_id)

    def check(self, owner, *, allow_stopped=False):
        meta = self.metadata(owner.task_id)
        if meta["execution_epoch"] != owner.epoch or meta["worker_token"] != owner.token:
            raise StaleOwner()
        if not allow_stopped and (meta["status"] in TERMINAL or meta["desired_state"] != "run"):
            raise StaleOwner()
        return meta

    def claim(self, task_id, instance_id, *, expected_epoch=None, takeover=False):
        with self.db.transaction():
            meta = self.metadata(task_id)
            if meta["status"] in TERMINAL:
                raise RecoveryConflict("terminal task cannot be claimed")
            if expected_epoch is not None and meta["execution_epoch"] != expected_epoch:
                raise RecoveryConflict("ownership changed")
            if meta["worker_token"] and not takeover:
                raise RecoveryConflict("task already owned")
            epoch, token = meta["execution_epoch"] + 1, uuid.uuid4().hex
            key = meta["device_serial"] or "default"
            self.db._conn.execute("INSERT OR IGNORE INTO device_runtime(device_key) VALUES (?)", (key,))
            device = dict(self.db._conn.execute(
                "SELECT * FROM device_runtime WHERE device_key=?", (key,)).fetchone())
            if device["task_id"] and device["task_id"] != task_id:
                raise RecoveryConflict("device occupied or quarantined by another task")
            fence = device["last_token"] + 1
            self.db._conn.execute(
                "UPDATE device_runtime SET last_token=?,task_id=?,epoch=?,quarantine=1 WHERE device_key=?",
                (fence, task_id, epoch, key),
            )
            self.db._conn.execute(
                "UPDATE tasks SET execution_epoch=?,worker_token=?,owner_instance_id=? WHERE id=?",
                (epoch, token, instance_id, task_id),
            )
            return Ownership(task_id, epoch, token, fence, key)

    def control(self, task_id, desired):
        if desired not in {"run", "pause", "cancel"}:
            raise ValueError("invalid control intent")
        with self.db.transaction():
            self.db._conn.execute(
                "UPDATE tasks SET desired_state=?,control_revision=control_revision+1 WHERE id=?",
                (desired, task_id),
            )

    def release(self, owner, *, quiescent):
        with self.db.transaction():
            self.check(owner, allow_stopped=True)
            if quiescent:
                self.db._conn.execute(
                    "UPDATE device_runtime SET task_id=NULL,epoch=NULL,quarantine=0,last_token=last_token+1 "
                    "WHERE device_key=? AND task_id=? AND epoch=?",
                    (owner.device_key, owner.task_id, owner.epoch),
                )
            self.db._conn.execute("UPDATE tasks SET worker_token=NULL WHERE id=?", (owner.task_id,))

    def quiescent(self, owner, generation):
        with self.db.transaction():
            self.check(owner, allow_stopped=True)
            self.db._conn.execute(
                "UPDATE device_runtime SET quarantine=0,driver_generation=? WHERE device_key=? AND last_token=?",
                (generation, owner.device_key, owner.device_fence),
            )

    def operations(self, task_id, *, pending=False):
        sql = "SELECT * FROM task_operations WHERE task_id=?"
        if pending:
            sql += " AND status!='committed'"
        rows = self.db._conn.execute(sql + " ORDER BY step_seq,created_at,ordinal", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def prepare(self, owner, state, *, operation_id, kind, args, invocation_id,
                call_id="", basis="", intent=None, units=0, parent_id=None, ordinal=0):
        """T1: normalized intent, local post-state and reservation in one transaction."""
        with self.db.transaction():
            self.db._references_committed(intent or {})
            meta = self.check(owner)
            prior = self.db._conn.execute(
                "SELECT * FROM task_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                if prior["task_id"] != owner.task_id or prior["args_digest"] != digest(args):
                    raise RecoveryConflict("operation identity payload conflict")
                return dict(prior)
            if state.revisable.limits.expired():
                from shared.task_budgets import TaskBudgetExhausted
                raise TaskBudgetExhausted("task_time")
            pending = self.db._conn.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM budget_ledger WHERE task_id=? AND dimension='device_actions'",
                (owner.task_id,),
            ).fetchone()[0]
            limit = state.revisable.limits.device_actions
            if units and limit is not None and state.revisable.execution_count + pending + units > limit:
                from shared.task_budgets import TaskBudgetExhausted
                raise TaskBudgetExhausted("device_actions", units)
            now = time.time()
            snapshot = {"state": state.model_dump(mode="json"), **(intent or {})}
            self.db._conn.execute(
                """INSERT INTO task_operations(operation_id,task_id,step_seq,invocation_id,call_id,
                   ordinal,parent_id,kind,args_json,args_digest,epoch,device_fence,control_revision,
                   base_state_version,basis_observation_id,intent_json,status,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (operation_id, owner.task_id, state.step_number, invocation_id, call_id, ordinal,
                 parent_id, kind, encode(args), digest(args), owner.epoch, owner.device_fence,
                 meta["control_revision"], meta["state_version"], basis, encode(snapshot), "dispatching", now, now),
            )
            self.db._conn.execute(
                "INSERT INTO budget_ledger(task_id,operation_id,dimension,reserved) VALUES (?,?,?,?)",
                (owner.task_id, operation_id, "device_actions", units),
            )
            # Preserve the complete decision and current context before the effect.
            self.publish_manifest(owner.task_id, state=state)
            return dict(self.db._conn.execute(
                "SELECT * FROM task_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def receipt(self, operation_id, result, *, source="driver", generation="local", sequence=1):
        """T2: receipts are append-only facts, including late old-owner results."""
        with self.db.transaction():
            latest=self.db._conn.execute("SELECT result_json FROM operation_receipts WHERE operation_id=? ORDER BY received_at DESC LIMIT 1",(operation_id,)).fetchone()
            cursor = self.db._conn.execute(
                "INSERT OR IGNORE INTO operation_receipts VALUES (?,?,?,?,?,?)",
                (operation_id, source, generation, sequence, encode(result), time.time()),
            )
            row = self.db._conn.execute(
                "SELECT task_id,status,result_state,result_json FROM task_operations WHERE operation_id=?", (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if not cursor.rowcount:
                return
            if latest and latest[0]==encode(result):
                return
            if row["status"] == "committed":
                self.db._conn.execute("UPDATE task_operations SET operation_version=operation_version+1 WHERE operation_id=?",(operation_id,))
                self.db.append_agent_record(row["task_id"], "recovery_fact", operation_id,
                                            {"operation_id": operation_id, "result": result, "source": source})
            else:
                self.db._conn.execute(
                    "UPDATE task_operations SET status='result_recorded',result_state='known',result_json=?,"
                    "operation_version=operation_version+1,updated_at=? WHERE operation_id=?",
                    (encode(result), time.time(), operation_id),
                )

    def record_unknown(self, operation_id, *, evidence=None):
        with self.db.transaction():
            row = self.db._conn.execute("SELECT * FROM task_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["result_json"]:
                return json.loads(row["result_json"])
            result = {"execution_result": "unknown", "receipt_available": False,
                      "reason": "interrupted_before_durable_receipt", "operation_id": operation_id,
                      "action": json.loads(row["args_json"]), "interruption_gap": True}
            if isinstance(evidence,dict):
                result["execution_stage"]={key:evidence[key] for key in ("status","generation","subactions") if key in evidence}
            self.db._conn.execute(
                "UPDATE task_operations SET status='result_recorded',result_state='unknown_recorded',result_json=?,"
                "operation_version=operation_version+1,updated_at=? WHERE operation_id=?",
                (encode(result), time.time(), operation_id),
            )
            return result

    def settle(self, task_id, seq):
        """Called inside T3; move reservations once without any human approval."""
        rows = self.db._conn.execute(
            "SELECT operation_id,result_state,result_json FROM task_operations WHERE task_id=? AND step_seq=? AND status!='committed'",
            (task_id, seq),
        ).fetchall()
        for row in rows:
            if row["result_state"] is None:
                raise RecoveryConflict("step cannot commit before a known or unknown result is recorded")
            reserved=self.db._conn.execute("SELECT reserved FROM budget_ledger WHERE task_id=? AND operation_id=?",
                                          (task_id,row["operation_id"])).fetchone()[0]
            charged=self.charge_units(row,reserved)
            uncertain=charged if row["result_state"]=="unknown_recorded" else 0
            self.db._conn.execute(
                "UPDATE budget_ledger SET settled=settled+?,uncertain=uncertain+?,reserved=0,"
                "entry_version=entry_version+1 WHERE task_id=? AND operation_id=?",
                (charged,uncertain,task_id,row["operation_id"]),
            )
            self.db.append_agent_record(task_id,"budget_settlement",row["operation_id"],{
                "reserved":reserved,"charged":charged,"released":reserved-charged,
                "state":"settled_uncertain" if uncertain else "settled",
                "proof":"original_driver_zero_units" if charged<reserved else None})
            self.db._conn.execute("UPDATE task_operations SET status='committed' WHERE operation_id=?", (row["operation_id"],))

    @staticmethod
    def charge_units(row,reserved):
        result=json.loads(row["result_json"] or "{}")
        # Only an original trusted zero-unit dispatch receipt proves that the
        # physical boundary was not entered. No page/model inference refunds.
        if row["result_state"]=="known" and result.get("detail",{}).get("device_action_units")==0:
            return 0
        return reserved

    def context_snapshot(self,task_id,*,state=None):
        meta = self.metadata(task_id)
        content = state.model_dump(mode="json") if state is not None else json.loads(meta["state_json"])
        # Capture current required bodies, never every historical trace/event.
        kinds = ("note","prompt_policy","skill_snapshot","hitl_config","human_input","user_adjustment")
        placeholders = ",".join("?" for _ in kinds)
        records = [dict(row) for row in self.db._conn.execute(
            f"SELECT a.* FROM agent_records a JOIN (SELECT kind,record_key,MAX(version) version "
            f"FROM agent_records WHERE task_id=? AND kind IN ({placeholders}) GROUP BY kind,record_key) b "
            "ON a.kind=b.kind AND a.record_key=b.record_key AND a.version=b.version WHERE a.task_id=?",
            (task_id,*kinds,task_id),
        )]
        for key in content["revisable"]["dialogue_refs"]:
            row = self.db._conn.execute("SELECT * FROM agent_records WHERE task_id=? AND kind='dialogue' "
                                       "AND record_key=? ORDER BY version DESC LIMIT 1",(task_id,key)).fetchone()
            if row is None:
                raise RecoveryConflict("required_dialogue_unavailable")
            records.append(dict(row))
        return {"version": PROTOCOL_VERSION, "state": content, "records": records,
                    "control_revision": meta["control_revision"], "state_version": meta["state_version"],
                    "dependencies":getattr(self.db,"recovery_dependencies",{})}

    def publish_manifest(self, task_id, *, state=None):
        meta = self.metadata(task_id)
        if meta["recovery_protocol_version"] != PROTOCOL_VERSION or self.artifacts is None:
            return None
        snapshot=self.context_snapshot(task_id,state=state)
        ref = self.artifacts.save_json("recovery", snapshot)
        self.db._conn.execute("UPDATE tasks SET active_manifest_id=? WHERE id=?", (ref, task_id))
        self.db.append_agent_record(task_id,"recovery_manifest",ref,
                                    {"ref":ref,"state_version":meta["state_version"]})
        self.db.after_commit(lambda:self.artifacts.release_pins({ref}))
        return ref

    def finalize_tools(self,task_id):
        """Close returned nonphysical calls at a role/task boundary, without an Executor tick."""
        with self.db.transaction():
            rows=[dict(row) for row in self.db._conn.execute(
                "SELECT * FROM task_operations WHERE task_id=? AND kind='external_tool' AND status='result_recorded'",(task_id,))]
            for row in rows:
                self.db.append_agent_record(task_id,"tool_result",row["operation_id"],{
                    "operation_id":row["operation_id"],"call_id":row["call_id"],"invocation_id":row["invocation_id"],
                    "intent":json.loads(row["intent_json"]),"result":json.loads(row["result_json"])})
                self.db._conn.execute("UPDATE budget_ledger SET reserved=0,entry_version=entry_version+1 WHERE task_id=? AND operation_id=?",
                                      (task_id,row["operation_id"]))
                self.db._conn.execute("UPDATE task_operations SET status='committed' WHERE operation_id=?",(row["operation_id"],))

    def _file_refs(self,value):
        refs = set()
        if isinstance(value,dict):
            for item in value.values():
                refs.update(self._file_refs(item))
        elif isinstance(value,list):
            for item in value:
                refs.update(self._file_refs(item))
        elif isinstance(value,str):
            # Record payloads are canonical JSON; strings that are refs remain
            # exact, while user prose is never interpreted as executable code.
            if value.startswith(("{","[")):
                try:
                    refs.update(self._file_refs(json.loads(value)))
                except (ValueError,RecursionError):
                    pass
            elif "/" in value and len(value)<512 and self.artifacts:
                try:
                    from pathlib import Path
                    stem=Path(value).stem
                    if len(stem) in {32,64} and all(char in "0123456789abcdef" for char in stem):
                        self.artifacts.resolve(value)
                        refs.add(value)
                except (ValueError,OSError):
                    pass
        return refs

    def protected_refs(self):
        """Retention-only reachability scan; never runs on normal action admission."""
        refs = set()
        for row in self.db._conn.execute("SELECT active_manifest_id,state_json FROM tasks"):
            if row[0]:
                refs.add(row[0])
                # Embedded bodies remain required even when their DB records
                # need restoration. Corrupt roots abort GC, never execution.
                refs.update(self._file_refs(json.loads(self.artifacts.verify(row[0]))))
            if row[1]:
                refs.update(self._file_refs(json.loads(row[1])))
        for row in self.db._conn.execute("SELECT payload_json FROM agent_records WHERE kind IN "
                                        "('dialogue','skill_snapshot','prompt_policy','execution_attempt','recovery_manifest')"):
            refs.update(self._file_refs(json.loads(row[0])))
        for row in self.db._conn.execute("SELECT intent_json,result_json FROM task_operations"):
            refs.update(self._file_refs(json.loads(row[0])))
            if row[1]:
                refs.update(self._file_refs(json.loads(row[1])))
        return refs

    def request(self, task_id, key, payload, response):
        with self.db.transaction():
            old = self.db._conn.execute("SELECT * FROM recovery_requests WHERE task_id=? AND request_key=?", (task_id, key)).fetchone()
            if old:
                if old["payload_digest"] != digest(payload):
                    raise RecoveryConflict("request_key payload conflict")
                return json.loads(old["response_json"])
            self.db._conn.execute("INSERT INTO recovery_requests VALUES (?,?,?,?,?)",
                                  (task_id, key, digest(payload), encode(response), time.time()))
            if response.get("accepted"):
                self.db.append_agent_record(task_id,"recovery_request",key,{"pending":True})
            return response

    def pending_requests(self,task_id):
        return [row["key"] for row in self.db.list_agent_records(task_id,"recovery_request")
                if row["payload"].get("pending")]

    def finish_requests(self,task_id,keys):
        with self.db.transaction():
            for key in keys:
                self.db.append_agent_record(task_id,"recovery_request",key,{"pending":False})

    def event(self, task_id, phase, *, reason=None, **facts):
        meta = self.metadata(task_id)
        projection = json.loads(meta["recovery_json"])
        if (phase=="detected" and projection.get("phase")=="ready"
                and meta["last_committed_seq"]>projection.get("ready_seq",meta["last_committed_seq"])):
            projection={}  # Only actual subsequent progress starts a new cycle.
        if phase=="ready":
            facts["ready_seq"]=meta["last_committed_seq"]
        projection.update(facts, phase=phase, reason=reason, updated_at=time.time())
        with self.db.transaction():
            self.db._conn.execute("UPDATE tasks SET recovery_json=? WHERE id=?", (encode(projection), task_id))
            self.db.append_agent_record(task_id, "recovery_event", uuid.uuid4().hex, projection)
            sink = getattr(self.db,"recovery_event_sink",None)
            if sink:
                self.db.after_commit(lambda:sink(task_id,projection))
        return projection

    def projection(self,task_id):
        meta=self.metadata(task_id)
        if not meta.get("recovery_protocol_version"):
            return {}
        value=json.loads(meta["recovery_json"])
        value.update(policy=meta["recovery_policy"],last_committed_seq=meta["last_committed_seq"],
                     requires_human=False,available_actions=[])
        if meta["status"] not in TERMINAL:
            value["available_actions"].append("cancel")
            if meta["status"] in RECOVERING and meta["recovery_policy"]!="disabled":
                value["available_actions"].append("recover")
        value["unknown_operations"]=[dict(row) for row in self.db._conn.execute(
            "SELECT operation_id,kind,status FROM task_operations WHERE task_id=? AND result_state='unknown_recorded' "
            "ORDER BY created_at DESC LIMIT 8",(task_id,))]
        usage=self.db._conn.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN json_extract(a.payload_json,'$.token_cost_status')='reported' THEN 0 ELSE 1 END),0) "
            "FROM agent_records a JOIN (SELECT record_key,MAX(version) version FROM agent_records WHERE task_id=? AND kind='invocation_round' GROUP BY record_key) b "
            "ON a.record_key=b.record_key AND a.version=b.version WHERE a.task_id=? AND a.kind='invocation_round'",(task_id,task_id)).fetchone()
        value["model_usage"]={"rounds":usage[0],"unknown_usage_rounds":usage[1]}
        device=self.db._conn.execute("SELECT quarantine FROM device_runtime WHERE task_id=?",(task_id,)).fetchone()
        value["quarantined"]=bool(device and device[0])
        return value
