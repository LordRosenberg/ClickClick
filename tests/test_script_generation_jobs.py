import asyncio
from contextvars import ContextVar
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from agent.skills import script_generation_jobs as generation
from agent.skills.script_schema import GuiScript, script_fingerprint
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.recovery_store import RecoveryStore
from shared.schemas import AgentState, TaskStatus


def _new_task(db, enabled=True):
    return db.create_task("Open a task", AgentState(instruction="Open a task", generate_test_script=enabled))


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    db.artifacts = artifacts
    RecoveryStore(db, artifacts)
    yield db, artifacts, settings
    db.close()


async def _finish(jobs, task_id):
    async with asyncio.timeout(4):
        while (status := jobs.status(task_id))["status"] not in {"draft", "blocked", "error"}:
            await asyncio.sleep(0.01)
    return status


def _draft(destination: Path, *, wrapped: bool = False) -> None:
    pixels = b"test artifact"
    template = {"png_path": "assets/result.png", "reference_width": 100, "reference_height": 100,
                "sha256": hashlib.sha256(pixels).hexdigest()}
    app = {"type": "app_is", "app_id": "com.example.demo"}
    before = {"type": "target_present", "target": {"text": "Open"}}
    after = {"type": "visual_present", "target": template}
    script = GuiScript.model_validate({
        "app_id": "com.example.demo", "script_id": "open-task", "initial": [app, before],
        "steps": [{"id": "open", "before": [before], "after": [after],
                   "action": {"type": "tap", "target": {"text": "Open"}}}],
        "final": [after],
    })
    (destination / "assets").mkdir(parents=True)
    (destination / "assets" / "result.png").write_bytes(pixels)
    (destination / "assets" / "unreferenced.png").write_bytes(b"must not be archived")
    document = script.model_dump(mode="json")
    if wrapped:
        document = {"script": document, "script_hash": script_fingerprint(script), "provenance": {"task_id": "source"},
                    "review": {"decision": "accept"}, "replay_required": True}
    (destination / "script.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.asyncio
async def test_intent_requires_success_and_no_historical_backfill(runtime, monkeypatch):
    db, artifacts, settings = runtime
    jobs = generation.ScriptGenerationJobs(db, artifacts, settings)
    historical = _new_task(db)
    off = _new_task(db, enabled=False)
    failing = _new_task(db)
    recorded = _new_task(db)
    assert jobs.request(recorded.id)
    assert not jobs.request(recorded.id)
    assert jobs.request(failing.id)
    assert not jobs.request(off.id)
    assert not jobs.enqueue(recorded.id)
    db.update_task(historical.id, status=TaskStatus.SUCCEEDED)
    db.update_task(off.id, status=TaskStatus.SUCCEEDED)
    db.update_task(failing.id, status=TaskStatus.FAILED)
    assert not jobs.request(historical.id)
    assert jobs.status(failing.id)["reason"] == "source_task_not_succeeded"
    db.update_task(recorded.id, status=TaskStatus.SUCCEEDED)
    called = []

    async def author(task, *, destination, **kwargs):
        called.append(task.id)
        _draft(destination)
        return {"status": "candidate"}

    monkeypatch.setattr(generation, "_author", author)
    jobs.resume_pending()
    assert (await _finish(jobs, recorded.id))["status"] == "draft"
    assert called == [recorded.id]
    assert jobs.status(historical.id) is None and jobs.status(off.id) is None
    await jobs.close()


@pytest.mark.asyncio
async def test_committed_bundle_is_task_scoped_retained_and_deduplicated(runtime, monkeypatch):
    db, artifacts, settings = runtime
    job = generation.ScriptGenerationJobs(db, artifacts, settings)
    concurrent = generation.ScriptGenerationJobs(db, artifacts, settings)
    task = _new_task(db)
    assert job.request(task.id)
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    entered, release = asyncio.Event(), asyncio.Event()
    source_context = ContextVar("source_context", default=None)
    calls = []

    async def author(task, *, destination, budget, **kwargs):
        calls.append(task.id)
        assert source_context.get() is None and budget.max_actions == 0
        entered.set()
        await release.wait()
        _draft(destination, wrapped=True)
        return {"status": "candidate"}

    monkeypatch.setattr(generation, "_author", author)
    token = source_context.set("source task")
    try:
        assert job.enqueue(task.id)
        await asyncio.wait_for(entered.wait(), timeout=4)
    finally:
        source_context.reset(token)
    assert not job.enqueue(task.id)
    assert concurrent.enqueue(task.id)
    await asyncio.sleep(0)
    release.set()
    record = await _finish(job, task.id)
    assert record["status"] == "draft" and record["step_modes"] == ["tree"] and record["replay_required"]
    assert record["bundle_ref"] in RecoveryStore(db, artifacts).protected_refs()
    with ZipFile(BytesIO(artifacts.verify(record["bundle_ref"]))) as archive:
        assert archive.namelist() == ["script.json", "assets/result.png"]
        envelope = json.loads(archive.read("script.json"))
        assert envelope["script"]["script_id"] == "open-task"
        assert envelope["script_hash"] == record["script_hash"] and envelope["provenance"]["task_id"] == "source"
    concurrent.resume_pending()
    await asyncio.sleep(0)
    assert calls == [task.id] and db.get_task(task.id).status is TaskStatus.SUCCEEDED
    await job.close()
    await concurrent.close()


@pytest.mark.asyncio
async def test_interrupted_authoring_is_resumable_and_failures_do_not_change_task(runtime, monkeypatch):
    db, artifacts, settings = runtime
    task = _new_task(db)
    job = generation.ScriptGenerationJobs(db, artifacts, settings)
    job.request(task.id)
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(generation, "_author", pending)
    job.enqueue(task.id)
    await asyncio.wait_for(entered.wait(), timeout=4)
    await job.close()
    assert job.status(task.id)["status"] == "queued"

    async def bad(*args, **kwargs):
        return {"status": "rejected", "reason": "recorded_frame_unavailable"}

    monkeypatch.setattr(generation, "_author", bad)
    restarted = generation.ScriptGenerationJobs(db, artifacts, settings)
    restarted.resume_pending()
    result = await _finish(restarted, task.id)
    assert result["status"] == "blocked" and result["reason"] == "recorded_frame_unavailable"
    assert db.get_task(task.id).status is TaskStatus.SUCCEEDED
    assert not restarted.enqueue(task.id)
    await restarted.close()


def test_rejects_envelope_with_changed_script_after_review(tmp_path):
    _draft(tmp_path, wrapped=True)
    document = json.loads((tmp_path / "script.json").read_bytes())
    document["script"]["script_id"] = "different"
    with pytest.raises(ValueError, match="candidate hash mismatch"):
        generation._validated_script(json.dumps(document).encode("utf-8"))


@pytest.mark.asyncio
async def test_recorded_draft_resumes_verification_without_regenerating(runtime, monkeypatch):
    db, artifacts, settings = runtime
    task = _new_task(db)
    entered = asyncio.Event()
    author_count = 0

    async def author(task, *, destination, **kwargs):
        nonlocal author_count
        author_count += 1
        _draft(destination, wrapped=True)
        return {"status": "candidate"}

    class Verification:
        async def verify(self, source, bundle):
            assert source.id == task.id and bundle.is_file()
            if not entered.is_set():
                entered.set()
                await asyncio.Future()
            return {"verification_status": "automatically_registered"}

    monkeypatch.setattr(generation, "_author", author)
    verifier = Verification()
    jobs = generation.ScriptGenerationJobs(db, artifacts, settings, verification=verifier)
    assert jobs.request(task.id)
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    jobs.enqueue(task.id)
    await asyncio.wait_for(entered.wait(), timeout=4)
    started = jobs.status(task.id)
    assert started["status"] == "verifying" and started["bundle_ref"]
    await jobs.close()

    resumed = generation.ScriptGenerationJobs(db, artifacts, settings, verification=verifier)
    try:
        resumed.resume_pending()
        result = await _finish(resumed, task.id)
        assert result["verification_status"] == "automatically_registered" and not result["replay_required"]
        assert result["bundle_ref"] == started["bundle_ref"] and author_count == 1
        assert db.get_task(task.id).status is TaskStatus.SUCCEEDED
        with pytest.raises(ValueError, match="not awaiting"):
            resumed.approve(task.id)
    finally:
        await resumed.close()
