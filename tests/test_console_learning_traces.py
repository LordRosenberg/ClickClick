"""Console source isolation, invalid outputs and missing-cost semantics."""
import copy
import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from agent.skills.exploration import ExplorationBackend
from agent.skills.learning import LearningBudget
from agent.skills.telemetry import ResearchTelemetry
from agent.skills.pending import stage_pending_patch
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState
from shared.llm_gateway import GatewayResponse, ToolCall
from test_task_skill_learning import context


@pytest.mark.asyncio
async def test_reviewer_transcript_keeps_independent_reads_and_invalid_output_without_source_writes(context, monkeypatch):
    settings, db, task, store, library = context
    b = ExplorationBackend(task, db=db, artifacts=store.artifacts, driver=SimpleNamespace(), settings=settings, library=library)
    before = copy.deepcopy(store.records("observation"))
    budget = LearningBudget(max_calls=5)
    requests = []
    async def complete(model, messages, **kwargs):
        requests.append(copy.deepcopy(messages))
        kwargs["attempt_meter"]("model_call_started", {})
        if len(requests) == 1:
            return GatewayResponse(model=model, content="inspect evidence", tool_calls=[ToolCall(
                id="read-1", name="read_review_evidence", arguments=json.dumps({"source":"source/observation:screen@1"}))])
        return GatewayResponse(model=model, content="invalid JSON reviewer output")
    monkeypatch.setattr("agent.skills.exploration.complete", complete)
    with pytest.raises(ValueError, match="invalid skill review JSON"):
        await b.review("test-model", "independent review", {"candidate": "rule"}, budget)
    directory = store.artifacts.root / "skill-learning" / "runs" / task.id / b.telemetry.id
    records = [json.loads(path.read_text(encoding="utf-8")) for path in (directory / "records").glob("*.json")]
    final = next(row for row in records if row["status"] == "output_received")
    messages = final["payload"]["messages"]
    assert messages[0]["content"] == "independent review"
    assert messages[-1]["content"] == "invalid JSON reviewer output"
    assert any(row.get("role") == "tool" and "Search" in row["content"] for row in messages)
    assert "invalid JSON reviewer output" not in str(requests[-1])
    assert store.records("observation") == before and budget.calls == 2
    assert final["cost"]["provider_usage"]["unknown_responses"] == 2
    b.close()


def test_telemetry_redacts_json_credentials_images_and_has_no_prompt_side_effects(tmp_path):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    trace = ResearchTelemetry(artifacts, "source-id")
    raw = {"messages": [{"role":"user", "content":json.dumps({"authorization":"test-private", "app":"demo"})},
                        {"role":"user", "content":[{"type":"image_url", "image_url":{"url":"data:image/png;base64,AAAA"}}]}]}
    before = copy.deepcopy(raw)
    trace.record("learner", "diagnosis", raw, cost={"calls":1}, histories={})
    data = next((artifacts.root / "skill-learning/runs/source-id" / trace.id / "records").glob("*.json")).read_text(encoding="utf-8")
    assert "test-private" not in data and "AAAA" not in data and "demo" in data
    assert raw == before
    assert "[REDACTED]" in data and "image attachment omitted" in data


