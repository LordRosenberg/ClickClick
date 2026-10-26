"""Execution-side deduplication and fencing independent of backend step commits."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import threading
from contextlib import contextmanager

from shared.file_lock import FileLock,FileLockBusy
from shared.recovery_store import RecoveryConflict, digest, encode
from shared.schemas import ActionResult

current_input = ContextVar("durable_driver_input", default=None)


class ExecutionJournal:
    def __init__(self, root: Path):
        root = root.resolve()
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.generation = uuid.uuid4().hex
        self._db_lock=threading.RLock()
        self.conn = sqlite3.connect(root / "execution.db", timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS guards(device_key TEXT PRIMARY KEY, fence INTEGER NOT NULL,
            task_id TEXT NOT NULL, epoch INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY,device_key TEXT NOT NULL,
            task_id TEXT NOT NULL,args_digest TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,
            generation TEXT NOT NULL,created_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS children(pid INTEGER NOT NULL, started_at REAL NOT NULL,
            operation_id TEXT NOT NULL,PRIMARY KEY(pid,started_at));
          CREATE TABLE IF NOT EXISTS subactions(operation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,return_code INTEGER,PRIMARY KEY(operation_id,ordinal));
        """)

    @contextmanager
    def write(self):
        with self._db_lock:
            with self.conn:
                yield

    def begin_subaction(self,envelope):
        with self.write():
            self.check(envelope)
            ordinal=self.conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM subactions WHERE operation_id=?",
                                      (envelope["operation_id"],)).fetchone()[0]
            self.conn.execute("INSERT INTO subactions VALUES (?,?,'dispatching',NULL)",(envelope["operation_id"],ordinal))
            return ordinal

    def end_subaction(self,envelope,ordinal,return_code):
        with self.write():
            self.conn.execute("UPDATE subactions SET status='returned',return_code=? WHERE operation_id=? AND ordinal=?",
                              (return_code,envelope["operation_id"],ordinal))

    def subactions(self,operation_id):
        return [dict(row) for row in self.conn.execute("SELECT ordinal,status,return_code FROM subactions WHERE operation_id=? ORDER BY ordinal",(operation_id,))]

    def _lock(self, key):
        return FileLock(self.root / f"input-{digest(key)}.lock")

    def check(self, envelope):
        row = self.conn.execute("SELECT * FROM guards WHERE device_key=?", (envelope["device_key"],)).fetchone()
        if not row or row["closed"] or (row["fence"], row["task_id"], row["epoch"]) != (
                envelope["device_fence"], envelope["task_id"], envelope["epoch"]):
            raise RecoveryConflict("stale device input authority")

    async def fence(self, key, fence, task_id, epoch, *, timeout=5):
        """Revoke first, then drain. Control queries never wait on the action lock."""
        with self.write():
            row = self.conn.execute("SELECT * FROM guards WHERE device_key=?", (key,)).fetchone()
            if row and (row["fence"] > fence or (
                row["fence"] == fence and (row["task_id"],row["epoch"]) != (task_id,epoch))):
                raise RecoveryConflict("device fence conflict")
            self.conn.execute("INSERT INTO guards VALUES (?,?,?,?,0) ON CONFLICT(device_key) DO UPDATE SET "
                              "fence=excluded.fence,task_id=excluded.task_id,epoch=excluded.epoch,closed=0",
                              (key, fence, task_id, epoch))
        deadline = time.monotonic() + timeout
        while True:
            lock = self._lock(key)
            try:
                lock.acquire()
            except RuntimeError:
                if time.monotonic() >= deadline:
                    return {"quiescent": False, "generation": self.generation, "reason": "old_input_chain_live"}
                await asyncio.sleep(.025)
                continue
            try:
                if not await asyncio.to_thread(self._reap_children, key):
                    return {"quiescent": False,"generation":self.generation,"reason":"input_child_not_reaped"}
                return {"quiescent": True, "generation": self.generation}
            finally:
                lock.close()

    def _reap_children(self, key):
        rows = self.conn.execute("SELECT c.* FROM children c JOIN operations o ON o.operation_id=c.operation_id "
                                 "WHERE o.device_key=?", (key,)).fetchall()
        import psutil
        for row in rows:
            try:
                process = psutil.Process(row["pid"])
                if abs(process.create_time() - row["started_at"]) > .01:
                    continue
                if os.name!="nt":
                    import signal
                    os.killpg(row["pid"],signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except psutil.TimeoutExpired:
                        os.killpg(row["pid"],signal.SIGKILL)
                        process.wait(timeout=2)
                    continue
                process.terminate()
                try:
                    process.wait(timeout=2)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            except (psutil.NoSuchProcess,ProcessLookupError):
                if os.name!="nt":
                    import signal
                    group=[]
                    for candidate in psutil.process_iter():
                        try:
                            if os.getpgid(candidate.pid)==row["pid"]:
                                group.append(candidate)
                        except (OSError,psutil.NoSuchProcess):
                            continue
                    try:
                        if group and all(item.environ().get("CLICKCLICK_INPUT_GROUP_NONCE")==row["operation_id"] for item in group):
                            os.killpg(row["pid"],signal.SIGKILL)
                        elif group:
                            return False
                    except (OSError,psutil.Error):
                        return False
            except (psutil.AccessDenied, psutil.TimeoutExpired):
                return False
        with self.write():
            for row in rows:
                self.conn.execute("DELETE FROM children WHERE pid=? AND started_at=?", (row["pid"],row["started_at"]))
        if os.name!="nt":
            for operation in self.conn.execute("SELECT operation_id FROM operations WHERE device_key=?",(key,)):
                path=self.root/f"child-{operation[0]}.lock"
                if path.exists():
                    try:
                        with FileLock(path):
                            pass
                    except RuntimeError:
                        return False
        return True

    def get(self, operation_id):
        row = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            return {"status": "not_found"}
        return {"status": row["status"], "generation": row["generation"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "subactions":self.subactions(operation_id)}

    def _existing_result(self,envelope):
        row=self.conn.execute("SELECT * FROM operations WHERE operation_id=?",(envelope["operation_id"],)).fetchone()
        if row is None:
            return None
        if row["task_id"]!=envelope["task_id"] or row["device_key"]!=envelope["device_key"] or row["args_digest"]!=digest(envelope["action"]):
            raise RecoveryConflict("operation payload conflict")
        if row["result_json"]:
            return ActionResult.model_validate_json(row["result_json"])
        return ActionResult(success=False,message="execution_result_unknown",
                            detail={"execution_result":"unknown","operation_id":envelope["operation_id"],"dispatch_state":row["status"]})

    async def act(self,envelope,execute):
        self.check(envelope)
        existing=self._existing_result(envelope)
        if existing is not None:
            return existing
        try:
            return await self._act_locked(envelope,execute)
        except FileLockBusy:
            existing=self._existing_result(envelope)
            if existing is not None:
                return existing
            raise RecoveryConflict("device_input_busy") from None

    async def _act_locked(self, envelope, execute):
        key, identity = envelope["device_key"], envelope["operation_id"]
        with self._lock(key):
            self.check(envelope)
            row = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (identity,)).fetchone()
            if row:
                if row["task_id"] != envelope["task_id"] or row["device_key"]!=key or row["args_digest"] != digest(envelope["action"]):
                    raise RecoveryConflict("operation payload conflict")
                if row["result_json"]:
                    return ActionResult.model_validate_json(row["result_json"])
                return ActionResult(success=False, message="execution_result_unknown",
                                    detail={"execution_result":"unknown","operation_id":identity})
            with self.write():
                self.conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?)",
                                  (identity,key,envelope["task_id"],digest(envelope["action"]),"accepted",None,self.generation,time.time()))
            token = current_input.set((self, envelope))
            try:
                self.check(envelope)
                result = await execute()
                if envelope.get("sensitive"):
                    secret = envelope["action"].get("text")
                    if secret:
                        def scrub(value):
                            if isinstance(value,str):
                                return value.replace(secret,"[REDACTED]")
                            if isinstance(value,dict):
                                return {key:scrub(item) for key,item in value.items()}
                            if isinstance(value,list):
                                return [scrub(item) for item in value]
                            return value
                        result = ActionResult.model_validate(scrub(result.model_dump(mode="json")))
                result.detail["execution_subactions"]=self.subactions(identity)
                with self.write():
                    self.conn.execute("UPDATE operations SET status='returned',result_json=? WHERE operation_id=?",
                                      (result.model_dump_json(), identity))
                return result
            finally:
                current_input.reset(token)

    def close_task(self, key, task_id, epoch):
        with self.write():
            self.conn.execute("UPDATE guards SET closed=1 WHERE device_key=? AND task_id=? AND epoch=?", (key,task_id,epoch))

    def register_child(self, pid, envelope):
        import psutil
        try:
            started = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            return
        with self.write():
            self.conn.execute("INSERT OR IGNORE INTO children VALUES (?,?,?)", (pid,started,envelope["operation_id"]))

    def unregister_child(self, pid):
        with self.write():
            self.conn.execute("DELETE FROM children WHERE pid=?", (pid,))


