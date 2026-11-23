import asyncio
import json
from copy import deepcopy

from httpx import ASGITransport, AsyncClient

from agent.runtime import create_orchestrator
from agent.skills.library import serialize_skill_markdown
from agent.skills.version_audit import audit_task_versions
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import TaskStatus
from tests.test_task_pause import app, reply


async def test_opt_in_build_capture_keeps_normal_task_actions_and_calls_unchanged(tmp_path, monkeypatch):
    package = "com.example.demo"
    skill = "demo-play"
    rule = "On the Demo App screen in the Ready to start state, the primary playback button is labeled exactly Play."
    skills_dir = tmp_path / "skills"
    path = skills_dir / "apps" / package / "workflows" / skill / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(serialize_skill_markdown(
        name=skill, description="Start playback", version="1.0.0", app=package,
        kind="workflow", capability="play",
        body=f"## Procedure\n{rule} Tap that button.\n\n## Verification\nCheck Playing.",
    ), encoding="utf-8")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(skills_dir))

    results = []
    for enabled in (False, True):
        settings = Settings(_env_file=None, data_dir=tmp_path / str(enabled), default_model="fixture",
                            jev_mode="off", skill_version_audit_enabled=enabled)
        db = Database(settings.db_path)
        artifacts = ArtifactStore(settings.artifacts_dir)
        driver = FixtureDriver()
        runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings,
                                      driver=driver, artifacts=artifacts)
        audited = asyncio.Event()
        release_audit = asyncio.Event()
        calls, reads = [], []
        task = db.create_task("Start playback in com.example.demo", device_serial="fixture")
        original_act = driver.act
        driver.tree = deepcopy(driver.tree)
        button = driver.tree["tree"]["children"][0]
        status = {"class": "android.widget.TextView", "text": "Ready to start", "bounds": [100, 600, 450, 680]}
        driver.tree["tree"]["children"].append(status)
        build_code = "2"
        playing = False

        async def act(action):
            nonlocal playing
            result = await original_act(action)
            if action.type == "tap_xy" and (action.x, action.y) == (250, 250):
                playing = True
                status["text"] = "Playing"
            return result

        async def read_build(app):
            reads.append(app)
            return {"package": app, "version_code": build_code, "version_name": f"{build_code}.0"}

        async def audit(finished, store, current_settings, builds):
            assert finished.status == TaskStatus.SUCCEEDED
            assert builds[package]["version_code"] == "2"
            assert builds[package]["observation_id"]
            assert store.task_id == finished.id
            await audit_task_versions(finished, store, current_settings, builds)
            audited.set()
            await release_audit.wait()
            raise RuntimeError("optional provider failure")

        async def complete(model, messages, *, tools, **kwargs):
            await asyncio.sleep(0)
            kwargs["attempt_meter"]("model_call_started", {})
            names = {tool["function"]["name"] for tool in tools}
            if "submit_planner_decision" in names:
                calls.append("planner")
                if driver.actions:
                    assert playing
                    return reply("submit_planner_decision", {"decision": "complete", "reason": "Playing is visible"})
                return reply("submit_planner_decision", {"decision": "execute", "reason": "Play",
                             "plan": {"current_stage": {"goal": "Start playback", "target_app": package,
                                                        "skill_ids": [skill]}}})
            calls.append("executor")
            update = next(json.loads(m["content"])["runtime_update"] for m in reversed(messages)
                          if isinstance(m.get("content"), str) and m["content"].startswith('{"runtime_update"'))
            assert any(rule in str(m.get("content")) for m in messages)
            args = {"decision": "finish" if driver.actions else "act", "summary": "Play",
                    "observation_id": update["current_observation_id"]}
            if not driver.actions:
                args["action"] = {"type": "tap", "index": 0}
            return reply("submit_executor_step", args)

        driver.read_app_build = read_build
        driver.act = act
        monkeypatch.setattr("agent.session.complete", complete)
        monkeypatch.setattr("agent.skills.version_audit.audit_task_versions", audit)
        try:
            assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
            if enabled:
                await asyncio.wait_for(audited.wait(), timeout=2)
                assert runtime._skill_version_audits
                assert reads == [package]
                snapshots = db.list_agent_records(task.id, "skill_app_build")
                assert snapshots[0]["payload"]["version_code"] == "2"
                findings = db.list_agent_records(task.id, "skill_version_audit")
                assert [(row["payload"]["skill_id"], row["payload"]["status"])
                        for row in findings] == [(skill, "baseline")]
                assert findings[0]["payload"]["window_sources"]
            else:
                assert reads == []
                assert db.list_agent_records(task.id, "skill_app_build") == []
            state = db.get_task(task.id).state
            results.append((list(calls), [act.type for act in driver.actions],
                            state.role_invocation_count, state.revisable.execution_count))
            if enabled:
                release_audit.set()
                await asyncio.gather(*runtime._skill_version_audits)
                monkeypatch.setattr("agent.skills.version_audit.audit_task_versions", audit_task_versions)
                audit_calls = []

                async def learn(finished, *, settings, library, store, backend, manual, version_changes, budget):
                    assert db.get_task(finished.id).status == TaskStatus.SUCCEEDED
                    assert backend.driver is None and not manual and budget.max_actions == 0
                    budget.meter("model_call_started", {})
                    change, = version_changes
                    audit_calls.append(change)
                    assert rule in change["projection"]
                    observations = [row["payload"]["text"] for row in store.records("observation")]
                    assert any('raw_text="Start"' in text and "Ready to start" in text for text in observations)
                    assert any("Playing" in text for text in observations)
                    return {"ok": True, "pending_id": "changed-build-proposal", "eligible": False,
                            "reason": "candidate_staged"}

                monkeypatch.setattr("agent.skills.learning.run_task_learning", learn)
                build_code = "3"
                button["text"] = "Start"
                status["text"] = "Ready to start"
                playing = False
                driver.actions.clear()
                calls.clear()
                task = db.create_task("Start playback in com.example.demo", device_serial="fixture")
                assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED and playing
                await asyncio.gather(*runtime._skill_version_audits)
                record, = db.list_agent_records(task.id, "skill_version_audit")
                assert record["payload"]["assessment"] == "unassessed"
                assert record["payload"]["pending_id"] == "changed-build-proposal"
                assert record["payload"]["build"]["version_code"] == "3"
                assert record["payload"]["previous_build"]["version_code"] == "2"
                assert len(audit_calls) == 1
                upgraded = db.get_task(task.id).state
                assert (calls, [act.type for act in driver.actions],
                        upgraded.role_invocation_count, upgraded.revisable.execution_count) == results[-1]
        finally:
            release_audit.set()
            await asyncio.gather(*runtime._skill_version_audits)
            assert db.get_task(task.id).status == TaskStatus.SUCCEEDED
            db.close()
    assert results[0] == results[1]
    calls, actions, provider_calls, action_count = results[0]
    assert actions == ["tap_xy"] and action_count == 1
    assert len(calls) == provider_calls and calls.count("executor") == 2


async def test_task_skill_links_exposes_assessment_and_separate_audit_run(app):
    db = app.state.db
    task = db.create_task("Read the Skill evidence", device_serial="fixture")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/tasks/{task.id}/skill-links"
        original = await client.get(url)
        assert original.status_code == 200 and "version_audits" not in original.json()

        finding = {"skill_id": "demo-play", "status": "completed", "assessment": "contradicted"}
        run = {"status": "completed", "budget": {"calls": 1, "actions": 0}}
        db.append_agent_record(task.id, "skill_version_audit", "workflow", finding)
        db.append_agent_record(task.id, "skill_version_audit_run", "task", run)
        with_audit = (await client.get(url)).json()
        assert with_audit["version_audits"] == [finding]
        assert with_audit["version_audit_run"] == run
        assert with_audit["skill_ids"] == original.json()["skill_ids"]
