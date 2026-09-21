"""Resolver-ticket, recoverable launch, and workflow-selection regressions."""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from agent.executor import Executor
from agent.prompts import render_executor_system, render_planner_system
from agent.read_tools import make_search_installed_apps_handler
from agent.session import AgentSession, tools_for_role
from agent.skills.library import SkillLibrary
from agent.tool_registry import AgentRole, ToolExecutionContext, ToolStatus, stable_hash
from perception.observation import ObservationPackage
from shared.app_resolver import ResolverTicketStore
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from shared.schemas import AgentState, Action, ActionResult, AppResolutionProvenance, AppResolutionResult, AppResolutionStatus, CanonicalUI, ExecutorStep, ObservationMode


def _executor_payload(action: dict, act: str, **overrides) -> dict:
    payload = {
        "decision": "act",
        "action": action,
        "summary": act,
    }
    payload.update(overrides)
    return payload


def _planner_payload(**overrides) -> dict:
    payload = {
        "mode": "execute",
        "requirement_ref": "final_ui_state:1",
        "next_subgoal": "open",
        "subgoal_completion_criteria": {
            "success_conditions": ["the requested end state is visible"],
        },
        "plan": ["open"],
    }
    payload.update(overrides)
    return payload


def test_ticket_binding_consumption_expiry_and_redacted_fingerprint():
    now = [10.0]
    store = ResolverTicketStore(ttl_s=2.0, clock=lambda: now[0])
    ticket = store.issue(
        task_id="t1", device_id="d1", subgoal_id="sg1",
        query="Example App", resolver_generation="g1",
    )
    mismatch = store.consume(
        ticket.resolution_ticket,
        task_id="t1", device_id="d2", subgoal_id="sg1",
    )
    assert mismatch.accepted is False and mismatch.reason == "device_mismatch"
    assert ticket.resolution_ticket not in mismatch.model_dump_json()
    accepted = store.consume(
        ticket.resolution_ticket,
        task_id="t1", device_id="d1", subgoal_id="sg1",
    )
    assert accepted.accepted is True and accepted.reason == "accepted"
    replay = store.consume(
        ticket.resolution_ticket,
        task_id="t1", device_id="d1", subgoal_id="sg1",
    )
    assert replay.reason == "consumed"

    expiring = store.issue(
        task_id="t1", device_id="d1", subgoal_id="sg1",
        query="Later", resolver_generation="g1",
    )
    now[0] = 12.1
    expired = store.consume(
        expiring.resolution_ticket,
        task_id="t1", device_id="d1", subgoal_id="sg1",
    )
    assert expired.reason == "expired"


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"task_id": "other"}, "task_mismatch"),
        ({"device_id": "other"}, "device_mismatch"),
        ({"subgoal_id": "other"}, "subgoal_mismatch"),
    ],
)
def test_ticket_rejects_each_bound_dimension(override, reason):
    store = ResolverTicketStore()
    ticket = store.issue(
        task_id="t", device_id="d", subgoal_id="sg", query="Demo",
        resolver_generation="g1",
    )
    binding = {
        "task_id": "t", "device_id": "d", "subgoal_id": "sg",
    }
    binding.update(override)
    result = store.consume(ticket.resolution_ticket, **binding)
    assert result.accepted is False and result.reason == reason


@pytest.mark.asyncio
async def test_search_allows_model_repaired_query_with_scoped_ticket():
    class Driver:
        calls = 0

        async def search_installed_apps(self, query, *, limit):
            self.calls += 1
            return [{"package": "com.demo"}]

    driver = Driver()
    store = ResolverTicketStore()
    ticket = store.issue(
        task_id="t", device_id="d", subgoal_id="sg",
        query="Demo", resolver_generation="g",
    )
    handler = make_search_installed_apps_handler(
        driver=driver, ticket_store=store,
    )
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR,
        invocation_id="i",
        state={
            "task_id": "t", "device_id": "d", "subgoal_id": "sg",
            "app_resolution_generations": {"other": "g"},
        },
    )
    absent = await handler({"query": "Other"}, context)
    assert absent.status == "precondition_not_met"
    assert absent.data["reason"] == "absent" and driver.calls == 0
    result = await handler({
        "query": "Other", "resolution_ticket": ticket.resolution_ticket,
    }, context)
    assert result.status == "succeeded"
    assert result.data["query"] == "Other"
    assert result.data["candidates"] == [{"package": "com.demo"}]
    assert driver.calls == 1