@pytest.mark.asyncio
async def test_console_reads_external_source_only_reuses_task_execution_links_and_lazy_refs(tmp_path, monkeypatch):
    primary_root = tmp_path / "primary"
    eval_root = tmp_path / "eval"
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(primary_root))
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    db = Database(eval_root / "clickclick.db")
    artifacts = ArtifactStore(eval_root / "artifacts")
    task = db.create_task("source", AgentState(instruction="source"))
    probe = db.create_task("explore", AgentState(instruction="explore"))
    trace = ResearchTelemetry(artifacts, task.id)
    trace.record("reviewer", "candidate_review", {"messages":[{"role":"assistant", "content":"local rule only"}]},
                 cost={"calls":2,"actions":0}, histories={"exploration":SimpleNamespace(task_id=probe.id), "missing_external":SimpleNamespace(task_id="missing-child")})
    artifacts.save_json("skill-learning/conversations/legacy-mixed", {"job_id":"legacy-mixed",
        "source_binding":{"task_id":task.id}, "version":1, "history":[]})
    unrelated = ResearchTelemetry(artifacts, "unrelated-source")
    unrelated.record("learner", "diagnosis", {"secret_app":"other-task-data"}, cost={}, histories={})
    # A fake catalog with the requested ID in a different artifact root cannot win.
    collision = ResearchTelemetry(app.state.artifacts, task.id)
    collision.record("learner", "diagnosis", {"wrong_root":True}, cost={}, histories={})
    sources = app.state.data_sources
    monkeypatch.setattr(sources, "_discover_paths", lambda: {db.path.resolve()})
    sources.refresh()
    stage_pending_patch(target_rel="apps/demo/core/SKILL.md", new_text="rule", gist="local rule",
        source_task_id=task.id, root=tmp_path / "skills", review_metadata={"review":{"verdict":"pass"}, "eligible":True})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/tasks/{task.id}/learning-traces")
            assert response.status_code == 200
            data = response.json()
            assert data["read_only"] and data["running"] is False
            assert {row["job_id"] for row in data["sessions"]} == {trace.id, "legacy-mixed"}
            filtered = (await client.get(f"/api/tasks/{task.id}/learning-traces", params={"job_id":trace.id})).json()
            assert filtered["session_total"] == 1 and filtered["sessions"][0]["job_id"] == trace.id
            legacy = (await client.get(f"/api/tasks/{task.id}/learning-traces", params={"job_id":"legacy-mixed"})).json()
            assert legacy["session_total"] == 1 and legacy["sessions"][0]["job_id"] == "legacy-mixed"
            session = next(row for row in data["sessions"] if row["job_id"] == trace.id)
            assert session["histories"][0]["task_id"] == probe.id and session["histories"][0]["available"]
            assert session["histories"][1]["available"] is False and session["histories"][1]["status"] is None
            assert "payload" not in session["entries"][0]  # Full transcripts are lazy.
            assert data["pending"][0]["verdict"] == "pass"
            ref = session["entries"][0]["ref"]
            transcript = (await client.get(f"/api/tasks/{task.id}/artifacts/{ref}")).json()
            assert "local rule only" in json.dumps(transcript)
            assert "wrong_root" not in json.dumps(data) and "other-task-data" not in json.dumps(data)
            assert (await client.get(f"/api/tasks/{probe.id}/timeline")).status_code == 200
            assert (await client.get("/api/tasks/missing/learning-traces")).status_code == 404
    finally:
        sources.close(); app.state.db.close(); db.close()


@pytest.mark.asyncio
async def test_legacy_checkpoints_choose_latest_without_inventing_review_or_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    db = app.state.db
    task = db.create_task("historical", AgentState(instruction="historical"))
    artifacts = app.state.artifacts
    for version in (1,3,2):
        artifacts.save_json("skill-learning/conversations/historical-job", {"job_id":"historical-job",
            "source_binding":{"task_id":task.id}, "version":version, "analysis":{"status":"unknown"}, "history":[]})
    artifacts.save_json("skill-learning/conversations/other-job", {"job_id":"other-job", "source_binding":{"task_id":"different"}, "version":100})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            data = (await client.get(f"/api/tasks/{task.id}/learning-traces")).json()
            assert len(data["sessions"]) == 1
            session = data["sessions"][0]
            assert session["version"] == 3 and session["cost"] is None
            assert session["entries"][0]["role"] == "learner"
            assert not data["pending"] and not data["results"]
    finally:
        app.state.data_sources.close(); db.close()


@pytest.mark.asyncio
async def test_authorized_skipped_learning_has_persisted_end_result_without_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    task = app.state.db.create_task("source", AgentState(instruction="source"))
    async def learn(*args, **kwargs):
        return {"ok":True,"skipped":True,"reason":"no_candidate_signal", "cost":{"calls":0,"actions":0}}
    monkeypatch.setattr(app.state.orchestrator, "learn_from_task", learn)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/tasks/{task.id}/learn", json={"accept_model_cost":True,"allow_device_operations":True})
            assert response.status_code == 200
            data = (await client.get(f"/api/tasks/{task.id}/learning-traces")).json()
            assert not data["pending"] and data["results"][0]["reason"] == "no_candidate_signal"
            assert data["results"][0]["cost"]["calls"] == 0
    finally:
        app.state.data_sources.close(); app.state.db.close()

