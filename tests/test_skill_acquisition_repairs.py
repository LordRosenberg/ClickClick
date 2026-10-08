"""Trusted lab restoration, raw activation, and independent research caps."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.skills.activation import candidate_activation
from agent.skills.environment import ResearchEnvironment, TrustedFixtureReset
from agent.skills.learning import LearningBudget, PROBE_REVIEW_SYSTEM, TARGET_SYSTEM
from agent.skills.library import serialize_skill_markdown, parse_skill_markdown
from agent.tool_registry import stable_hash
from test_task_skill_learning import context
from test_skill_incremental_environment import Store


def adapter(result=None):
    expected = {"case":"Case", "initial_state_hash":"actual-hash"}
    value = {"complete":True,"teardown_success":True,"initialized":True,**expected}
    if result is not None: value.update(result)
    return TrustedFixtureReset("Owned official fixture",expected,lambda:value)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {"complete":False}, {"initialized":False},
    {"teardown_success":False}, {"initial_state_hash":"other"}, {"case":"wrong"}])
async def test_disposable_restoration_needs_actual_matching_receipt(bad):
    store = Store()
    env = ResearchEnvironment(store, reset_adapter=adapter(bad))
    env.configure({"mode":"isolated","scope":"owned lab fixture", "expected_condition":"test unknown route",
        "cleanup":"harness reset", "cleanup_actions":1})
    intent = env.intent({"type":"type","text":"throwaway"},"current")
    env.effect(intent,{"success":True,"receipt":{"dispatch_succeeded":True}})
    assert not env.receipt()["verified_clean"]
    result = await env.reset_fixture()
    assert result["verified_clean"] is (bad is None)
    assert result["unresolved_effects"] == (0 if bad is None else 2)
    recovered = ResearchEnvironment.resume(store,env.id,reset_adapter=adapter())
    assert recovered.receipt()["unresolved_effects"] == result["unresolved_effects"]


@pytest.mark.asyncio
async def test_reset_absence_failure_or_cancel_never_washes_effects():
    env = ResearchEnvironment(Store())
    with pytest.raises(ValueError,match="no trusted"): await env.reset_fixture()
    async def broken(): raise RuntimeError("fixture failure")
    env = ResearchEnvironment(Store(),reset_adapter=TrustedFixtureReset("scope",{},broken))
    with pytest.raises(RuntimeError): await env.reset_fixture()
    assert env.receipt()["unresolved_effects"] == 1
    called = []
    env = ResearchEnvironment(Store(),cancelled=lambda:True,
        reset_adapter=TrustedFixtureReset("scope",{},lambda:called.append(True)))
    with pytest.raises(asyncio.CancelledError): await env.reset_fixture()
    assert not called and env.receipt()["status"] == "interrupted"


@pytest.mark.asyncio
async def test_lab_reset_cannot_resolve_arbitrary_registered_resources():
    env = ResearchEnvironment(Store(),reset_adapter=adapter())
    env.register_owned_resource("user_setting",before=0,owned_after=1,reader=lambda:1,restorer=lambda value:None)
    with pytest.raises(ValueError,match="ownership adapters"): await env.reset_fixture()
    assert env.receipt()["unresolved_effects"] == 1


def workflow():
    return serialize_skill_markdown(name="route",description="conditional hidden route", version="1",
        kind="workflow",app="com.demo",capability="test_route",
        body="## Procedure\nUse an observed alternative.\n## Verification\nCheck the result.\n")


def trace(store, role, active=None, messages=None):
    value = {"role":role,"messages":messages or []}
    if active is not None: value["active_skills"]=active
    ref = store.artifacts.save_json("llm",value)
    from agent.traces import TraceWriter
    TraceWriter(store.db,store.artifacts).write(store.task_id,kind="agent_llm_round_started",
        message="test",payload={"initial_request_ref":ref})
    return ref


def test_missing_activation_stays_unknown(context):
    *_, store, _ = context
    result = candidate_activation(store,workflow())
    assert result["executor_active_exact"] is None and result["stage_selected"] is None
    assert result["mechanism_adoption"] == "unassessed"


def test_planner_read_without_selection_is_not_executor_use(context):
    *_, store, _ = context
    trace(store,"planner",active=[],messages=[{"role":"user","content":'WORKFLOW CATALOG [{"id":"route"}]'},
        {"role":"tool","content":"[loaded skill:route@1] body"}])
    trace(store,"executor",active=[{"skill_id":"old-core","content_hash":"old"}])
    store.put("stage","s",{"goal":"original task","skill_ids":[]})
    result = candidate_activation(store,workflow())
    assert result["planner_catalog_exposed"] and result["planner_read"]
    assert result["stage_selected"] is False and result["executor_active_exact"] is False


def test_exact_projected_hash_required_and_never_proves_mechanism(context):
    *_, store, _ = context
    text = workflow(); expected = stable_hash(parse_skill_markdown(text).section_for("executor"))
    trace(store,"executor",active=[{"skill_id":"route","content_hash":"different-body"}])
    assert candidate_activation(store,text)["executor_active_exact"] is False
    trace(store,"executor",active=[{"skill_id":"route","content_hash":expected}])
    result = candidate_activation(store,text)
    assert result["executor_active_exact"] and result["mechanism_adoption"] == "unassessed"
    assert candidate_activation(store,text,max_requests=1)["executor_active_exact"] is None


def test_core_does_not_require_workflow_selection(context):
    *_, store, _ = context
    text = serialize_skill_markdown(name="core",description="app mechanic",version="2",kind="app_core",app="com.demo",body="## Shared\nHidden mechanic.")
    trace(store,"executor",active=[{"skill_id":"core","content_hash":stable_hash(parse_skill_markdown(text).section_for("executor"))}])
    store.put("stage","s",{"skill_ids":[]})
    result = candidate_activation(store,text)
    assert result["stage_selected"] is None and result["executor_active_exact"] is True


def test_stage_records_actual_selected_ids(context):
    from shared.revisable import Stage, Plan
    _,_,task,store,_ = context
    task.state.revisable.plan = Plan(current_stage=Stage(goal="task",target_app="com.demo",skill_ids=["route"]))
    store.record_stage(task.state)
    assert store.records("stage")[-1]["payload"]["skill_ids"] == ["route"]


def test_audit_differentiates_research_budgets_and_trusted_cleanup():
    assert "source.execution_limits" in PROBE_REVIEW_SYSTEM
    assert "environment_capabilities" in PROBE_REVIEW_SYSTEM
    assert "not first" in PROBE_REVIEW_SYSTEM
    assert "trusted disposable fixture reset" in TARGET_SYSTEM


def test_same_task_variant_uses_its_own_official_generation(tmp_path):
    from evaluation.skill_learning import AndroidWorldExperiment
    experiment = AndroidWorldExperiment.__new__(AndroidWorldExperiment)
    prepared=tmp_path/"prepared";(prepared/"latest-shallow").mkdir(parents=True)
    (prepared/"setup-complete.json").write_text("{}")
    experiment.model="unused"
    experiment.args=SimpleNamespace(androidworld_baseline=str(prepared),eval_python="eval-python",
        agent_python="agent-python",env_file=".env",adb="adb",seed_offset=0,variant_seed_offset=7,
        rounds=20,seconds=300)
    command,_=experiment._episode_launch(tmp_path/"baseline-variant","Case",tmp_path)
    assert command[command.index("--seed-offset")+1] == "7"
    command,_=experiment._episode_launch(tmp_path/"baseline-source","Case",tmp_path)
    assert command[command.index("--seed-offset")+1] == "0"


@pytest.mark.asyncio
async def test_complete_probe_restores_within_shared_capacity(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.fixture_reset_adapter=adapter()
    backend.environment=ResearchEnvironment(store,reset_adapter=backend.fixture_reset_adapter)
    backend.environment.configure({"mode":"isolated","scope":"owned fixture", "expected_condition":"measure", "cleanup":"adapter", "cleanup_actions":1})
    entry=backend.environment.intent({"type":"type","text":"test"},"current")
    backend.environment.effect(entry,{"success":True,"receipt":{"dispatch_succeeded":True}})
    budget=LearningBudget(max_calls=24,max_actions=2,cleanup_actions=1,max_seconds=900)
    budget.action()
    result=await backend.complete_probe(budget)
    assert result["verified_clean"] and budget.actions == 2
    assert backend.environment_transition["fixture_reset"] and budget.phase == "probe"
    assert backend.environment.stage == "probe"


@pytest.mark.asyncio
async def test_completed_probe_without_reset_adapter_keeps_user_device_unknown(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.environment=ResearchEnvironment(store)
    assert await backend.complete_probe(LearningBudget()) is None


def test_cancelled_official_initializer_never_launches_a_worker():
    from evaluation.skill_learning import AndroidWorldExperiment
    experiment=AndroidWorldExperiment.__new__(AndroidWorldExperiment)
    with pytest.raises(asyncio.CancelledError): experiment.ensure_source_fixture(cancelled=lambda:True)
    assert not hasattr(experiment,"_source_process")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, {"initial_state_hash":"mismatched"}])
async def test_lab_prepare_establishes_isolation_or_blocks_before_probe(context,bad):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LearningStopped
    from driver.fixture import FixtureDriver
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.fixture_reset_adapter=adapter(bad)
    backend.environment=ResearchEnvironment(store,reset_adapter=backend.fixture_reset_adapter)
    backend.environment.configure({"mode":"isolated","scope":"owned fixture","expected_condition":"measure","cleanup":"adapter","cleanup_actions":1})
    budget=LearningBudget(max_calls=24,max_actions=3,cleanup_actions=1,max_seconds=900)
    if bad:
        with pytest.raises(LearningStopped,match="preparation_unverified"):
            await backend.prepare_disposable_fixture(budget)
        assert backend.environment_transition is None
        assert not backend.environment.receipt()["verified_clean"]
    else:
        receipt=await backend.prepare_disposable_fixture(budget)
        assert receipt["verified_clean"]
        assert backend.environment_transition["fixture_reset"]
        assert backend.environment.stage == "prepare"
    assert budget.actions == 1 and budget.phase == "prepare"


def test_observe_plan_uses_actual_navigation_schema_and_keeps_mutations_unknown():
    env=ResearchEnvironment(Store())
    env.configure({"mode":"observe","scope":"app navigation","expected_condition":"inspect"})
    launch=env.intent({"type":"launch","app":"com.demo"},"fresh")
    env.effect(launch,{"success":True})
    assert env.receipt()["unresolved_effects"] == 1
    sleeping=env.intent({"type":"sleep","duration_ms":100},"fresh")
    env.effect(sleeping,{"success":True})
    assert env.receipt()["unresolved_effects"] == 1
    with pytest.raises(ValueError): env.intent({"type":"type","text":"mutation"},"fresh")


def test_mixed_executor_metadata_cannot_prove_candidate_absence(context):
    *_,store,_=context
    trace(store,"executor",active=[{"skill_id":"other","content_hash":"known"}])
    trace(store,"executor",active=None)
    result=candidate_activation(store,workflow())
    assert result["requests"]["executor"] == 2
    assert result["executor_active_exact"] is None


def test_mixed_stage_metadata_cannot_prove_non_selection(context):
    *_,store,_=context
    store.put("stage","known",{"skill_ids":[]})
    store.put("stage","historical",{"goal":"older uninstrumented stage"})
    assert candidate_activation(store,workflow())["stage_selected"] is None


@pytest.mark.asyncio
async def test_recovered_reset_invalidates_the_previous_checkpoint():
    store=Store();env=ResearchEnvironment(store,reset_adapter=adapter())
    env.checkpoint={"old":"state"}
    env._persist("checkpoint",{"value":env.checkpoint})
    await env.reset_fixture()
    assert env.checkpoint is None
    resumed=ResearchEnvironment.resume(store,env.id,reset_adapter=adapter())
    assert resumed.checkpoint is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected",[False,True])
async def test_validation_retains_pairs_and_only_classifies_known_infrastructure(tmp_path,unexpected):
    from evaluation.skill_learning import Experiment, EvaluationInfrastructureError
    from agent.skills.snapshot import LibrarySnapshot,library_manifest
    from agent.skills.learning import digest,validation_gate
    from test_task_skill_learning import candidate
    from test_skill_incremental_environment import library
    root,_,_=library(tmp_path);ex=Experiment.__new__(Experiment)
    ex.root=tmp_path/"experiment";ex.root.mkdir()
    ex.baseline_snapshot=LibrarySnapshot.freeze(root,tmp_path/"frozen")
    ex.baseline=ex.baseline_snapshot.root
    ex.config={};ex.case_baselines={};ex.validation_runs={}
    ex.args=SimpleNamespace(source="Case",variant="Variant",near_miss="Near",related_normal="Normal",
        environment="androidworld",source_first_validation=True)
    ex.release_source=lambda:None
    c=candidate();old=c.new_text;c.new_text += "Supported decision boundary.\n"
    calls=[]
    async def episode(name,task,skills):
        calls.append(name)
        if task == "Variant":
            if unexpected: raise RuntimeError("unrelated programmer error")
            raise EvaluationInfrastructureError("unscored observation failure")
        return {"evaluation_environment":"androidworld","score":1,"budgeted_success":True,
            "environment_status":"ready","initial_state_hash":"fixture","configuration_hash":"same",
            "logical_action_steps":10 if name.startswith("baseline") else 8,
            "library_hash":library_manifest(skills)["hash"],"goal":"same source goal"}
    ex._episode_async=episode
    if unexpected:
        with pytest.raises(RuntimeError,match="programmer error"): await ex.validate(c,old)
    else:
        result=await ex.validate(c,old)
        assert result["validation_stage"] == "infrastructure_incomplete"
        assert len(result["trials"]) == 1 and result["trials"][0]["efficiency_gain"] == 2
        assert validation_gate(c,digest(old),result,baseline_manifest=ex.baseline_snapshot.manifest) == (False,"incomplete_coverage")
        assert result["deferred_kinds"] == ["variant","near_miss","related_normal"]
    checkpoint=json.loads(next(ex.root.glob("validation-*.json")).read_text(encoding="utf-8"))
    assert len(checkpoint["trials"]) == 1
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("state",["fail","reject","insufficient","pass"])
async def test_preflight_blocks_known_defects_but_never_substitutes_for_final_review(context,monkeypatch,state):
    from agent.skills.learning import run_task_learning
    from test_task_skill_learning import Backend,candidate,validation,review
    settings,_,task,store,library=context;c=candidate()
    backend=Backend([{"decision":"explore","question":"Resolve quoted query behavior","max_actions":1},
        {"decision":"propose","candidate":c.model_dump()}])
    result=review("reject" if state == "reject" else "pass")
    if state == "fail":result["criteria"]["conflicts"]={"status":"fail","reason":"Literal existing rule contradicts draft"}
    elif state == "insufficient":
        result["verdict"]="insufficient"
        result["criteria"]["marginal_value"]={"status":"insufficient","reason":"Needs independent ordinary test"}
    seen=[]
    async def preflight(model,payload,budget):
        budget.meter("model_call_started",{});seen.append("preflight")
        return copy.deepcopy(result)
    backend.review_preflight=preflight
    async def validate(c,old):seen.append("validation");return validation(c,old)
    async def final_review(*args):seen.append("final");return review("reject")
    monkeypatch.setattr(LearningBudget,"ask",final_review)
    output=await run_task_learning(task,settings=settings,library=library,store=store,backend=backend,validator=validate)
    assert not output["eligible"] and not (library.root/c.target).exists()
    assert seen == (["preflight"] if state in {"fail","reject"} else ["preflight","validation","final"])
    assert len(output["preflight_reviews"]) == 1
    if state in {"fail","reject"}:assert output["reason"] == "blocked_by_review_or_validation"


@pytest.mark.asyncio
async def test_source_only_without_gain_defers_guard_spending(tmp_path):
    from evaluation.skill_learning import Experiment
    from agent.skills.snapshot import LibrarySnapshot,library_manifest
    from agent.skills.learning import validation_gate,digest
    from test_task_skill_learning import candidate
    from test_skill_incremental_environment import library
    root,_,_=library(tmp_path);ex=Experiment.__new__(Experiment)
    ex.root=tmp_path/"experiment";ex.root.mkdir();ex.baseline_snapshot=LibrarySnapshot.freeze(root,tmp_path/"frozen")
    ex.baseline=ex.baseline_snapshot.root;ex.config={};ex.case_baselines={};ex.validation_runs={}
    ex.args=SimpleNamespace(source="Case",variant="Variant",near_miss="Near",related_normal="Normal",environment="androidworld",source_first_validation=True)
    ex.release_source=lambda:None;c=candidate();old=c.new_text;c.new_text += "Boundary.\n";seen=[]
    async def episode(name,task,skills):
        seen.append(name)
        return {"evaluation_environment":"androidworld","score":1,"budgeted_success":True,"environment_status":"ready",
            "initial_state_hash":"same","configuration_hash":"same","logical_action_steps":10,"library_hash":library_manifest(skills)["hash"],"goal":"same"}
    ex._episode_async=episode
    result=await ex.validate(c,old)
    assert len(seen) == 2 and result["validation_stage"] == "source_without_gain"
    assert result["deferred_kinds"] == ["variant","near_miss","related_normal"]
    assert validation_gate(c,digest(old),result,baseline_manifest=ex.baseline_snapshot.manifest) == (False,"incomplete_coverage")


@pytest.mark.asyncio
async def test_native_admission_can_use_the_last_request_without_duplicate_review(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    from test_task_skill_learning import review
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    budget=LearningBudget(max_calls=1,max_actions=0,max_seconds=900)
    async def complete(model,messages,**kwargs):
        assert kwargs["tools"] is None
        kwargs["attempt_meter"]("model_call_started",{})
        return GatewayResponse(model=model,content=json.dumps(review()),tool_calls=[])
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    assert (await backend.review_preflight("fake",{},budget))["verdict"] == "pass"
    assert budget.max_calls == 1 and budget.calls == 1


def test_real_request_reduction_can_qualify_but_missing_counts_or_latency_cannot():
    from agent.skills.learning import validation_gate,digest
    from test_task_skill_learning import candidate,validation
    c=candidate();result=validation(c)
    for t in result["trials"]:t.update(baseline_success=True,efficiency_gain=0,request_gain=None)
    assert validation_gate(c,digest(""),result) == (False,"no_marginal_benefit")
    result["trials"][0]["elapsed_gain"]=100
    assert validation_gate(c,digest(""),result) == (False,"no_marginal_benefit")
    result["trials"][0]["request_gain"]=True
    assert validation_gate(c,digest(""),result) == (False,"no_marginal_benefit")
    result["trials"][0]["request_gain"]=2
    assert validation_gate(c,digest(""),result) == (True,"validated")


def test_trial_capsule_prioritizes_late_replan_and_bad_receipts_without_cause_labels(context):
    from evaluation.skill_learning import trial_execution_evidence
    settings,_,task,store,_=context
    for i in range(50):
        payload={"step":i,"decision":"act","executor_report":"Routine opening action "*80,
            "submitted_action":{"type":"tap"},"action_result":{"success":True}}
        if i == 42:
            payload.update(executor_report="Rejected dispatch; actual UI effect is unknown",
                action_result={"success":False,"receipt":{"dispatch_succeeded":False,
                    "native_action_performed":None}})
        if i == 47:
            payload.update(decision="replan",submitted_action=None,
                executor_report="Contents not established; find missing evidence before retry")
        store.put("event",str(i),payload)
    result=trial_execution_evidence({"runtime_directory":str(settings.data_dir),
        "clickclick_task_id":task.id,"execution_limits":{"max_steps":50}},character_budget=2500)
    assert len(json.dumps(result,ensure_ascii=False)) <= 2500
    by_step={item["step"]:item for item in result["events"]}
    assert by_step[47]["decision"] == "replan"
    assert by_step[42]["receipt"]["dispatch_succeeded"] is False
    assert by_step[42]["receipt"]["native_action_performed"] is None
    assert result["omitted_events"] == 50-len(result["events"])
    assert "root_cause" not in json.dumps(result)
    assert list(by_step) == sorted(by_step)


@pytest.mark.asyncio
async def test_native_research_batches_finding_with_one_currently_grounded_action(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context
    before=task.model_dump();requests=[]
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),
        settings=settings,library=library)
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        requests.append(messages)
        runtime=next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"),str) and m["content"].startswith('{"runtime_update"'))
        args={"decision":"act" if len(requests)==1 else "finish",
            "summary":"Check observed home" if len(requests)==1 else "Route checked; no writes",
            "observation_id":runtime["current_observation_id"]}
        calls=[]
        if len(requests)==1:
            args["action"]={"type":"home"}
            calls.append(ToolCall(id="note",name="write_note",arguments=json.dumps({
                "note_key":"finding","content":"Hypothesis remains unproved; next grounded check is home"})))
        calls.append(ToolCall(id="step"+str(len(requests)),name="submit_executor_step",arguments=json.dumps(args)))
        return GatewayResponse(model=model,content="",tool_calls=calls)
    monkeypatch.setattr("agent.session.complete",complete)
    budget=LearningBudget(max_calls=5,reserve_calls=1,max_actions=3)
    try:
        evidence=await backend.explore("Check home affordance",2,budget)
        assert evidence["outcome"] == "finish" and budget.calls == 2 and budget.actions == 1
        assert len(requests) == 2 and any("finding" in str(m) for m in requests[-1])
        for request in requests:
            prefixes = [json.loads(m["content"])["research_context_sources"] for m in request
                if isinstance(m.get("content"), str) and m["content"].startswith('{"research_context_sources":')]
            assert len(prefixes) == 1 and len(prefixes[0]) == 1
            assert prefixes[0][0]["research_context"] == backend.executor.research_context
        pinned = [r for r in backend.store.records("measurement") if r["payload"].get("type") == "research_context"]
        assert len(pinned) == 1
        raw = backend.store.read_dialogue(backend.state.revisable.dialogue_refs)
        assert not any("research_context_sources" in str(m) for m in raw)
        assert any('"research_context_reference":' in str(m.get("content")) for m in raw)
        tools=[t.payload["name"] for t in db.list_traces(backend.record.id) if t.kind == "agent_tool_started"]
        assert tools == ["write_note","submit_executor_step","submit_executor_step"]
        assert db.get_task(task.id).model_dump() == before and not db.list_traces(task.id)
        assert len(backend.store.records("event")) == 2
        from agent.skills.learning import probe_projection
        assert probe_projection(evidence)["frontier"]["report"] == "Route checked; no writes"
        assert backend.resolve_evidence(evidence["frontier"]["source"])[0] == "event"
    finally:backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",["raised","rejected","missing_foreground","programmer"])
async def test_research_observation_fault_retains_history_but_never_dispatches_from_old_grounding(context,monkeypatch,fault):
    from agent.skills.exploration import ExplorationBackend
    from agent.action_observation import ActionObservationTransaction
    from agent.skills.learning import probe_projection
    from driver.fixture import FixtureDriver
    from driver.observation_deadline import ObservationStageError
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;before=task.model_dump();captures=[];requests=[]
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    original=ActionObservationTransaction.observe_current
    outer=[]
    async def observe(self,**kwargs):
        if not outer:outer.append(self)
        if self is outer[0]:
            captures.append(1)
            if len(captures)==2:
                if fault == "raised":raise ObservationStageError("alignment","window_transition")
                if fault == "programmer":raise RuntimeError("unexpected programmer bug")
                package = await original(self,**kwargs)
                package.ui = package.ui.model_copy(update={"app_id":""})
                if fault == "rejected":
                    package.accepted = False
                    package.acceptance_reason = "grounding_evidence_unavailable"
                    package.actionable = False
                return package
        return await original(self,**kwargs)
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{});requests.append(messages)
        runtime=next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"),str) and m["content"].startswith('{"runtime_update"'))
        return GatewayResponse(model=model,content="",tool_calls=[ToolCall(id="home",name="submit_executor_step",
            arguments=json.dumps({"decision":"act","summary":"Check home","action":{"type":"home"},
                "observation_id":runtime["current_observation_id"]}))])
    monkeypatch.setattr(ActionObservationTransaction,"observe_current",observe)
    monkeypatch.setattr("agent.session.complete",complete)
    budget=LearningBudget(max_calls=8,reserve_calls=1,max_actions=4)
    try:
        if fault != "programmer":
            result=await backend.explore("Observe next transition",3,budget)
            assert result["outcome"] == "observation_unavailable"
            assert len(result["observation_failures"]) == 1 and len(backend.store.records("event")) == 1
            failure=probe_projection(result)["observation_failures"][0]
            kind,row,_=backend.resolve_evidence(failure["source"])
            assert kind == "measurement" and row["payload"]["diagnostics"]["reason"] == {
                "raised":"window_transition", "rejected":"grounding_evidence_unavailable",
                "missing_foreground":"foreground_application_unavailable"}[fault]
            assert backend.probe_directory()["items"][0]["observation_failures"] == [failure]
        else:
            with pytest.raises(RuntimeError,match="programmer bug"):
                await backend.explore("Observe next transition",3,budget)
        assert budget.calls == 1 and budget.actions == 1 and len(requests) == 1
        assert db.get_task(task.id).model_dump() == before
    finally:backend.close()


@pytest.mark.asyncio
async def test_executor_observation_fault_keeps_pending_dispatch_unknown(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend,LearningExecutor
    from driver.fixture import FixtureDriver
    from driver.observation_deadline import ObservationStageError
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    async def uncertain(self,question,package,**kwargs):
        self.budget.action()
        self.intent_id=self.environment.intent({"type":"tap","index":0},package.observation_id)
        raise ObservationStageError("acceptance","grounding_evidence_unavailable")
    monkeypatch.setattr(LearningExecutor,"act_once",uncertain)
    budget=LearningBudget(max_calls=5,reserve_calls=1,max_actions=4)
    try:
        result=await backend.explore("Check uncertain dispatch boundary",2,budget,context={"experiment":{
            "environment_plan":{"mode":"observe","scope":"inspect visible navigation",
                "expected_condition":"observe current screen","cleanup":"independent verification","cleanup_actions":1}}})
        assert result["outcome"] == "observation_unavailable" and budget.actions == 1
        assert backend.environment.receipt()["unresolved_effects"] == 1
        assert backend.environment.effects[-1]["result"]["dispatch_outcome"] == "unknown"
        assert backend.executor.intent_id is None and not backend.store.records("event")
        assert backend.environment.close()["status"] == "unresolved"
    finally:backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_package",[False,True])
async def test_controller_returns_partial_native_probe_to_learner_without_validating_a_fault(context,monkeypatch,rejected_package):
    from agent.skills.exploration import ExplorationBackend
    from agent.action_observation import ActionObservationTransaction
    from agent.skills.learning import run_task_learning
    from driver.fixture import FixtureDriver
    from driver.observation_deadline import ObservationStageError
    from shared.llm_gateway import GatewayResponse,ToolCall
    settings,db,task,store,library=context;payloads=[];outer=[];captures=[];validated=[]
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    original=ActionObservationTransaction.observe_current
    async def observe(self,**kwargs):
        if not outer:outer.append(self)
        if self is outer[0]:
            captures.append(1)
            if len(captures)==2:
                if not rejected_package:raise ObservationStageError("alignment","window_transition")
                package = await original(self,**kwargs)
                package.ui = package.ui.model_copy(update={"app_id":""})
                package.accepted = False
                package.acceptance_reason = "grounding_evidence_unavailable"
                package.actionable = False
                return package
        return await original(self,**kwargs)
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        runtime=next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"),str) and m["content"].startswith('{"runtime_update"'))
        return GatewayResponse(model=model,content="",tool_calls=[ToolCall(id="home",name="submit_executor_step",
            arguments=json.dumps({"decision":"act","summary":"Inspect home","action":{"type":"home"},
                "observation_id":runtime["current_observation_id"]}))])
    async def diagnose(model,system,payload,budget):
        budget.meter("model_call_started",{});payloads.append(copy.deepcopy(payload))
        if len(payloads)==1:return {"decision":"explore","question":"Inspect observed navigation",
            "hypothesis":"Navigation may transfer","test":"Observe before and after home","max_actions":3}
        assert payload["evidence"]["outcome"] == "observation_unavailable"
        assert payload["evidence"]["observation_failures"][0]["reason"] == (
            "grounding_evidence_unavailable" if rejected_package else "window_transition")
        return {"decision":"skip","reason":"No worthwhile verified rule from unavailable current observation"}
    async def audit(experiment,requested,budget,**kwargs):
        budget.meter("model_call_started",{});return {"verdict":"run","reasons":[]}
    async def validator(*args):validated.append(True);raise AssertionError("Fault is not a candidate")
    backend.diagnose=diagnose;backend.review_probe=audit
    monkeypatch.setattr(ActionObservationTransaction,"observe_current",observe)
    monkeypatch.setattr("agent.session.complete",complete)
    budget=LearningBudget(max_calls=12,reserve_calls=4,max_actions=4)
    try:
        result=await run_task_learning(task,settings=settings,library=library,store=store,
            backend=backend,budget=budget,validator=validator)
        assert result["skipped"] and not validated and result.get("pending_id") is None
        assert result["acquisition_ledger"]["recent_probes"][0]["outcome"] == "observation_unavailable"
        assert len(payloads)==3 and budget.calls == 5 and budget.actions == 1
        assert budget.max_calls==12 and budget.max_actions==4
        assert len(backend.store.records("event")) == 1
    finally:backend.close()


@pytest.mark.parametrize("phase",["writing","live","probe_repair"])
def test_research_handoffs_keep_goal_contracts_but_reference_repeated_source_history(phase):
    from agent.skills.exploration import diagnostic_payload_projection,live_experiment_context
    source_packet={"instruction":"exact task","execution_limits":{"device_actions":24},"counts":{"actions":20},
        "events":[{"source":"event:late@1","preview":"literal old evidence "*300}],
        "observations":[{"source":"observation:old@1","app":"com.demo","preview":"old screen "*400}],
        "failure_reason":"contents unconfirmed"}
    payload={"source":source_packet,"experiment":{"test":"Full setup, preservation and verification conditions"},
        "environment_transition":{"fixture_reset":True}}
    if phase == "writing":payload.update(candidate_json_schema={"properties":{"new_text":{"type":"string"}}},
        evidence={"question":"Unknown shortcut","notes":{"items":[{"source":"exploration/note:finding@1","preview":"Actual observed finding; transfer unresolved"}]}})
    if phase == "probe_repair":payload.update(probe_review={"verdict":"revise","reasons":["Read the source transition and distinguish existing guidance from a new condition"]},
        executor_action_contract={"anyOf":[{"properties":{"type":{"enum":["tap"]},"index":{"type":"integer"}},"required":["type","index"]}]},diagnostic_read_rounds=1)
    before=json.dumps(payload)
    result=live_experiment_context(payload,character_budget=4000) if phase == "live" else diagnostic_payload_projection(payload,character_budget=4000)
    assert len(json.dumps(result,ensure_ascii=False)) < 4000
    assert result["source"]["instruction"] == source_packet["instruction"]
    assert result["source"]["execution_limits"] == source_packet["execution_limits"]
    assert result["source"]["events"] == [{"source":"event:late@1"}]
    assert result["experiment"] == payload["experiment"] and result["environment_transition"] == payload["environment_transition"]
    if phase == "writing":assert result["evidence"]["notes"] == payload["evidence"]["notes"] and result["candidate_json_schema"] == payload["candidate_json_schema"]
    if phase == "probe_repair":
        assert result["probe_review"] == payload["probe_review"]
        assert result["executor_action_contract"] == payload["executor_action_contract"]
        assert result["diagnostic_read_rounds"] == 1
    assert json.dumps(payload)==before


def test_exact_research_context_reuse_keeps_every_budget_environment_and_image():
    from agent.context_projection import reuse_observation_text,estimate_context
    context_data={"source":{"instruction":"preserve exact values"},"experiment":{"test":"required check "*1000}}
    messages=[{"role":"user","content":json.dumps({"research_context":context_data,"environment":{"generation":i},
        "budget":{"remaining_calls":20-i}})} for i in range(3)]
    messages.append({"role":"user","content":[{"type":"image_url","image_url":{"url":"data:image/png;base64,abc"}}]})
    before=json.dumps(messages);projected=reuse_observation_text(messages)
    assert estimate_context(projected) < estimate_context(messages)/2
    first=json.loads(projected[0]["content"])
    assert first["research_context"] == context_data
    for i in range(3):
        current=json.loads(projected[i]["content"])
        assert current["environment"] == {"generation":i} and current["budget"] == {"remaining_calls":20-i}
        if i:assert current["research_context_reference"]["id"] == first["research_context_id"]
    assert projected[-1] == messages[-1] and json.dumps(messages) == before


def test_changed_research_question_or_ordinary_history_is_never_reused_as_old_context():
    from agent.context_projection import reuse_observation_text
    context_data={"test":"a"*2000}
    first={"role":"user","content":json.dumps({"research_context":context_data,"environment":{},"budget":{}})}
    changed={"role":"user","content":json.dumps({"research_context":{"test":"b"*2000},"environment":{},"budget":{}})}
    ordinary={"role":"tool","content":first["content"]}
    malformed={"role":"user","content":first["content"]+" invalid"}
    messages=[first,changed,ordinary,malformed]
    assert reuse_observation_text(messages) == messages


def test_verified_cleanup_survives_compact_probe_handoff_without_whole_device_claim():
    from agent.skills.learning import probe_projection
    from agent.skills.exploration import diagnostic_payload_projection
    receipt={"session_id":"owned","status":"open","verified_clean":True,"unresolved_effects":0,
        "plan":{"large":"not needed"},"reset_capability":{"scope":"official playlist fixture only"},
        "policy":"Clean applies only to verification scope"}
    result=diagnostic_payload_projection({"source":{"instruction":"Task"},"candidate_json_schema":{"type":"object"},
        "evidence":{"question":"Probe","environment_restoration":receipt}})
    compact=result["evidence"]["environment_restoration"]
    assert compact["verified_clean"] and compact["unresolved_effects"] == 0
    assert compact["reset_capability"]["scope"] == "official playlist fixture only" and "plan" not in compact
    assert probe_projection({"environment_restoration":{**receipt,"verified_clean":False}})["environment_restoration"]["verified_clean"] is False


@pytest.mark.asyncio
async def test_probe_reviewer_receives_existing_active_frozen_guidance_not_invented_body(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    from agent.tool_registry import stable_hash
    settings,db,task,store,library=context
    text=workflow();path=library.root/"apps/com.demo/workflows/test_route/SKILL.md";path.parent.mkdir(parents=True);path.write_text(text,encoding="utf-8")
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.freeze_library()
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        payload=json.loads(messages[1]["content"])
        data=payload["existing_active_guidance"]
        assert len(data["items"]) == 1 and data["omitted_ids"] == ["missing"]
        item=data["items"][0]
        assert item["id"] == "route" and item["content"] == parse_skill_markdown(text).section_for("executor")
        assert item["content_hash"] == stable_hash(item["content"])
        return GatewayResponse(model=model,content=json.dumps({"verdict":"revise","reasons":["Need a new discriminating comparison"]}),tool_calls=[])
    monkeypatch.setattr("agent.skills.learning.complete",complete)
    budget=LearningBudget(max_calls=6,reserve_calls=1,max_actions=4)
    result=await backend.review_probe({"question":"Investigate known route", "environment_plan":{"mode":"observe", "scope":"Owned fixture navigation", "expected_condition":"Current visible comparison"}},2,budget,context={"source":{
        "instruction":task.instruction,"active_skills":[{"skill_id":"route"},{"skill_id":"missing"},{"skill_id":"route"}]}})
    assert result["verdict"] == "revise" and budget.calls == 1 and budget.actions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls,can_read,expected_calls", [(8, True, 5), (6, False, 4)])
async def test_probe_repair_can_read_requested_native_evidence_without_new_capacity(
        context, monkeypatch, max_calls, can_read, expected_calls):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import run_task_learning
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    store.put("measurement", "boundary", {"detail": "Literal boundary evidence only in native history"})
    before = copy.deepcopy(store.records("measurement"))
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(),
        settings=settings, library=library)
    calls, audits = [], []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        payload = json.loads(messages[1]["content"])
        calls.append(messages)
        if not payload.get("probe_review"):
            return GatewayResponse(model=model, content=json.dumps({"decision":"explore",
                "question":"Test the unresolved boundary", "max_actions":1}))
        assert payload["diagnostic_read_rounds"] == int(can_read)
        tools = kwargs.get("tools")
        if tools:
            assert can_read and len(calls) == 2
            assert {t["function"]["name"] for t in tools} <= {
                "read_history", "read_execution_outline", "read_official_skill"}
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(
                id="boundary-read", name="read_history",
                arguments=json.dumps({"source":"measurement:boundary@1", "full":True}))])
        if can_read:
            results = [m for m in messages if m["role"] == "tool"]
            assert len(results) == 1 and results[0]["tool_call_id"] == "boundary-read"
            assert "Literal boundary evidence only in native history" in results[0]["content"]
        return GatewayResponse(model=model, content=json.dumps({"decision":"explore",
            "question":"Repaired boundary comparison", "max_actions":1}))
    async def audit(experiment, actions, budget, *, context):
        budget.check(exploration=True)
        budget.meter("model_call_started", {})
        audits.append(experiment)
        return {"verdict":"revise", "reasons":["Read source/measurement:boundary@1 before planning"]}
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend.review_probe = audit
    budget = LearningBudget(max_calls=max_calls, reserve_calls=2, max_actions=2)
    result = await run_task_learning(task, settings=settings, library=library, store=store,
        backend=backend, budget=budget)
    assert result["reason"] == "probe_review_stopped"
    assert len(audits) == 2 and result["acquisition_ledger"]["probe_repairs"] == 1
    assert budget.calls == expected_calls and budget.actions == 0
    assert budget.snapshot()["remaining_calls"] >= budget.reserve_calls
    assert backend.record is None and store.records("measurement") == before
    assert not list(library.root.rglob("SKILL.md"))


def test_review_contract_cap_uses_lossless_compact_wire_and_retains_all_fields(context, monkeypatch):
    from agent.skills.exploration import build_review_contracts
    from agent.skills.learning import LearningStopped
    settings, _, _, _, library = context
    schema = {"enum": list(range(13200))}
    monkeypatch.setattr("agent.skills.exploration.prompt", lambda role: role)
    monkeypatch.setattr("agent.revisable.session.executor_submission_schema", lambda: schema)
    contract = build_review_contracts(workflow(), library, settings)
    assert len(json.dumps(contract, ensure_ascii=False)) > 90000
    compact = json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
    assert len(compact) < 90000 and json.loads(compact) == contract
    assert contract["executor_submission_schema"] == schema
    assert set(contract["prompts"]) == {"planner", "reviewer", "executor"}
    monkeypatch.setattr("agent.revisable.session.executor_submission_schema", lambda: {"description":"x"*90001})
    with pytest.raises(LearningStopped, match="review_contract_budget"):
        build_review_contracts(workflow(), library, settings)


@pytest.mark.asyncio
async def test_pinned_research_context_hydrates_changed_contracts_without_mutating_history(context, monkeypatch):
    from agent.skills.exploration import LearningSession
    from agent.revisable.session import ExecutionSession
    from agent.skills.learning import digest, source
    settings, _, _, store, _ = context
    session = LearningSession("executor", "unused", settings=settings)
    session.store = store
    refs = []
    values = [{"experiment":{"test":"preserve original condition "*1000}},
              {"experiment":{"test":"different continuation condition "*1000}}]
    for i, value in enumerate(values):
        refs.append(source(store.put("measurement", "context"+str(i), {
            "type":"research_context", "content_hash":digest(value), "research_context":value}), "measurement"))
    envelopes = [{"role":"user", "content":json.dumps({"research_context_reference":refs[i],
        "environment":{"generation":i}, "budget":{"calls":i}})} for i in (0, 0, 1)]
    image = {"role":"user", "content":[{"type":"image_url", "image_url":{"url":"data:image/png;base64,abc"}}]}
    original = copy.deepcopy(envelopes)
    async def run(self, observations, **kwargs):
        supplied = kwargs["history_messages"]
        prefix = json.loads(supplied[0]["content"])["research_context_sources"]
        assert prefix == [{"source":ref, "research_context":value} for ref,value in zip(refs,values)]
        assert supplied[1:] == envelopes[:2] and observations == [envelopes[2], image]
        assert kwargs["history_message_names"] == ["research_context_sources", "old0", "old1"]
        return "checked"
    monkeypatch.setattr(ExecutionSession, "run", run)
    assert await session.run([envelopes[2], image], history_messages=envelopes[:2],
        history_message_names=["old0", "old1"]) == "checked"
    assert envelopes == original
    store.put("measurement", "broken", {"type":"research_context", "content_hash":"wrong", "research_context":values[0]})
    broken = {"role":"user", "content":json.dumps({"research_context_reference":"measurement:broken@1", "environment":{}, "budget":{}})}
    with pytest.raises(ValueError, match="invalid native research context"):
        await session.run([broken])


@pytest.mark.asyncio
async def test_probe_approval_cannot_outlive_the_live_research_deadline(context):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import run_task_learning
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    budget = LearningBudget(max_calls=12, reserve_calls=4, max_actions=4, max_seconds=120)
    async def diagnose(model, system, payload, current):
        current.meter("model_call_started", {})
        return {"decision":"explore", "question":"Test an unresolved boundary", "max_actions":2}
    async def audit(experiment, actions, current, *, context):
        current.meter("model_call_started", {})
        current.started -= 121
        return {"verdict":"run", "reasons":[]}
    backend.diagnose, backend.review_probe = diagnose, audit
    try:
        result = await run_task_learning(task, settings=settings, library=library,
            store=store, backend=backend, budget=budget)
        assert result["reason"] == "time_budget_reserved"
        assert budget.calls == 2 and budget.actions == 0
        assert backend.record is None and result.get("pending_id") is None
    finally:
        backend.close()


@pytest.mark.asyncio
async def test_diagnosis_and_audit_share_exact_frozen_rule_evidence(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings,db,task,store,library=context
    path=library.root/"apps/com.demo/workflows/test_route/SKILL.md"
    path.parent.mkdir(parents=True);path.write_text(workflow(),encoding="utf-8")
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    backend.freeze_library()
    path.write_text(workflow().replace("Use an observed alternative.","Changed live rule after freeze."),encoding="utf-8")
    payload={"source":{"instruction":task.instruction,"active_skills":[{"skill_id":"route"},{"id":"missing"},{"id":"route"}]}}
    original=copy.deepcopy(payload);seen=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        seen.append(json.loads(messages[1]["content"])["existing_active_guidance"])
        return GatewayResponse(model=model,content=json.dumps({"decision":"skip","verdict":"stop","reasons":[]}),tool_calls=[])
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    monkeypatch.setattr("agent.skills.learning.complete",complete)
    budget=LearningBudget(max_calls=8,reserve_calls=1,max_actions=4)
    try:
        await backend.diagnose("fake","Choose a useful unresolved question",payload,budget)
        await backend.review_probe({"question":"Check boundary", "environment_plan":{"mode":"observe", "scope":"Owned fixture navigation", "expected_condition":"Current visible comparison"}},2,budget,context=payload)
        assert seen[0] == seen[1] and len(seen[0]["items"]) == 1
        assert seen[0]["items"][0]["content"] == parse_skill_markdown(workflow()).section_for("executor")
        assert seen[0]["omitted_ids"] == ["missing"]
        assert payload == original and budget.calls == 2 and budget.actions == 0
    finally:backend.close()


def test_rule_evidence_omits_whole_oversized_bodies_and_keeps_later_rules(context):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    packs={key:SimpleNamespace(kind="workflow",app="com.demo",section_for=lambda role,text=text:text)
        for key,text in [("large","rule "*4000),("small","Small exact rule")]}
    backend.library=SimpleNamespace(get=packs.get)
    payload={"source":{"active_skills":[None,{}, {"id":"large"},{"id":"small"},{"id":"missing"},{"id":"large"}]}}
    try:
        value=backend.existing_active_guidance(payload)
        assert [x["id"] for x in value["items"]] == ["small"]
        assert value["items"][0]["content"] == "Small exact rule"
        assert value["omitted_ids"] == ["large","missing"]
        assert len(json.dumps(value["items"],ensure_ascii=False)) <= 12000
    finally:backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous",[False,True])
@pytest.mark.parametrize("with_guidance",[False,True])
async def test_initial_rule_evidence_overflow_keeps_full_contracts_and_readable_source(context,monkeypatch,continuous,with_guidance):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse
    settings,db,task,store,library=context
    path=library.root/"apps/com.demo/workflows/test_route/SKILL.md"
    if with_guidance:
        path.parent.mkdir(parents=True);path.write_text(workflow(),encoding="utf-8")
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library,retain_probe_state=continuous)
    backend.freeze_library()
    ledger={"source_problem":{"failure_reason":"exact original failure"},"recent_probes":[]}
    payload={"source":{"instruction":task.instruction,"active_skills":[{"id":"route"}],
        "events":[{"source":"event:4@1","step":4,"preview":"literal raw source "*3000}],
        "execution_limits":{"max_steps":30},"counts":{"actions":22}},
        "acquisition_ledger":ledger,"budget":{"remaining_calls":48},"diagnostic_read_rounds":0}
    original=copy.deepcopy(payload);seen=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        value=json.loads(messages[1]["content"]);seen.append(value)
        assert value["source"]["instruction"] == task.instruction
        assert value["source"]["execution_limits"] == {"max_steps":30}
        assert value["source"]["counts"] == {"actions":22}
        assert value["source"]["events"] == [{"source":"event:4@1","step":4}]
        assert value["source"]["post_review_history_policy"]["omitted_preview_fields"] == 1
        assert value["existing_active_guidance"] == backend.existing_active_guidance(payload)
        assert value["acquisition_ledger"]["source_problem"] == ledger["source_problem"]
        assert value["budget"] == payload["budget"]
        assert "executor_action_directory" in value and "executor_action_contract" not in value
        assert ("research_session" in value) == continuous
        assert len(json.dumps(value,ensure_ascii=False)) <= min(30000,50000-len(TARGET_SYSTEM)-20000)
        return GatewayResponse(model=model,content='{"decision":"skip","reason":"No verified gap"}',tool_calls=[])
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    try:
        result=await backend.diagnose("fake",TARGET_SYSTEM,payload,LearningBudget(max_calls=6,reserve_calls=1))
        assert result["decision"] == "skip" and len(seen) == 1 and payload == original
    finally:backend.close()


@pytest.mark.parametrize("matching",[False,True])
def test_compact_diagnostic_wire_reuses_only_identical_capability_with_full_target(matching):
    from agent.skills.exploration import diagnostic_payload_projection
    cap={"available":True,"scope":"exact owned fixture "*50}
    old=copy.deepcopy(cap) if matching else {**cap,"scope":"different prior scope"}
    payload={"source":{"instruction":"exact task"},"budget":{"calls":41},
        "environment_capabilities":cap,"research_session":{"mode":"continuous_owned_fixture",
            "environment":{"status":"open","unresolved_effects":16,"reset_capability":old}}}
    before=copy.deepcopy(payload)
    result=diagnostic_payload_projection(payload,character_budget=4000,compact=True)
    assert result["environment_capabilities"] == cap and payload == before
    receipt=result["research_session"]["environment"]
    assert receipt["status"] == "open" and receipt["unresolved_effects"] == 16
    assert receipt["reset_capability"] == ({"same_request_reference":"environment_capabilities"} if matching else old)
    wire=json.dumps(result,ensure_ascii=False,separators=(",",":"))
    assert json.loads(wire)==result and len(wire)<=4000


@pytest.mark.parametrize("field",["skills","actions"])
def test_compact_overflow_directory_is_lossless_and_leaves_source_untouched(field):
    from agent.skills.exploration import diagnostic_payload_projection
    rows=[{"id":"rule"+str(i),"app":"com.demo","path":"apps/com.demo/workflows/r"+str(i)+"/SKILL.md"} for i in range(12)] if field=="skills" else [
        {"type":"action"+str(i),"required":["type","index"],"parameters":["type","index","text"]} for i in range(12)]
    payload={"source":{"instruction":"preserve exact task"},"budget":{"remaining_calls":7}}
    if field=="skills":payload["source"]["official_skill_directory"]={"manifest_hash":"exacthash","items":rows,"omitted":32}
    else:payload["executor_action_directory"]=rows
    original=copy.deepcopy(payload);cap=len(json.dumps(payload,ensure_ascii=False,separators=(",",":")))-100
    value=diagnostic_payload_projection(payload,character_budget=cap,compact=True)
    table=value["source"]["official_skill_directory"]["items"] if field=="skills" else value["executor_action_directory"]
    assert table["encoding"]=="columnar_records"
    assert [dict(zip(table["columns"],row)) for row in table["rows"]] == rows
    assert value["source"]["instruction"]==payload["source"]["instruction"] and value["budget"]==payload["budget"]
    if field=="skills":assert value["source"]["official_skill_directory"]["manifest_hash"]=="exacthash" and value["source"]["official_skill_directory"]["omitted"]==32
    assert len(json.dumps(value,ensure_ascii=False,separators=(",",":"))) <= cap and payload == original


def test_columnar_directory_keeps_nonuniform_or_tiny_rows():
    from agent.skills.exploration import _columnar_directory
    for value in ([],[{"a":1}],[{"a":1},{"a":2},{"b":3}],None):
        assert _columnar_directory(value) is value


def test_probe_evidence_does_not_enable_compact_directory_encoding_by_itself():
    from agent.skills.learning import LearningStopped
    from agent.skills.exploration import diagnostic_payload_projection
    payload={"evidence":{},"executor_action_directory":[
        {"type":"action"+str(i),"required":["type","index"],"parameters":["type","index","text"]} for i in range(12)]}
    reference=diagnostic_payload_projection(payload)
    cap=len(json.dumps(reference,ensure_ascii=False))-50
    with pytest.raises(LearningStopped,match="diagnostic_context_budget"):
        diagnostic_payload_projection(payload,character_budget=cap)
    compact=diagnostic_payload_projection(payload,character_budget=cap-200,compact=True)
    assert compact["executor_action_directory"]["encoding"]=="columnar_records"


def test_post_review_directory_paging_preserves_exact_records_and_mandatory_context():
    from agent.skills.exploration import diagnostic_projection_with_reads, read_diagnostic_context
    rows=[{"id":str(i),"path":"apps/com.demo/"+str(i)+"/SKILL.md","description":"Exact directory text "+str(i)} for i in range(150)]
    payload={"source":{"instruction":"Exact goal", "official_skill_directory":{"manifest_hash":"frozen", "items":rows}},
        "candidate":{"new_text":"Exact candidate"},"review_feedback":{"criteria":{"conflicts":{"reason":"Exact blocking reason"}}},
        "budget":{"calls":7},"existing_active_guidance":{"items":[{"content":"Exact old rule"}]},"diagnostic_read_rounds":2}
    before=copy.deepcopy(payload);pages={}
    result=diagnostic_projection_with_reads(payload,character_budget=1600,directories=pages)
    for key in ("candidate","review_feedback","budget","existing_active_guidance"):
        assert result[key]==payload[key]
    pointer=result["source"]["official_skill_directory"]
    assert pointer["manifest_hash"]=="frozen" and pointer["read_tool"]=="read_diagnostic_context"
    offset=0;parts=[]
    while offset is not None:
        page=read_diagnostic_context(pages,pointer["ref"],offset,length=137)
        assert page["content_hash"]==pointer["content_hash"]
        parts.append(page["text"]);offset=page["next_offset"]
    assert json.loads("".join(parts))==payload["source"]["official_skill_directory"]
    assert payload==before and len(json.dumps(result,ensure_ascii=False,separators=(",",":")))<=1600
    with pytest.raises(ValueError,match="unknown"):
        read_diagnostic_context(pages,"unregistered")
    for offset,length in [(-1,10),(True,10),(0,4001),(0,0)]:
        with pytest.raises(ValueError,match="invalid"):
            read_diagnostic_context(pages,pointer["ref"],offset,length)


@pytest.mark.parametrize("feedback,reads",[(False,2),(True,0)])
def test_directory_paging_supports_probe_handoff_but_requires_read_capacity(feedback,reads):
    from agent.skills.exploration import diagnostic_projection_with_reads
    from agent.skills.learning import LearningStopped
    payload={"source":{"official_skill_directory":{"items":[{"text":"x"*4000}]}},
        "review_feedback":{"verdict":"revise"} if feedback else None,"diagnostic_read_rounds":reads}
    if not reads:
        with pytest.raises(LearningStopped,match="diagnostic_context_budget"):
            diagnostic_projection_with_reads(payload,character_budget=1000,directories={})
    else:
        pages = {}
        value = diagnostic_projection_with_reads(payload,character_budget=1000,directories=pages)
        assert value["source"]["official_skill_directory"]["ref"] in pages
        assert value["review_feedback"] == payload["review_feedback"]


@pytest.mark.asyncio
async def test_native_repair_can_read_deferred_directory_inside_existing_caps(context,monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LEARN_SYSTEM
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings,db,task,store,library=context
    backend=ExplorationBackend(task,db=db,artifacts=store.artifacts,driver=FixtureDriver(),settings=settings,library=library)
    rows=[{"id":str(i),"path":"apps/com.demo/r"+str(i)+"/SKILL.md","description":"Knowledge "*25} for i in range(100)]
    payload={"source":{"instruction":task.instruction,"official_skill_directory":{"items":rows}},
        "candidate_json_schema":{},"candidate":{"new_text":"candidate text "*600},
        "review_feedback":{"verdict":"revise","changes":["exact required revision"]},"diagnostic_read_rounds":1}
    original=copy.deepcopy(payload);seen=[]
    async def complete(model,messages,**kwargs):
        kwargs["attempt_meter"]("model_call_started",{})
        assert len(json.dumps(messages,ensure_ascii=False))<=50000
        packet=json.loads(messages[1]["content"])
        assert packet["candidate"]==payload["candidate"] and packet["review_feedback"]==payload["review_feedback"]
        assert packet["source"]["instruction"]==task.instruction
        seen.append(messages)
        if len(seen)==1:
            assert any(t["function"]["name"]=="read_diagnostic_context" for t in kwargs["tools"])
            ref=packet["source"]["official_skill_directory"]["ref"]
            return GatewayResponse(model=model,content="",tool_calls=[ToolCall(id="page",name="read_diagnostic_context",arguments=json.dumps({"ref":ref,"length":1000}))])
        page=json.loads(next(m["content"] for m in messages if m.get("tool_call_id")=="page"))
        assert page["text"]==json.dumps(payload["source"]["official_skill_directory"],ensure_ascii=False,separators=(",",":"))[:1000]
        assert page["next_offset"]==1000
        return GatewayResponse(model=model,content='{"decision":"skip","reason":"More evidence needed"}',tool_calls=[])
    monkeypatch.setattr("agent.skills.exploration.complete",complete)
    budget=LearningBudget(max_calls=4,reserve_calls=0,max_actions=0)
    try:
        result=await backend.diagnose("fake",LEARN_SYSTEM,payload,budget)
        assert result["decision"]=="skip" and budget.calls==2 and budget.actions==0 and payload==original
    finally:backend.close()


def test_large_review_pages_archived_evidence_but_keeps_actual_receipt_and_open_plan():
    from agent.skills.exploration import diagnostic_projection_with_reads, read_diagnostic_context
    receipt={"status":"closed","verified_clean":True,"unresolved_effects":0,"scope":"owned fixture"}
    payload={"source":{"instruction":"exact goal"},"candidate":{"new_text":"exact candidate"},
        "review_feedback":{"changes":["blocking revision"]},"diagnostic_read_rounds":2,
        "evidence":{"outcome":"finish","actions_attempted":15,"environment_restoration":receipt,"events":{"events":[{"text":"x"*9000}]},"notes":{"items":[{"source":"exploration/note:1@1","title":"x"*6000}]}},
        "research_session":{"environment":{"status":"open","verified_clean":False,"plan":{"scope":"live plan"}}}}
    before=copy.deepcopy(payload);pages={}
    value=diagnostic_projection_with_reads(payload,character_budget=2200,directories=pages)
    assert value["evidence"]["environment_restoration"]==receipt
    assert value["evidence"]["actions_attempted"]==15 and value["research_session"]==payload["research_session"]
    ref=value["evidence"]["ref"]
    assert json.loads(pages[ref])==payload["evidence"] and payload==before
    assert value["candidate"]==payload["candidate"] and value["review_feedback"]==payload["review_feedback"]


def test_closed_clean_plan_may_be_paged_without_altering_cleanup_facts():
    from agent.skills.exploration import diagnostic_projection_with_reads
    environment={"status":"closed","verified_clean":True,"unresolved_effects":0,
        "effect_count":18,"reset_capability":{"scope":"owned only"},"plan":{"preparation":"p"*7000}}
    payload={"source":{},"review_feedback":{"verdict":"revise"},"diagnostic_read_rounds":1,
        "research_session":{"environment":environment}}
    pages={};value=diagnostic_projection_with_reads(payload,character_budget=1500,directories=pages)
    receipt=value["research_session"]["environment"]
    assert {k:v for k,v in receipt.items() if k!="plan"}=={k:v for k,v in environment.items() if k!="plan"}
    assert json.loads(pages[receipt["plan"]["ref"]])==environment["plan"]


def test_review_contract_distinguishes_full_file_from_exact_executor_activation(context):
    from agent.skills.exploration import build_review_contracts
    from agent.skills.learning import digest
    settings,db,task,store,library=context
    path=library.root/"apps/com.demo/workflows/test_route/SKILL.md"
    path.parent.mkdir(parents=True);path.write_text(workflow(),encoding="utf-8")
    contracts=build_review_contracts(workflow(),library,settings)
    item=next(x for x in contracts["related_skills"] if x["id"]=="route")
    assert item["hash"]==digest(workflow())
    assert item["hash_scope"]=="complete_SKILL.md_utf8"
    assert item["executor_projection_hash"]==stable_hash(parse_skill_markdown(workflow()).section_for("executor"))
    assert item["hash"]!=item["executor_projection_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_action", [False, True])
async def test_note_rejection_resolves_undispatched_research_intent(context, monkeypatch, retry_action):
    from agent.skills.exploration import ExplorationBackend
    from driver.fixture import FixtureDriver
    from shared.llm_gateway import GatewayResponse, ToolCall
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts,
        driver=FixtureDriver(), settings=settings, library=library)
    requests = []
    async def complete(model, messages, **kwargs):
        kwargs["attempt_meter"]("model_call_started", {})
        requests.append(messages)
        runtime = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
            if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
        args = {"decision": "act" if len(requests) == 1 or retry_action and len(requests) == 2 else "finish",
            "summary": "Inspect current screen", "observation_id": runtime["current_observation_id"]}
        if args["decision"] == "act": args["action"] = {"type": "home"}
        if len(requests) == 1:
            args["notes"] = [{"note_key": "contradiction", "content": "raw finding",
                "observation_ids": [runtime["current_observation_id"]],
                "unresolved": "still a question", "resolution": "also resolved"}]
        return GatewayResponse(model=model, content="", tool_calls=[ToolCall(
            id="step" + str(len(requests)), name="submit_executor_step", arguments=json.dumps(args))])
    monkeypatch.setattr("agent.session.complete", complete)
    budget = LearningBudget(max_calls=6, reserve_calls=1, max_actions=4)
    try:
        result = await backend.explore("Inspect current screen", 3, budget)
        assert result["outcome"] == "finish"
        effects = backend.environment.effects
        assert len(effects) == 1 + int(retry_action)
        assert effects[0]["result"]["receipt"]["dispatch_succeeded"] is False
        assert effects[0]["verified"] is True
        assert backend.executor.intent_id is None
        assert budget.actions == 1 + int(retry_action)  # Rejected attempts still count.
        assert not backend.store.records("note")
        assert len(backend.store.records("event")) == 1 + int(retry_action)
        assert backend.environment.receipt()["unresolved_effects"] == 0
    finally:
        backend.close()


def test_post_probe_paging_keeps_latest_frontier_and_open_plan():
    from agent.skills.exploration import diagnostic_projection_with_reads, read_diagnostic_context
    payload = {"source": {"instruction": "Keep full source goal"}, "review_feedback": [],
        "candidate_json_schema": {"type": "object"}, "diagnostic_read_rounds": 2,
        "research_session": {"environment": {"status": "open", "verified_clean": False,
            "unresolved_effects": 2, "plan": {"cleanup": "reserved actual reset"}}},
        "evidence": {"question": "Is transition supported?", "outcome": "replan",
            "frontier": {"source": "exploration/event:7@1", "report": "Observed negative transition"},
            "learning_task_id": "native-probe", "notes": {"items": [{"preview": "raw " * 8000}]}}}
    before = copy.deepcopy(payload); pages = {}
    result = diagnostic_projection_with_reads(payload, character_budget=1400, directories=pages)
    assert result["evidence"]["frontier"] == payload["evidence"]["frontier"]
    assert result["research_session"] == payload["research_session"]
    assert result["candidate_json_schema"] == payload["candidate_json_schema"]
    assert result["review_feedback"] == []
    ref = result["evidence"]["ref"]
    assert json.loads(pages[ref]) == payload["evidence"] and payload == before


def test_archived_probe_directory_exposes_literal_final_negative_with_scoped_reference(context):
    from agent.skills.exploration import ExplorationBackend
    settings, db, task, store, library = context
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=SimpleNamespace(),
        settings=settings, library=library)
    row = store.put("event", "7", {"step": 7, "decision": "replan",
        "executor_report": "Attempted transition did not occur; cause remains unknown."})
    backend.add_history("prior", store, task.state)
    backend.add_probe_evidence("prior", {"learning_task_id": task.id, "question": "Check transition",
        "outcome": "replan", "frontier": {"source": "exploration/event:7@1", "decision": "replan",
            "report": row["payload"]["executor_report"]}})
    frontier = backend.probe_directory()["items"][0]["frontier"]
    assert frontier["source"] == "prior/event:7@1"
    assert backend.resolve_evidence(frontier["source"])[1]["payload"]["executor_report"] == frontier["report"]
    assert not frontier["truncated"] and "not independent" in frontier["policy"]


@pytest.mark.parametrize("schema", [False, True])
def test_initial_diagnosis_preserves_source_facts_until_capacity_requires_directory(schema):
    import copy
    from agent.skills.exploration import diagnostic_payload_projection
    payload = {"initial_diagnosis": True, "source": {
        "instruction": "Preserve the target while inspecting it",
        "events": [{"source": "event:4@1", "decision": "replan", "preview": "Open failed after enabling the prerequisite"}],
        "observations": [{"source": "observation:5@1", "preview": "Prerequisite is now enabled"}],
        "action_costs": {"actions": 4, "transitions": [{"before": "disabled", "after": "enabled"}]}}}
    if schema:
        payload["candidate_json_schema"] = {"type": "object"}
    original = copy.deepcopy(payload)
    result = diagnostic_payload_projection(payload, character_budget=4000, compact=True)
    assert result["source"] == original["source"]
    projected = diagnostic_payload_projection({**payload, "_source_history_directory": True}, character_budget=4000, compact=True)
    assert projected["source"]["events"][0]["source"] == "event:4@1"
    assert "preview" not in projected["source"]["events"][0]
    assert projected["source"]["post_review_history_policy"]["omitted_preview_fields"] == 2
    assert payload == original


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled,actions", [(True, 20), (False, 0), (False, 20)])
async def test_initial_action_context_matches_actual_exploration_capacity(context, monkeypatch, disabled, actions):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import TARGET_SYSTEM, Candidate, LearningBudget, build_packet
    from shared.llm_gateway import GatewayResponse
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    before = build_packet(task, store)
    async def complete(model, messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        readonly = disabled or actions == 0
        assert payload["live_exploration_disabled"] is readonly
        assert "executor_action_directory" in payload and "executor_action_contract" not in payload
        assert all(("parameters" in item) is not readonly for item in payload["executor_action_directory"])
        assert payload["source"] == before
        kwargs["attempt_meter"]("model_call_started", {})
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"No supported useful rule"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(), settings=settings, library=library)
    budget = LearningBudget(max_actions=actions)
    await backend.diagnose("test", TARGET_SYSTEM, {"source": before, "initial_diagnosis": True,
        "candidate_json_schema": Candidate.model_json_schema(), "live_exploration_disabled": disabled}, budget)
    assert budget.actions == 0 and budget.calls == 1 and backend.record is None


@pytest.mark.asyncio
async def test_diagnosis_keeps_previous_evidence_when_next_small_read_fits(context, monkeypatch):
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LEARN_SYSTEM, Candidate, LearningBudget
    from shared.llm_gateway import GatewayResponse, ToolCall
    from driver.fixture import FixtureDriver
    settings, db, task, store, library = context
    store.put("note", "long", {"title": "long", "content": "observed fact " * 600})
    store.put("note", "short", {"title": "short", "content": "dependent fact"})
    calls = []
    async def complete(model, messages, **kwargs):
        calls.append(messages)
        assert len(json.dumps(messages, ensure_ascii=False)) <= 50000
        kwargs["attempt_meter"]("model_call_started", {})
        if len(calls) <= 2:
            key = "long" if len(calls) == 1 else "short"
            return GatewayResponse(model=model, content="", tool_calls=[ToolCall(id=key, name="read_history",
                arguments=json.dumps({"source": "note:" + key + "@1", "full": True}))])
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        assert set(results) == {"long", "short"}
        assert "observed fact" in results["long"]["data"]["items"][0]["text"]
        assert "dependent fact" in results["short"]["data"]["items"][0]["text"]
        return GatewayResponse(model=model, content='{"decision":"skip","reason":"Both linked facts considered"}')
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    backend = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=FixtureDriver(), settings=settings, library=library)
    await backend.diagnose("test", LEARN_SYSTEM, {"source": {"instruction": task.instruction},
        "candidate_json_schema": Candidate.model_json_schema(), "required_conditions": "p" * 12000,
        "diagnostic_read_rounds": 2}, LearningBudget())