@pytest.mark.asyncio
async def test_unticketed_search_is_rejected():
    class Driver:
        calls = 0

        async def search_installed_apps(self, query, *, limit):
            self.calls += 1
            return []

    driver = Driver()
    handler = make_search_installed_apps_handler(
        driver=driver, ticket_store=ResolverTicketStore(),
    )
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="i", state={})
    result = await handler({"query": "Demo"}, context)
    assert result.status == ToolStatus.PRECONDITION_NOT_MET
    assert driver.calls == 0
    assert context.state["ticket_lifecycle"][0]["reason"] == "absent"


class _ResolverDriver:
    serial = "device-A"

    def __init__(self) -> None:
        self.search_calls = 0
        self.acts: list[Action] = []
        self.recorded_selections: list[tuple[str, str]] = []
        self.foreground_package = "com.initial"

    async def resolve_installed_app(self, app: str):
        if app in {"com.demo", "com.other", "Demo"}:
            package = app if app.startswith("com.") else "com.demo"
            return AppResolutionResult(
                requested_name=app,
                normalized_query=app.casefold(),
                status=AppResolutionStatus.RESOLVED,
                resolver_generation="g1",
                package=package,
                provenance=(
                    AppResolutionProvenance.EXACT_PACKAGE
                    if app.startswith("com.") else AppResolutionProvenance.CURATED_ALIAS
                ),
            ).model_dump(mode="json")
        return AppResolutionResult(
            requested_name=app,
            normalized_query=" ".join(app.casefold().split()),
            status=AppResolutionStatus.MISS,
            resolver_generation="g1",
        ).model_dump(mode="json")

    async def search_installed_apps(self, query: str, *, limit: int):
        self.search_calls += 1
        return [{"package": "com.demo", "alias": "Demo", "provenance": "installed_package"}]

    async def validate_installed_app(self, package: str) -> bool:
        return package in {"com.demo", "com.other"}

    async def act(self, action: Action) -> ActionResult:
        self.acts.append(action)
        if action.type == "launch":
            self.foreground_package = str(action.app or "")
        return ActionResult(success=True, message="ok")

    async def get_frame(self):
        from driver.fixture import FixtureDriver

        tree, pixels = await FixtureDriver().get_frame()
        tree["package"] = self.foreground_package
        return tree, pixels

    async def current_foreground_identity(self, *, timeout_s=None):
        del timeout_s
        return {
            "package": self.foreground_package,
            "activity": "Main",
            "component": f"{self.foreground_package}/Main",
            "sources": [],
            "conflict": False,
        }

    async def record_installed_app_selection(
        self, display_name: str, package: str,
    ) -> bool:
        self.recorded_selections.append((display_name, package))
        return True


@pytest.mark.asyncio
async def test_display_name_local_hit_preflights_without_search():
    driver = _ResolverDriver()
    executor = Executor(driver=driver, model="m", settings=Settings())
    step = ExecutorStep(action=Action(type="launch", app="Demo"), summary="launch")
    context = ToolExecutionContext(
        role=AgentRole.EXECUTOR, invocation_id="i",
        state={"task_id": "t", "device_id": "device-A", "subgoal_id": "sg"},
    )
    result = await executor._launch_preflight(step, context)  # noqa: SLF001
    assert result is None
    assert step.action is not None and step.action.app == "com.demo"
    assert context.state["launch_preflight"]["provenance"] == "curated_alias"
    assert driver.search_calls == 0


