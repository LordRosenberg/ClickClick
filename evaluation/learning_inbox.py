"""Operator-owned statistics inbox. No model, device or publication side effects."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from agent.skills.policy import ContributionReport


def aggregate_reports(values) -> dict:
    seen, counts, outcomes = {}, Counter(), Counter()
    for value in values:
        report = ContributionReport.checked(value)
        normalized = report.model_dump(mode="json")
        key = str(report.report_id)
        if key in seen:
            if seen[key] != normalized:
                raise ValueError("conflicting duplicate report")
            continue
        seen[key] = normalized
        outcomes[report.outcome] += 1
        for app in report.apps or ["unknown"]:
            for signal in report.signals:
                counts[(app, signal)] += 1
    return {"schema_version": 1, "reports": len(seen), "outcomes": dict(outcomes),
        "issues": [{"app": app, "signal": signal, "reports": count,
            "status": "needs_operator_reproduction"}
            for (app, signal), count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))],
        "publication_eligible": False, "model_calls": 0, "device_actions": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.reports) > 1000:
        parser.error("at most 1000 reports per batch")
    values = []
    for path in args.reports:
        if path.stat().st_size > 128000:
            parser.error("report exceeds 128000 bytes")
        values.append(json.loads(path.read_text(encoding="utf-8")))
    result = aggregate_reports(values)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
