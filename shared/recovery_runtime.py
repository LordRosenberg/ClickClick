"""Local recovery coordinator: isolate, adopt facts, restore context, hand off."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from shared.recovery_context import DurableDriver, ExecutionContext, current_execution
from shared.recovery_store import RECOVERING, TERMINAL, RecoveryConflict, RecoveryStore, StaleOwner
from shared.schemas import AgentState, TaskStatus


class InfrastructureInterrupted(RuntimeError):
    pass


class RecoveryUnrecoverable(RecoveryConflict):
    """Current required bodies cannot be recovered from durable sources."""


def configuration_fact(settings):
    """Nonsecret repair revision; no credential read or model probe."""
    from pathlib import Path
    import os
    from desktop.configuration import console_config_path,model_config_path
    from shared.recovery_store import digest
    paths=[console_config_path(settings.data_dir),model_config_path(settings.data_dir)]
    token_dir=settings.chatgpt_token_dir or os.environ.get("CHATGPT_TOKEN_DIR") or str(Path.home()/".config/litellm/chatgpt")
    paths.append(Path(token_dir)/os.environ.get("CHATGPT_AUTH_FILE","auth.json"))
    stamps=[]
    for path in paths:
        try:
            stat=path.stat()
            stamps.append((stat.st_mtime_ns,stat.st_size))
        except OSError:
            stamps.append(None)
    return digest(stamps)


def instance_id(orch):
    if not hasattr(orch, "backend_instance_id"):
        import os
        import psutil
        orch.backend_instance_id = uuid.uuid4().hex
        orch.db.set_local_preference("backend_instance:"+orch.backend_instance_id,{
            "instance_id":orch.backend_instance_id,"pid":os.getpid(),
            "process_started_at":psutil.Process().create_time(),"host_boot_at":psutil.boot_time(),"started_at":time.time()})
    return orch.backend_instance_id


def journal_for(orch):
    if not hasattr(orch, "execution_journal"):
        from driver.execution_journal import ExecutionJournal
        orch.execution_journal = ExecutionJournal(orch.db.path.parent / "driver-execution")
    return orch.execution_journal


async def isolate(driver, journal, owner, *, timeout=5):
    remote = getattr(driver, "fence_device", None)
    if remote:
        try:
            capabilities = await driver.recovery_capabilities()
        except Exception as exc:
            raise RecoveryConflict("driver_recovery_capability_unavailable") from exc
        if capabilities.get("protocol_version") != 1:
            raise RecoveryConflict("driver_recovery_unsupported")
        return await remote(owner.device_key, owner.device_fence, owner.task_id, owner.epoch, timeout=timeout)
    return await journal.fence(owner.device_key, owner.device_fence, owner.task_id, owner.epoch, timeout=timeout)


async def run_owned_task(orch, task_id, **limits):
    store = RecoveryStore(orch.db, orch.artifacts)
    meta = store.metadata(task_id)
    if meta["recovery_protocol_version"] != 1:
        return await orch._run_owned_task(task_id, **limits)
    task = orch.db.get_task(task_id)
    if task.status.value in TERMINAL or task.status in {TaskStatus.PAUSED, TaskStatus.WAITING_FOR_INPUT}:
        return task.status
    if task.status.value in RECOVERING:
        return task.status
    if meta["desired_state"] == "pause":
        orch.request_pause(task_id)
    elif meta["desired_state"] == "cancel":
        orch.request_cancel(task_id)
    if store.operations(task_id, pending=True):
        store.event(task_id,"detected",reason="unclosed_operation")
        orch.db.update_task(task_id,status=TaskStatus.RECOVERING)
        return TaskStatus.RECOVERING
    try:
        owner = store.claim(task_id, instance_id(orch), expected_epoch=meta["execution_epoch"])
    except RecoveryConflict:
        return orch.db.get_task(task_id).status
    token = None
    driver = None
    journal = journal_for(orch)
    try:
        driver = await asyncio.to_thread(orch._resolve_driver,task.device_serial)
        isolated = await isolate(driver,journal,owner)
        if not isolated.get("quiescent"):
            store.event(task_id,"blocked",reason=isolated.get("reason","old_input_chain_live"))
            orch.db.update_task(task_id,status=TaskStatus.RECOVERY_BLOCKED)
            return TaskStatus.RECOVERY_BLOCKED
        store.quiescent(owner,isolated["generation"])
        cached = getattr(orch,"_recovery_packages",{}).get(task_id)
        if cached is not None and (
                cached.capture_meta.get("recovery_driver_generation")!=isolated["generation"]
                or cached.capture_meta.get("recovery_control_revision")!=store.metadata(task_id)["control_revision"]):
            orch._recovery_packages.pop(task_id,None)
        execution = ExecutionContext(store,owner,task.state)
        execution.driver = DurableDriver(driver,execution,journal)
        token = current_execution.set(execution)
        return await orch._run_owned_task(task_id,**limits)
    except StaleOwner:
        return orch.db.get_task(task_id).status
    except (InfrastructureInterrupted,OSError,ConnectionError,RecoveryConflict) as exc:
        store.check(owner,allow_stopped=True)
        store.event(task_id,"detected",reason=str(exc)[:160])
        orch.db.update_task(task_id,status=TaskStatus.RECOVERING)
        return TaskStatus.RECOVERING
    finally:
        if token is not None:
            current_execution.reset(token)
        try:
            store.check(owner,allow_stopped=True)
            quiet = False
            try:
                if driver is not None:
                    closer = getattr(driver,"close_execution",None)
                    if closer:
                        await closer(owner.device_key,task_id,owner.epoch)
                    else:
                        journal.close_task(owner.device_key,task_id,owner.epoch)
                # Acquiring the input lock proves no operation is still sending.
                with journal._lock(owner.device_key):
                    quiet = journal._reap_children(owner.device_key)
                if getattr(driver,"close_execution",None):
                    drained = await isolate(driver,journal,owner,timeout=.1)
                    quiet = bool(drained.get("quiescent"))
                    await driver.close_execution(owner.device_key,task_id,owner.epoch)
            except Exception:
                quiet=False  # Keep quarantine; cleanup failure cannot mask the task result.
            store.release(owner,quiescent=quiet)
        except StaleOwner:
            pass


class RecoveryCoordinator:
    def __init__(self, orch, settings):
        self.orch,self.settings = orch,settings
        self.store = RecoveryStore(orch.db,orch.artifacts)
        self._locks = {}
        self._recovering = set()

    async def safety_cleanup(self,live_tasks):
        """Terminal/manual/disabled work still releases surviving input chains."""
        db = self.orch.db
        rows = [dict(row) for row in db._conn.execute("SELECT * FROM device_runtime WHERE task_id IS NOT NULL")]
        for device in rows:
            task_id = device["task_id"]
            lock = self._locks.setdefault(task_id,asyncio.Lock())
            if task_id in live_tasks or lock.locked():
                continue
            # Explicit API recovery is not necessarily in the scheduler's live
            # handles. Share its lock across cleanup's asynchronous isolation.
            async with lock:
                await self._cleanup_device(device)

    async def _cleanup_device(self,device):
        from shared.recovery_store import Ownership
        db = self.orch.db
        task_id = device["task_id"]
        meta = self.store.metadata(task_id)
        if meta["status"] not in TERMINAL and meta["recovery_policy"]=="safe_auto":
            return
        try:
            driver = await asyncio.to_thread(self.orch._resolve_driver,meta["device_serial"])
            fence = device["last_token"]+1
            with db.transaction():
                changed = db._conn.execute("UPDATE device_runtime SET last_token=?,quarantine=1 WHERE device_key=? AND last_token=?",
                                          (fence,device["device_key"],device["last_token"]))
                if not changed.rowcount:
                    return
            owner = Ownership(task_id,meta["execution_epoch"],"safety_cleanup",fence,device["device_key"])
            result = await isolate(driver,journal_for(self.orch),owner)
            if not result.get("quiescent"):
                return
            closer = getattr(driver,"close_execution",None)
            if closer:
                await closer(owner.device_key,task_id,owner.epoch)
            else:
                journal_for(self.orch).close_task(owner.device_key,task_id,owner.epoch)
            with db.transaction():
                db._conn.execute("UPDATE device_runtime SET task_id=NULL,epoch=NULL,quarantine=0 "
                                 "WHERE device_key=? AND last_token=?",(device["device_key"],fence))
                db._conn.execute("UPDATE tasks SET worker_token=NULL WHERE id=? AND execution_epoch=?",
                                 (task_id,owner.epoch))
        except Exception:
            return

    async def recover(self, task_id, *, explicit=False):
        lock = self._locks.setdefault(task_id,asyncio.Lock())
        joined = task_id in self._recovering
        epoch = self.store.metadata(task_id)["execution_epoch"]
        async with lock:
            # Scanner/API callers may have queued while one recovery was in
            # flight. They must not take ownership from its new continuation.
            if joined or self.store.metadata(task_id)["execution_epoch"] != epoch:
                return {"task_id":task_id,"status":self.orch.db.get_task(task_id).status.value,
                        "accepted":False,"reason":"joined_existing_recovery"}
            self._recovering.add(task_id)
            try:
                return await self._recover(task_id,explicit=explicit)
            finally:
                self._recovering.discard(task_id)

    async def _recover(self, task_id, *, explicit):
        db,store = self.orch.db,self.store
        meta = store.metadata(task_id)
        if meta["status"] in TERMINAL:
            return {"task_id":task_id,"status":meta["status"],"accepted":False}
        if meta["recovery_protocol_version"] != 1:
            return {"task_id":task_id,"status":meta["status"],"accepted":False,"reason":"legacy_recovery_unsupported"}
        if meta["recovery_policy"] == "disabled":
            return {"task_id":task_id,"status":meta["status"],"accepted":False,"reason":"crash_recovery_disabled"}
        if not explicit and meta["recovery_policy"] != "safe_auto":
            return {"task_id":task_id,"status":meta["status"],"accepted":False,"reason":"explicit_recovery_required"}
        if meta["status"] in {"paused","waiting_for_input"}:
            return {"task_id":task_id,"status":meta["status"],"accepted":False}
        projection = json.loads(meta["recovery_json"])
        config_changed=projection.get("configuration_fact")!=configuration_fact(self.settings)
        if (not explicit and projection.get("reason") in {"model_authorization_unavailable","model_configuration_unavailable","model_quota_unavailable"}
                and not config_changed):
            return {"task_id":task_id,"status":meta["status"],"accepted":False,"reason":projection["reason"]}
        now = time.time()
        attempt = projection.get("attempt_no",0)
        if not explicit and projection.get("next_retry_at",0) > now:
            return {"task_id":task_id,"status":meta["status"],"accepted":False}
        # New resource availability may unlock one bounded check after the burst.
        resource_fact = "available"
        driver = None
        try:
            driver = await asyncio.to_thread(self.orch._resolve_driver,meta["device_serial"])
            health = getattr(driver,"health",None)
            if health:
                status=await asyncio.wait_for(health(),5)
                if not status.get("ok",False):
                    resource_fact = "unavailable"
                elif (meta["device_serial"] and status.get("serial")
                      and status["serial"] not in {meta["device_serial"],meta["device_serial"].split("/",1)[-1]}):
                    resource_fact = "identity_mismatch"
        except Exception:
            resource_fact = "unavailable"
        repair_changed=config_changed and projection.get("reason") in {"model_authorization_unavailable","model_configuration_unavailable","model_quota_unavailable"}
        if not explicit and not repair_changed and attempt >= self.settings.task_recovery_max_auto_attempts and projection.get("resource_fact") == resource_fact:
            if meta["status"]!="recovery_blocked":
                db.update_task(task_id,status=TaskStatus.RECOVERY_BLOCKED)
                store.event(task_id,"blocked",reason="auto_recovery_attempts_exhausted",
                            last_recovery_reason=projection.get("reason"))
                meta["status"]="recovery_blocked"
            return {"task_id":task_id,"status":meta["status"],"accepted":False}
        if (not explicit and meta["status"]=="recovery_blocked" and projection.get("reason")=="device_unavailable"
                and projection.get("device_wait_deadline_at",float("inf"))<=now and resource_fact=="unavailable"):
            return {"task_id":task_id,"status":meta["status"],"accepted":False}
        try:
            owner = store.claim(task_id,instance_id(self.orch),expected_epoch=meta["execution_epoch"],takeover=True)
        except RecoveryConflict as exc:
            return {"task_id":task_id,"status":db.get_task(task_id).status.value,"accepted":False,"reason":str(exc)}
        db.update_task(task_id,status=TaskStatus.RECOVERING)
        first = projection.get("first_detected_at",now)
        attempt += 1
        store.event(task_id,"verifying_owner",attempt_no=attempt,first_detected_at=first,
                    fault_cycle_id=projection.get("fault_cycle_id",uuid.uuid4().hex),resource_fact=resource_fact)
        try:
            if resource_fact=="identity_mismatch":
                raise RecoveryConflict("device_identity_mismatch")
            if resource_fact == "unavailable":
                deadline = projection.get("device_wait_deadline_at",first+self.settings.task_recovery_device_wait_s)
                status = TaskStatus.RECOVERY_BLOCKED if now >= deadline else TaskStatus.WAITING_FOR_DEVICE
                db.update_task(task_id,status=status)
                store.event(task_id,"blocked" if now >= deadline else "waiting_for_device",reason="device_unavailable",
                            device_wait_deadline_at=deadline,attempt_no=max(0,attempt-1),next_retry_at=now+self.settings.task_recovery_scan_interval_s)
                return {"task_id":task_id,"status":status.value,"accepted":True}
            async with asyncio.timeout(self.settings.task_recovery_attempt_timeout_s):
                isolated = await isolate(driver,journal_for(self.orch),owner)
                if not isolated.get("quiescent"):
                    raise RecoveryConflict(isolated.get("reason","old_input_chain_live"))
                store.quiescent(owner,isolated["generation"])
                store.event(task_id,"validating_state")
                current = db.get_task(task_id).state
                if not meta["active_manifest_id"] or self.orch.artifacts is None:
                    raise RecoveryConflict("required_manifest_unavailable")
                snapshot = self._load_snapshot(task_id,meta)
                if snapshot.get("version") != 1:
                    raise RecoveryConflict("recovery_format_unsupported")
                state = AgentState.model_validate(snapshot["state"])
                if current:
                    state.role_invocation_count = max(current.role_invocation_count,state.role_invocation_count)
                    state.revisable.execution_count = max(current.revisable.execution_count,state.revisable.execution_count)
                self._verify_context(state,snapshot)
                store.event(task_id,"collecting_results")
                pending = store.operations(task_id,pending=True)
                for operation in pending:
                    if operation["result_json"]:
                        continue
                    query = getattr(driver,"get_operation",None)
                    try:
                        receipt = await asyncio.wait_for(query(operation["operation_id"]),2) if query else journal_for(self.orch).get(operation["operation_id"])
                    except Exception:
                        receipt = None  # Historical lookup failure is unknown, not a new write barrier.
                    store.check(owner,allow_stopped=True)
                    if isinstance(receipt,dict) and receipt.get("result"):
                        store.receipt(operation["operation_id"],receipt["result"],generation=receipt.get("generation","local"))
                    else:
                        store.record_unknown(operation["operation_id"],evidence=receipt)
                store.event(task_id,"recording_results")
                previous = state.recovery.get("operations",[])
                rows = [row for row in store.operations(task_id) if row["operation_id"] in {item["operation_id"] for item in pending}]
                handoff = [{"previous_operation_id":row["operation_id"],"action":json.loads(row["args_json"]),
                            "execution_result":"unknown" if row["result_state"]=="unknown_recorded" else "known",
                            "result":json.loads(row["result_json"]),"original_observation_id":row["basis_observation_id"]}
                           for row in rows]
                for item,row in zip(handoff,rows):
                    plan=json.loads(row["intent_json"]).get("compound_plan",[])
                    if plan:
                        siblings=[op for op in rows if op["parent_id"]==row["parent_id"]]
                        issued={op["ordinal"] for op in siblings}
                        item["not_started_subactions"]=[{"ordinal":i,"action":action,"execution_result":"not_started"}
                                                        for i,action in enumerate(plan) if i not in issued]
                state.recovery = {"interruption_gap":True,"operations":handoff or previous,
                                  "observation_source":"recovery_current","recovered_at":time.time()}
                dependencies=getattr(db,"recovery_dependencies",{})
                if snapshot.get("dependencies",{})!=dependencies:
                    state.recovery["configuration_changes"]={"previous":snapshot.get("dependencies",{}),"current":dependencies}
                self._close_steps(task_id,state)
                store.event(task_id,"restoring_context")
                state.revisable.delivered_context = {}
                state.revisable.last_review_signature = ""
                latest = store.metadata(task_id)
                desired = latest["desired_state"]
                if desired == "cancel" or latest["status"] in TERMINAL:
                    db.update_task(task_id,status=TaskStatus.CANCELLED,state=state)
                    target = TaskStatus.CANCELLED
                elif desired == "pause":
                    self.orch._prepare_checkpoint(task_id,state)
                    db.update_task(task_id,status=TaskStatus.PAUSED,state=state)
                    target = TaskStatus.PAUSED
                else:
                    store.event(task_id,"refreshing_device")
                    if not state.revisable.limits.expired():
                        from agent.action_observation import ActionObservationTransaction
                        package = await ActionObservationTransaction(driver,self.orch.observation_builder).observe_current(
                            source="recovery_current",attach_image=True)
                        store.check(owner,allow_stopped=True)
                        if not package.accepted:
                            raise RecoveryConflict("current_observation_unavailable")
                        self.orch._persist_observation(package)
                        package.capture_meta.update(recovery_driver_generation=isolated["generation"],
                                                    recovery_control_revision=meta["control_revision"])
                        state.recovery["current_observation_id"] = package.observation_id
                        from agent.revisable.store import TaskStore
                        TaskStore(db,self.orch.artifacts,task_id).observe(package,state)
                        if not hasattr(self.orch,"_recovery_packages"):
                            self.orch._recovery_packages = {}
                        self.orch._recovery_packages[task_id] = package
                    # Control intent is re-read after asynchronous evidence work.
                    latest = store.metadata(task_id)
                    if latest["desired_state"] != "run" or latest["control_revision"] != meta["control_revision"]:
                        raise RecoveryConflict("control_changed_during_recovery")
                    db.update_task(task_id,status=TaskStatus.QUEUED,state=state)
                    target = TaskStatus.QUEUED
                with db.transaction():
                    store.publish_manifest(task_id,state=state)
                    store.event(task_id,"ready",reason=None,next_retry_at=0,
                                unknown_operations=[item["previous_operation_id"] for item in state.recovery["operations"] if item["execution_result"]=="unknown"])
                return {"task_id":task_id,"status":target.value,"accepted":True}
        except StaleOwner:
            return {"task_id":task_id,"status":db.get_task(task_id).status.value,"accepted":False,
                    "reason":"recovery_owner_retired"}
        except (Exception,) as exc:
            try:
                store.check(owner,allow_stopped=True)
            except StaleOwner:
                return {"task_id":task_id,"status":db.get_task(task_id).status.value,"accepted":False,
                        "reason":"recovery_owner_retired"}
            latest = store.metadata(task_id)
            if latest["status"] not in TERMINAL:
                db.update_task(task_id,status=TaskStatus.FAILED if isinstance(exc,RecoveryUnrecoverable) else TaskStatus.RECOVERY_BLOCKED,
                               failure_reason=str(exc)[:160] if isinstance(exc,RecoveryUnrecoverable) else None)
            backoffs = self.settings.task_recovery_backoff_s
            delay = backoffs[min(attempt-1,len(backoffs)-1)] if backoffs else self.settings.task_recovery_scan_interval_s
            store.event(task_id,"unrecoverable" if isinstance(exc,RecoveryUnrecoverable) else "blocked",reason=str(exc)[:160],next_retry_at=time.time()+delay)
            return {"task_id":task_id,"status":db.get_task(task_id).status.value,"accepted":True,"reason":str(exc)[:160]}
        finally:
            try:
                # Claim remains quarantined if isolation failed; only its owner is retired.
                device = db._conn.execute("SELECT quarantine FROM device_runtime WHERE device_key=?", (owner.device_key,)).fetchone()
                store.release(owner,quiescent=bool(device and not device[0]))
            except StaleOwner:
                pass

    def _load_snapshot(self,task_id,meta):
        artifacts=self.orch.artifacts
        try:
            snapshot=json.loads(artifacts.verify(meta["active_manifest_id"]))
            self._verify_bodies(AgentState.model_validate(snapshot["state"]),snapshot)
            return snapshot
        except (OSError,ValueError,KeyError,RecoveryConflict) as original:
            # A complete DB post-state (or the later T1 state) plus operation
            # facts is a valid recovery point; never roll execution backwards.
            pending=self.store.operations(task_id,pending=True)
            state=AgentState.model_validate(json.loads(pending[-1]["intent_json"])["state"]) if pending else self.orch.db.get_task(task_id).state
            if state is None:
                raise RecoveryUnrecoverable("required_state_unavailable") from original
            # Search history only for missing current records, only on fallback.
            refs=set(state.revisable.dialogue_refs)
            missing={key for key in refs if not self.orch.db.get_agent_record(task_id,"dialogue",key)}
            if missing:
                candidates=self.orch.db._conn.execute("SELECT payload_json FROM agent_records WHERE task_id=? AND kind='recovery_manifest' ORDER BY rowid DESC LIMIT 64",(task_id,)).fetchall()
                for candidate in candidates:
                    try:
                        saved=json.loads(artifacts.verify(json.loads(candidate[0])["ref"]))
                    except (OSError,ValueError,KeyError):
                        continue
                    for record in saved.get("records",[]):
                        if record["kind"]=="dialogue" and record["record_key"] in missing:
                            with self.orch.db.transaction():
                                self.orch.db._conn.execute("INSERT OR IGNORE INTO agent_records VALUES (?,?,?,?,?)",
                                    (record["task_id"],record["kind"],record["record_key"],record["version"],record["payload_json"]))
                            missing.remove(record["record_key"])
                    if not missing:
                        break
            try:
                snapshot=self.store.context_snapshot(task_id,state=state)
                self._verify_bodies(state,snapshot)
            except (FileNotFoundError,ValueError,KeyError,RecoveryConflict) as exc:
                raise RecoveryUnrecoverable("required_context_unrecoverable:"+str(exc)[:100]) from exc
            self.store.event(task_id,"validating_state",fallback="complete_post_state_and_operation_facts")
            return snapshot

    def _verify_bodies(self,state,snapshot):
        for ref in state.revisable.dialogue_refs:
            saved=next((item for item in snapshot["records"] if item["kind"]=="dialogue" and item["record_key"]==ref),None)
            if saved is None:
                raise RecoveryConflict("required_dialogue_unavailable")
            self.orch.artifacts.verify(json.loads(saved["payload_json"])["ref"])

    def _verify_context(self,state,snapshot):
        # Immutable complete bodies can restore absent records; newer durable
        # versions/control facts are retained rather than rolled back.
        with self.orch.db.transaction():
            for record in snapshot["records"]:
                json.loads(record["payload_json"])
                self.orch.db._conn.execute("INSERT OR IGNORE INTO agent_records VALUES (?,?,?,?,?)",
                    (record["task_id"],record["kind"],record["record_key"],record["version"],record["payload_json"]))
        self._verify_bodies(state,snapshot)

    def _close_steps(self, task_id, state):
        db,store = self.orch.db,self.store
        pending = store.operations(task_id,pending=True)
        for seq in sorted({row["step_seq"] for row in pending}):
            operations = [row for row in pending if row["step_seq"]==seq]
            intent = json.loads(operations[-1]["intent_json"])
            if not any(row["kind"]=="device_action" for row in operations) and intent.get("role")!="executor":
                store.finalize_tools(task_id)
                with db.transaction():
                    db.update_task(task_id,state=state)
                    store.publish_manifest(task_id,state=state)
                continue
            messages = (json.loads(self.orch.artifacts.verify(intent["messages_ref"]))
                        if intent.get("messages_ref") else list(intent.get("messages",[])))
            grouped={}
            for row in operations:
                if (row["call_id"] and row["invocation_id"]==operations[-1]["invocation_id"]
                        and json.loads(row["intent_json"]).get("round_seq")==intent.get("round_seq")):
                    grouped.setdefault(row["call_id"],[]).append({"operation_id":row["operation_id"],"result":json.loads(row["result_json"])})
            results={call_id:children[0]["result"] if len(children)==1 else {"children":children,"interruption_gap":True}
                     for call_id,children in grouped.items()}
            # The terminal submission may already have its valid "accepted"
            # tool reply saved before physical dispatch. Its physical outcome
            # is a separate user runtime event, not a second tool reply.
            saved_call_ids=set()
            for ref in state.revisable.dialogue_refs:
                row=db.get_agent_record(task_id,"dialogue",ref)
                if row and row["payload"]["step"]==seq:
                    for message in json.loads(self.orch.artifacts.verify(row["payload"]["ref"])):
                        if message.get("role")=="tool":
                            saved_call_ids.add(message.get("tool_call_id"))
            if grouped and set(grouped)<=saved_call_ids:
                messages=[]
            else:
                outstanding={}
                for message in messages:
                    for call in message.get("tool_calls",[]):
                        outstanding[call["id"]]=call
                    if message.get("role")=="tool":
                        outstanding.pop(message.get("tool_call_id"),None)
                for call_id in outstanding:
                    messages.append({"role":"tool","tool_call_id":call_id,
                                     "content":json.dumps(results.get(call_id,{"execution_result":"unknown","reason":"interrupted_call"}),ensure_ascii=False)})
            messages.append({"role":"user","content":json.dumps({"action_result":results,"step":seq,"interruption_gap":True},ensure_ascii=False)})
            units=sum(store.charge_units(row,db._conn.execute(
                "SELECT reserved FROM budget_ledger WHERE task_id=? AND operation_id=?",(task_id,row["operation_id"])).fetchone()[0])
                      for row in operations)
            state.revisable.execution_count += units
            state.step_number = max(state.step_number,seq+1)
            with db.transaction():
                if messages and intent.get("role")=="executor":
                    from agent.revisable.store import TaskStore
                    TaskStore(db,self.orch.artifacts,task_id).save_dialogue(messages,state,step_seq=seq)
                report = {"action":json.loads(operations[0]["args_json"]) if operations[0]["kind"]=="device_action" else None,
                          "summary":"Interrupted attempt; restored from durable execution facts",
                          "success":False,"reason":"interruption_gap","basis_observation_id":operations[0]["basis_observation_id"],
                          "recovery_operations":[{"operation_id":row["operation_id"],"result":json.loads(row["result_json"])} for row in operations]}
                db.add_step_and_update_state(task_id,state.revisable.stage_id,seq,report,state)
