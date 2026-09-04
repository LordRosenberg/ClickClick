"""Run frozen GPT-5.4 Planner/Reviewer gates without a device dispatcher."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from typing import Any

import agent.reviewer as reviewer_module
from agent.planner import Planner
from agent.reviewer import Reviewer
from perception.input_evidence import build_interaction_state
from perception.observation import ObservationPackage
from shared.config import get_settings
from shared.schemas import (
    ActiveCompletionContract,
    ActiveTaskCompletionContract,
    AgentState,
    CanonicalUI,
    MemoryEvent,
    ObservationMode,
    ProgressEntry,
    SubmittedActionSnapshot,
    SubgoalContractBody,
    TaskContractBody,
    TaskMemory,
    UIElement,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evaluation" / "planner_reviewer_zero_device_cases.json"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "planner-reviewer-zero-device-v13.json"
SOURCE_PATHS = (
    "evaluation/planner_reviewer_zero_device_eval.py",
    "agent/decision_context.py",
    "agent/decision_role.py",
    "agent/planner.py",
    "agent/reviewer.py",
    "agent/prompts/planner_system.md",
    "agent/prompts/reviewer_scope_system.md",
    "agent/prompts/reviewer_system.md",
    "shared/llm_gateway.py",
    "shared/schemas.py",
)


class ZeroDeviceDriver:
    """Expose observation refresh only; device dispatch is intentionally absent."""

    async def get_frame(self) -> tuple[dict[str, Any], bytes | None]:
        raise RuntimeError("zero-device fixture has no live observation provider")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_manifest() -> dict[str, str]:
    return {path: _sha256((ROOT / path).read_bytes()) for path in SOURCE_PATHS}


def _app_id(case: dict[str, Any]) -> str:
    if case.get("app"):
        return str(case["app"])
    case_id = str(case["id"])
    if "bilibili" in case_id:
        return "tv.danmaku.bili"
    if "settings" in case_id or "current-state" in case_id:
        return "com.android.settings"
    if "calculator" in case_id or "ordered-value" in case_id:
        return "com.android.calculator2"
    return "com.android.browser"


def _package(case: dict[str, Any], repeat: int) -> ObservationPackage:
    elements = [
        UIElement(
            index=index,
            role="android.widget.TextView",
            text=str(text),
            bounds=[20, 80 + index * 90, 1040, 150 + index * 90],
            clickable=True,
            depth=1,
        )
        for index, text in enumerate(case.get("ui") or [])
    ]
    focused_editable = str(case.get("focused_editable") or "")
    if focused_editable:
        elements.append(
            UIElement(
                index=len(elements),
                role="android.widget.EditText",
                text=focused_editable,
                bounds=[20, 1200, 1040, 1320],
                clickable=True,
                states={"editable": True, "focused": True},
                depth=1,
            )
        )
    app_id = _app_id(case)
    ui = CanonicalUI(
        app_id=app_id,
        activity=".ZeroDeviceFixture",
        elements=elements,
        semantic_tree=elements,
        capture_provider="frozen_synthetic_tree",
        capture_complete=True,
    )
    text = "\n".join(
        [f"foreground_package={app_id}"]
        + [f'[{item.index}] TextView raw_text="{item.text}" clickable' for item in elements]
    )
    return ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=text,
        image_for_llm=None,
        clean_png=None,
        interaction_state=build_interaction_state(ui),
        annotated_png=None,
        gap_reasons=[],
        observation_id=f"obs-{case['id']}-{repeat}",
        actionable=True,
        index_actionable=True,
        frame_width=1080,
        frame_height=2400,
        capture_meta={
            "provider": "frozen_synthetic_tree",
            "tree_provider": "frozen_synthetic_tree",
            "complete": True,
            "coordinate_compatible": True,
            "evidence_tier": "tree_only",
        },
    )


def _task_contract(case: dict[str, Any]) -> TaskContractBody:
    return TaskContractBody.model_validate(case["task_contract"])


def _subgoal_contract(case: dict[str, Any]) -> SubgoalContractBody:
    return SubgoalContractBody.model_validate(case["subgoal_contract"])


def _event(raw: dict[str, Any], subgoal: str) -> MemoryEvent:
    decision = str(raw["decision"])
    intent = str(raw.get("intent") or decision)
    if decision != "act":
        return MemoryEvent(
            kind="attempt",
            step=int(raw.get("step") or 0),
            lineage_id="case-lineage",
            subgoal=subgoal,
            model_intent=intent,
            action_type=decision,
            submitted_action_type=decision,
            post_dispatch_observation="not_applicable",
        )
    action_type = str(raw["action"])
    action = SubmittedActionSnapshot(
        type=action_type,
        index=0 if action_type == "tap" else None,
        text=str(raw["text"]) if "text" in raw else None,
        key=str(raw["key"]) if "key" in raw else None,
    )
    dispatch = str(raw.get("dispatch") or "dispatched")
    return MemoryEvent(
        kind="attempt",
        step=int(raw.get("step") or 0),
        lineage_id="case-lineage",
        subgoal=subgoal,
        model_intent=intent,
        action_type=action_type,
        submitted_action_type=action_type,
        submitted_action=action,
        dispatch_status=dispatch,
        post_dispatch_observation=str(raw.get("post_observation") or "accepted"),
    )


def _state(case: dict[str, Any]) -> AgentState:
    if case["role"] in {"scope", "scope_planner"}:
        return AgentState(instruction=str(case["instruction"]))
    state = AgentState(
        instruction=str(case["instruction"]),
        task_completion_contract=ActiveTaskCompletionContract(
            contract_id="case-task-contract",
            body=_task_contract(case),
        ),
    )
    state.task_memory.progress = [
        ProgressEntry(
            progress_id=f"case-progress-{index}",
            requirement_ref=str(progress["requirement_ref"]),
            statement=str(progress["statement"]),
            evidence_handles=["accepted-history"],
            packet_digest="sha256:accepted-history",
            accepted_step=index,
        )
        for index, progress in enumerate(case.get("accepted_progress") or [], start=1)
    ]
    if case["role"] == "reviewer":
        subgoal = str(case["subgoal"])
        state.current_subgoal = subgoal
        state.active_completion_contract = ActiveCompletionContract(
            contract_id="case-subgoal-contract",
            lineage_id="case-lineage",
            boundary_generation=1,
            target_requirement_ref=str(case["target_requirement_ref"]),
            body=_subgoal_contract(case),
        )
        state.active_timeline_lineage_ids = ["case-lineage"]
        state.task_memory = TaskMemory(
            events=[_event(event, subgoal) for event in case.get("events") or []],
            progress=state.task_memory.progress,
        )
    return state


def _usage(refs: dict[str, Any]) -> dict[str, Any]:
    rounds = list(refs.get("agent_rounds") or [])
    totals: dict[str, Any] = {
        "rounds": len(rounds),
        "input_tokens": 0,
        "cached_read_tokens": 0,
        "output_tokens": 0,
        "latency_ms": 0.0,
        "cache_hits": 0,
    }
    for row in rounds:
        usage = row.get("usage") or {}
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["cached_read_tokens"] += int(usage.get("cached_read_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["latency_ms"] += float(row.get("latency_ms") or 0)
        totals["cache_hits"] += int(bool(usage.get("cache_hit")))
    return totals


def _contains_groups(text: str, groups: list[list[str]]) -> bool:
    folded = text.casefold()
    return all(any(str(term).casefold() in folded for term in group) for group in groups)


def _validate(case: dict[str, Any], decision: Any, refs: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if case["role"] == "planner":
        actual = decision.mode.value
    else:
        actual = decision.verdict.value
        minimum = int(case.get("min_accepted_progress") or 0)
        if len(decision.accepted_progress) < minimum:
            failures.append(f"accepted_progress={len(decision.accepted_progress)} < {minimum}")
        maximum = case.get("max_accepted_progress")
        if maximum is not None and len(decision.accepted_progress) > int(maximum):
            failures.append(f"accepted_progress={len(decision.accepted_progress)} > {maximum}")
        minimum_facts = int(case.get("min_remembered_facts") or 0)
        if len(decision.remembered_facts) < minimum_facts:
            failures.append(f"remembered_facts={len(decision.remembered_facts)} < {minimum_facts}")
        fact_text = " ".join(fact.value for fact in decision.remembered_facts).casefold()
        fact_terms = [str(term).casefold() for term in case.get("required_fact_value_any") or []]
        if fact_terms and not any(term in fact_text for term in fact_terms):
            failures.append(f"remembered facts lack one of: {fact_terms}")

    allowed = set(case.get("allowed_modes") or case.get("allowed_verdicts") or [])
    if actual not in allowed:
        failures.append(f"unexpected outcome: {actual}")

    if case["role"] == "planner" and actual == "execute":
        subgoal = decision.next_subgoal.casefold()
        forbidden = [str(term).casefold() for term in case.get("forbidden_subgoal_terms") or []]
        matched = [term for term in forbidden if term in subgoal]
        if matched:
            failures.append(f"forbidden subgoal terms: {matched}")
        required_any = [str(term).casefold() for term in case.get("required_subgoal_any") or []]
        if required_any and not any(term in subgoal for term in required_any):
            failures.append(f"subgoal lacks one of: {required_any}")
        contract_text = " ".join(decision.completion_contract.success_conditions).casefold()
        forbidden_contract = [
            str(term).casefold() for term in case.get("forbidden_contract_terms") or []
        ]
        matched_contract = [term for term in forbidden_contract if term in contract_text]
        if matched_contract:
            failures.append(f"forbidden contract terms: {matched_contract}")
        plan_text = " ".join(decision.plan).casefold()
        required_plan = [str(term).casefold() for term in case.get("required_plan_any") or []]
        if required_plan and not any(term in plan_text for term in required_plan):
            failures.append(f"plan lacks one of: {required_plan}")

    if case["role"] == "reviewer":
        final_text = "\n".join(item.text for item in decision.answers).casefold()
        required_text = [str(term).casefold() for term in case.get("required_final_text_any") or []]
        if required_text and not any(term in final_text for term in required_text):
            failures.append(f"final text lacks one of: {required_text}")
        forbidden_text = [
            str(term).casefold() for term in case.get("forbidden_final_text_terms") or []
        ]
        matched_text = [term for term in forbidden_text if term in final_text]
        if matched_text:
            failures.append(f"forbidden final text terms: {matched_text}")
        forbidden_handles = set(case.get("forbidden_evidence_handles") or [])
        matched_handles = sorted(forbidden_handles.intersection(decision.evidence_handles))
        if matched_handles:
            failures.append(f"forbidden evidence handles: {matched_handles}")
        for prefix in case.get("required_evidence_handle_prefixes") or []:
            if not any(handle.startswith(str(prefix)) for handle in decision.evidence_handles):
                failures.append(f"missing evidence handle prefix: {prefix}")

    maximum_rounds = int(case.get("max_rounds") or 0)
    actual_rounds = len(refs.get("agent_rounds") or [])
    if maximum_rounds and actual_rounds > maximum_rounds:
        failures.append(f"rounds={actual_rounds} > {maximum_rounds}")
    allowed_tools = {
        "observe_screen",
        "search_skills",
        "submit_planner_decision" if case["role"] == "planner" else "submit_reviewer_decision",
    }
    tool_names = {
        str(row.get("name") or row.get("tool_name") or "")
        for row in refs.get("tool_calls") or []
    }
    unexpected = sorted(name for name in tool_names if name and name not in allowed_tools)
    if unexpected:
        failures.append(f"unexpected tools: {unexpected}")
    return failures


def _requirement_groups(scope: TaskContractBody) -> dict[str, list[str]]:
    return {
        "must_happen": list(scope.must_happen),
        "final_ui_state": list(scope.final_ui_state),
        "answer": list(scope.answer),
    }


def _validate_scope_only(
    case: dict[str, Any], scope: TaskContractBody, refs: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    groups = _requirement_groups(scope)
    for name, minimum in (case.get("minimum_requirements") or {}).items():
        if len(groups.get(name, [])) < int(minimum):
            failures.append(f"{name}={len(groups.get(name, []))} < {minimum}")
    for name, maximum in (case.get("maximum_requirements") or {}).items():
        if len(groups.get(name, [])) > int(maximum):
            failures.append(f"{name}={len(groups.get(name, []))} > {maximum}")
    for name in ("must_happen", "final_ui_state", "answer"):
        required = case.get(f"required_{name}_term_groups") or []
        if required and not _contains_groups(" ".join(groups[name]), required):
            failures.append(f"{name} lacks required term groups")
    available = list(groups["must_happen"])
    for required in case.get("required_distinct_must_happen_term_groups") or []:
        match = next((text for text in available if _contains_groups(text, required)), None)
        if match is None:
            failures.append("distinct must_happen requirement lacks required term groups")
        else:
            available.remove(match)
    if len(refs.get("agent_rounds") or []) > int(case.get("max_rounds") or 1):
        failures.append("scope used more than one model round")
    return failures


def _validate_scope_planner(
    case: dict[str, Any],
    scope: TaskContractBody,
    decision: Any,
    scope_refs: dict[str, Any],
    planner_refs: dict[str, Any],
) -> list[str]:
    failures = _validate_scope_only(case, scope, scope_refs)
    total = sum(len(values) for values in _requirement_groups(scope).values())
    maximum = int(case.get("max_scope_requirements") or 0)
    if maximum and total > maximum:
        failures.append(f"scope_requirements={total} > {maximum}")
    if decision.mode.value not in set(case.get("allowed_modes") or []):
        failures.append(f"unexpected outcome: {decision.mode.value}")
    if decision.mode.value == "execute":
        subgoal = decision.next_subgoal.casefold()
        required = [str(term).casefold() for term in case.get("required_subgoal_any") or []]
        if required and not any(term in subgoal for term in required):
            failures.append(f"subgoal lacks one of: {required}")
    for label, refs in (("scope", scope_refs), ("planner", planner_refs)):
        rounds = len(refs.get("agent_rounds") or [])
        if rounds > int(case.get("max_rounds_per_role") or 1):
            failures.append(f"{label}_rounds={rounds}")
    return failures


def _base_result(case: dict[str, Any], repeat: int, started: float) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "role": case["role"],
        "repeat": repeat,
        "wall_latency_ms": round((time.monotonic() - started) * 1000, 3),
        "device_actions": 0,
        "driver_has_act": False,
        "human_audit": case["human_audit"],
    }


async def _run_once(case: dict[str, Any], model: str, repeat: int) -> dict[str, Any]:
    state = _state(case)
    driver = ZeroDeviceDriver()
    provider_attempts = 0

    def meter(_kind: str, _payload: dict[str, Any]) -> None:
        nonlocal provider_attempts
        provider_attempts += 1

    started = time.monotonic()
    if case["role"] in {"scope", "scope_planner"}:
        reviewer = Reviewer(driver, None, model=model, settings=get_settings())
        planner = (
            Planner(driver, None, model=model, settings=get_settings())
            if case["role"] == "scope_planner"
            else None
        )
        try:
            scope, scope_refs = await reviewer.author_task_scope(
                state,
                task_id=f"zero-{case['id']}-{repeat}",
                model_call_meter=meter,
            )
            if planner is None:
                failures = _validate_scope_only(case, scope, scope_refs)
                return {
                    **_base_result(case, repeat, started),
                    "passed": not failures,
                    "failures": failures,
                    "scope": scope.model_dump(mode="json"),
                    "usage": _usage(scope_refs),
                    "provider_attempts": provider_attempts,
                }
            state.task_completion_contract = ActiveTaskCompletionContract(
                contract_id="case-task-contract",
                body=scope,
            )
            decision, planner_refs, _observation_refs = await planner.decide(
                state,
                _package(case, repeat),
                task_id=f"zero-{case['id']}-{repeat}",
                model_call_meter=meter,
            )
            failures = _validate_scope_planner(
                case, scope, decision, scope_refs, planner_refs,
            )
            combined_refs = {
                "agent_rounds": [
                    *(scope_refs.get("agent_rounds") or []),
                    *(planner_refs.get("agent_rounds") or []),
                ],
            }
            return {
                **_base_result(case, repeat, started),
                "passed": not failures,
                "failures": failures,
                "scope": scope.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "usage": _usage(combined_refs),
                "provider_attempts": provider_attempts,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                **_base_result(case, repeat, started),
                "passed": False,
                "failures": [f"{type(exc).__name__}: {exc}"],
                "provider_attempts": provider_attempts,
            }
        finally:
            await reviewer.aclose()
            if planner is not None:
                await planner.aclose()

    role = (
        Planner(driver, None, model=model, settings=get_settings())
        if case["role"] == "planner"
        else Reviewer(driver, None, model=model, settings=get_settings())
    )
    try:
        if case["role"] == "planner":
            decision, refs, _observation_refs = await role.decide(
                state,
                _package(case, repeat),
                deviation=str(case.get("deviation") or ""),
                task_id=f"zero-{case['id']}-{repeat}",
                model_call_meter=meter,
            )
        else:
            decision, refs, _observation_refs = await role.decide(
                state,
                _package(case, repeat),
                executor_report=str(case.get("executor_report") or ""),
                boundary_reason=str(case.get("boundary_reason") or ""),
                terminal_review=bool(case.get("terminal_review", True)),
                review_requirement_ref=str(case.get("review_requirement_ref") or ""),
                task_id=f"zero-{case['id']}-{repeat}",
                model_call_meter=meter,
            )
        failures = _validate(case, decision, refs)
        return {
            **_base_result(case, repeat, started),
            "passed": not failures,
            "failures": failures,
            "decision": decision.model_dump(mode="json"),
            "tool_calls": refs.get("tool_calls") or [],
            "usage": _usage(refs),
            "provider_attempts": provider_attempts,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **_base_result(case, repeat, started),
            "passed": False,
            "failures": [f"{type(exc).__name__}: {exc}"],
            "provider_attempts": provider_attempts,
        }
    finally:
        await role.aclose()


async def run(
    cases_path: Path,
    output: Path,
    selected: set[str],
    reviewer_prompt: Path | None = None,
) -> int:
    freeze = json.loads(cases_path.read_text(encoding="utf-8"))
    if reviewer_prompt is not None:
        prompt_text = reviewer_prompt.read_text(encoding="utf-8")
        reviewer_module.render_reviewer_system = lambda: prompt_text
    cases = [
        case for case in freeze["cases"]
        if not selected or str(case["id"]) in selected
    ]
    results: list[dict[str, Any]] = []
    stopped_early = False
    for case in cases:
        for repeat in range(1, int(freeze["repeats"]) + 1):
            result = await _run_once(case, str(freeze["model"]), repeat)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
            if not result["passed"] and freeze.get("stop_on_first_failure", True):
                stopped_early = True
                break
        if stopped_early:
            break
    report = {
        "schema_version": 1,
        "freeze": freeze,
        "cases_sha256": _sha256(cases_path.read_bytes()),
        "source_manifest": _source_manifest(),
        "reviewer_prompt": (
            {"path": str(reviewer_prompt), "sha256": _sha256(reviewer_prompt.read_bytes())}
            if reviewer_prompt is not None
            else None
        ),
        "selected_cases": sorted(selected),
        "results": results,
        "summary": {
            "passed": sum(int(row["passed"]) for row in results),
            "failed": sum(int(not row["passed"]) for row in results),
            "planned_runs": len(cases) * int(freeze["repeats"]),
            "completed_runs": len(results),
            "stopped_early": stopped_early,
            "device_actions": 0,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return int(stopped_early)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--reviewer-prompt", type=Path)
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(
            run(
                args.cases,
                args.output,
                set(args.case),
                reviewer_prompt=args.reviewer_prompt,
            )
        )
    )


if __name__ == "__main__":
    main()
