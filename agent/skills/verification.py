"""Compact Reviewer-selected checks; all execution authority belongs to the host."""
from __future__ import annotations

import inspect

CHECKS = ("source", "variant", "near_miss", "related_normal")
POLICY = """Optional verification: {checks:[source|variant|near_miss|related_normal],reason:text}.
Learner may propose it; only Reviewer selects execution. Explain the decision-changing
gap, what stays fixed and what changes. Use declared host checks only; no app routes,
fixture answers or invented inputs. Choose the smallest useful subset, source first.
A supported local rule needs no verification by default. Verification may resolve a
concrete task-benefit/transfer/risk question without making it a knowledge blocker.
Positive source evidence can justify a parameter variant; single outcomes do not prove
stable benefit. No default matrix or repeated unchanged checks. After actual results,
Learner may retain the scoped rule, revise, explore a concrete gap or stop; independent
review still controls revisions. Utility and local knowledge are separate conclusions.
For an explicit verification_goal, adjudicate that objective separately from local
admission: select a useful available check or explain in criterion reasons why no
check can change the requested conclusion. Local admission alone does not answer it.
checks:[] with a reason explicitly declines execution; it proves no utility.
"""


def verification_plan(value, *, available=None):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"checks", "reason"}:
        raise ValueError("verification requires checks/reason")
    checks, reason = value["checks"], value["reason"]
    if (not isinstance(checks, list) or len(checks) > len(CHECKS)
            or any(type(x) is not str or x not in CHECKS for x in checks)
            or len(set(checks)) != len(checks)):
        raise ValueError("invalid verification checks")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("verification requires a bounded decision-changing reason")
    if available is not None and any(x not in available for x in checks):
        raise ValueError("verification check unavailable")
    return {"checks": [x for x in CHECKS if x in checks], "reason": reason.strip()}


def available_checks(validator):
    if not callable(validator):
        return []
    signature = inspect.signature(validator).parameters
    if "checks" not in signature or "budget" not in signature:
        return []
    owner = getattr(validator, "__self__", validator)
    declared = getattr(owner, "validation_checks", None)
    if not callable(declared):
        return []
    result = declared()
    if not isinstance(result, (list, tuple)) or any(x not in CHECKS for x in result):
        raise ValueError("invalid host verification capabilities")
    return [x for x in CHECKS if x in result]


def check_conditions(validator):
    owner = getattr(validator, "__self__", validator)
    declared = getattr(owner, "validation_check_conditions", None)
    return declared() if callable(declared) else {}