@pytest.mark.asyncio
async def test_app_resolution_requires_the_current_driver_contract():
    executor = Executor(driver=object(), model="m", settings=Settings())

    with pytest.raises(RuntimeError, match="resolve_installed_app"):
        await executor._resolve_installed_app("Demo")  # noqa: SLF001


@pytest.mark.asyncio
async def test_app_resolution_rejects_malformed_driver_result():
    class Driver:
        async def resolve_installed_app(self, app: str):
            return {"requested_name": app, "status": "resolved"}

    executor = Executor(driver=Driver(), model="m", settings=Settings())

    with pytest.raises(ValidationError):
        await executor._resolve_installed_app("Demo")  # noqa: SLF001


@pytest.mark.asyncio
async def test_launch_miss_search_and_resubmit_stay_in_one_agent_call(monkeypatch):
    settings = Settings(agent_max_total_rounds=6, agent_max_read_calls=2)
    driver = _ResolverDriver()
    executor = Executor(driver=driver, model="m", settings=settings)
    session = executor._session  # noqa: SLF001
    session.reset_lifecycle("task:t")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())
    observed_ticket = ""
    round_no = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal round_no, observed_ticket
        round_no += 1
        if round_no == 1:
            arguments = _executor_payload(
                {"type": "launch", "app": "Mystery App"}, "launch",
            )
            name = "submit_executor_step"
        elif round_no == 2:
            wire = "\n".join(str(message.get("content") or "") for message in messages)
            match = re.search(r'"resolution_ticket":"([^"]+)"', wire)
            assert match is not None
            observed_ticket = match.group(1)
            arguments = {"query": "Mystery App", "resolution_ticket": observed_ticket}
            name = "search_installed_apps"
        else:
            arguments = _executor_payload(
                {"type": "launch", "app": "com.demo"}, "launch",
            )
            name = "submit_executor_step"
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"c{round_no}", name=name, arguments=json.dumps(arguments))],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    result = await session.run(
        [{"role": "user", "content": "O"}],
        handlers={
                "search_installed_apps": make_search_installed_apps_handler(
                    driver=driver, ticket_store=executor._ticket_store,  # noqa: SLF001
                ),
        },
        context_state={
            "task_id": "t", "device_id": "device-A", "subgoal_id": "sg",
            "app_resolution_generations": {},
            "terminal_preflight": executor._launch_preflight,  # noqa: SLF001
        },
    )
    assert result.decision.action is not None
    assert result.decision.action.app == "com.demo"
    assert driver.search_calls == 1
    assert [record.status for record in result.tool_calls] == [
        "precondition_not_met", "succeeded", "succeeded",
    ]
    assert len({record.tool_catalog_hash for record in result.llm_rounds}) == 1
    snapshot = json.dumps(result.request_snapshot, ensure_ascii=False)
    assert observed_ticket and observed_ticket not in snapshot
    assert "[REDACTED]" in snapshot


@pytest.mark.asyncio
async def test_installed_package_outside_bounded_candidates_is_dispatched(monkeypatch):
    settings = Settings(agent_max_total_rounds=6, agent_max_read_calls=2)
    driver = _ResolverDriver()
    executor = Executor(driver=driver, model="m", settings=settings)
    round_no = 0

    async def fake_complete(model, messages, **kwargs):
        nonlocal round_no
        del model, kwargs
        round_no += 1
        if round_no == 1:
            name = "submit_executor_step"
            arguments = _executor_payload(
                {"type": "launch", "app": "Mystery App"}, "resolve app",
            )
        elif round_no == 2:
            wire = "\n".join(str(message.get("content") or "") for message in messages)
            match = re.search(r'"resolution_ticket":"([^"]+)"', wire)
            assert match is not None
            name = "search_installed_apps"
            arguments = {
                "query": "Mystery App",
                "resolution_ticket": match.group(1),
            }
        else:
            name = "submit_executor_step"
            arguments = _executor_payload(
                {"type": "launch", "app": "com.other"}, "launch installed app",
            )
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id=f"c{round_no}", name=name, arguments=json.dumps(arguments),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    package = ObservationPackage(
        ui=CanonicalUI(app_id="com.initial", activity="Main"),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="tree",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
    )

    step, result, *_ = await executor.act_once(
        "launch Mystery App", package, task_id="outside-candidates",
    )

    assert step.action is not None and step.action.app == "com.other"
    assert result.success is True
    assert [action.app for action in driver.acts] == ["com.other"]
    assert driver.recorded_selections == []


