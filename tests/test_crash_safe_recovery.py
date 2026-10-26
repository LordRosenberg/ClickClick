"""Recovery contracts: real persisted boundaries, model handoff and normal work."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.traces import TraceWriter
from driver.execution_journal import ExecutionJournal
from driver.fixture import FixtureDriver
from perception.observation import ObservationBuilder
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.recovery_context import ExecutionContext, current_execution
from shared.recovery_runtime import RecoveryCoordinator
from shared.recovery_store import RecoveryConflict, RecoveryStore, StaleOwner
from shared.schemas import Action, ActionResult, AgentState, TaskStatus


def setup(tmp_path, *, policy="safe_auto"):
    db = Database(tmp_path / "test.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    db.artifacts = artifacts
    state = AgentState(instruction="Go home")
    state.revisable.limits.device_actions = 5
    state.revisable.next_role = "executor"
    task = db.create_task(state.instruction,state,device_serial="fixture")
    store = RecoveryStore(db,artifacts)
    store.enable(task.id,policy)
    return db,artifacts,state,task,store


def prepare(store,state,task, *, operation_id="original", owner=None):
    owner = owner or store.claim(task.id,"old")
    messages = [{"role":"assistant","content":None,"tool_calls":[
        {"id":"call","type":"function","function":{"name":"submit_executor_step","arguments":"{}"}}]}]
    store.prepare(owner,state,operation_id=operation_id,kind="device_action",args={"type":"home"},
                  invocation_id="invocation",call_id="call",basis="old-observation",units=1,
                  intent={"messages":messages,"role":"executor"})
    store.db.update_task(task.id,status=TaskStatus.RUNNING)
    return owner


def fake_orch(db,artifacts,tmp_path,driver=None):
    driver = driver or FixtureDriver()
    def persist(package):
        if package.tree_ref is None:
            package.tree_ref = artifacts.save_json("trees",{"text":package.text_for_llm})
    return SimpleNamespace(db=db,artifacts=artifacts,execution_journal=ExecutionJournal(tmp_path/"journal"),
        observation_builder=ObservationBuilder(),_resolve_driver=lambda _:driver,
        _persist_observation=persist)


def test_incremental_migration_and_newer_schema_rejected(tmp_path):
    db,_,state,task,store = setup(tmp_path)
    db.close()
    reopened = Database(tmp_path/"test.db")
    assert reopened.get_task(task.id).instruction == state.instruction
    assert RecoveryStore(reopened).metadata(task.id)["recovery_policy"] == "safe_auto"
    reopened._conn.execute("PRAGMA user_version=999")
    reopened.close()
    with pytest.raises(RecoveryConflict,match="newer"):
        Database(tmp_path/"test.db")


def test_artifact_corruption_is_repaired_and_active_publication_is_pinned(tmp_path):
    artifacts = ArtifactStore(tmp_path)
    ref = artifacts.save_json("context",{"exact":"body"})
    artifacts.resolve(ref).write_bytes(b"truncated")
    with pytest.raises(ValueError,match="integrity"):
        artifacts.verify(ref)
    assert artifacts.save_json("context",{"exact":"body"}) == ref
    assert json.loads(artifacts.verify(ref)) == {"exact":"body"}
    assert artifacts.prune("context",max_files=0) == 0


def test_owner_epoch_and_device_fence_survive_release_and_old_cleanup(tmp_path):
    db,_,_,task,store = setup(tmp_path)
    old = store.claim(task.id,"old")
    new = store.claim(task.id,"new",expected_epoch=old.epoch,takeover=True)
    with pytest.raises(StaleOwner):
        store.release(old,quiescent=True)
    assert store.metadata(task.id)["worker_token"] == new.token
    store.release(new,quiescent=True)
    second = db.create_task("next",device_serial="fixture")
    store.enable(second.id)
    next_owner = store.claim(second.id,"new")
    assert next_owner.device_fence > new.device_fence


def test_t1_persist_failure_prevents_dispatch_and_key_conflict(tmp_path):
    db,_,state,task,store = setup(tmp_path)
    owner = prepare(store,state,task)
    with pytest.raises(RecoveryConflict,match="payload conflict"):
        store.prepare(owner,state,operation_id="original",kind="device_action",args={"type":"back"},invocation_id="new")
    assert len(store.operations(task.id)) == 1


async def test_driver_dedup_fence_and_control_remain_available_while_action_blocks(tmp_path):
    journal = ExecutionJournal(tmp_path)
    await journal.fence("device",1,"task",1)
    envelope = {"operation_id":"op","device_key":"device","device_fence":1,"task_id":"task","epoch":1,"action":{"type":"home"}}
    started,finish = asyncio.Event(),asyncio.Event()
    executions = []
    async def execute():
        executions.append(1)
        started.set()
        await finish.wait()
        return ActionResult(success=True,message="done")
    running = asyncio.create_task(journal.act(envelope,execute))
    await started.wait()
    assert journal.get("op")["status"] == "accepted"
    blocked = await journal.fence("device",2,"task",2,timeout=.05)
    assert not blocked["quiescent"]
    finish.set()
    await running
    assert (await journal.fence("device",2,"task",2))["quiescent"]
    with pytest.raises(RecoveryConflict,match="authority"):
        await journal.act(envelope,execute)
    envelope.update(device_fence=2,epoch=2)
    assert (await journal.act(envelope,execute)).success
    assert len(executions) == 1
    with journal.conn:
        journal.conn.execute("UPDATE operations SET result_json=NULL WHERE operation_id='op'")
    result = await journal.act(envelope,execute)
    assert result.detail["execution_result"] == "unknown"
    assert len(executions) == 1


@pytest.mark.parametrize("has_receipt",[False,True])
async def test_recover_closes_original_once_and_hands_current_package_to_saved_role(tmp_path,has_receipt):
    db,artifacts,state,task,store = setup(tmp_path)
    prepare(store,state,task)
    orch = fake_orch(db,artifacts,tmp_path)
    if has_receipt:
        store.receipt("original",{"success":True,"message":"done","detail":{}})
    settings = Settings(_env_file=None,data_dir=tmp_path)
    coordinator = RecoveryCoordinator(orch,settings)
    result = await coordinator.recover(task.id)
    assert result["status"] == "queued",result
    recovered = db.get_task(task.id).state
    assert recovered.revisable.next_role == "executor"
    assert recovered.step_number == recovered.revisable.execution_count == 1
    assert recovered.recovery["current_observation_id"] != "old-observation"
    assert recovered.recovery["operations"][0]["execution_result"] == ("known" if has_receipt else "unknown")
    assert orch._recovery_packages[task.id].observation_source == "recovery_current"
    assert len(db.list_steps(task.id)) == 1
    ledger = dict(db._conn.execute("SELECT * FROM budget_ledger").fetchone())
    assert ledger["reserved"] == 0 and ledger["settled"] == 1
    assert ledger["uncertain"] == (0 if has_receipt else 1)
    # Another interruption after T3 cannot lose unknown handoff or charge again.
    db.update_task(task.id,status=TaskStatus.RECOVERING)
    assert (await coordinator.recover(task.id))["status"] == "queued"
    assert len(db.list_steps(task.id)) == 1
    assert db.get_task(task.id).state.revisable.execution_count == 1
    assert db.get_task(task.id).state.recovery["operations"]
    messages = json.loads(artifacts.read_text(db.latest_agent_record(task.id,"dialogue")["payload"]["ref"]))
    assert sum(message.get("tool_call_id")=="call" for message in messages)==1


async def test_unknown_new_model_same_parameters_are_permitted_and_late_receipt_does_not_recharge(tmp_path):
    db,artifacts,state,task,store = setup(tmp_path)
    prepare(store,state,task)
    orch = fake_orch(db,artifacts,tmp_path)
    coordinator = RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    assert (await coordinator.recover(task.id))["status"]=="queued"
    current = db.get_task(task.id).state
    new_owner = store.claim(task.id,"continued")
    store.prepare(new_owner,current,operation_id="new-model-choice",kind="device_action",args={"type":"home"},
                  invocation_id="new-model-invocation",basis=current.recovery["current_observation_id"],units=1)
    assert len(store.operations(task.id))==2
    store.receipt("original",{"success":True,"message":"late result"},generation="late")
    assert len(db.list_steps(task.id))==1
    assert db._conn.execute("SELECT SUM(settled) FROM budget_ledger").fetchone()[0]==1
    assert store.operations(task.id)[0]["result_state"]=="unknown_recorded"
    assert db.latest_agent_record(task.id,"recovery_fact")


async def test_manual_disabled_and_paused_do_not_auto_continue(tmp_path):
    db,artifacts,state,task,store = setup(tmp_path,policy="manual")
    prepare(store,state,task)
    coordinator = RecoveryCoordinator(fake_orch(db,artifacts,tmp_path),Settings(_env_file=None,data_dir=tmp_path))
    assert (await coordinator.recover(task.id))["reason"]=="explicit_recovery_required"
    assert (await coordinator.recover(task.id,explicit=True))["status"]=="queued"
    with pytest.raises(RecoveryConflict,match="frozen"):
        store.enable(task.id,"disabled")
    disabled=db.create_task("disabled",state,device_serial="other")
    store.enable(disabled.id,"disabled")
    db.update_task(disabled.id,status=TaskStatus.RECOVERING)
    assert (await coordinator.recover(disabled.id,explicit=True))["reason"]=="crash_recovery_disabled"
    db.update_task(task.id,status=TaskStatus.PAUSED)
    assert not (await coordinator.recover(task.id))["accepted"]


def test_recover_request_retry_keeps_original_response_and_rejects_changed_payload(tmp_path):
    _,_,_,task,store = setup(tmp_path)
    first=store.request(task.id,"key",{"operation":"recover"},{"accepted":True})
    assert store.request(task.id,"key",{"operation":"recover"},{"accepted":False})==first
    with pytest.raises(RecoveryConflict):
        store.request(task.id,"key",{"operation":"different"},{})


@pytest.mark.parametrize("lose_receipt",[False,True])
@pytest.mark.parametrize("architecture",["plan_executor","plan_reviewer"])
@pytest.mark.parametrize("hitl",[False,True])
async def test_full_runtime_unknown_recovery_and_normal_path_keep_original_roles(tmp_path,monkeypatch,lose_receipt,architecture,hitl,auth_loss=False):
    from agent.runtime import create_orchestrator
    from shared.llm_gateway import GatewayResponse, ToolCall
    from PIL import Image
    import io
    image=io.BytesIO()
    Image.new("RGB",(1080,2400),"white").save(image,"PNG")
    monkeypatch.setattr("driver.fixture._blank_png",lambda:image.getvalue())
    db,artifacts,state,task,store=setup(tmp_path)
    # Start with Planner for an ordinary task; saved Executor is retained after interruption.
    state.revisable.next_role="planner"
    db.update_task(task.id,state=state)
    with db.transaction():
        store.publish_manifest(task.id,state=state)
    settings=Settings(_env_file=None,data_dir=tmp_path,default_model="test",agent_architecture=architecture)
    from shared.hitl import HumanInputBroker,HitlConfig
    HumanInputBroker(db).configure(task.id,HitlConfig(enabled=hitl))
    driver=FixtureDriver()
    runtime=create_orchestrator(db,TraceWriter(db,artifacts),settings=settings,artifacts=artifacts,driver=driver)
    original=driver.act
    async def act(action):
        result=await original(action)
        if lose_receipt and len(driver.actions)==1:
            raise ConnectionError("injected lost response after physical effect")
        return result
    driver.act=act
    received=[]
    repaired=False
    def reply(name,args):
        return GatewayResponse(content="",model="test",stop_reason="tool_calls",
                               tool_calls=[ToolCall(id="call",name=name,arguments=json.dumps(args))])
    async def complete(model,messages,*,tools,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{"round":1})
        names={tool["function"]["name"] for tool in tools}
        if "submit_reviewer_decision" in names:
            received.append("reviewer")
            return reply("submit_reviewer_decision",{"decision":"complete","reason":"Home visible"})
        if "submit_planner_decision" in names:
            received.append("planner")
            if driver.actions:
                return reply("submit_planner_decision",{"decision":"complete","reason":"Home visible"})
            return reply("submit_planner_decision",{"decision":"execute","reason":"Home",
                         "plan":{"current_stage":{"goal":"Go home"}}})
        received.append("executor")
        update=next(json.loads(message["content"])["runtime_update"] for message in reversed(messages)
                    if isinstance(message.get("content"),str) and message["content"].startswith('{"runtime_update"'))
        if lose_receipt and driver.actions:
            if auth_loss and not repaired:
                from shared.llm_gateway import GatewayAuthError
                raise GatewayAuthError("injected expired authorization")
            assert update["recovery_summary"]["operations"][0]["execution_result"]=="unknown"
            assert update["recovery_summary"]["current_observation_id"]==update["current_observation_id"]
            assert "may or may not have executed" in json.dumps(messages)
        args={"decision":"finish" if driver.actions else "act","summary":"Home","observation_id":update["current_observation_id"]}
        if not driver.actions:
            args["action"]={"type":"home"}
        return reply("submit_executor_step",args)
    monkeypatch.setattr("agent.session.complete",complete)
    status=await runtime.run_task(task.id)
    if lose_receipt:
        assert status==TaskStatus.RECOVERING
        recovered=await RecoveryCoordinator(runtime,settings).recover(task.id)
        assert recovered["status"]=="queued",recovered
        before=len(received)
        status=await runtime.run_task(task.id)
        assert received[before]=="executor"
        if auth_loss:
            assert status==TaskStatus.RECOVERY_BLOCKED
            assert not (await RecoveryCoordinator(runtime,settings).recover(task.id))["accepted"]
            from desktop.configuration import model_config_path
            model_config_path(tmp_path).write_text("{}",encoding="utf-8")
            repaired=True
            assert (await RecoveryCoordinator(runtime,settings).recover(task.id))["status"]=="queued"
            status=await runtime.run_task(task.id)
    assert status==TaskStatus.SUCCEEDED
    assert len(driver.actions)==1
    assert db.get_task(task.id).state.revisable.execution_count==1
    assert len(store.operations(task.id))==1
    if not lose_receipt:
        final_role="planner" if architecture=="plan_executor" else "reviewer"
        assert received==["planner","executor","executor",final_role]
        assert not db._conn.execute("SELECT 1 FROM agent_records WHERE task_id=? AND kind='recovery_event'",
                                    (task.id,)).fetchone()
        assert store.operations(task.id)[0]["result_state"]=="known"


async def test_recovery_model_auth_failure_waits_for_a_real_repair_fact(tmp_path,monkeypatch):
    await test_full_runtime_unknown_recovery_and_normal_path_keep_original_roles(
        tmp_path,monkeypatch,True,"plan_executor",False,auth_loss=True)


@pytest.mark.parametrize("boundary",["T1","accepted","effect","driver_receipt","T2","T3"])
async def test_hard_kill_each_durable_effect_boundary_does_not_replay(tmp_path,boundary):
    import subprocess
    import sys
    import time
    from driver.processes import background_process_kwargs
    worker=Path(__file__).with_name("recovery_process_fixture.py")
    process=subprocess.Popen([sys.executable,"-X","utf8",str(worker),str(tmp_path),boundary],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,**background_process_kwargs())
    try:
        deadline=time.monotonic()+25
        while not (tmp_path/"gate.json").exists():
            assert process.poll() is None,process.communicate()
            assert time.monotonic()<deadline,"worker did not reach durable fault gate"
            await asyncio.sleep(.03)
        gate=json.loads((tmp_path/"gate.json").read_text())
        process.kill()
        await asyncio.to_thread(process.wait,10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
        process.communicate()
    db=Database(tmp_path/"test.db")
    artifacts=ArtifactStore(tmp_path/"artifacts")
    db.artifacts=artifacts
    orch=fake_orch(db,artifacts,tmp_path)
    coordinator=RecoveryCoordinator(orch,Settings(_env_file=None,data_dir=tmp_path))
    before=(tmp_path/"effects.jsonl").read_text().splitlines() if (tmp_path/"effects.jsonl").exists() else []
    result=await coordinator.recover(gate["task_id"])
    assert result["status"]=="queued",result
    after=(tmp_path/"effects.jsonl").read_text().splitlines() if (tmp_path/"effects.jsonl").exists() else []
    assert before==after
    store=RecoveryStore(db,artifacts)
    operation=store.operations(gate["task_id"])[0]
    assert operation["status"]=="committed"
    assert operation["result_state"]==("unknown_recorded" if boundary in {"T1","accepted","effect"} else "known")
    assert len(db.list_steps(gate["task_id"]))==1
    assert db.get_task(gate["task_id"]).state.revisable.execution_count==1
