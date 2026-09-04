"""Focused tool and routing tests for the simplified three-role protocol."""

from __future__ import annotations

import pytest

from agent.orchestrator import Orchestrator
from agent.prompts import (
    render_executor_system,
    render_planner_system,
    render_reviewer_scope_system,
    render_reviewer_system,
)
from agent.session import AgentSession, tools_for_role
from agent.tool_registry import AgentRole, ToolExecutionContext, ToolStatus
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import (
    ExecutorStepSubmit,
    PlannerDecision,
    PlannerMode,
    ReviewerAcceptedProgress,
    ReviewerDecision,
    ReviewerRememberedFact,
    ReviewerVerdict,
    SubgoalContractBody,
    TaskContractBody,
    TaskStatus,
)
from tests.fake_agents import FakeExecutor, FakePlanner, FakeReviewer


def _tool(role: str, name: str, *, reviewer_protocol: str = "boundary") -> dict:
    matches = [
        tool
        for tool in tools_for_role(role, reviewer_protocol=reviewer_protocol)
        if tool["function"]["name"] == name
    ]
    assert len(matches) == 1
    return matches[0]


def _planner_execute(
    name: str = "Open the first visible result",
    target: str = "final_ui_state:1",
) -> dict:
    return {
        "mode": "execute",
        "next_subgoal": name,
        "completion_contract": {
            "success_conditions": [f"{name} is established"],
            "disqualifying_clauses": [],
        },
        "plan": [name, "Report the visible title"],
        "target_requirement_ref": target,
    }


def _planner_context() -> ToolExecutionContext:
    return ToolExecutionContext(
        role=AgentRole.PLANNER,
        invocation_id="planner-test",
        state={
            "planner_requirement_categories": {
                "must_happen:1": "must_happen",
                "final_ui_state:1": "final_ui_state",
                "answer:1": "answer",
            },
            "planner_accepted_requirement_refs": ["must_happen:1"],
        },
    )


def _reviewer_context(*, active: bool = True) -> ToolExecutionContext:
    return ToolExecutionContext(
        role=AgentRole.REVIEWER,
        invocation_id="reviewer-test",
        state={
            "reviewer_packet_digest": "sha256:packet",
            "reviewer_evidence_handles": [
                "current",
                "evidence-current",
                "task-action:1",
                "progress:1",
            ],
            "reviewer_requirement_categories": {
                "must_happen:1": "must_happen",
                "final_ui_state:1": "final_ui_state",
                "answer:1": "answer",
            },
            "reviewer_dispatched_action_handles": ["task-action:1"],
            "reviewer_progress_bindings": [{
                "progress_id": "p1",
                "evidence_handle": "progress:1",
                "requirement_ref": "must_happen:1",
            }],
            "reviewer_temporal_evidence_handles": [],
            "reviewer_current_evidence_handles": [
                "current",
                "evidence-current",
            ],
            "reviewer_has_active_subgoal": active,
        },
    )


def _accepted(ref: str, handle: str) -> dict:
    return {
        "requirement_ref": ref,
        "statement": f"{ref} is established",
        "evidence_handles": [handle],
    }


def _reviewer_submit(
    verdict: str,
    *,
    progress: list[dict] | None = None,
    facts: list[dict] | None = None,
    handles: list[str] | None = None,
    answers: list[dict] | None = None,
) -> dict:
    payload = {
        "verdict": verdict,
        "accepted_progress": list(progress or []),
        "remembered_facts": list(facts or []),
        "superseded_progress_ids": [],
        "reason": "Exact protocol test",
        "evidence_handles": list(handles or []),
    }
    if answers is not None:
        payload["answers"] = answers
    return payload