def _selection_library(tmp_path) -> SkillLibrary:
    xhs_core = tmp_path / "apps" / "com.xingin.xhs" / "core"
    xhs_workflow = tmp_path / "apps" / "com.xingin.xhs" / "workflows" / "xhs"
    generic = tmp_path / "generic" / "extra"
    xhs_core.mkdir(parents=True)
    xhs_workflow.mkdir(parents=True)
    generic.mkdir(parents=True)
    (xhs_core / "SKILL.md").write_text(
        "---\nname: xhs-core\ndescription: xhs core\nversion: 2.0.0\napp: com.xingin.xhs\nkind: app_core\n---\n\nXHS CORE BODY\n",
        encoding="utf-8",
    )
    (xhs_workflow / "SKILL.md").write_text(
        "---\nname: xhs\ndescription: xhs workflow\nversion: 2.0.0\napp: com.xingin.xhs\nkind: workflow\ncapability: xhs_task\n---\n\n## Procedure\n1. XHS WORKFLOW BODY\n\n## Verification\n- result visible\n",
        encoding="utf-8",
    )
    (generic / "SKILL.md").write_text(
        "---\nname: extra\ndescription: extra\nversion: 1.0.0\nkind: generic\n---\n\nEXTRA BODY\n",
        encoding="utf-8",
    )
    return SkillLibrary(tmp_path)


def test_selected_target_bundle_switches_without_duplicate_bodies(tmp_path):
    session = AgentSession("executor", "m", library=_selection_library(tmp_path))
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_foreground_app("com.android.launcher")
    assert "XHS WORKFLOW BODY" not in json.dumps(session._k_wire())  # noqa: SLF001
    session.set_target_app("com.xingin.xhs", ["xhs"])
    assert json.dumps(session._k_wire(), ensure_ascii=False).count("XHS WORKFLOW BODY") == 1  # noqa: SLF001
    session.set_foreground_app("com.demo.other")
    assert "XHS WORKFLOW BODY" in json.dumps(session._k_wire())  # noqa: SLF001
    session.set_foreground_app("com.xingin.xhs")
    rendered = json.dumps(session._k_wire(), ensure_ascii=False)  # noqa: SLF001
    assert json.dumps(session._k_wire(), ensure_ascii=False).count("XHS CORE BODY") == 1  # noqa: SLF001
    assert rendered.count("XHS WORKFLOW BODY") == 1
    assert session.reset_lifecycle("task:new") is True
    assert session._k_wire() == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_executor_reuses_exact_app_bundle_across_subgoals(
    monkeypatch, tmp_path,
):
    artifacts = ArtifactStore(tmp_path / "artifacts")

    class Driver:
        serial = "device"

        async def act(self, action):
            return ActionResult(success=True, message=action.type)

        async def get_frame(self):
            from driver.fixture import FixtureDriver

            return await FixtureDriver().get_frame()

        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "com.xingin.xhs",
                "activity": ".Main",
                "component": "com.xingin.xhs/.Main",
                "sources": [],
                "conflict": False,
            }

    executor = Executor(Driver(), artifacts=artifacts, model="m", settings=Settings())
    executor._session = AgentSession(  # noqa: SLF001
        "executor", "m", library=_selection_library(tmp_path / "skills"), settings=Settings(),
    )
    package = ObservationPackage(
        ui=CanonicalUI(app_id="com.xingin.xhs"),
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="tree",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
    )

    async def fake_complete(model, messages, **kwargs):
        return GatewayResponse(content="", model="m", stop_reason="tool_calls", tool_calls=[
            ToolCall(id="s", name="submit_executor_step", arguments=json.dumps(
                _executor_payload(
                    {"type": "sleep", "duration_ms": 1}, "wait",
                )
            )),
        ])

    monkeypatch.setattr("agent.session.complete", fake_complete)
    frozen = ["generic"]
    executor.configure_skills(
        frozen_skill_dirs=frozen,
        target_app="com.xingin.xhs",
        workflow_ids=["xhs"],
    )
    *_, first_refs = await executor.act_once("sg-a", package, task_id="task-one")

    executor.configure_skills(
        frozen_skill_dirs=frozen,
        target_app="com.xingin.xhs",
        workflow_ids=["xhs"],
    )
    *_, second_refs = await executor.act_once("sg-b", package, task_id="task-one")

    assert [item["skill_id"] for item in first_refs["active_skills"]] == ["xhs", "xhs-core"]
    assert [item["skill_id"] for item in second_refs["active_skills"]] == ["xhs", "xhs-core"]
    second_input = json.loads(artifacts.read_text(str(second_refs["llm_input_ref"])))
    second_messages = json.dumps(second_input["rounds"][0]["messages"])
    assert second_messages.count("XHS WORKFLOW BODY") == 1
    assert second_messages.count("XHS CORE BODY") == 1


