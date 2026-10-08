"""Optional task-local persistence of completed, typed Jev judgments."""
from __future__ import annotations

import json
import math
import uuid

from agent.revisable.citation_check import canonical, digest

CACHE_VERSION = "clickclick.jev-completed.v1"
MAX_ENTRIES = 1000


def valid_result(value, rules):
    required={
        "verdict", "confidence", "probabilities", "issue_probabilities", "actual_model"
    }
    if not isinstance(value,dict) or set(value) not in (required,required|{'fidelity_probabilities'},required|{'fidelity_probabilities','detail_fidelity_probability'}):
        return False
    def probability(number):
        return type(number) in {int, float} and math.isfinite(number) and 0 <= number <= 1
    probabilities, issues = value["probabilities"], value["issue_probabilities"]
    if 'detail_fidelity_probability' in value and not probability(value['detail_fidelity_probability']):return False
    if 'fidelity_probabilities' in value:
        from agent.revisable.jev_facets import MAX_FACETS
        f=value['fidelity_probabilities']
        if not isinstance(f,list) or not 1<=len(f)<=MAX_FACETS or not all(probability(v) for v in f):return False
    choice = {"supported": "supports", "contradicted": "contradicts", "unsupported": "says_nothing"}.get(value["verdict"])
    return (choice is not None and probability(value["confidence"])
        and isinstance(probabilities, dict) and set(probabilities) == {"supports", "contradicts", "says_nothing"}
        and all(probability(v) for v in probabilities.values())
        and abs(sum(probabilities.values()) - 1) <= 0.02
        and probabilities[choice] >= max(probabilities.values())
        and isinstance(issues, dict) and set(issues) == set(rules)
        and all(probability(v) for v in issues.values())
        and isinstance(value["actual_model"], str) and bool(value["actual_model"].strip()))


class CompletedChecks:
    """Cache misses/corruption/storage faults never become a support judgment."""
    def __init__(self, artifacts, rules):
        self.artifacts, self.rules = artifacts, rules
        self.errors = 0

    def namespace(self, task_id):
        return "jev-cache/" + digest(task_id)

    def path(self, task_id, key):
        return self.artifacts.resolve(self.namespace(task_id) + "/" + key + ".json")

    def load(self, task_id, key):
        try:
            path = self.path(task_id, key)
            if not path.exists() or path.stat().st_size > 32768:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["version"] != CACHE_VERSION or record["key"] != key
                    or record["task"] != digest(task_id)
                    or record["result_hash"] != digest(record["result"])
                    or not valid_result(record["result"], self.rules)):
                return None
            return record["result"]
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            self.errors += 1
            return None

    def save(self, task_id, key, result):
        temporary = None
        try:
            if not valid_result(result, self.rules):
                return
            path = self.path(task_id, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(uuid.uuid4().hex + ".tmp")
            temporary.write_text(canonical({"version": CACHE_VERSION, "key": key,
                "task": digest(task_id), "result": result, "result_hash": digest(result)}), encoding="utf-8")
            temporary.replace(path)
        except (OSError, ValueError):
            self.errors += 1
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.errors += 1

    def prune(self, task_id):
        try:
            self.artifacts.prune(self.namespace(task_id), max_files=MAX_ENTRIES)
        except (OSError, ValueError):
            self.errors += 1