@pytest.mark.asyncio
async def test_planner_execute_and_review_are_disjoint_and_ref_bound() -> None:
    registry = AgentSession("planner", "gpt-5.4")._build_registry({})
    context = _planner_context()

    execute = await registry.execute(
        "submit_planner_decision",
        _planner_execute(),
        context,
    )
    review = await registry.execute(
        "submit_planner_decision",
        {"mode": "review", "review_requirement_ref": "answer:1"},
        context,
    )
    pending_context = _planner_context()
    pending_context.state["planner_accepted_requirement_refs"] = []
    pending_occurrence_review = await registry.execute(
        "submit_planner_decision",
        {"mode": "review", "review_requirement_ref": "must_happen:1"},
        pending_context,
    )
    accepted_ref = await registry.execute(
        "submit_planner_decision",
        _planner_execute(target="must_happen:1"),
        context,
    )
    mixed = await registry.execute(
        "submit_planner_decision",
        {
            "mode": "review",
            "review_requirement_ref": "answer:1",
            "next_subgoal": "Operate the UI",
        },
        context,
    )

    assert execute.status == ToolStatus.SUCCEEDED
    assert isinstance(execute.terminal_value, PlannerDecision)
    assert execute.terminal_value.mode == PlannerMode.EXECUTE
    assert execute.terminal_value.plan == [
        "Open the first visible result",
        "Report the visible title",
    ]
    assert isinstance(execute.terminal_value.completion_contract, SubgoalContractBody)
    assert review.status == ToolStatus.SUCCEEDED
    assert review.terminal_value.mode == PlannerMode.REVIEW
    assert review.terminal_value.review_requirement_ref == "answer:1"
    assert pending_occurrence_review.status == ToolStatus.INVALID_ARGUMENTS
    assert accepted_ref.status == ToolStatus.INVALID_ARGUMENTS
    assert mixed.status == ToolStatus.INVALID_ARGUMENTS

    properties = _tool("planner", "submit_planner_decision")["function"][
        "parameters"
    ]["properties"]
    assert set(properties) == {
        "mode",
        "next_subgoal",
        "completion_contract",
        "plan",
        "target_requirement_ref",
        "review_requirement_ref",
    }


@pytest.mark.asyncio
async def test_executor_act_and_boundary_decisions_are_disjoint() -> None:
    registry = AgentSession("executor", "gpt-5.4")._build_registry({})
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR,
        invocation_id="executor-test",
    )

    act = await registry.execute(
        "submit_executor_step",
        {
            "decision": "act",
            "summary": "Wait briefly for the current page load",
            "action": {"type": "sleep", "duration_ms": 100},
        },
        context,
    )
    boundaries = [
        await registry.execute(
            "submit_executor_step",
            {"decision": decision, "summary": "Boundary reason"},
            context,
        )
        for decision in ("request_review", "request_replan")
    ]
    invalid_boundaries = [
        await registry.execute(
            "submit_executor_step",
            {
                "decision": decision,
                "summary": "Boundary reason",
                "action": {"type": "sleep"},
            },
            context,
        )
        for decision in ("request_review", "request_replan")
    ]

    assert act.status == ToolStatus.SUCCEEDED
    assert all(result.status == ToolStatus.SUCCEEDED for result in boundaries)
    assert all(result.terminal_value.action is None for result in boundaries)
    assert all(
        result.status == ToolStatus.INVALID_ARGUMENTS
        for result in invalid_boundaries
    )
    parameters = _tool("executor", "submit_executor_step")["function"]["parameters"]
    assert set(parameters["properties"]) == {"decision", "summary", "action"}
    assert parameters["properties"]["decision"]["enum"] == [
        "act",
        "request_review",
        "request_replan",
    ]


@pytest.mark.asyncio
async def test_scope_protocol_authors_only_categorized_task_requirements() -> None:
    registry = AgentSession(
        "reviewer",
        "gpt-5.4",
        reviewer_protocol="scope",
    )._build_registry({})
    context = ToolExecutionContext(
        role=AgentRole.REVIEWER,
        invocation_id="scope-test",
    )
    payload = {
        "must_happen": ["Submit the exact query"],
        "final_ui_state": ["Results are visible"],
        "answer": ["Report the first title"],
        "disqualifying_clauses": ["Do not follow an account"],
    }

    accepted = await registry.execute("submit_reviewer_scope", payload, context)
    extra = await registry.execute(
        "submit_reviewer_scope",
        {**payload, "next_subgoal": "Open search"},
        context,
    )

    assert accepted.status == ToolStatus.SUCCEEDED
    assert isinstance(accepted.terminal_value, TaskContractBody)
    assert extra.status == ToolStatus.INVALID_ARGUMENTS
    properties = _tool(
        "reviewer",
        "submit_reviewer_scope",
        reviewer_protocol="scope",
    )["function"]["parameters"]["properties"]
    assert set(properties) == {
        "must_happen",
        "final_ui_state",
        "answer",
        "disqualifying_clauses",
    }


