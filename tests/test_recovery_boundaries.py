"""Recovery must preserve ownership and facts without adding semantic gates."""

import asyncio
import json
import sqlite3
import time

import pytest

from agent.revisable.store import TaskStore
from agent.revisable.tools import WriteNote,save_note
from agent.tool_registry import AgentRole,AgentToolRegistry,AgentToolSpec,AgentToolResult,ToolCategory,ToolExecutionContext
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database,SCHEMA_SQL
from shared.recovery_context import ExecutionContext,current_execution
from shared.recovery_runtime import RecoveryCoordinator
from shared.recovery_store import RecoveryStore,RecoveryConflict
from shared.schemas import Action,TaskStatus
from tests.test_crash_safe_recovery import setup,prepare,fake_orch


def test_old_database_backup_is_consistent_and_old_writer_cannot_mutate_new_schema(tmp_path):
    path=tmp_path/"test.db"
    with sqlite3.connect(path) as legacy:
        legacy.executescript(SCHEMA_SQL)
        legacy.execute("INSERT INTO tasks(id,instruction,status,created_at,updated_at) VALUES ('legacy','kept','paused',1,1)")
    db=Database(path)
    assert db.get_task("legacy").instruction=="kept"
    backup=path.with_suffix(".pre-recovery-v1.backup")
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT instruction FROM tasks WHERE id='legacy'").fetchone()[0]=="kept"
    with sqlite3.connect(path) as unsupported:
        with pytest.raises(sqlite3.OperationalError,match="clickclick_schema_writer"):
            unsupported.execute("UPDATE tasks SET instruction='lost' WHERE id='legacy'")
    assert db.get_task("legacy").instruction=="kept"


def test_pin_is_visible_to_another_store_and_current_refs_survive_retention(tmp_path):
    db,artifacts,_,task,store=setup(tmp_path)
    other=ArtifactStore(artifacts.root)
    ref=artifacts.save_json("context",{"body":"pending"})
    assert other.prune("context",max_files=0)==0
    artifacts.release_pins({ref})
    assert other.prune("context",max_files=0)==1
    manifest=store.metadata(task.id)["active_manifest_id"]
    assert artifacts.prune("recovery",max_files=0)==0
    assert artifacts.resolve(manifest).exists()


