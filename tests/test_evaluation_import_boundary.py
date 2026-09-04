from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from evaluation.decision_roles_gate import _source_manifest, _write_new
from evaluation.planner_reviewer_zero_device_eval import _state, _validate_scope_only
from shared.schemas import TaskContractBody


ROOT = Path(__file__).resolve().parents[1]


def _evaluation_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names if alias.name == "evaluation")
            imports.extend(
                alias.name for alias in node.names if alias.name.startswith("evaluation.")
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "evaluation" or module.startswith("evaluation."):
                imports.append(module)
    return imports


def test_production_agent_and_driver_readiness_do_not_depend_on_evaluation() -> None:
    production_paths = sorted((ROOT / "agent").rglob("*.py"))
    production_paths.append(ROOT / "driver" / "android.py")

    violations = {
        str(path.relative_to(ROOT)): _evaluation_imports(path)
        for path in production_paths
        if _evaluation_imports(path)
    }

    assert violations == {}


def test_decision_roles_gate_has_no_executor_or_dispatch_path() -> None:
    path = ROOT / "evaluation" / "decision_roles_gate.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    classes = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    methods = {
        node.name
        for node in classes["FrozenObservationDriver"].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "agent.executor" not in imports
    assert "agent.orchestrator" not in imports
    assert "act" not in methods


def test_decision_roles_gate_claim_is_atomic_and_source_manifest_is_bound(
    tmp_path: Path,
) -> None:
    claim = tmp_path / "claim.json"
    _write_new(claim, json.dumps({"ordinal": 1}).encode("utf-8"))
    with pytest.raises(FileExistsError):
        _write_new(claim, json.dumps({"ordinal": 1}).encode("utf-8"))

    assert len(_source_manifest(ROOT)) == 64


def test_reviewer_zero_device_fixture_preserves_progress_with_events() -> None:
    state = _state({
        "role": "reviewer",
        "instruction": "finish",
        "task_contract": {
            "must_happen": ["operation occurred"],
            "final_ui_state": [],
            "answer": [],
            "disqualifying_clauses": [],
        },
        "accepted_progress": [{
            "requirement_ref": "must_happen:1",
            "statement": "input was constructed",
        }],
        "subgoal": "commit input",
        "target_requirement_ref": "must_happen:1",
        "subgoal_contract": {
            "success_conditions": ["commit occurred"],
            "disqualifying_clauses": [],
        },
        "events": [{
            "step": 1,
            "intent": "commit",
            "decision": "act",
            "action": "tap",
        }],
    })

    assert [item.statement for item in state.task_memory.progress] == [
        "input was constructed",
    ]
    assert len(state.task_memory.events) == 1


def test_scope_gate_keeps_independently_missing_history_separate() -> None:
    case = {
        "minimum_requirements": {"must_happen": 2, "final_ui_state": 1},
        "required_distinct_must_happen_term_groups": [
            [["search"], ["Example Domain"]],
            [["first"], ["open"]],
        ],
    }
    split = TaskContractBody(
        must_happen=[
            "search for Example Domain",
            "open the first result",
        ],
        final_ui_state=["the result page is open"],
    )
    combined = TaskContractBody(
        must_happen=["search for Example Domain and open the first result"],
        final_ui_state=["the result page is open"],
    )

    assert _validate_scope_only(case, split, {"agent_rounds": [{}]}) == []
    assert _validate_scope_only(case, combined, {"agent_rounds": [{}]})


def test_scope_gate_keeps_settings_result_only_current() -> None:
    case = {
        "maximum_requirements": {"must_happen": 0},
        "minimum_requirements": {"answer": 1},
        "required_answer_term_groups": [
            ["第一个", "first"], ["设置项", "name"]
        ],
    }
    result_only = TaskContractBody(
        final_ui_state=["系统设置首页可见"],
        answer=["报告第一个完整可见的设置项名称"],
    )
    invented_open = TaskContractBody(
        must_happen=["打开系统设置"],
        answer=["报告第一个完整可见的设置项名称"],
    )

    assert _validate_scope_only(case, result_only, {"agent_rounds": [{}]}) == []
    assert _validate_scope_only(case, invented_open, {"agent_rounds": [{}]})
