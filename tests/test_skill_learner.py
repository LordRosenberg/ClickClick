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
