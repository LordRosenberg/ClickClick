"""The prepared adapter reuses ordinary A/B isolation without trusting fallback success."""

import asyncio
from dataclasses import replace

import pytest

from agent.skills.learning import Candidate, LearningBudget
from agent.skills.skill_edits import DeleteCandidate
from agent.skills.snapshot import library_manifest
from evaluation.gui_skill_regression import (
    CleanupReceipt,
    IndependentAssessment,
    OrdinaryResult,
    PreparedCase,
    PreparedState,
    validate_gui_skill,
)


APP = "net.gsantner.markor"
TARGET = f"apps/{APP}/workflows/rename/SKILL.md"
BODY = f"""---
name: markor-rename
description: Rename a saved Markor note.
version: 1.0.0
kind: workflow
app: {APP}
capability: rename_note
---

## Procedure

Return to the file list and use the context menu.

## Verification

Check the filename.
"""


class Fixture:
    def __init__(self):
        self.calls = []
        self.prepare_update = {}
        self.execute_update = {}
        self.assess_update = {}
        self.clean = True

    async def prepare(self, context):
        self.calls.append((context.nonce, "prepare"))
        value = PreparedState(context.nonce, context.case.app_id, context.case.build,
                              "fresh-workflow-state", "runtime-settings", True, False, "before")
        if not context.output.name.startswith("baseline-"):
            value = replace(value, **self.prepare_update)
        return value

    async def execute(self, context, skills, rounds, seconds):
        self.calls.append((context.nonce, "execute"))
        candidate = not context.output.name.startswith("baseline-")
        body = (skills / TARGET).read_text(encoding="utf-8")
        assert ("Use Rename directly" in body) is candidate
        value = OrdinaryResult(context.nonce, "runtime-settings", library_manifest(skills)["hash"],
                               True, 1 if candidate else 2, 1, 0.01)
        return replace(value, **self.execute_update) if candidate else value

    async def assess(self, context, preparation, execution):
        self.calls.append((context.nonce, "assess"))
        value = IndependentAssessment(context.nonce, True, context.case.branch_expected, True, "after")
        return replace(value, **self.assess_update) if not context.output.name.startswith("baseline-") else value

    async def cleanup(self, context):
        self.calls.append((context.nonce, "cleanup"))
        return CleanupReceipt(context.nonce, self.clean, "reset")


@pytest.fixture
def request_args(tmp_path):
    canonical = tmp_path / "canonical"
    source = canonical / TARGET
    source.parent.mkdir(parents=True)
    source.write_text(BODY, encoding="utf-8")
    candidate = Candidate(target=TARGET, new_text=BODY.replace("use the context menu", "Use Rename directly"),
                          gist="Rename steps", evidence=["event:one@1"], scope="Saved note", benefit="Fewer steps",
                          risks="Must preserve body")
    return dict(candidate=candidate, old_text=BODY, baseline_skills=canonical, output=tmp_path / "report",
                cases={"source": PreparedCase("rename", "Rename the note", APP, "146", True),
                       "near_miss": PreparedCase("folder", "Rename a folder", APP, "146", False)},
                fixture=Fixture(), model="frozen-model", rounds=2, seconds=2, cleanup_seconds=1)


async def test_paired_run_isolates_exact_overlay_and_tracks_all_registered_branches(request_args):
    budget = LearningBudget(max_calls=60, reserve_calls=0, max_actions=10, max_seconds=45)
    result = await validate_gui_skill(**request_args, budget=budget)

    assert result["validation_stage"] == "complete"
    assert result["prepared_gate"]["status"] == "passed"
    assert result["baseline_library_hash"] != result["candidate_library_hash"]
    assert result["trials"][0]["efficiency_gain"] == 1
    assert len({arm["nonce"] for arm in result["arms"].values()}) == 4
    assert all(arm["preparation"]["build"] == "146" for arm in result["arms"].values())
    assert [(child["actions"], child["calls"]) for child in budget.validation_children] == [(2, 1), (1, 1)] * 2
    assert (request_args["baseline_skills"] / TARGET).read_text() == BODY
    assert (request_args["output"] / "prepared-validation.json").is_file()


