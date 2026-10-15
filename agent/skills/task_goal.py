"""Source-local task recovery, separate from independently admitted knowledge."""
from __future__ import annotations
import hashlib

POLICY = """Task-success goal: local admission does not complete the source task.
Retain admitted knowledge; assess the earliest remaining important blocker using
native actions/observations. Separate missing delivery/adoption, a refuted method
and a new blocker. Explore a grounded discriminating test before paying for a
whole-task trial when useful; related supported findings may form one candidate.
Preserve admitted guidance in changed files and include required patches in the
exact combined proposal. Reviewer alone selects useful declared checks, source
first; reuse the sealed baseline. No forced trial or invented working route.
Skip with a concrete reason when no worthwhile feasible step remains. All phases
share the original budget. A probe finish or local pass is not task success.
"""


class TaskGoal:
    def __init__(self):
        self.status = "unresolved"
        self.retained = {}
        self.candidates = {}
        self.stop = None

    def observe(self, result, candidates, validation, *, candidate_hash, base_hash, baseline_manifest=None):
        rows = result.get("pending_ids") or ([{"id": result["pending_id"],
            "target": candidates[0]["target"], "eligible": result.get("eligible", False)}]
            if result.get("pending_id") else [])
        accepted = {r["target"] for r in rows if r.get("eligible") is True}
        for row in rows:
            if row.get("eligible") is True:
                self.retained[row["target"]] = dict(row)
        for candidate in candidates:
            if candidate["target"] in accepted:
                self.candidates[candidate["target"]] = candidate
        # Never promote an unscored probe, foreign body, or unmatched task run.
        from agent.skills.snapshot import _hash
        files = dict((baseline_manifest or {}).get("files", {}))
        files.update({c["target"]: hashlib.sha256(c["new_text"].encode()).hexdigest() for c in candidates})
        overlay_hash = _hash(files)
        if (accepted and {c["target"] for c in candidates} <= accepted and baseline_manifest
                and isinstance(validation, dict)
                and validation.get("candidate_hash") == candidate_hash
                and validation.get("base_hash") == base_hash
                and validation.get("baseline_library_hash") == baseline_manifest["hash"]
                and validation.get("candidate_library_hash") == overlay_hash
                and any(t.get("kind") == "source" and t.get("candidate_success") is True
                    and type(t.get("baseline_success")) is bool and bool(t.get("case_id"))
                    and t.get("matched_environment") is True
                    and t.get("independent_oracle") is True
                    and t.get("baseline_library_hash") == baseline_manifest["hash"]
                    and t.get("candidate_library_hash") == overlay_hash
                    and bool(t.get("config_hash")) for t in validation.get("trials", []))):
            self.status = "verified_success"

    def feedback(self, current=None):
        current = current or {}
        supplied = current.get("patches", [current])
        return {"status": self.status,
            "retained_candidates": [c for c in self.candidates.values() if c not in supplied],
            "retained_pending_ids": list(self.retained.values()),
            "policy": POLICY}

    def matched_failed_source(self, validation, *, candidate_hash, base_hash, baseline_manifest, candidates):
        """A real unresolved source check, not a sibling failure or stale body."""
        from agent.skills.snapshot import _hash
        if not baseline_manifest or not isinstance(validation, dict):
            return False
        files = dict(baseline_manifest.get("files", {}))
        files.update({c["target"]: hashlib.sha256(c["new_text"].encode()).hexdigest() for c in candidates})
        overlay = _hash(files)
        if (validation.get("candidate_hash") != candidate_hash
                or validation.get("base_hash") != base_hash
                or validation.get("baseline_library_hash") != baseline_manifest["hash"]
                or validation.get("candidate_library_hash") != overlay):
            return False
        return any(t.get("kind") == "source" and t.get("baseline_success") is False
            and t.get("candidate_success") is False and t.get("matched_environment") is True
            and t.get("independent_oracle") is True and bool(t.get("case_id"))
            and bool(t.get("config_hash")) and t.get("baseline_library_hash") == baseline_manifest["hash"]
            and t.get("candidate_library_hash") == overlay for t in validation.get("trials", []))

    def finish(self, result, budget):
        result = dict(result)
        result.update(task_goal={"status": self.status, "stop": self.stop},
            retained_pending_ids=list(self.retained.values()), cost=budget.snapshot())
        return result