_job_handle = None
_job_lock = threading.Lock()


def ensure_input_job():
    with _job_lock:
        _ensure_input_job()


def _ensure_input_job():
    """On Windows, owned child processes die when the runtime is hard-killed.

    Assign the runtime before spawning, avoiding the spawn-to-journal crash gap.
    The handle stays open for process lifetime. Existing processes are untouched.
    """
    global _job_handle
    if os.name != "nt" or _job_handle is not None:
        return
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [("ProcessTime",ctypes.c_int64),("JobTime",ctypes.c_int64),
                    ("LimitFlags",wintypes.DWORD),("Min",ctypes.c_size_t),("Max",ctypes.c_size_t),
                    ("Active",wintypes.DWORD),("Affinity",ctypes.c_size_t),
                    ("Priority",wintypes.DWORD),("Scheduling",wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name,ctypes.c_uint64) for name in ("ReadOps","WriteOps","OtherOps","ReadBytes","WriteBytes","OtherBytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("Basic",Basic),("IO",IO),("ProcessMemory",ctypes.c_size_t),
                    ("JobMemory",ctypes.c_size_t),("PeakProcess",ctypes.c_size_t),("PeakJob",ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None,None)
    limits = Extended()
    limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,kernel.GetCurrentProcess()):
        if handle:
            kernel.CloseHandle(handle)
        raise OSError(ctypes.get_last_error(),"cannot isolate runtime input children")
    _job_handle = handle