async def test_registered_original_case_runs_and_requires_a_valid_control(request_args, monkeypatch):
    key = "registered-1"
    request_args["cases"][key] = PreparedCase("previous-note", "Rename an existing note", APP, "146", True)
    fixture = request_args["fixture"]
    original = fixture.assess

    async def assess(context, preparation, execution):
        result = await original(context, preparation, execution)
        if context.output.name == "baseline-" + key:
            result = replace(result, score=False)
        elif context.output.name.endswith(key):
            result = replace(result, branch_observed=False)
        return result

    monkeypatch.setattr(fixture, "assess", assess)
    report = await validate_gui_skill(**request_args)
    assert [trial["kind"] for trial in report["trials"]] == ["source", "near_miss", key]
    assert report["trials"][-1]["candidate_branch_required"] is False
    assert report["trials"][-1]["candidate_branch_matches"] is False
    assert report["prepared_gate"] == {"status": "inconclusive", "failed": [],
                                        "unknown": [{"kind": key, "reason": "control_not_successful"}]}


@pytest.mark.parametrize("update,reason", [
    ({"nonce": "other"}, "preparation_nonce_mismatch"),
    ({"build": "147"}, "app_build_mismatch"),
    ({"goal_already_met": True}, "initial_conditions_unverified_or_goal_already_met"),
    ({"initial_state_hash": "different"}, "paired_initial_identity_mismatch"),
    ({"configuration_hash": ""}, "initial_identity_unavailable"),
])
async def test_unverified_initial_state_is_unscored_and_cleaned(request_args, update, reason):
    fixture = request_args["fixture"]
    fixture.prepare_update = update
    report = await validate_gui_skill(**request_args)

    assert report["validation_stage"] == "infrastructure_incomplete"
    assert report["prepared_gate"]["status"] == "inconclusive"
    assert report["error"]["reason"] == reason
    assert [kind for _, kind in fixture.calls] == ["prepare", "execute", "assess", "cleanup", "prepare", "cleanup"]


async def test_business_success_via_wrong_branch_does_not_pass_gate(request_args):
    request_args["fixture"].assess_update = {"branch_observed": False}
    report = await validate_gui_skill(**request_args)
    assert report["trials"][0]["candidate_oracle_success"] is True
    assert report["trials"][0]["candidate_success"] is True
    assert report["trials"][0]["candidate_branch_matches"] is False
    assert report["prepared_gate"]["status"] == "failed"


async def test_oracle_and_bounded_ordinary_completion_are_separate(request_args):
    request_args["fixture"].execute_update = {"completed": False}
    report = await validate_gui_skill(**request_args)
    assert report["trials"][0]["candidate_oracle_success"] is True
    assert report["trials"][0]["candidate_success"] is False
    assert report["prepared_gate"]["status"] == "failed"


async def test_unverified_cleanup_blocks_next_arm(request_args):
    request_args["fixture"].clean = False
    report = await validate_gui_skill(**request_args)
    assert len(report["arms"]) == 1
    assert report["error"]["reason"] == "cleanup_not_verified"
    assert report["arms"]["baseline-source"]["assessment"]["score"] is True
    assert report["trials"] == []


async def test_cancellation_still_verifies_cleanup(request_args):
    fixture = request_args["fixture"]
    started = asyncio.Event()

    async def executing(*args):
        started.set()
        await asyncio.Future()

    fixture.execute = executing
    task = asyncio.create_task(validate_gui_skill(**request_args))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [kind for _, kind in fixture.calls] == ["prepare", "cleanup"]
    assert (request_args["output"] / "baseline-source" / "episode-result.json").is_file()


async def test_deletion_defers_without_touching_fixture(request_args):
    candidate = request_args["candidate"].model_dump(exclude={"new_text"})
    request_args["candidate"] = DeleteCandidate(**candidate, operation="delete", base_hash="a" * 64)
    report = await validate_gui_skill(**request_args)
    assert report["validation_stage"] == "unsupported_operation"
    assert not request_args["fixture"].calls
