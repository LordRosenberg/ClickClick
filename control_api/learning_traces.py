"""Project source-bound learning artifacts into the existing task detail page."""
from __future__ import annotations

import json
from pathlib import Path


# Old jobs have no source-local catalog. Bound their compatibility scan explicitly.
LEGACY_SCAN_LIMIT = 1000


def _read(artifacts, path):
    try:
        ref = path.relative_to(artifacts.root).as_posix()
        authorized = artifacts.resolve(ref)
        if authorized != path.resolve():
            return None
        value = json.loads(authorized.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def cost_summary(value):
    if not isinstance(value, dict):
        return None
    summary = {key: value.get(key) for key in ("calls", "actions", "seconds", "job_seconds",
        "calls_include_reserved_upper_bounds", "actions_include_reserved_upper_bounds")}
    usage = value.get("provider_usage") or {}
    if isinstance(usage, dict):
        records = usage.get("records") or []
        totals = [row.get("total_tokens") if isinstance(row, dict) else None for row in records]
        known = bool(totals) and usage.get("unknown_responses") == 0 and all(type(n) is int for n in totals)
        summary["provider_usage"] = {key: usage.get(key) for key in
            ("responses", "available_responses", "unknown_responses")}
        summary["provider_usage"]["total_tokens"] = sum(totals) if known else None
    return summary


def task_learning_traces(task_id, source, data_sources, skills_root, *, session_offset=0,
        session_limit=20, entry_limit=60, entry_before=None, job_id=None, result_offset=0, result_limit=20):
    artifacts = source.artifacts
    sessions = []
    notes = []
    known_conversations = set()
    # Never resolve an input path outside this task's artifact store.
    directory = artifacts.resolve("skill-learning/runs/" + task_id)
    if directory.is_dir():
        for job in sorted(directory.iterdir()):
            if not job.is_dir():
                continue
            catalogs = sorted((job / "catalog").glob("*.json"),
                              key=lambda path: path.stat().st_mtime_ns, reverse=True)
            latest, catalog_ref = None, None
            for path in catalogs:
                value = _read(artifacts, path)
                if value and value.get("source_task_id") == task_id and value.get("job_id") == job.name:
                    latest = value
                    catalog_ref = path.relative_to(artifacts.root).as_posix()
                    break
            if latest is None:
                notes.append("A learning catalog is missing or unreadable; its outcome is unknown.")
                continue
            entries = []
            for item in latest.get("entries", []):
                if not isinstance(item, dict) or not isinstance(item.get("ref"), str):
                    continue
                try:
                    path = artifacts.resolve(item["ref"])
                except ValueError:
                    continue
                if job.resolve() not in path.parents:
                    continue
                entries.append(item)
            related = []
            for history in latest.get("histories", []):
                if not isinstance(history, dict) or not isinstance(history.get("task_id"), str):
                    continue
                located = data_sources.locate(history["task_id"])
                related.append({"namespace": history.get("namespace"), "task_id": history["task_id"], "available": located is not None,
                    "status": located[0].status.value if located else None,
                    "data_source": located[1].label if located else None})
            sessions.append({**latest, "entries": entries, "histories": related,
                "cost": cost_summary(latest.get("cost")), "catalog_ref": catalog_ref,
                "origin": "native_research_telemetry"})
            # New catalogs supersede compatibility scans for their source-bound job.
            for item in entries:
                if item.get("role") == "learner":
                    record = _read(artifacts, artifacts.resolve(item["ref"]))
                    ref = ((record or {}).get("payload") or {}).get("conversation_ref")
                    if isinstance(ref, str):
                        known_conversations.add(ref.split("/")[2] if len(ref.split("/")) > 2 else "")
                    break
    old = artifacts.resolve("skill-learning/conversations")
    if old.is_dir():
        # Existing conversations predate the catalog. This is a compatibility read,
        # not a cross-task case index or background curator.
        paths = sorted(old.glob("*/*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        if len(paths) > LEGACY_SCAN_LIMIT:
            notes.append("Legacy scan reached its limit; older missing records remain unknown.")
        found = {}
        for path in paths[:LEGACY_SCAN_LIMIT]:
            if path.parent.name in known_conversations:
                continue
            value = _read(artifacts, path)
            if not value or (value.get("source_binding") or {}).get("task_id") != task_id:
                continue
            conversation_id = value.get("job_id")
            if not isinstance(conversation_id, str):
                continue
            if conversation_id not in found or value.get("version", 0) > found[conversation_id][0].get("version", 0):
                found[conversation_id] = (value, path)
        for conversation_id, (value, path) in found.items():
            sessions.append({"job_id": conversation_id, "source_task_id": task_id,
                "origin": "legacy_learner_conversation", "version": value.get("version"),
                "updated_at": path.stat().st_mtime,
                "entries": [{"ref": path.relative_to(artifacts.root).as_posix(), "order": 1,
                    "role": "learner", "phase": "retained_conversation", "status": "historical",
                    "created_at": path.stat().st_mtime}], "histories": [], "cost": None})
    pending = []
    pending_root = Path(skills_root) / "_pending"
    if pending_root.is_dir():
        for path in pending_root.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(item, dict) or item.get("source_task_id") != task_id:
                continue
            review = item.get("review") or {}
            pending.append({"id": item.get("id", path.stem), "status": item.get("status"),
                "gist": item.get("gist"), "target": item.get("target"),
                "created_at": item.get("created_at"), "review": {key: review.get(key) for key in
                    ("eligible", "gate_reason", "candidate_hash")},
                "verdict": (review.get("review") or {}).get("verdict")})
    results = []
    for path in artifacts.resolve("skill-learning/results/" + task_id).glob("*.json"):
        value = _read(artifacts, path)
        if value and value.get("source_task_id") == task_id:
            results.append({"ref": path.relative_to(artifacts.root).as_posix(),
                "created_at": value.get("created_at"), "status": value.get("status"),
                "reason": (value.get("result") or {}).get("reason"),
                "cost": cost_summary((value.get("result") or {}).get("cost"))})
    sessions.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    if job_id is not None:
        sessions = [row for row in sessions if row["job_id"] == job_id]
    session_total = len(sessions)
    sessions = sessions[session_offset:session_offset + session_limit]
    for session in sessions:
        original = session["entries"]
        session["entry_total"] = len(original)
        session["latest_phase"] = original[-1].get("phase") if original else None
        eligible = [row for row in original if entry_before is None or row.get("order", 0) < entry_before]
        session["entries"] = eligible[-entry_limit:]
        session["older_entry_count"] = max(0, len(eligible) - entry_limit)
        session["omitted_entry_count"] = len(original) - len(session["entries"])
    results.sort(key=lambda row: row.get("created_at", 0), reverse=True)
    result_total = len(results)
    return {"task_id": task_id, "sessions": sessions, "session_total": session_total,
        "session_offset": session_offset, "session_limit": session_limit,
        "pending": pending[-20:], "pending_omitted_count": max(0, len(pending) - 20),
        "results": results[result_offset:result_offset + result_limit], "result_total": result_total,
        "result_offset": result_offset, "result_limit": result_limit,
        "notes": notes,
        "policy": "Analysis and independent review are model judgments. Cost snapshots are cumulative, not additive. Missing transcripts, tokens and evaluation results are unknown. Linked execution tasks reuse their native detail page."}
