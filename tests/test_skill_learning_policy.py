"""User cost, private evidence, consent and official inbox boundaries."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from agent.revisable.store import TaskStore
from agent.runtime import create_orchestrator
from agent.skills.learning import LearningStopped
from agent.skills.policy import (ContributionReport, LearningPreferences, PersonalLearningRequest,
    export_contribution, preferences, record_local_clue, save_preferences)
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from evaluation.learning_inbox import aggregate_reports
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus


@pytest.fixture
def local(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", use_fixture_driver=True)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    task = db.create_task("Private patient Alice: secret-api-key; " * 300,
        AgentState(instruction="PRIVATE", skill_learn=True), device_serial="private-device")
    db.update_task(task.id, status=TaskStatus.FAILED, failure_reason="PRIVATE ERROR")
    task = db.get_task(task.id)
    store = TaskStore(db, artifacts, task.id)
    store.put("observation", "private-screen", {"app": "com.demo", "text": "PRIVATE SCREEN",
        "image_ref": "private-image"})
    for i in range(3):
        store.put("event", str(i), {"step": i, "submitted_action": {
            "type": "type", "text": "PRIVATE TEXT", "x": 200, "y": 300},
            "action_result": {"receipt": {"visible_change": False}}, "executor_report": "PRIVATE REPORT"})
    yield settings, db, task, store, artifacts
    db.close()


def orchestrator(local, mode="legacy"):
    settings, db, _, _, artifacts = local
    return create_orchestrator(db, TraceWriter(db, artifacts),
        settings=settings.model_copy(update={"skill_learning_mode": mode}),
        driver=FixtureDriver(), artifacts=artifacts)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "task_explore"])
@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
@pytest.mark.parametrize("old_flag", [False, True])
async def test_terminal_only_records_without_models_or_device(local, monkeypatch, mode, outcome, old_flag):
    _, db, task, _, _ = local
    task.state.skill_learn = old_flag
    db.update_task(task.id, state=task.state)
    orch = orchestrator(local, mode)
    def forbidden(*args, **kwargs): raise AssertionError("no post-task model/device/research")
    monkeypatch.setattr(orch, "learn_from_task", forbidden)
    monkeypatch.setattr(orch, "_resolve_driver", forbidden)
    monkeypatch.setattr("agent.skills.learner.run_skill_learner", forbidden)
    monkeypatch.setattr("agent.skills.learning.run_task_learning", forbidden)
    if outcome == "succeeded": orch._succeed(task.id, task.state, "done")
    elif outcome == "failed": orch._fail(task.id, task.state, "failed")
    else:
        db.update_task(task.id, status=TaskStatus.RUNNING)
        orch._cancel(task.id, task.state, mode="soft")
    await asyncio.sleep(0)
    orch._schedule_skill_learner(task.id)
    rows = db.list_agent_records(task.id, "learning_clue")
    assert len(rows) == 1 and rows[0]["version"] == 1
    assert rows[0]["payload"]["research_calls"] == rows[0]["payload"]["research_actions"] == 0
    assert rows[0]["payload"]["report"]["outcome"] == outcome
    assert not orch._learning_jobs and db.get_task(task.id).status.value == outcome


def test_collection_off_and_contribution_independent(local):
    _, db, task, store, _ = local
    assert preferences(db).model_dump() == {"local_recording": True, "contribution_enabled": False}
    save_preferences(db, LearningPreferences(local_recording=False, contribution_enabled=True))
    assert record_local_clue(task, store)["reason"] == "local_recording_disabled"
    assert not store.records("learning_clue")
    with pytest.raises(ValueError, match="not_found"): export_contribution(task.id, db)
    save_preferences(db, LearningPreferences())
    record_local_clue(task, store)
    with pytest.raises(PermissionError, match="disabled"): export_contribution(task.id, db)


def test_preferences_survive_reopen_and_revocation_blocks_old_reports(local):
    settings, db, task, store, _ = local
    record_local_clue(task, store)
    save_preferences(db, LearningPreferences(contribution_enabled=True))
    second = Database(settings.db_path)
    try:
        assert preferences(second).contribution_enabled
        assert export_contribution(task.id, second)["signals"] == [
            "task_failed", "repeated_action_without_visible_change"]
        save_preferences(second, LearningPreferences())
        with pytest.raises(PermissionError): export_contribution(task.id, db)
    finally: second.close()


def test_export_is_allowlisted_even_with_private_saved_fields(local):
    _, db, task, store, _ = local
    record_local_clue(task, store)
    saved = copy.deepcopy(store.get("learning_clue", "terminal")["payload"])
    saved["report"]["instruction"] = "PRIVATE INJECTED"
    saved["report"]["counts"]["failure_reason"] = "PRIVATE"
    store.put("learning_clue", "terminal", saved)
    save_preferences(db, LearningPreferences(contribution_enabled=True))
    report = export_contribution(task.id, db)
    text = json.dumps(report)
    assert all(secret not in text for secret in ["Private", "PRIVATE", "private-device", task.id,
        "private-screen", "private-image", "secret-api-key", "source_refs", "input_text\": {", '"x"', '"y"'])
    assert report["apps"] == ["com.demo"] and report["action_types"] == {"type": 3}
    assert set(report) == set(ContributionReport.model_fields)
    assert report["report_id"] != task.id
    again = export_contribution(task.id, db)
    assert again == report


@pytest.mark.asyncio
@pytest.mark.parametrize("research_request", [None, {}, {"accept_model_cost": True}, {"allow_device_operations": True}])
async def test_flags_force_or_partial_consent_cannot_start_research(local, monkeypatch, research_request):
    _, _, task, _, _ = local
    orch = orchestrator(local, "task_explore")
    def forbidden(*args, **kwargs): raise AssertionError("unauthorized access")
    monkeypatch.setattr(orch, "_resolve_driver", forbidden)
    monkeypatch.setattr("agent.skills.exploration.ExplorationBackend", forbidden)
    result = await orch.learn_from_task(task.id, force=True, request=research_request)
    assert not result["ok"] and "required" in result["reason"]
    assert not orch._learning_jobs


@pytest.mark.asyncio
async def test_personal_run_uses_accepted_caps_and_strict_pipeline(local, monkeypatch):
    _, _, task, store, _ = local
    orch = orchestrator(local)  # Legacy deployment cannot bypass independent review.
    calls = []
    class Backend:
        def __init__(self, *args, **kwargs):
            self.source_store = store; self.library = SimpleNamespace()
        async def release_device(self): calls.append("release")
        def close(self): calls.append("close")
    async def learn(*args, budget, manual, **kwargs):
        assert manual
        assert (budget.max_calls, budget.max_actions, budget.max_seconds, budget.max_job_seconds) == (12, 2, 30, 30)
        for _ in range(12): budget.meter("model_call_started", {})
        with pytest.raises(LearningStopped, match="model_budget"): budget.meter("model_call_started", {})
        budget.action(); budget.action()
        with pytest.raises(LearningStopped, match="action_budget"): budget.action()
        return {"ok": True, "skipped": True}
    def forbidden(*args, **kwargs): raise AssertionError("no legacy quality bypass")
    monkeypatch.setattr("agent.skills.learner.run_skill_learner", forbidden)
    monkeypatch.setattr("agent.skills.exploration.ExplorationBackend", Backend)
    monkeypatch.setattr("agent.skills.learning.run_task_learning", learn)
    result = await orch.learn_from_task(task.id, request=PersonalLearningRequest(
        accept_model_cost=True, allow_device_operations=True, max_calls=12, max_actions=2, max_seconds=30))
    assert result["billing_scope"] == "personal" and result["publication_scope"] == "local_pending"
    assert calls == ["release", "close"] and not orch._learning_jobs
    assert store.get("learning_request", "personal")["payload"]["max_actions"] == 2


@pytest.mark.parametrize("change", [{"max_calls": 1000}, {"max_actions": 0}, {"max_seconds": -1},
    {"max_calls": True}, {"accept_model_cost": "true"}, {"billing_scope": "official"}])
def test_invalid_or_self_declared_official_personal_request(change):
    with pytest.raises(ValidationError): PersonalLearningRequest.model_validate(change)


def test_inbox_deduplicates_without_granting_publication(local):
    _, db, task, store, _ = local
    record_local_clue(task, store)
    save_preferences(db, LearningPreferences(contribution_enabled=True))
    report = export_contribution(task.id, db)
    output = aggregate_reports([report, report])
    assert output["reports"] == 1
    assert all(issue["reports"] == 1 and issue["status"] == "needs_operator_reproduction" for issue in output["issues"])
    assert not output["publication_eligible"] and output["model_calls"] == output["device_actions"] == 0
    other = {**report, "outcome": "succeeded"}
    with pytest.raises(ValueError, match="conflicting duplicate"): aggregate_reports([report, other])
    with pytest.raises(ValidationError): aggregate_reports([{**report, "instruction": "PRIVATE"}])
    with pytest.raises(ValueError): aggregate_reports([{**report, "apps": ["private@example.com"]}])
    with pytest.raises(ValidationError): aggregate_reports([{**report, "action_types": {"PRIVATE": 1}}])
    with pytest.raises(ValidationError): aggregate_reports([{**report, "action_types": {"tap": True}}])


@pytest.mark.asyncio
async def test_api_consent_export_and_budget_boundaries(local, monkeypatch):
    settings, _, _, _, _ = local
    monkeypatch.setattr("control_api.main.get_settings", lambda: settings)
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    task = app.state.db.create_task("PRIVATE", AgentState(instruction="PRIVATE"))
    app.state.db.update_task(task.id, status=TaskStatus.FAILED)
    task = app.state.db.get_task(task.id)
    record_local_clue(task, TaskStore(app.state.db, app.state.artifacts, task.id))
    received = []
    async def learn(task_id, *, request): received.append(request); return {"ok": True, "skipped": True}
    monkeypatch.setattr(app.state.orchestrator, "learn_from_task", learn)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            url = f"/api/tasks/{task.id}/learn"
            assert (await client.post(url)).status_code == 422
            assert (await client.post(url, json={})).status_code == 400
            assert (await client.post(url, json={"accept_model_cost": True})).status_code == 400
            body = {"accept_model_cost": True, "allow_device_operations": True, "max_calls": 12, "max_actions": 5, "max_seconds": 60}
            assert (await client.post(url, json={**body, "max_calls": 1000})).status_code == 422
            assert not received
            assert (await client.post(url, json=body)).status_code == 200
            assert received[0].model_dump() == body
            report_url = f"/api/tasks/{task.id}/learning-contribution"
            assert (await client.get(report_url)).status_code == 403
            assert (await client.put("/api/learning/preferences", json={"local_recording": True, "contribution_enabled": True})).status_code == 200
            assert (await client.get(report_url)).status_code == 200
            assert (await client.put("/api/learning/preferences", json={"local_recording": True, "contribution_enabled": False})).status_code == 200
            assert (await client.get(report_url)).status_code == 403
    finally:
        app.state.db.close()


def test_missing_counts_stay_unknown(local):
    _, db, task, store, _ = local
    task = task.model_copy(update={"state": None})
    record_local_clue(task, store)
    save_preferences(db, LearningPreferences(contribution_enabled=True))
    counts = export_contribution(task.id, db)["counts"]
    assert counts["actions"] is None and counts["model_requests"] is None


def test_operator_inbox_cli_has_no_learning_side_effects(local, tmp_path):
    import subprocess
    import sys
    _, db, task, store, _ = local
    record_local_clue(task, store)
    save_preferences(db, LearningPreferences(contribution_enabled=True))
    report = tmp_path / "report.json"
    report.write_text(json.dumps(export_contribution(task.id, db)), encoding="utf-8")
    output = tmp_path / "issues.json"
    result = subprocess.run([sys.executable, "-m", "evaluation.learning_inbox",
        str(report), str(report), "--output", str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    aggregate = json.loads(output.read_text(encoding="utf-8"))
    assert aggregate["reports"] == 1 and not aggregate["publication_eligible"]
    assert aggregate["model_calls"] == aggregate["device_actions"] == 0
