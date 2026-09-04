"""Mechanical task/subgoal contract helpers.

Requirement meaning stays model-owned. This module only projects declarations
and checks that their required structural groups are present.
"""

from __future__ import annotations

from typing import Any

from shared.schemas import SubgoalContractBody, TaskContractBody


def model_visible_contract_body(
    contract: TaskContractBody | SubgoalContractBody,
) -> dict[str, Any]:
    return contract.model_dump(mode="json", exclude_none=True)


def declaration_error(
    contract: TaskContractBody | SubgoalContractBody | None,
) -> str:
    if contract is None:
        return "contract_missing"
    if isinstance(contract, TaskContractBody):
        if not (
            contract.must_happen
            or contract.final_ui_state
            or contract.answer
        ):
            return "task_requirements_missing"
        return ""
    if not contract.success_conditions:
        return "subgoal_success_conditions_missing"
    return ""


__all__ = ["model_visible_contract_body", "declaration_error"]
