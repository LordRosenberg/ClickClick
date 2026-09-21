"""Preserve interrupted attempts before explicit, freshly probed quota recovery."""
import hashlib
import json
from pathlib import Path
import time
import uuid


def quota_retry_allowed(result: dict) -> bool:
    return (result.get("infrastructure_failure") == "model_quota_exhausted"
            and result.get("score") is None and result.get("valid") is False
            and result.get("teardown_ok") is True and not result.get("teardown_error"))


def archive_quota_attempt(batch: Path, case: str) -> Path:
    """Called only under the batch lock after a successful live model probe."""
    batch = batch.resolve()
    if Path(case).name != case or case in (".", ".."):
        raise ValueError("Invalid case name")
    source = (batch / "episodes" / case / "plan_executor").resolve()
    destination = (batch / "interrupted-attempts" / case / uuid.uuid4().hex).resolve()
    # Verify both absolute recursive-move targets remain within the named batch.
    if not source.is_relative_to(batch) or not destination.is_relative_to(batch):
        raise ValueError("Attempt paths escape the batch")
    result_bytes = (source / "result.json").read_bytes()
    if not quota_retry_allowed(json.loads(result_bytes)):
        raise RuntimeError("Only a cleaned-up quota interruption may be retried")
    destination.parent.mkdir(parents=True, exist_ok=True)
    journal = batch / "quota-resumes" / (destination.name + ".json")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({
        "case": case, "archived_attempt": str(destination.relative_to(batch)),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "requested_at": time.time(), "retry": "official initialization with unchanged frozen parameters",
    }, indent=2), encoding="utf-8")
    source.rename(destination)
    return destination
