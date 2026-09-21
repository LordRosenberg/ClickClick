"""Provider outage exclusion must not hide ordinary task or model errors."""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def classification(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "evaluation/androidworld"))
    return importlib.import_module("result_classification")


@pytest.mark.parametrize("reason, expected", [
    ("plan_runtime:GatewayQuotaError:quota exhausted: usage_limit_reached", "model_quota_exhausted"),
    ("plan_runtime:GatewayTransientError:transient (429): rate limited", "model_transport_unavailable"),
    ("The task says usage_limit_reached", None),
    ("plan_runtime:GatewayAuthError:auth error (403)", "model_access_denied"),
    ("plan_runtime:GatewayTransientError:connection: disconnected", "model_transport_unavailable"),
    ("plan_runtime:GatewayTransientError:timeout: request timed out", "model_transport_unavailable"),
    ("plan_runtime:GatewayTransientError:rate-limit: too many requests", "model_transport_unavailable"),
    ("plan_runtime:GatewayTransientError:transient (500): ChatgptException - Cannot connect to host chatgpt.com:443", "model_transport_unavailable"),
    ("plan_runtime:GatewayTransientError:transient (500): internal error", None),
    ("plan_runtime:GatewayTransientError:unclassified (treated transient): invalid value", None),
    ("plan_runtime:GatewayBudgetError:context too long", None),
    ("Planner says Cannot connect to host chatgpt.com:443", None),
    ("planner_inconclusive", None),
    (None, None),
])
def test_only_explicit_provider_failures_are_excluded(classification, reason, expected):
    assert classification.model_infrastructure_failure({"failure_reason": reason}) == expected


def test_analysis_preserves_raw_record_but_separates_transport_failure(classification, tmp_path):
    analyze_episode = importlib.import_module("analyze_results").analyze_episode
    result = {"case": "Example", "architecture": "plan_executor", "valid": True,
              "score": 0, "budgeted_success": False,
              "failure_reason": "plan_runtime:GatewayTransientError:connection: disconnected"}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    analyzed = analyze_episode(path)
    assert analyzed["valid"] is True
    assert analyzed["evaluation_valid"] is False
    assert analyzed["infrastructure_failure"] == "model_transport_unavailable"
    assert analyzed["score"] == 0
    assert json.loads(path.read_text(encoding="utf-8")) == result

@pytest.mark.parametrize('wins,total,expected', [
    (0, 0, False), (0, 1, False), (1, 2, False), (0, 9, False),
    (0, 10, True), (6, 10, True), (7, 10, False),
])
def test_accuracy_floor_uses_valid_budgeted_success(classification,wins,total,expected):
    rows=[{'valid':True,'budgeted_success':i<wins} for i in range(total)]
    rows.append({'valid':False,'budgeted_success':False})
    reason=classification.accuracy_pause_reason(rows,.65)
    assert bool(reason) is expected
    if reason: assert reason['valid']==total


def test_accuracy_decline_and_exact_floor(classification):
    row=lambda passed: {'valid':True,'budgeted_success':passed}
    assert classification.accuracy_pause_reason([row(True)]*10+[row(True)]*8+[row(False)]*2,.65)['reason']=='recent_success_rate_decline'
    assert classification.accuracy_pause_reason([row(True)]*6+[row(False)]*4+[row(True)]*7+[row(False)]*3,.65) is None
    assert classification.accuracy_pause_reason([row(True)]*9+[row(False)]*2,.65) is None
