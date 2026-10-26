"""Hard-exit fixture. Run in its own process; the test kills it at a durable gate."""

import asyncio
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from driver.execution_journal import ExecutionJournal
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import RecoveryStore
from shared.schemas import ActionResult,AgentState,TaskStatus


def publish(path,payload):
    with path.open("w",encoding="utf-8") as stream:
        json.dump(payload,stream)
        stream.flush()
        os.fsync(stream.fileno())


async def main(root,boundary):
    root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    db=Database(root/"test.db")
    artifacts=ArtifactStore(root/"artifacts")
    db.artifacts=artifacts
    store=RecoveryStore(db,artifacts)
    state=AgentState(instruction="effect fixture")
    state.revisable.next_role="executor"
    task=db.create_task(state.instruction,state,device_serial="fixture")
    store.enable(task.id,"safe_auto")
    db.update_task(task.id,status=TaskStatus.RUNNING)
    owner=store.claim(task.id,"killed-backend")
    journal=ExecutionJournal(root/"journal")
    await journal.fence("fixture",owner.device_fence,task.id,owner.epoch)
    def gate(at):
        if boundary==at:
            publish(root/"gate.json",{"task_id":task.id,"boundary":at,"pid":os.getpid()})
            while True:
                time.sleep(.05)
    if boundary in {"file_publish","file_published"}:
        original_replace=os.replace
        def replace(source,target):
            if Path(target).parent.name=="recovery":
                gate("file_publish")
                original_replace(source,target)
                gate("file_published")
            else:
                original_replace(source,target)
        os.replace=replace
        state.revisable.feedback="not committed"
        with db.transaction():
            store.publish_manifest(task.id,state=state)
        return
    store.prepare(owner,state,operation_id="physical",kind="device_action",args={"type":"home"},
                  invocation_id="provider-round",call_id="call",basis="before-kill",units=1,
                  intent={"role":"executor","messages":[{"role":"assistant","content":None,"tool_calls":[
                      {"id":"call","type":"function","function":{"name":"submit_executor_step","arguments":"{}"}}]}]})
    gate("T1")
    if boundary=="input_child":
        from driver.adb import _run_async
        envelope={"operation_id":"physical","device_key":"fixture","device_fence":owner.device_fence,
                  "task_id":task.id,"epoch":owner.epoch,"action":{"type":"home"}}
        command=[sys.executable,"-c","import time\nfrom pathlib import Path\np=Path("+repr(str(root/"input-heartbeat"))+")\nwhile True:\n with p.open('a') as f: f.write('input\\n')\n time.sleep(.05)\n"]
        running=asyncio.create_task(journal.act(envelope,lambda:_run_async(command,timeout=60)))
        while not (root/"input-heartbeat").exists():
            if running.done():
                await running
            await asyncio.sleep(.02)
        child=journal.conn.execute("SELECT pid,started_at FROM children LIMIT 1").fetchone()
        publish(root/"gate.json",{"task_id":task.id,"boundary":boundary,"pid":os.getpid(),"child_pid":child["pid"]})
        await running
        return
    async def effect():
        gate("accepted")
        # Independent external state outlives both execution journals.
        with (root/"effects.jsonl").open("a",encoding="utf-8") as stream:
            stream.write(json.dumps({"operation_id":"physical"})+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        gate("effect")
        return ActionResult(success=True,message="fixture physical effect")
    envelope={"operation_id":"physical","device_key":"fixture","device_fence":owner.device_fence,
              "task_id":task.id,"epoch":owner.epoch,"action":{"type":"home"}}
    result=await journal.act(envelope,effect)
    gate("driver_receipt")
    store.receipt("physical",result.model_dump(mode="json"),generation=journal.generation)
    gate("T2")
    state.step_number=1
    state.revisable.execution_count=1
    db.add_step_and_update_state(task.id,state.revisable.stage_id,0,{"success":True},state)
    gate("T3")


if __name__=="__main__":
    asyncio.run(main(*sys.argv[1:]))
