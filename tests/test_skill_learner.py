"""SkillLearner pending stage / approve / reject tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.skills.library import SkillLibrary, serialize_skill_markdown
from agent.skills.pending import (
    approve_pending,
    get_pending,
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


def _stage_retirement(root, monkeypatch, *, removal_coverage="entire_skill", pending_check=False):
    from agent.skills.learning import CRITERIA, digest
    from agent.skills.skill_edits import DeleteCandidate
    from agent.skills.snapshot import library_manifest

    destination = root / "apps/com.demo/workflows/obsolete/SKILL.md"
    destination.parent.mkdir(parents=True)
    old_text = serialize_skill_markdown(name="obsolete", description="Old path", app="com.demo",
        kind="workflow", capability="search", body="## Procedure\nUse the old entry.\n\n## Verification\nCheck the results.")
    destination.write_bytes(old_text.replace("\n", "\r\n").encode("utf-8"))
    resource = destination.with_name("example.txt")
    resource.write_text("still referenced elsewhere", encoding="utf-8")
    candidate = DeleteCandidate(operation="delete", target=destination.relative_to(root).as_posix(),
        base_hash=hashlib.sha256(destination.read_bytes()).hexdigest(), gist="Retire the obsolete workflow",
        evidence=["source/event:0@1", "source/observation:old@1", "source/observation:new@1"],
        scope="Entire obsolete search workflow", benefit="Remove a misleading entry", risks="Check shared guidance")
    candidate_hash = digest(candidate.model_dump())
    review = {"candidate_hash": candidate_hash, "verdict": "pass", "acceptance": "source_evidence",
              "removal_coverage": removal_coverage, "changes": [], "tests": [],
              "criteria": {k: {"status": "pass", "reason": "Reviewed against recorded evidence"} for k in CRITERIA}}
    if pending_check:
        review["verification"] = {"checks": ["source"], "reason": "Confirm that removal preserves task success"}
    contracts = {"related_skills": [{"id": "obsolete"}]}
    monkeypatch.setattr("agent.skills.exploration.build_review_contracts", lambda *args: contracts)
    patch = stage_pending_patch(operation="delete", target_rel=candidate.target, new_text="", old_text=old_text,
        gist=candidate.gist, app="com.demo", source_task_id="temporary-task", root=root,
        review_metadata={"experiment": "task_explore_delete", "candidate_hash": candidate_hash,
            "base_hash": candidate.base_hash, "candidate_contract": candidate.model_dump(),
            "baseline_manifest": library_manifest(root), "contracts_hash": digest(contracts),
            "review": review, "review_hash": digest(review), "eligible": True,
            "evidence": candidate.evidence, "scope": candidate.scope})
    return patch, destination, resource


def test_pending_deletion_reviews_diff_and_preserves_archive_and_resources(tmp_path, monkeypatch):
    patch, destination, resource = _stage_retirement(tmp_path, monkeypatch)
    original = destination.read_bytes()
    diff = pending_diff(patch.id, root=tmp_path)
    assert diff["operation"] == "delete" and "+++ /dev/null" in diff["diff"] and diff["new_text"] == ""
    assert destination.read_bytes() == original
    assert approve_pending(patch.id, root=tmp_path) == destination
    assert not destination.exists() and resource.read_text() == "still referenced elsewhere"
    assert SkillLibrary(tmp_path).get("obsolete") is None
    assert patch.path.with_suffix(".deleted").read_bytes() == original
    assert get_pending(patch.id, root=tmp_path).status == "approved"

    destination.write_bytes(original)
    assert approve_pending(patch.id, root=tmp_path) == destination
    assert destination.read_bytes() == original


def test_pending_deletion_rejection_can_be_retried_and_prevents_approval(tmp_path, monkeypatch):
    patch, destination, _ = _stage_retirement(tmp_path, monkeypatch)
    original = destination.read_bytes()
    reject_pending(patch.id, root=tmp_path, reason="still useful under another condition")
    rejected = get_pending(patch.id, root=tmp_path).meta
    reject_pending(patch.id, root=tmp_path, reason="duplicate attempt")
    assert get_pending(patch.id, root=tmp_path).meta == rejected
    with pytest.raises(ValueError, match="not awaiting approval"):
        approve_pending(patch.id, root=tmp_path)
    assert destination.read_bytes() == original


@pytest.mark.parametrize("change", ["canonical", "partial_review", "pending_check"])
def test_pending_deletion_blocks_stale_or_incomplete_review(tmp_path, monkeypatch, change):
    patch, destination, _ = _stage_retirement(tmp_path, monkeypatch,
        removal_coverage="one_rule" if change == "partial_review" else "entire_skill",
        pending_check=change == "pending_check")
    if change == "canonical":
        destination.write_bytes(destination.read_bytes() + b"\r\nAnother valid fallback.\r\n")
    current = destination.read_bytes()
    with pytest.raises(ValueError):
        approve_pending(patch.id, root=tmp_path)
    assert destination.read_bytes() == current
    assert get_pending(patch.id, root=tmp_path).status == "pending"


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_pending_deletion_recovers_interrupted_publication(tmp_path, monkeypatch, error):
    from agent.skills import pending

    patch, destination, resource = _stage_retirement(tmp_path, monkeypatch)
    original = destination.read_bytes()
    real_write = pending._write_metadata

    def interrupt_after_move(path, metadata):
        if metadata.get("status") == "approved":
            raise error("test interruption before final status")
        real_write(path, metadata)

    with monkeypatch.context() as interruption:
        interruption.setattr(pending, "_write_metadata", interrupt_after_move)
        with pytest.raises(error):
            approve_pending(patch.id, root=tmp_path)

    assert get_pending(patch.id, root=tmp_path).status == "pending"
    if error is OSError:
        assert destination.read_bytes() == original
    else:
        assert not destination.exists()
        with pytest.raises(ValueError, match="already been decided or started"):
            reject_pending(patch.id, root=tmp_path)
        resource.write_text("updated after the interruption", encoding="utf-8")
        recreated = original + b"\r\nNew version after the interruption.\r\n"
        destination.write_bytes(recreated)
    approve_pending(patch.id, root=tmp_path)
    assert get_pending(patch.id, root=tmp_path).status == "approved"
    assert patch.path.with_suffix(".deleted").read_bytes() == original
    if error is KeyboardInterrupt:
        assert destination.read_bytes() == recreated
        assert resource.read_text(encoding="utf-8") == "updated after the interruption"
