"""Adversarial recovery boundaries beyond the basic hard-exit matrix."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from httpx import ASGITransport

from agent.driver_client import DriverClient
from agent.revisable.store import TaskStore
from driver.execution_journal import ExecutionJournal
from driver.fixture import FixtureDriver
from shared.config import Settings
from shared.db import Database
from shared.recovery_runtime import RecoveryCoordinator
from shared.recovery_store import RecoveryConflict,RecoveryStore
from shared.schemas import ActionResult,TaskStatus
from tests.test_crash_safe_recovery import setup,prepare,fake_orch


def test_concurrent_takeover_has_exactly_one_epoch_winner(tmp_path):
    db,_,_,task,store=setup(tmp_path)
    owner=store.claim(task.id,"old")
    barrier=threading.Barrier(2)
    def takeover(index):
        own_db=Database(db.path)
        try:
            barrier.wait(timeout=5)
            return RecoveryStore(own_db).claim(task.id,str(index),expected_epoch=owner.epoch,takeover=True)
        except RecoveryConflict:
            return None
        finally:
            own_db.close()
    with ThreadPoolExecutor(2) as pool:
        winners=list(pool.map(takeover,range(2)))
    assert sum(value is not None for value in winners)==1


def test_repeated_migration_after_interruption_keeps_records(tmp_path):
    db,_,_,task,_=setup(tmp_path)
    db._conn.execute("PRAGMA user_version=0")
    db.close()
    reopened=Database(tmp_path/"test.db")
    assert reopened.get_task(task.id)
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0]==1
    assert len(reopened.list_tasks())==1


def test_same_body_publications_have_independent_pending_pins(tmp_path):
    _,artifacts,_,_,_=setup(tmp_path)
    ref=artifacts.save_json("pending",{"same":"body"})
    assert artifacts.save_json("pending",{"same":"body"})==ref
    artifacts.release_pins({ref})
    assert artifacts.prune("pending",max_files=0)==0
    artifacts.release_pins({ref})
    assert artifacts.prune("pending",max_files=0)==1


def test_embedded_current_dialogue_is_gc_protected_without_db_record(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    TaskStore(db,artifacts,task.id).save_dialogue([{"role":"user","content":"required"}],state)
    row=db.latest_agent_record(task.id,"dialogue")
    with db.transaction():
        store.publish_manifest(task.id,state=state)
        db._conn.execute("DELETE FROM agent_records WHERE kind='dialogue'")
    artifacts.release_pins({row["payload"]["ref"]})
    assert artifacts.prune("dialogue",max_files=0)==0
    assert artifacts.verify(row["payload"]["ref"])


async def test_current_manifest_corruption_uses_complete_t1_state_not_old_progress(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    state.revisable.feedback="latest T1 state"
    prepare(store,state,task)
    artifacts.resolve(store.metadata(task.id)["active_manifest_id"]).write_bytes(b"truncated")
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="queued",result
    assert db.get_task(task.id).state.revisable.feedback=="latest T1 state"
    assert db.get_task(task.id).state.step_number==1
    assert any(row["payload"].get("fallback") for row in db.list_agent_records(task.id,"recovery_event"))


async def test_lost_required_dialogue_is_explicit_unrecoverable_without_new_dispatch(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    TaskStore(db,artifacts,task.id).save_dialogue([{"role":"user","content":"required"}],state)
    prepare(store,state,task)
    artifacts.resolve(db.latest_agent_record(task.id,"dialogue")["payload"]["ref"]).unlink()
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="failed",result
    assert "required_context_unrecoverable" in result["reason"]
    assert not orch._resolve_driver("fixture").actions


async def test_rpc_inflight_retry_queries_and_fence_remain_responsive(tmp_path,monkeypatch):
    import driver.rpc_server as rpc
    monkeypatch.setattr("shared.config.get_settings",lambda:Settings(_env_file=None,data_dir=tmp_path))
    backing=FixtureDriver()
    started,finish=asyncio.Event(),asyncio.Event()
    effects=[]
    async def act(action):
        effects.append(action)
        started.set()
        await finish.wait()
        return ActionResult(success=True,message="done")
    backing.act=act
    app=rpc.create_driver_app(backing)
    remote=DriverClient("http://fixture",serial="fixture",transport=ASGITransport(app=app))
    assert (await remote.recovery_capabilities())["protocol_version"]==1
    await remote.fence_device("fixture",1,"task",1)
    envelope={"operation_id":"same","device_key":"fixture","device_fence":1,"task_id":"task","epoch":1,"action":{"type":"home"}}
    first=asyncio.create_task(remote.act_durable(envelope))
    await asyncio.wait_for(started.wait(),2)
    retry=await asyncio.wait_for(remote.act_durable(envelope),1)
    assert retry.detail["execution_result"]=="unknown"
    assert (await remote.get_operation("same"))["status"]=="accepted"
    assert not (await remote.fence_device("fixture",2,"task",2,timeout=.02))["quiescent"]
    finish.set()
    assert (await first).success
    await remote.fence_device("fixture",2,"task",2)
    envelope.update(epoch=2,device_fence=2)
    assert (await remote.act_durable(envelope)).success
    assert len(effects)==1


async def test_compound_prefix_is_closed_with_not_started_tail_and_no_replay(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    owner=store.claim(task.id,"old")
    store.prepare(owner,state,operation_id="prefix",kind="device_action",args={"type":"home"},invocation_id="inv",
                  call_id="call",parent_id="compound",ordinal=0,units=1,
                  intent={"role":"executor","compound_plan":[{"type":"home"},{"type":"back"}]})
    store.receipt("prefix",{"success":True,"message":"done","detail":{}})
    db.update_task(task.id,status=TaskStatus.RECOVERING)
    orch=fake_orch(db,artifacts,tmp_path)
    assert (await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id))["status"]=="queued"
    current=db.get_task(task.id).state
    assert current.recovery["operations"][0]["not_started_subactions"]==[
        {"ordinal":1,"action":{"type":"back"},"execution_result":"not_started"}]
    assert current.revisable.execution_count==1
    assert not orch._resolve_driver("fixture").actions


async def test_wrong_device_identity_blocks_before_observation(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    async def wrong():
        return {"ok":True,"serial":"other-device"}
    orch._resolve_driver("fixture").health=wrong
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["reason"]=="device_identity_mismatch"
    assert not hasattr(orch,"_recovery_packages")


async def test_expired_task_mechanically_closes_unknown_without_observation(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    state.revisable.limits.deadline_at=1
    with db.transaction():
        db.update_task(task.id,state=state)
        store.publish_manifest(task.id,state=state)
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="queued",result
    assert not hasattr(orch,"_recovery_packages")
    assert db.get_task(task.id).state.revisable.limits.deadline_at==1


def test_legacy_duplicate_sequences_only_restrict_that_task(tmp_path):
    db=Database(tmp_path/"test.db")
    bad=db.create_task("legacy conflict",device_serial="bad")
    good=db.create_task("separate task",device_serial="good")
    db.add_step(bad.id,None,0,{"first":True})
    db.add_step(bad.id,None,0,{"second":True})
    artifacts=__import__("shared.artifacts",fromlist=["ArtifactStore"]).ArtifactStore(tmp_path/"artifacts")
    store=RecoveryStore(db,artifacts)
    with pytest.raises(RecoveryConflict,match="legacy_step_identity_conflict"):
        store.enable(bad.id)
    store.enable(good.id)
    assert len(db.list_steps(bad.id))==2
    assert store.metadata(good.id)["recovery_protocol_version"]==1


async def test_only_original_zero_unit_receipt_releases_reservation(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    store.receipt("original",{"success":False,"message":"not dispatched","detail":{"device_action_units":0}})
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="queued"
    assert db.get_task(task.id).state.revisable.execution_count==0
    assert dict(db._conn.execute("SELECT * FROM budget_ledger").fetchone())["settled"]==0
    assert db.get_agent_record(task.id,"budget_settlement","original")["payload"]["released"]==1


async def test_explicit_recovery_waits_for_inflight_safety_cleanup(tmp_path,monkeypatch):
    import shared.recovery_runtime as runtime
    db,artifacts,state,task,store=setup(tmp_path,policy="manual")
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    original=runtime.isolate
    isolated,proceed=asyncio.Event(),asyncio.Event()
    async def held(*args,**kwargs):
        if args[2].token=="safety_cleanup":
            isolated.set()
            await proceed.wait()
        return await original(*args,**kwargs)
    monkeypatch.setattr(runtime,"isolate",held)
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    cleanup=asyncio.create_task(coordinator.safety_cleanup(set()))
    await asyncio.wait_for(isolated.wait(),5)
    epoch=store.metadata(task.id)["execution_epoch"]
    recovery=asyncio.create_task(coordinator.recover(task.id,explicit=True))
    try:
        await asyncio.sleep(.05)
        assert not recovery.done()
        assert store.metadata(task.id)["execution_epoch"]==epoch
    finally:
        proceed.set()
        await cleanup
    assert (await recovery)["status"]=="queued"


@pytest.mark.parametrize("fault_point",["observation","observation_error","receipt","receipt_error"])
async def test_retired_recovery_cannot_publish_observation_or_ready(tmp_path,fault_point):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    capture=driver.get_frame
    observed,proceed=asyncio.Event(),asyncio.Event()
    async def held(*_):
        observed.set()
        await proceed.wait()
        if fault_point.endswith("error"):
            raise OSError("old asynchronous operation failed")
        if fault_point=="receipt":
            return None
        return await capture()
    if fault_point.startswith("receipt"):
        driver.get_operation=held
    else:
        driver.get_frame=held
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    recovery=asyncio.create_task(coordinator.recover(task.id))
    await asyncio.wait_for(observed.wait(),5)
    winner=store.claim(task.id,"new owner",takeover=True)
    before=db.get_task(task.id).state.model_dump(mode="json")
    proceed.set()
    result=await recovery
    assert not result["accepted"] and result["reason"]=="recovery_owner_retired"
    assert store.metadata(task.id)["worker_token"]==winner.token
    assert db.get_task(task.id).state.model_dump(mode="json")==before
    assert not getattr(orch,"_recovery_packages",{})
    assert store.projection(task.id)["phase"]!="ready"


async def test_terminal_cancel_keeps_quarantine_until_scoped_safety_cleanup(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    owner=prepare(store,state,task)
    db.update_task(task.id,status=TaskStatus.CANCELLED)
    store.control(task.id,"cancel")
    assert db.busy_serials()=={"fixture":task.id}
    orch=fake_orch(db,artifacts,tmp_path)
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    await coordinator.safety_cleanup(set())
    assert db.get_task(task.id).status==TaskStatus.CANCELLED
    assert not db.busy_serials()
    assert store.operations(task.id)[0]["status"]=="dispatching"  # Retain uncertainty; never revive.


def test_concurrent_atomic_publication_and_gc_keep_all_pending_bodies(tmp_path):
    from shared.artifacts import ArtifactStore
    stores=[ArtifactStore(tmp_path/"artifacts") for _ in range(2)]
    def publish(index):
        return [stores[index].save_json("race",{"writer":index,"seq":seq}) for seq in range(10)]
    with ThreadPoolExecutor(3) as pool:
        writers=[pool.submit(publish,index) for index in range(2)]
        removed=pool.submit(stores[0].prune,"race",max_files=0)
        refs=[ref for writer in writers for ref in writer.result()]
        assert removed.result()==0
    assert all(stores[0].verify(ref) for ref in refs)


def test_a_later_execution_failure_starts_a_new_fault_cycle_but_retries_do_not(tmp_path):
    db,_,state,task,store=setup(tmp_path)
    store.event(task.id,"blocked",attempt_no=3,fault_cycle_id="old",device_wait_deadline_at=1)
    store.event(task.id,"detected",reason="same unfinished recovery")
    assert store.projection(task.id)["attempt_no"]==3
    store.event(task.id,"ready")
    store.event(task.id,"detected",reason="ready without execution progress")
    assert store.projection(task.id)["attempt_no"]==3
    store.event(task.id,"ready")
    state.step_number=1
    db.add_step_and_update_state(task.id,None,0,{"success":True},state)
    store.event(task.id,"detected",reason="later execution interruption")
    assert "attempt_no" not in store.projection(task.id)
    assert "device_wait_deadline_at" not in store.projection(task.id)


def test_optional_diagnostic_failure_does_not_block_canonical_t1(tmp_path,monkeypatch):
    from agent.traces import TraceWriter
    db,artifacts,state,task,store=setup(tmp_path)
    original=artifacts.save_json
    def save(kind,payload):
        if kind=="llm":
            raise OSError("diagnostic volume unavailable")
        return original(kind,payload)
    monkeypatch.setattr(artifacts,"save_json",save)
    trace=TraceWriter(db,artifacts).write(task.id,kind="llm",payload={"text":"x"*200000})
    assert trace.message=="diagnostic_trace_unavailable"
    prepare(store,state,task)
    assert store.operations(task.id)[0]["status"]=="dispatching"


@pytest.mark.parametrize("boundary",["file_publish","file_published"])
async def test_hard_exit_during_file_publication_cannot_activate_partial_state(tmp_path,boundary):
    import subprocess
    import sys
    import time
    from pathlib import Path
    from driver.processes import background_process_kwargs
    from shared.artifacts import ArtifactStore
    process=subprocess.Popen([sys.executable,str(Path(__file__).with_name("recovery_process_fixture.py")),str(tmp_path),boundary],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,**background_process_kwargs())
    try:
        deadline=time.monotonic()+20
        while not (tmp_path/"gate.json").exists():
            assert process.poll() is None,process.communicate()
            assert time.monotonic()<deadline
            await asyncio.sleep(.025)
        task_id=json.loads((tmp_path/"gate.json").read_text())["task_id"]
        process.kill()
        process.wait(timeout=5)
        db=Database(tmp_path/"test.db")
        artifacts=ArtifactStore(tmp_path/"artifacts")
        db.artifacts=artifacts
        store=RecoveryStore(db,artifacts)
        active=store.metadata(task_id)["active_manifest_id"]
        assert json.loads(artifacts.verify(active))["state"]["revisable"]["feedback"]==""
        orch=fake_orch(db,artifacts,tmp_path)
        result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task_id)
        assert result["status"]=="queued",result
        assert db.get_task(task_id).state.revisable.feedback==""
        assert not store.operations(task_id)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


async def test_ready_generation_change_drops_cached_observation_before_handoff(tmp_path):
    from types import SimpleNamespace
    from shared.recovery_runtime import run_owned_task
    db,artifacts,state,task,store=setup(tmp_path)
    orch=fake_orch(db,artifacts,tmp_path)
    orch._recovery_packages={task.id:SimpleNamespace(capture_meta={"recovery_driver_generation":"retired", "recovery_control_revision":0})}
    async def run(task_id,**kwargs):
        assert task_id not in orch._recovery_packages
        return TaskStatus.RUNNING
    orch._run_owned_task=run
    assert await run_owned_task(orch,task.id)==TaskStatus.RUNNING


@pytest.mark.parametrize("dimension",["model_calls","task_time"])
async def test_exhausted_recovered_budget_never_starts_a_model_request(tmp_path,monkeypatch,dimension):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    settings=Settings(_env_file=None,data_dir=tmp_path,default_model="test")
    runtime=create_orchestrator(db,TraceWriter(db,artifacts),settings=settings,artifacts=artifacts,driver=FixtureDriver())
    result=await RecoveryCoordinator(runtime,settings).recover(task.id)
    assert result["status"]=="queued"
    recovered=db.get_task(task.id).state
    if dimension=="model_calls":
        recovered.role_invocation_count=1
        recovered.revisable.limits.model_calls=1
    else:
        recovered.revisable.limits.deadline_at=1
    with db.transaction():
        db.update_task(task.id,state=recovered)
        store.publish_manifest(task.id,state=recovered)
    async def forbidden(*args,**kwargs):
        raise AssertionError("exhausted recovery must not call a model")
    monkeypatch.setattr("agent.session.complete",forbidden)
    assert await runtime.run_task(task.id)==TaskStatus.FAILED
    assert db.get_task(task.id).state.revisable.stop_reason=="budget_exhausted"


async def test_new_control_and_note_during_observation_preserve_cut_and_retire_old_writer(tmp_path):
    from shared.recovery_context import ExecutionContext,current_execution
    from shared.recovery_store import StaleOwner
    db,artifacts,state,task,store=setup(tmp_path)
    old=prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    capture=driver.get_frame
    observed=asyncio.Event()
    proceed=asyncio.Event()
    async def held():
        observed.set()
        await proceed.wait()
        return await capture()
    driver.get_frame=held
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    recovery=asyncio.create_task(coordinator.recover(task.id))
    await asyncio.wait_for(observed.wait(),5)
    token=current_execution.set(ExecutionContext(store,old,state))
    try:
        with pytest.raises(StaleOwner):
            db.update_task(task.id,state=state)
        with pytest.raises(StaleOwner):
            TaskStore(db,artifacts,task.id).put("note","stale",{"content":"old writer"})
    finally:
        current_execution.reset(token)
    TaskStore(db,artifacts,task.id).put("note","new",{"title":"new","content":"new durable fact"})
    store.control(task.id,"pause")
    proceed.set()
    assert (await recovery)["status"]=="recovery_blocked"
    orch._prepare_checkpoint=lambda *_:None
    assert (await coordinator.recover(task.id,explicit=True))["status"]=="paused"
    assert TaskStore(db,artifacts,task.id).get("note","new")["payload"]["content"]=="new durable fact"
    assert not db.get_agent_record(task.id,"note","stale")
    assert len(db.list_steps(task.id))==1


async def test_frozen_hitl_configuration_is_restorable_from_manifest(tmp_path):
    from shared.hitl import HumanInputBroker,HitlConfig
    db,artifacts,state,task,store=setup(tmp_path)
    HumanInputBroker(db).configure(task.id,HitlConfig(enabled=True))
    prepare(store,state,task)
    with db.transaction():
        db._conn.execute("DELETE FROM agent_records WHERE task_id=? AND kind='hitl_config'",(task.id,))
    orch=fake_orch(db,artifacts,tmp_path)
    assert (await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id))["status"]=="queued"
    assert HumanInputBroker(db).config(task.id)["enabled"]


async def test_no_progress_failure_after_ready_exposes_automatic_attempt_exhaustion(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path)
    prepare(store,state,task)
    store.event(task.id,"ready",attempt_no=3,resource_fact="available")
    store.event(task.id,"detected",reason="same failure without normal progress")
    db.update_task(task.id,status=TaskStatus.RECOVERING)
    orch=fake_orch(db,artifacts,tmp_path)
    result=await RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path)).recover(task.id)
    assert result["status"]=="recovery_blocked" and not result["accepted"]
    assert store.projection(task.id)["reason"]=="auto_recovery_attempts_exhausted"


async def test_safety_cleanup_cannot_retire_an_active_explicit_manual_recovery(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path,policy="manual")
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    capture=driver.get_frame
    observed,proceed=asyncio.Event(),asyncio.Event()
    async def held():
        observed.set()
        await proceed.wait()
        return await capture()
    driver.get_frame=held
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    active=asyncio.create_task(coordinator.recover(task.id,explicit=True))
    try:
        await asyncio.wait_for(observed.wait(),5)
        owner=store.metadata(task.id)["worker_token"]
        assert owner
        await coordinator.safety_cleanup(set())
        assert store.metadata(task.id)["worker_token"]==owner,"cleanup retired the active recovery owner"
    finally:
        proceed.set()
        result=await active
    assert result["status"]=="queued"


async def test_overlapping_recovery_callers_join_without_second_takeover(tmp_path):
    db,artifacts,state,task,store=setup(tmp_path,policy="manual")
    prepare(store,state,task)
    orch=fake_orch(db,artifacts,tmp_path)
    driver=orch._resolve_driver("fixture")
    capture=driver.get_frame
    observed,proceed=asyncio.Event(),asyncio.Event()
    captures=[]
    async def held():
        captures.append(True)
        observed.set()
        await proceed.wait()
        return await capture()
    driver.get_frame=held
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    first=asyncio.create_task(coordinator.recover(task.id,explicit=True))
    await asyncio.wait_for(observed.wait(),5)
    epoch=store.metadata(task.id)["execution_epoch"]
    second=asyncio.create_task(coordinator.recover(task.id,explicit=True))
    await asyncio.sleep(.05)
    proceed.set()
    results=await asyncio.gather(first,second)
    assert results[0]["status"]==results[1]["status"]=="queued"
    assert store.metadata(task.id)["execution_epoch"]==epoch,"joined request performed another takeover"
    assert len(captures)==1,"joined request captured a second recovery observation"
    assert len(db.list_steps(task.id))==1