@pytest.mark.asyncio
async def test_reviewer_five_verdicts_and_fact_ownership_are_strict() -> None:
    registry = AgentSession("reviewer", "gpt-5.4")._build_registry({})
    active = _reviewer_context(active=True)
    inactive = _reviewer_context(active=False)

    accepted = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "accept",
            progress=[_accepted("must_happen:1", "task-action:1")],
            facts=[{
                "key": "first_title",
                "value": "Example Domain",
                "evidence_handles": ["current"],
            }],
        ),
        active,
    )
    retry = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit("retry", handles=["current"]),
        active,
    )
    retry_without_subgoal = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit("retry", handles=["current"]),
        inactive,
    )
    replan = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit("replan", handles=["current"]),
        inactive,
    )
    done = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            progress=[_accepted("final_ui_state:1", "current")],
            handles=["progress:1"],
            answers=[{
                "requirement_ref": "answer:1",
                "text": "The first title is Example Domain",
                "evidence_handles": ["current"],
            }],
        ),
        active,
    )
    done_with_observation_ref = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            progress=[_accepted("final_ui_state:1", "evidence-current")],
            handles=["progress:1"],
            answers=[{
                "requirement_ref": "answer:1",
                "text": "The first title is Example Domain",
                "evidence_handles": ["evidence-current"],
            }],
        ),
        active,
    )
    blocked = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit("blocked", handles=["current"]),
        active,
    )
    nonterminal_text = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "replan",
            handles=["current"],
            answers=[{
                "requirement_ref": "answer:1",
                "text": "Not terminal",
                "evidence_handles": ["current"],
            }],
        ),
        active,
    )

    assert accepted.status == ToolStatus.SUCCEEDED
    assert accepted.terminal_value.remembered_facts == [
        ReviewerRememberedFact(
            key="first_title",
            value="Example Domain",
            evidence_handles=["current"],
        ),
    ]
    assert retry.status == ToolStatus.SUCCEEDED
    assert retry_without_subgoal.status == ToolStatus.INVALID_ARGUMENTS
    assert replan.status == ToolStatus.SUCCEEDED
    assert done.status == ToolStatus.SUCCEEDED
    assert done_with_observation_ref.status == ToolStatus.SUCCEEDED
    assert isinstance(done.terminal_value, ReviewerDecision)
    assert done.terminal_value.user_facing_answer() == "The first title is Example Domain"
    assert blocked.status == ToolStatus.SUCCEEDED
    assert nonterminal_text.status == ToolStatus.INVALID_ARGUMENTS

    parameters = _tool("reviewer", "submit_reviewer_decision")["function"]["parameters"]
    assert set(parameters["properties"]) == {
        "verdict",
        "accepted_progress",
        "remembered_facts",
        "superseded_progress_ids",
        "reason",
        "evidence_handles",
        "answers",
    }
    assert [item.value for item in ReviewerVerdict] == [
        "accept",
        "retry",
        "replan",
        "done",
        "blocked",
    ]


def _execute(name: str) -> PlannerDecision:
    return PlannerDecision(
        mode=PlannerMode.EXECUTE,
        next_subgoal=name,
        completion_contract=SubgoalContractBody(
            success_conditions=[f"{name} is established"],
        ),
        plan=[name],
        target_requirement_ref="final_ui_state:1",
    )


def _boundary(summary: str = "boundary ready"):
    return ExecutorStepSubmit(
        decision="request_review",
        summary=summary,
    ).to_step()


def _review(
    verdict: ReviewerVerdict,
    *,
    packet: str,
) -> ReviewerDecision:
    progress = (
        [ReviewerAcceptedProgress(
            requirement_ref="final_ui_state:1",
            statement="The requested result is visible",
            evidence_handles=["current"],
        )]
        if verdict == ReviewerVerdict.DONE
        else []
    )
    return ReviewerDecision(
        verdict=verdict,
        accepted_progress=progress,
        reason=f"{verdict.value} route",
        evidence_handles=["current"],
        packet_digest=packet,
    )


def _runtime(tmp_path, planner, reviewer, executor) -> tuple[Database, Orchestrator]:
    db = Database(tmp_path / "runtime.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    runtime = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: planner,
        lambda: reviewer,
        lambda: executor,
        driver=FixtureDriver(),
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    return db, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_planner_calls"),
    [
        (ReviewerVerdict.ACCEPT, 2),
        (ReviewerVerdict.RETRY, 1),
        (ReviewerVerdict.REPLAN, 2),
    ],
)
async def test_nonterminal_verdict_routing(
    tmp_path,
    verdict: ReviewerVerdict,
    expected_planner_calls: int,
) -> None:
    planner = FakePlanner([_execute("first route"), _execute("second route")])
    reviewer = FakeReviewer(
        [
            _review(verdict, packet="first"),
            _review(ReviewerVerdict.DONE, packet="done"),
        ],
        task_scope=TaskContractBody(final_ui_state=["The requested result is visible"]),
    )
    executor = FakeExecutor([_boundary("first boundary"), _boundary("done")])
    db, runtime = _runtime(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("Show the requested result")

    assert db.get_task(task_id).status == TaskStatus.SUCCEEDED
    assert len(planner.seen_states) == expected_planner_calls
    assert len(executor.seen_logs) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (ReviewerVerdict.DONE, TaskStatus.SUCCEEDED),
        (ReviewerVerdict.BLOCKED, TaskStatus.FAILED),
    ],
)
async def test_terminal_verdict_routing(
    tmp_path,
    verdict: ReviewerVerdict,
    status: TaskStatus,
) -> None:
    planner = FakePlanner([_execute("only route")])
    reviewer = FakeReviewer(
        [
            _review(verdict, packet="terminal"),
        ],
        task_scope=TaskContractBody(final_ui_state=["The requested result is visible"]),
    )
    executor = FakeExecutor([_boundary("terminal boundary")])
    db, runtime = _runtime(tmp_path, planner, reviewer, executor)

    task_id = await runtime.start_task("Show the requested result")

    assert db.get_task(task_id).status == status
    assert len(planner.seen_states) == 1