def test_local_note_append_is_once_per_call_but_new_call_can_repeat(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    owner=store.claim(task.id,"test")
    context=ExecutionContext(store,owner,state,invocation_id="invocation",call_id="note-call")
    token=current_execution.set(context)
    try:
        notes=TaskStore(db,artifacts,task.id)
        args=WriteNote(note_key="n",content="one",append=True)
        first=save_note(notes,args,state)
        assert save_note(notes,args,state).data==first.data
        assert notes.get("note","n")["payload"]["content"]=="one"
        context.call_id="new-model-note-call"
        save_note(notes,args,state)
        assert notes.get("note","n")["payload"]["content"]=="one\none"
    finally:
        current_execution.reset(token)


def test_paired_notes_and_t1_reservation_rollback_together(tmp_path,monkeypatch):
    db,artifacts,state,task,store=setup(tmp_path)
    owner=store.claim(task.id,"test")
    context=ExecutionContext(store,owner,state,call_id="paired")
    notes=TaskStore(db,artifacts,task.id)
    context.local_write=lambda:save_note(notes,WriteNote(note_key="n",content="saved with intent"),state)
    original=artifacts.save_json
    def broken(kind,payload):
        if kind=="recovery":
            raise OSError("required state publication failed")
        return original(kind,payload)
    monkeypatch.setattr(artifacts,"save_json",broken)
    token=current_execution.set(context)
    try:
        with pytest.raises(OSError):
            context.prepare_action(Action(type="home"))
    finally:
        current_execution.reset(token)
    assert not notes.records("note")
    assert not store.operations(task.id)
    assert not db._conn.execute("SELECT * FROM budget_ledger").fetchall()


async def test_missing_external_metadata_does_not_block_first_call_and_unknown_does_not_lock_next(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    owner=store.claim(task.id,"test")
    context=ExecutionContext(store,owner,state,call_id="external-call")
    context.messages=[{"role":"assistant","tool_calls":[{"id":"external-call","type":"function","function":{"name":"external","arguments":"{}"}}]}]
    registry=AgentToolRegistry()
    effects=[]
    async def external(args,ctx):
        effects.append(args)
        if len(effects)==1:
            raise ConnectionError("response lost after effect")
        return AgentToolResult(summary="done")
    registry.register(AgentToolSpec(name="external",description="test",category=ToolCategory.KNOWLEDGE,
                                   roles=(AgentRole.EXECUTOR,),parameters={"type":"object"}),external)
    token=current_execution.set(context)
    try:
        result=await registry.execute("external",{},ToolExecutionContext(AgentRole.EXECUTOR,"invocation"))
        assert result.data["execution_result"]=="unknown"
        context.call_id="new-model-call"
        assert (await registry.execute("external",{},ToolExecutionContext(AgentRole.EXECUTOR,"new-invocation"))).summary=="done"
    finally:
        current_execution.reset(token)
    assert len(effects)==len(store.operations(task.id))==2
    assert store.operations(task.id)[0]["result_state"]=="unknown_recorded"


async def test_historical_result_lookup_failure_is_unknown_not_a_blocker(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    async def unavailable(_):
        raise ConnectionError("old result service unavailable")
    driver.get_operation=unavailable
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="queued",result
    assert db.get_task(task.id).state.recovery["operations"][0]["execution_result"]=="unknown"


async def test_complete_manifest_restores_current_note_and_does_not_need_unused_history(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    TaskStore(db,artifacts,task.id).put("note","important",{"title":"Important","content":"Exact retained body"})
    prepare(store,state,task)
    with db.transaction():
        db._conn.execute("DELETE FROM agent_records WHERE task_id=? AND kind='note'",(task.id,))
    unused=artifacts.save_json("old_diagnostic",{"not":"required"})
    artifacts.resolve(unused).unlink()
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="queued",result
    assert TaskStore(db,artifacts,task.id).get("note","important")["payload"]["content"]=="Exact retained body"


async def test_pause_intent_closes_unknown_without_model_or_observation(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    store.control(task.id,"pause")
    orch=fake_orch(db,artifacts,tmp_path)
    def checkpoint(task_id,state):
        state.revisable.checkpoint_resumable=True
    orch._prepare_checkpoint=checkpoint
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="paused",result
    assert not hasattr(orch,"_recovery_packages")
    assert db.get_task(task.id).state.step_number==1
    assert store.operations(task.id)[0]["result_state"]=="unknown_recorded"


async def test_exhausted_device_wait_does_not_restart_and_new_availability_unblocks(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    available=False
    async def health():
        return {"ok":available}
    driver.health=health
    settings=Settings(_env_file=None,data_dir=tmp_path,task_recovery_device_wait_s=.01,task_recovery_scan_interval_s=.01)
    recovery=RecoveryCoordinator(orch,settings)
    assert (await recovery.recover(task.id))["status"]=="waiting_for_device"
    deadline=json.loads(store.metadata(task.id)["recovery_json"])["device_wait_deadline_at"]
    await asyncio.sleep(.025)
    assert (await recovery.recover(task.id))["status"]=="recovery_blocked"
    assert not (await recovery.recover(task.id))["accepted"]
    assert json.loads(store.metadata(task.id)["recovery_json"])["device_wait_deadline_at"]==deadline
    available=True
    await asyncio.sleep(.025)
    assert (await recovery.recover(task.id))["status"]=="queued"


def test_late_material_fact_invalidates_only_decisions_using_that_operation(tmp_path):
    from shared.task_control import TaskSteered
    db,artifacts,state,task,store=setup(tmp_path)
    owner=prepare(store,state,task)
    store.record_unknown("original")
    state.step_number=1
    db.add_step_and_update_state(task.id,None,0,{},state)
    state.recovery={"operations":[{"previous_operation_id":"original"}]}
    context=ExecutionContext(store,owner,state)
    context.admit_facts()
    store.receipt("original",{"success":True,"message":"late fact"},generation="late")
    with pytest.raises(TaskSteered):
        context.check_decision()
    context.admit_facts()
    store.receipt("original",{"success":True,"message":"late fact"},generation="duplicate-source",sequence=2)
    context.check_decision()


@pytest.mark.parametrize("name,value",[("task_recovery_backoff_s",[-1]),("task_recovery_backoff_s",[]),
                                      ("task_recovery_device_wait_s",0),("task_recovery_max_auto_attempts",0)])
def test_invalid_recovery_configuration_is_rejected(name,value):
    with pytest.raises(ValueError):
        Settings(_env_file=None,**{name:value})


async def test_hard_exit_stops_owned_input_child_and_recovery_does_not_reuse_pid_blindly(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from driver.processes import background_process_kwargs
    worker=Path(__file__).with_name("recovery_process_fixture.py")
    process=subprocess.Popen([sys.executable,"-X","utf8",str(worker),str(tmp_path),"input_child"],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,**background_process_kwargs())
    try:
        deadline=time.monotonic()+20
        while not (tmp_path/"gate.json").exists():
            assert process.poll() is None,process.communicate()
            assert time.monotonic()<deadline
            await asyncio.sleep(.025)
        gate=json.loads((tmp_path/"gate.json").read_text())
        process.kill()
        await asyncio.to_thread(process.wait,10)
        db=Database(tmp_path/"test.db")
        artifacts=ArtifactStore(tmp_path/"artifacts")
        db.artifacts=artifacts
        orch=fake_orch(db,artifacts,tmp_path)
        result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(gate["task_id"])
        assert result["status"]=="queued",result
        before=(tmp_path/"input-heartbeat").read_text()
        await asyncio.sleep(.15)
        assert (tmp_path/"input-heartbeat").read_text()==before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        process.communicate()
