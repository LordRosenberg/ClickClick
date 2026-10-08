"""Whole-library provenance, real lifecycle faults, and shared-capacity boundaries."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.skills.environment import ResearchEnvironment, ResearchPlan
from agent.skills.snapshot import LibrarySnapshot, library_manifest
from agent.skills.learning import LearningBudget, LearningStopped, validation_gate, digest
from agent.skills.library import serialize_skill_markdown
from agent.skills.pending import approve_pending, stage_pending_patch
from test_task_skill_learning import candidate, validation, review, context


class Store:
    def __init__(self): self.rows = []
    def put(self, kind, key, payload):
        self.rows.append({"kind":kind,"key":key,"payload":copy.deepcopy(payload)})
    def records(self, kind): return [r for r in self.rows if r["kind"] == kind]


def library(tmp_path):
    root = tmp_path / "official"
    target = root / "apps/com.demo/core/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(candidate().new_text,encoding="utf-8")
    resource = target.parent / "references/query.txt"
    resource.parent.mkdir(); resource.write_text("literal query rules",encoding="utf-8")
    generic = root / "generic/core/SKILL.md"; generic.parent.mkdir(parents=True)
    generic.write_text(serialize_skill_markdown(name="generic",description="preservation",version="1",kind="generic",body="Preserve unrelated data."),encoding="utf-8")
    hidden = root / "_pending/example.md"; hidden.parent.mkdir(); hidden.write_text("not active")
    return root, target, resource


def test_snapshot_freezes_resources_and_overlay_changes_only_target(tmp_path):
    root,target,resource = library(tmp_path)
    snap = LibrarySnapshot.freeze(root,tmp_path / "frozen")
    c = candidate(); c.new_text += "A supported boundary.\n"
    overlay = snap.overlay(c.target,c.new_text,tmp_path / "overlay")
    assert set(snap.manifest["files"]) == {c.target,"apps/com.demo/core/references/query.txt","generic/core/SKILL.md"}
    assert overlay.manifest["files"]["generic/core/SKILL.md"] == snap.manifest["files"]["generic/core/SKILL.md"]
    resource.write_text("external update")
    assert snap.read("apps/com.demo/core/references/query.txt",length=7)["text"] == "literal"
    assert library_manifest(root) != snap.manifest
    with pytest.raises(ValueError): snap.read("../official/_pending/example.md")
    with pytest.raises(ValueError): snap.overlay("../outside/SKILL.md",c.new_text,tmp_path/"bad")


@pytest.mark.parametrize("outcome",["gain","no_gain","regression","wrong_library"])
def test_incremental_gate_checks_marginal_gain_and_whole_library(tmp_path,outcome):
    root,_,_ = library(tmp_path); snap = LibrarySnapshot.freeze(root,tmp_path/"baseline")
    c = candidate(); old=c.new_text; c.new_text += "A tested boundary.\n"
    overlay=snap.overlay(c.target,c.new_text,tmp_path/"overlay")
    result=validation(c,old); result.update(baseline_library_hash=snap.manifest["hash"],candidate_library_hash=overlay.manifest["hash"])
    for trial in result["trials"]:
        trial.update(baseline_library_hash=snap.manifest["hash"],candidate_library_hash=overlay.manifest["hash"])
    if outcome == "no_gain":
        for trial in result["trials"]: trial.update(baseline_success=True,efficiency_gain=0)
    elif outcome == "regression": result["trials"][-1]["candidate_success"]=False
    elif outcome == "wrong_library": result["trials"][0]["baseline_library_hash"]="other"
    accepted,reason=validation_gate(c,digest(old),result,baseline_manifest=snap.manifest)
    assert accepted == (outcome == "gain")
    assert reason == {"gain":"validated","no_gain":"no_marginal_benefit","regression":"confirmed_regression","wrong_library":"trial_library_mismatch"}[outcome]


def test_pending_resource_drift_invalidates_previously_reviewed_candidate(tmp_path):
    root,_,resource=library(tmp_path); snap=LibrarySnapshot.freeze(root,tmp_path/"frozen")
    c=candidate(); old=c.new_text;c.new_text += "New rule.\n"
    overlay=snap.overlay(c.target,c.new_text,tmp_path/"overlay")
    v=validation(c,old);v.update(baseline_library_hash=snap.manifest["hash"],candidate_library_hash=overlay.manifest["hash"])
    for t in v["trials"]:t.update(baseline_library_hash=v["baseline_library_hash"],candidate_library_hash=v["candidate_library_hash"])
    p=stage_pending_patch(target_rel=c.target,new_text=c.new_text,gist=c.gist,root=root,old_text=old,
        review_metadata={"experiment":"task_explore","candidate_hash":digest(c.new_text),"base_hash":digest(old),"review":review(),"validation":v,"baseline_manifest":snap.manifest})
    resource.write_text("Changed helper changes the validated runtime")
    with pytest.raises(ValueError,match="library/resources changed"):approve_pending(p.id,root=root)
    assert (root/c.target).read_text(encoding="utf-8") == old


def plan():return ResearchPlan(mode="isolated",scope="owned throwaway playlist",expected_condition="temporary item available",cleanup="delete only owned temporary item",cleanup_actions=2).model_dump()


@pytest.mark.asyncio
async def test_no_initializer_unverified_claims_and_restart_cannot_fake_cleanup():
    store=Store(); env=ResearchEnvironment(store); env.configure(plan())
    await env.prepare(); env.set_stage("probe")
    intent=env.intent({"type":"tap","index":1},"actual-screen")
    assert store.rows[-1]["payload"]["event"] == "intent"
    env.effect(intent,{"success":True}); env.set_stage("cleanup")
    env.record_claim("I restored it",["event:1@1"])
    assert (await env.verify())["unresolved_effects"] == 1
    assert env.close()["status"] == "unresolved"
    resumed=ResearchEnvironment.resume(store,env.id)
    assert resumed.receipt()["unresolved_effects"] == 1
    with pytest.raises(ValueError,match="not open"):resumed.intent({"type":"home"},"screen")


@pytest.mark.asyncio
async def test_executor_claim_refs_use_probe_memory_and_keep_explicit_scope(context):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.research_tools import register_research_tools
    from agent.revisable.store import TaskStore
    from agent.tool_registry import AgentToolRegistry, AgentRole, ToolExecutionContext
    from driver.fixture import FixtureDriver
    from shared.schemas import AgentState
    settings,db,task,source,library=context
    backend=ExplorationBackend(task,db=db,artifacts=source.artifacts,
        driver=FixtureDriver(),settings=settings,library=library)
    probe=db.create_task("probe",AgentState(instruction="probe"))
    backend.store=TaskStore(db,source.artifacts,probe.id)
    backend.store.put("observation","screen",{"text":"probe screen"})
    backend.store.put("note","finding",{"title":"Probe finding","content":"Observed locally"})
    backend.environment=ResearchEnvironment(backend.store)
    backend.environment.configure(plan())
    intent=backend.environment.intent({"type":"tap","index":1},"screen")
    backend.environment.effect(intent,{"success":True})
    registry=AgentToolRegistry()
    register_research_tools(registry,backend,LearningBudget())
    result=await registry.execute("research_environment",{
        "operation":"record_claim","description":"Local finding with original context",
        "evidence_refs":["screen","note:finding@1","source/observation:screen@1"]},
        ToolExecutionContext(role=AgentRole.EXECUTOR,invocation_id="claim"))
    assert result.status.value=="succeeded"
    refs=backend.environment.claims[-1]["evidence_refs"]
    assert refs==["exploration/screen","exploration/note:finding@1","source/observation:screen@1"]
    assert backend.resolve_evidence(refs[0])[1]["payload"]["text"]=="probe screen"
    assert backend.resolve_evidence(refs[2])[1]["payload"]["text"]=="Search"
    assert not backend.environment.receipt()["verified_clean"]


@pytest.mark.asyncio
async def test_executor_claim_does_not_fall_back_to_source_or_accept_unknown_namespace(context):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.research_tools import register_research_tools
    from agent.revisable.store import TaskStore
    from agent.tool_registry import AgentToolRegistry, AgentRole, ToolExecutionContext
    from driver.fixture import FixtureDriver
    from shared.schemas import AgentState
    settings,db,task,source,library=context
    backend=ExplorationBackend(task,db=db,artifacts=source.artifacts,
        driver=FixtureDriver(),settings=settings,library=library)
    probe=db.create_task("probe",AgentState(instruction="probe"))
    backend.store=TaskStore(db,source.artifacts,probe.id)
    backend.environment=ResearchEnvironment(backend.store)
    registry=AgentToolRegistry()
    register_research_tools(registry,backend,LearningBudget())
    for ref in ["screen","unregistered/observation:screen@1"]:
        result=await registry.execute("research_environment",{
            "operation":"record_claim","description":"Must not resolve outside scope",
            "evidence_refs":[ref]},ToolExecutionContext(role=AgentRole.EXECUTOR,invocation_id=ref))
        assert result.status.value=="failed"
    assert backend.environment.claims==[]


@pytest.mark.asyncio
async def test_trusted_verification_requires_complete_matching_state_and_receipt():
    value={"complete":True,"owned_resources":[]};env=ResearchEnvironment(Store(),verifier=lambda:copy.deepcopy(value))
    env.configure(plan());await env.capture_checkpoint()
    first=env.intent({"type":"tap"},"one");env.effect(first,{"success":True})
    value["owned_resources"]=["test-object"]
    assert (await env.verify())["unresolved_effects"] == 1
    value["owned_resources"]=[]
    missing=env.intent({"type":"tap"},"two")
    assert (await env.verify())["unresolved_effects"] == 1  # Missing post-dispatch receipt survives equality.
    env.effect(missing,{"receipt":{"dispatch_succeeded":False}})
    assert (await env.verify())["verified_clean"]
    assert env.close()["status"] == "closed"


@pytest.mark.asyncio
async def test_user_takeover_stops_restore_and_owned_restore_uses_atomic_expectation():
    value={"value":"research"}; calls=[]; stopped=False
    def restore(*,expected,replacement):
        calls.append((expected,replacement))
        if value["value"] != expected:return False
        value["value"]=replacement;return True
    env=ResearchEnvironment(Store(),cancelled=lambda:stopped)
    env.register_owned_resource("owned",before="original",owned_after="research",reader=lambda:value["value"],restorer=restore)
    value["value"]="user edit"
    assert not (await env.restore_owned_resource("owned"))["restored"] and not calls
    value["value"]="research"
    assert (await env.restore_owned_resource("owned"))["restored"]
    stopped=True
    with pytest.raises(asyncio.CancelledError):await env.restore_owned_resource("owned")
    assert calls == [("research","original")]
    assert env.close()["status"] == "interrupted"


@pytest.mark.asyncio
async def test_preparation_failure_does_not_claim_readiness_or_start_new_job():
    store=Store();env=ResearchEnvironment(store)
    def fail():raise RuntimeError("capability unavailable")
    with pytest.raises(RuntimeError):await env.prepare(fail)
    assert store.rows[-1]["payload"]["event"] == "prepare_failed"
    assert env.id == store.rows[0]["payload"]["session_id"]
    assert env.checkpoint is None
    assert env.close()["status"] == "unresolved"


def test_cleanup_reserve_shares_total_cap_and_job_clock_survives_validation():
    budget=LearningBudget(max_calls=5,reserve_calls=2,max_actions=4,cleanup_actions=2,max_seconds=500,max_job_seconds=20)
    budget.set_phase("prepare");budget.action();budget.set_phase("probe");budget.action()
    with pytest.raises(LearningStopped,match="action_budget"):budget.action()
    budget.set_phase("cleanup");budget.action();budget.action()
    with pytest.raises(LearningStopped,match="action_budget"):budget.action()
    budget.record_usage(None);budget.record_usage({"input_tokens":10,"cached_read_tokens":4})
    assert budget.snapshot()["provider_usage"]["available_responses"] == 1
    budget.started += 100  # Separate validation accounting must not extend overall job.
    budget.job_started -= 21
    with pytest.raises(LearningStopped,match="time_budget"):budget.check()
    assert sum(s["actions"] for s in budget.snapshot()["stage_costs"].values()) == 4


@pytest.mark.asyncio
async def test_native_research_phases_keep_memory_and_stable_prefix(context,monkeypatch):
    import json
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    from shared.schemas import ExecutorDecisionKind
    settings,db,task,source_store,library=context
    original=task.model_dump();driver=FixtureDriver();requests=[]
    backend=ExplorationBackend(task,db=db,artifacts=source_store.artifacts,driver=driver,settings=settings,library=library)
    backend.freeze_library()
    sequence=[("research_environment",{"operation":"stage","stage":"prepare"}),
              ("write_note",{"note_key":"research_anchor","content":"Observation: current home works; hypothesis: route transfers; unresolved: writes"}),
              ("research_environment",{"operation":"stage","stage":"probe"}),
              ("submit_executor_step",{"decision":"act","summary":"Check home affordance","action":{"type":"home"}}),
              ("research_environment",{"operation":"stage","stage":"cleanup"}),
              ("research_environment",{"operation":"verify"}),
              ("submit_executor_step",{"decision":"finish","summary":"No registered data mutations"})]
    async def gateway(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        requests.append(messages)
        name,args=sequence[len(requests)-1];args=dict(args)
        anchor=next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"),str) and m["content"].startswith('{"runtime_update"'))
        if name == "submit_executor_step":args["observation_id"]=anchor["current_observation_id"]
        return GatewayResponse(content="",model=model,usage={"input_tokens":100,"cached_read_tokens":40},
            tool_calls=[ToolCall(id=str(len(requests)),name=name,arguments=json.dumps(args))])
    monkeypatch.setattr("agent.session.complete",gateway)
    budget=LearningBudget(max_calls=20,reserve_calls=2,max_actions=5)
    try:
        evidence=await backend.explore("Check current route without resetting data",3,budget,
            context={"experiment":{"hypothesis":"Home route preserves data"}})
        assert evidence["outcome"] == "finish" and evidence["actions_attempted"] == 1
        assert len({m[0]["content"] for m in requests}) == 1
        assert "Home route preserves data" not in requests[0][0]["content"]
        assert any("research_anchor" in str(m) for m in requests[-1])
        assert backend.environment.receipt()["stage"] == "cleanup"
        assert backend.environment.close()["status"] == "closed"
        assert set(budget.snapshot()["stage_costs"]) == {"prepare","probe","cleanup"}
        assert budget.snapshot()["provider_usage"]["available_responses"] == len(requests)
        assert db.get_task(task.id).model_dump() == original
        assert not source_store.records("measurement")
        traces=db.list_traces(backend.record.id)
        assert len([t for t in traces if t.kind == "agent_llm_round_started"]) == len(requests)
        assert [t.payload["name"] for t in traces if t.kind == "agent_tool_started"] == [name for name,_ in sequence]
        assert not db.list_traces(task.id)
    finally:backend.close()


@pytest.mark.asyncio
async def test_owned_resource_receipt_survives_restart_and_requires_independent_check():
    store=Store();value={"version":1,"content":"before"}
    env=ResearchEnvironment(store,verifier=lambda:{"complete":True,"scope":"owned resource"})
    await env.capture_checkpoint()
    value["content"]="after"
    def restore(*,expected,replacement):
        if value != expected:return False
        value.clear();value.update(replacement);return True
    before={"version":1,"content":"before"};after=dict(value)
    env.register_owned_resource("temporary",before=before,owned_after=after,reader=lambda:dict(value),restorer=restore)
    assert env.close()["status"] == "unresolved"
    resumed=ResearchEnvironment.resume(store,env.id,verifier=lambda:{"complete":True,"scope":"owned resource"})
    assert resumed.receipt()["unresolved_effects"] == 1
    resumed.reopen_for_cleanup();resumed.attach_owned_resource("temporary",reader=lambda:dict(value),restorer=restore)
    assert (await resumed.restore_owned_resource("temporary"))["restored"]
    assert resumed.receipt()["unresolved_effects"] == 1  # Dispatch success is not verification.
    await resumed.verify()
    assert resumed.close()["status"] == "closed"
    assert ResearchEnvironment.resume(store,env.id).receipt()["verified_clean"]


@pytest.mark.asyncio
async def test_takeover_during_verification_never_marks_effects_resolved():
    stopped=False
    async def verifier():
        nonlocal stopped
        stopped=True
        return {"complete":True,"scope":"owned"}
    env=ResearchEnvironment(Store(),cancelled=lambda:stopped,verifier=verifier)
    env.checkpoint={"complete":True,"scope":"owned"};env.configure(plan())
    effect=env.intent({"type":"tap"},"real");env.effect(effect,{"success":True})
    with pytest.raises(asyncio.CancelledError):await env.verify()
    assert env.receipt()["unresolved_effects"] == 1
    assert env.close()["status"] == "interrupted"


def test_observe_navigation_is_permitted_without_claiming_zero_effects():
    env=ResearchEnvironment(Store())
    p=plan();p.update(mode="observe",scope="inspect destination menu without committing")
    env.configure(p)
    effect=env.intent({"type":"tap","index":2},"current-screen")
    env.effect(effect,{"success":True})
    assert env.receipt()["unresolved_effects"] == 1
    with pytest.raises(ValueError,match="writing requires isolated"):
        env.intent({"type":"input_text","text":"mutation"},"current-screen")


@pytest.mark.asyncio
async def test_cancelled_validation_signals_stop_and_joins_device_worker(tmp_path):
    import threading
    from evaluation.skill_learning import Experiment
    e=Experiment.__new__(Experiment);e.root=tmp_path
    entered,cleaned=threading.Event(),threading.Event()
    def episode(name,case,skills):
        entered.set()
        for _ in range(200):
            if (tmp_path/name/"STOP").exists():
                cleaned.set();raise RuntimeError("operator-stop: no scored outcome")
            threading.Event().wait(.01)
        raise AssertionError("worker was abandoned without STOP")
    e.episode=episode
    task=asyncio.create_task(e._episode_async("candidate","case",tmp_path))
    await asyncio.to_thread(entered.wait,1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert cleaned.is_set()
    import json
    assert json.loads((tmp_path/"candidate/validation-stop.json").read_text())["worker_joined"]


@pytest.mark.asyncio
async def test_cancelled_initializer_preserves_preaction_intent_and_interruption():
    env=ResearchEnvironment(Store())
    async def initializer():raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await env.prepare(initializer)
    assert env.close()["status"] == "interrupted"
    assert env.receipt()["unresolved_effects"] == 1
    assert ResearchEnvironment.resume(env.store,env.id).receipt()["status"] == "interrupted"


@pytest.mark.asyncio
async def test_checkpoint_cannot_alias_mutable_adapter_state():
    state={"complete":True,"objects":[]}
    env=ResearchEnvironment(Store(),verifier=lambda:state)
    env.configure(plan());await env.capture_checkpoint()
    action=env.intent({"type":"tap"},"screen");env.effect(action,{"success":True})
    state["objects"].append("new data")
    assert (await env.verify())["unresolved_effects"] == 1
    assert env.checkpoint["objects"] == []


def test_prior_cost_snapshot_does_not_grow_with_future_requests():
    budget=LearningBudget();budget.record_usage(None)
    previous=budget.snapshot();budget.record_usage({"input_tokens":20})
    assert len(previous["provider_usage"]["records"]) == 1