def test_role_prompts_share_the_same_boundaries() -> None:
    planner = " ".join(render_planner_system().lower().split())
    executor = " ".join(render_executor_system().lower().split())
    scope = " ".join(render_reviewer_scope_system().lower().split())
    reviewer = " ".join(render_reviewer_system().lower().split())

    assert "do not operate ui, accept progress, or end the task" in planner
    assert "directional horizon, not a second contract" in planner
    assert "pending `must_happen` cannot be proved" in planner
    assert "do not `review` a sibling requirement" in planner
    assert "do not plan future subgoals or decide the whole task" in executor
    assert "semantic timeline is memory, not proof by itself" in executor
    assert "active timeline has no matching dispatch" in executor
    assert "do not inspect ui, plan a route, or judge completion" in scope
    assert "ordinary open, enter, go to, or stay wording describes this destination" in scope
    assert "empty `must_happen`" in scope
    assert "cannot prove a `must_happen` requirement" in reviewer
    assert "inherited matching ui may prove `final_ui_state` and the value of an `answer`" in reviewer
    assert "later visible query, input, or result string cannot prove" in reviewer
    assert "only when the judged requirement itself needs" in reviewer
    assert "do not infer one requirement" in reviewer
    assert "every contract `answer` is written" in reviewer
    assert "omit `answers` when the contract has none" in reviewer
    assert "questions the user asked to be told" in scope
    assert "or answers" in planner
    assert "judge the current boundary from delivered evidence" in reviewer
    assert "must not operate the device or plan future work" in reviewer


@pytest.mark.asyncio
async def test_done_omits_answers_when_contract_has_none() -> None:
    registry = AgentSession("reviewer", "gpt-5.4")._build_registry({})
    context = _reviewer_context(active=True)
    context.state["reviewer_requirement_categories"] = {
        "must_happen:1": "must_happen",
        "final_ui_state:1": "final_ui_state",
    }
    invented = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            progress=[_accepted("final_ui_state:1", "current")],
            handles=["progress:1"],
            answers=[{
                "requirement_ref": "answer:1",
                "text": "WLAN is visible",
                "evidence_handles": ["current"],
            }],
        ),
        context,
    )
    omitted = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            progress=[_accepted("final_ui_state:1", "current")],
            handles=["progress:1"],
        ),
        context,
    )

    assert invented.status == ToolStatus.INVALID_ARGUMENTS
    assert omitted.status == ToolStatus.SUCCEEDED
    assert omitted.terminal_value.answers == []
    assert omitted.terminal_value.user_facing_answer() == ""


@pytest.mark.asyncio
async def test_done_must_cover_every_contract_answer_ref() -> None:
    registry = AgentSession("reviewer", "gpt-5.4")._build_registry({})
    context = _reviewer_context(active=True)
    context.state["reviewer_requirement_categories"] = {
        "answer:1": "answer",
        "answer:2": "answer",
    }
    partial = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            handles=["current"],
            answers=[{
                "requirement_ref": "answer:1",
                "text": "WLAN",
                "evidence_handles": ["current"],
            }],
        ),
        context,
    )
    complete = await registry.execute(
        "submit_reviewer_decision",
        _reviewer_submit(
            "done",
            handles=["current"],
            answers=[
                {
                    "requirement_ref": "answer:1",
                    "text": "WLAN",
                    "evidence_handles": ["current"],
                },
                {
                    "requirement_ref": "answer:2",
                    "text": "On",
                    "evidence_handles": ["current"],
                },
            ],
        ),
        context,
    )

    assert partial.status == ToolStatus.INVALID_ARGUMENTS
    assert complete.status == ToolStatus.SUCCEEDED
    assert complete.terminal_value.user_facing_answer() == "WLAN\nOn"
