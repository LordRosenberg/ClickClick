"""Read-only audit of frozen AndroidWorld params against deterministic seeds."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import pickle
import random
import sys
from typing import Any

import numpy as np


def fingerprint(value: Any) -> str:
    if hasattr(value, "tobytes") and hasattr(value, "size") and hasattr(value, "mode"):
        payload = (
            f"image:{value.mode}:{value.size}:".encode()
            + value.tobytes()
        )
    else:
        payload = pickle.dumps(value)
    return hashlib.sha256(payload).hexdigest()


def audit(batch: Path, start_index: int) -> dict[str, Any]:
    batch = batch.resolve()
    sys.path[:0] = [str(batch / "latest-shallow"), str(batch / "runner")]
    from android_world import registry

    classes = registry.TaskRegistry().get_registry("android_world")
    rows = pickle.loads((batch / "frozen-params.pkl").read_bytes())
    findings = []
    raw_construct_errors = []
    construct_errors = []
    value_mismatches = []
    for index, row in enumerate(rows):
        cls = classes[row["task"]]
        seed = row["seed"]
        random.seed(seed)
        np.random.seed(seed)
        generated = cls.generate_random_params()
        generated["seed"] = seed
        frozen = row["params"]
        missing = sorted(set(generated) - set(frozen))
        extra = sorted(set(frozen) - set(generated))
        mismatched = []
        for key in sorted(set(generated) & set(frozen)):
            try:
                equal = fingerprint(generated[key]) == fingerprint(frozen[key])
            except Exception as exc:  # Preserve a typed audit result, not a guess.
                equal = False
                mismatched.append({"key": key, "comparison_error": repr(exc)})
                continue
            if not equal:
                mismatched.append({"key": key})
        if missing or extra or mismatched:
            finding = {
                "index": index,
                "task": row["task"],
                "missing_from_frozen": missing,
                "extra_in_frozen": extra,
                "value_mismatches": mismatched,
            }
            findings.append(finding)
            if mismatched:
                value_mismatches.append(finding)

        if index >= start_index:
            try:
                cls(copy.deepcopy(frozen))
            except Exception as exc:
                raw_construct_errors.append({
                    "index": index,
                    "task": row["task"],
                    "error": repr(exc),
                })
            candidate = copy.deepcopy(frozen)
            for key in missing:
                candidate[key] = generated[key]
            try:
                cls(copy.deepcopy(candidate))
            except Exception as exc:
                construct_errors.append({
                    "index": index,
                    "task": row["task"],
                    "error": repr(exc),
                })

    return {
        "policy": "deterministic-seed-frozen-param-audit-v1",
        "task_count": len(rows),
        "start_index": start_index,
        "remaining_task_count": len(rows) - start_index,
        "findings": findings,
        "missing_key_findings_remaining": [
            row for row in findings
            if row["index"] >= start_index and row["missing_from_frozen"]
        ],
        "value_mismatches": value_mismatches,
        "remaining_raw_construct_errors": raw_construct_errors,
        "remaining_construct_errors_after_missing_key_repair": construct_errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.batch, args.start_index)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
