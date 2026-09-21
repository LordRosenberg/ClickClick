"""Separate explicit provider failures from task accuracy without hiding agent errors."""


def accuracy_pause_reason(results: list[dict], floor: float) -> dict | None:
    """Stop between cases without changing any original score or valid flag."""
    valid = [row for row in results if row.get('valid')]
    if len(valid) < 10:
        return None
    passed = sum(bool(row.get('budgeted_success')) for row in valid)
    rate = passed / len(valid)
    detail = {'valid': len(valid), 'passed': passed, 'success_rate': rate, 'floor': floor}
    if rate < floor:
        return {'reason': 'success_rate_below_floor', **detail}
    if len(valid) >= 20:
        recent = sum(bool(row.get('budgeted_success')) for row in valid[-10:])
        previous = sum(bool(row.get('budgeted_success')) for row in valid[-20:-10])
        if previous - recent >= 2:
            return {'reason': 'recent_success_rate_decline', **detail,
                    'recent_10_rate': recent / 10, 'previous_10_rate': previous / 10}
    return None


def model_access_failed(result: dict) -> bool:
    # Use the runtime's exception type, not words from an agent's explanation.
    return str(result.get("failure_reason") or "").startswith("plan_runtime:GatewayAuthError:")


def model_infrastructure_failure(result: dict) -> str | None:
    if str(result.get("failure_reason") or "").startswith("plan_runtime:GatewayQuotaError:"):
        return "model_quota_exhausted"
    if model_access_failed(result):
        return "model_access_denied"
    reason = str(result.get("failure_reason") or "")
    prefix = "plan_runtime:GatewayTransientError:"
    if not reason.startswith(prefix):
        return None
    detail = reason[len(prefix):]
    if detail.startswith(("connection:", "timeout:", "rate-limit:", "transient (429):")):
        return "model_transport_unavailable"
    # A relay may wrap a connection failure in HTTP 500. A generic 500 or the
    # gateway's unclassified-transient fallback is insufficient for exclusion.
    if detail.startswith("transient (500):") and "Cannot connect to host " in detail:
        return "model_transport_unavailable"
    return None
