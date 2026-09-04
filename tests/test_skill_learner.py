"""SkillLearner pending stage / approve / reject tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.skills.library import SkillLibrary, serialize_skill_markdown
from agent.skills.pending import (
    approve_pending,
    list_pending,
    pending_diff,
    reject_pending,
    stage_pending_patch,
)
from shared.schemas import AgentState, TaskRecord, TaskStatus


def test_learner_never_infers_app_scope_from_instruction(tmp_path: Path):
    from agent.skills.learner import _pick_target_app

    apps = tmp_path / "apps" / "com.demo"
    apps.mkdir(parents=True)
    (apps / "SKILL.md").write_text(
        serialize_skill_markdown(
            name="demo",
            description="demo app",
            version="0.1.0",
            app="com.demo",
            body="## Hints\n- use current evidence\n",
        ),
        encoding="utf-8",
    )
    task = TaskRecord(
        id="no-prose-scope",
        instruction="打开 com.demo 并搜索内容",
        status=TaskStatus.SUCCEEDED,
        created_at=0,
        updated_at=0,
        state=AgentState(instruction="打开 com.demo 并搜索内容"),
    )

    assert _pick_target_app(task, SkillLibrary(tmp_path)) is None


def test_learner_uses_runtime_frozen_app_scope(tmp_path: Path):
    from agent.skills.learner import _pick_target_app

    task = TaskRecord(
        id="runtime-scope",
        instruction="ambiguous prose",
        status=TaskStatus.SUCCEEDED,
        created_at=0,
        updated_at=0,
        state=AgentState(
            instruction="ambiguous prose",
            frozen_skill_dirs=["generic", "apps/com.demo"],
        ),
    )

    assert _pick_target_app(task, SkillLibrary(tmp_path)) == "com.demo"


def test_stage_approve_reject_pending(tmp_path: Path):
    lib_root = tmp_path
    apps = lib_root / "apps" / "com.demo"
    apps.mkdir(parents=True)
    old = serialize_skill_markdown(
        name="demo",
        description="old",
        version="0.1.0",
        app="com.demo",
        body="old body\n",
    )
    (apps / "SKILL.md").write_text(old, encoding="utf-8")

    new = serialize_skill_markdown(
        name="demo",
        description="new",
        version="0.1.1",
        app="com.demo",
        body="new body with pitfalls\n",
    )
    pending = stage_pending_patch(
        target_rel="apps/com.demo/SKILL.md",
        new_text=new,
        gist="add pitfalls",
        source_task_id="task-1",
        outcome="failure",
        app="com.demo",
        root=lib_root,
        old_text=old,
    )
    assert pending.id
    items = list_pending(root=lib_root)
    assert {p.id for p in items} == {pending.id}

    diff = pending_diff(pending.id, root=lib_root)
    assert "new body" in diff["diff"] or "new body" in diff["new_text"]

    dest = approve_pending(pending.id, root=lib_root)
    assert dest.exists()
    assert "new body" in dest.read_text(encoding="utf-8")
    assert list_pending(root=lib_root) == []

    # Reject path
    pending2 = stage_pending_patch(
        target_rel="apps/com.demo/SKILL.md",
        new_text=new.replace("0.1.1", "0.1.2"),
        gist="noop",
        root=lib_root,
        old_text=dest.read_text(encoding="utf-8"),
    )
    reject_pending(pending2.id, reason="noise", root=lib_root)
    assert list_pending(root=lib_root) == []
    # Canonical unchanged by reject
    assert "0.1.2" not in dest.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_learner_stages_pending(monkeypatch, tmp_path: Path):
    from agent.skills import learner as learner_mod

    apps = tmp_path / "apps" / "com.demo"
    apps.mkdir(parents=True)
    (apps / "SKILL.md").write_text(
        serialize_skill_markdown(
            name="demo",
            description="d",
            version="0.1.0",
            app="com.demo",
            body="surfaces\n",
        ),
        encoding="utf-8",
    )
    lib = SkillLibrary(tmp_path)

    class _Resp:
        content = (
            '{"skip": false, "gist": "pitfall", "name": "demo", '
            '"description": "d", "version": "0.1.1", "app": "com.demo", '
            '"body": "## Pitfalls\\n- stuck on popup\\n"}'
        )

    async def fake_complete(model, messages, **kw):
        return _Resp()

    monkeypatch.setattr(learner_mod, "complete", fake_complete)

    task = TaskRecord(
        id="t1",
        instruction="打开 demo",
        status=TaskStatus.FAILED,
        created_at=0,
        updated_at=0,
        failure_reason="stuck",
        state=AgentState(
            instruction="打开 demo",
            skill_learn=True,
            frozen_skill_dirs=["generic", "apps/com.demo"],
        ),
        skill_learn=True,
    )
    result = await learner_mod.run_skill_learner(task, library=lib)
    assert result.get("ok")
    assert not result.get("skipped")
    assert result.get("pending_id")
    assert result["destination"] == "candidate"
    pending = list_pending(root=tmp_path)
    assert pending
    assert pending[0].target_rel.startswith("_candidates/com.demo/")
    assert pending[0].meta["review"]["module_kind"] == "candidate"


@pytest.mark.asyncio
async def test_learner_targets_an_existing_workflow_merge(monkeypatch, tmp_path: Path):
    from agent.skills import learner as learner_mod

    workflow_dir = tmp_path / "apps" / "com.demo" / "workflows" / "search"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "SKILL.md").write_text(
        serialize_skill_markdown(
            name="demo-search",
            description="search in demo",
            version="1.0.0",
            app="com.demo",
            kind="workflow",
            capability="search",
            body=(
                "## Procedure\n1. submit a query\n\n"
                "## Verification\n- results are visible\n\n"
                "## Hints\n- use current evidence"
            ),
        ),
        encoding="utf-8",
    )

    class _Resp:
        content = json.dumps({
            "skip": False,
            "gist": "refine search verification",
            "destination": "workflow_merge",
            "target_workflow_id": "demo-search",
            "name": "ignored-name",
            "app": "com.demo",
            "category": "hint",
            "body": (
                "## Procedure\n1. submit a query explicitly\n\n"
                "## Verification\n- the real result list is visible\n\n"
                "## Hints\n- suggestions are not results"
            ),
        })

    async def fake_complete(model, messages, **kwargs):
        return _Resp()

    monkeypatch.setattr(learner_mod, "complete", fake_complete)
    task = TaskRecord(
        id="workflow-merge",
        instruction="search demo",
        status=TaskStatus.SUCCEEDED,
        created_at=0,
        updated_at=0,
        state=AgentState(
            instruction="search demo",
            skill_learn=True,
            frozen_skill_dirs=["generic", "apps/com.demo"],
        ),
        skill_learn=True,
    )

    result = await learner_mod.run_skill_learner(
        task, library=SkillLibrary(tmp_path), force=True,
    )

    assert result["destination"] == "workflow_merge"
    assert result["target"] == "apps/com.demo/workflows/search/SKILL.md"
    pending = list_pending(root=tmp_path)
    assert pending[0].meta["review"]["capability"] == "search"


@pytest.mark.asyncio
async def test_learner_respects_skill_learn_false(monkeypatch, tmp_path: Path):
    from agent.skills import learner as learner_mod

    called = {"n": 0}

    async def fake_complete(model, messages, **kw):
        called["n"] += 1
        raise AssertionError("should not call LLM")

    monkeypatch.setattr(learner_mod, "complete", fake_complete)
    task = TaskRecord(
        id="t2",
        instruction="x",
        status=TaskStatus.SUCCEEDED,
        created_at=0,
        updated_at=0,
        state=AgentState(instruction="x", skill_learn=False),
        skill_learn=False,
    )
    result = await learner_mod.run_skill_learner(task, library=SkillLibrary(tmp_path))
    assert result.get("skipped")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_learner_constraint_requires_metadata_and_operator_approval(
    monkeypatch, tmp_path: Path,
):
    from agent.skills import learner as learner_mod

    task = TaskRecord(
        id="t3",
        instruction="x",
        status=TaskStatus.FAILED,
        created_at=0,
        updated_at=0,
        state=AgentState(instruction="x", skill_learn=True),
        skill_learn=True,
    )

    class _MissingEvidence:
        content = (
            '{"skip":false,"name":"generic_learned","category":"constraint",'
            '"scope":"search","evidence":"","body":"## Hints\\n- prefer search"}'
        )

    async def missing_complete(model, messages, **kw):
        return _MissingEvidence()

    monkeypatch.setattr(learner_mod, "complete", missing_complete)
    rejected = await learner_mod.run_skill_learner(
        task, library=SkillLibrary(tmp_path), force=True,
    )
    assert rejected["reason"] == "constraint_requires_scope_and_evidence"

    class _ValidConstraint:
        content = (
            '{"skip":false,"name":"generic_learned","category":"constraint",'
            '"scope":"search surface","evidence":"trace:t3",'
            '"body":"## Constraints\\n- do not select suggestion | scope: search surface | evidence: trace:t3"}'
        )

    async def valid_complete(model, messages, **kw):
        return _ValidConstraint()

    monkeypatch.setattr(learner_mod, "complete", valid_complete)
    staged = await learner_mod.run_skill_learner(
        task, library=SkillLibrary(tmp_path), force=True,
    )
    assert staged["operator_approval_required"] is True
    pending = list_pending(root=tmp_path)
    assert len(pending) == 1
    assert "operator_approval_required: true" in pending_diff(
        pending[0].id, root=tmp_path,
    )["new_text"].lower()