def test_executor_catalog_is_byte_stable_and_ticket_is_required():
    first = tools_for_role("executor")
    second = tools_for_role("executor")
    assert stable_hash(first) == stable_hash(second)
    assert [tool["function"]["name"] for tool in first] == [
        "inspect_image_regions", "load_skill", "observe_screen", "search_installed_apps", "submit_executor_step",
    ]
    search = next(tool for tool in first if tool["function"]["name"] == "search_installed_apps")
    assert search["function"]["parameters"]["required"] == ["query", "resolution_ticket"]
    wire = json.dumps(first, ensure_ascii=False)
    assert wire.count("do not navigate launcher UI to find it") == 1
    assert wire.count("in this invocation") == 1
    assert "translate the App name or use a package keyword" in wire
    assert "results are bounded hints" in wire.lower()
    assert "intended exact installed package" in wire


@pytest.mark.asyncio
async def test_dynamic_local_hit_and_late_miss_keep_prefix_and_tool_hash(monkeypatch):
    session = AgentSession("executor", "m")
    session.reset_lifecycle("task")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(render_executor_system())

    async def fake_complete(model, messages, **kwargs):
        return GatewayResponse(
            content="", model="m", stop_reason="tool_calls",
            tool_calls=[ToolCall(
                id="s", name="submit_executor_step",
                arguments=json.dumps(_executor_payload({"type": "sleep"}, "wait")),
            )],
        )

    monkeypatch.setattr("agent.session.complete", fake_complete)
    hit = await session.run(
        [{"role": "user", "content": "O1"}],
        history_messages=[{"role": "user", "content": "resolved: com.demo"}],
    )
    miss = await session.run(
        [{"role": "user", "content": "O2"}],
        history_messages=[{
            "role": "user",
            "content": 'miss: {"resolution_ticket":"late-secret"}',
        }],
    )
    assert hit.llm_rounds[0].tool_catalog_hash == miss.llm_rounds[0].tool_catalog_hash
    assert hit.llm_rounds[0].stable_prefix_hash == miss.llm_rounds[0].stable_prefix_hash
    assert "late-secret" not in json.dumps(miss.request_snapshot)