@pytest.mark.asyncio
async def test_native_outcome_catalog_keeps_terminal_cost_distinct_from_prior_sampling(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    task = app.state.db.create_task("source", AgentState(instruction="source"))
    trace = ResearchTelemetry(app.state.artifacts, task.id)
    trace.record("reviewer", "candidate_review", {"decision":{"verdict":"pass"}}, cost={"calls":2}, histories={})
    trace.record("learner", "learning_outcome", {"result":{"ok":True,"eligible":True,"reason":"awaiting_human_approval"}}, cost={"calls":7,"actions":2,"provider_usage":{"unknown_responses":7}}, histories={},status="completed")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session = (await client.get(f"/api/tasks/{task.id}/learning-traces")).json()["sessions"][0]
            assert session["cost"]["calls"] == 7
            entry = session["entries"][-1]
            assert entry["phase"] == "learning_outcome" and entry["status"] == "completed"
            record = (await client.get(f"/api/tasks/{task.id}/artifacts/{entry['ref']}")).json()
            assert record["payload"]["result"]["eligible"] is True
            assert record["cost"]["provider_usage"]["unknown_responses"] == 7
    finally:
        app.state.data_sources.close(); app.state.db.close()

@pytest.mark.asyncio
async def test_learning_trace_windows_are_explicit_and_older_records_stay_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    task = app.state.db.create_task("source", AgentState(instruction="source"))
    traces = []
    for job in range(3):
        trace = ResearchTelemetry(app.state.artifacts, task.id)
        for i in range(8):
            trace.record("learner", "diagnosis", {"messages": [{"role":"assistant","content":str(i)}]},
                cost={"calls":i+1,"provider_usage":{"records":[{"total_tokens":20}],"unknown_responses":0}}, histories={})
        traces.append(trace)
    for i in range(4):
        app.state.artifacts.save_json("skill-learning/results/" + task.id,
            {"source_task_id":task.id,"created_at":i,"status":"stopped","result":{"reason":"test"}})
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/tasks/{task.id}/learning-traces?session_limit=1&entry_limit=3&result_limit=2")
            data = response.json()
            assert data["session_total"] == 3 and len(data["sessions"]) == 1
            assert data["result_total"] == 4 and len(data["results"]) == 2
            session = data["sessions"][0]
            assert [entry["order"] for entry in session["entries"]] == [6,7,8]
            assert session["entry_total"] == 8 and session["older_entry_count"] == session["omitted_entry_count"] == 5
            assert "records" not in session["cost"]["provider_usage"]
            assert session["cost"]["provider_usage"]["total_tokens"] == 20
            older = (await client.get(f"/api/tasks/{task.id}/learning-traces?job_id={session['job_id']}&entry_before=6&entry_limit=3")).json()["sessions"][0]
            assert [entry["order"] for entry in older["entries"]] == [3,4,5]
            ref = older["entries"][0]["ref"]
            assert (await client.get(f"/api/tasks/{task.id}/artifacts/{ref}")).status_code == 200
            catalog = (await client.get(f"/api/tasks/{task.id}/artifacts/{session['catalog_ref']}")).json()
            assert len(catalog["entries"]) == 8
            next_page = (await client.get(f"/api/tasks/{task.id}/learning-traces?session_limit=1&session_offset=1&result_limit=2&result_offset=2")).json()
            assert next_page["sessions"][0]["job_id"] != session["job_id"] and len(next_page["results"]) == 2
            for query in ("session_limit=21", "entry_limit=101", "session_offset=-1", "entry_before=0", "result_limit=21"):
                assert (await client.get(f"/api/tasks/{task.id}/learning-traces?{query}")).status_code == 422
    finally:
        app.state.data_sources.close(); app.state.db.close()
