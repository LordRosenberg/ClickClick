"""Runtime-owned text-check metadata; never an action or completion gate."""

import hashlib
import json

from agent.revisable.summary import SECTIONS, parse_summary


CHECK_SCOPE = "text_fidelity_only"
SOURCE_CHECK_POLICY = (
    "These checks concern the quoted source summary claims, not the note citing them. "
    "Text fidelity is not independent visual or world verification. Unchecked does not mean false "
    "and creates no additional verification requirement."
)


def item_key(text, sources, note_source=""):
    value = json.dumps([text, sorted(set(sources)), note_source], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def collect_checks(summary, report):
    """Bind the final report by exact claim and provenance, not shifted item indices."""
    if report is None:
        return {}
    if not isinstance(report, dict):
        report = {}
    rows = report.get("results", [])
    rows = [row for row in rows if isinstance(row, dict)
            and isinstance(row.get("sources"), list)
            and all(isinstance(source, str) for source in row["sources"])] if isinstance(rows, list) else []
    result = {}
    for section, _ in SECTIONS:
        for item in getattr(summary, section):
            matches = [r for r in rows if r.get("text") == item.text
                       and set(r.get("sources", [])) == set(item.sources)]
            # Ambiguity or an interrupted check never borrows an earlier candidate's verdict.
            row = matches[0] if matches and all(r == matches[0] for r in matches) else {}
            decision = row.get("decision")
            status = ("passed" if decision == "pass" and row.get("validated") is True
                      else "uncertain" if decision == "uncertain_bypass"
                      else "rejected" if decision == "repair"
                      else "not_checked")
            issues = row.get("coverage_issues")
            reason = (issues[0] if isinstance(issues, list) and issues else
                      row.get("error_category") or row.get("degraded_reason") or "incomplete_check")
            if not isinstance(reason, str):
                reason = "incomplete_check"
            result[item_key(item.text, item.sources, item.note_source)] = {
                "scope": CHECK_SCOPE, "status": status,
                **({"reason": reason} if status == "not_checked" else {}),
                "audit_ref": report.get("audit_ref"),
                "source_hash": row.get("source_hash"),
            }
    return result


def visible_check(check):
    """A small closed vocabulary; provider diagnostics remain in the audit artifact."""
    if not isinstance(check, dict) or check.get("scope") != CHECK_SCOPE:
        return None
    status = check.get("status")
    if not isinstance(status, str) or status not in {"passed", "uncertain", "rejected", "not_checked"}:
        return None
    result = {"scope": CHECK_SCOPE, "status": status}
    if status == "not_checked":
        reason = check.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        result["reason"] = ("capacity" if reason in {"provider_context_limit", "source_limit_exceeded"}
                            else "source_unavailable" if reason in {
                                "unreadable_source", "missing_claim_or_sources", "no_original_evidence"}
                            else "check_unavailable")
    return result


def compaction_checks(store, row):
    """Read persisted metadata, or recover exact historical checks without rewriting history."""
    payload = row["payload"]
    if "item_text_checks" in payload:
        checks = payload["item_text_checks"]
        return checks if isinstance(checks, dict) else {}
    summary = parse_summary(payload.get("summary", ""))
    if summary is None:
        return {}
    # Old records have only report references. Use the last available report; no
    # guessing from aggregate degraded flags or unrelated candidates.
    refs = payload.get("jev_check_refs")
    refs = [ref for ref in refs if isinstance(ref, str) and ref] if isinstance(refs, list) else []
    if not refs:
        return {}
    try:
        report = json.loads(store.artifacts.read_text(refs[-1]))
        if not isinstance(report, dict):
            report = {"results": []}
        report.setdefault("audit_ref", refs[-1])
    except (OSError, ValueError, TypeError):
        report = {"results": []}
    return collect_checks(summary, report)


def summary_checks(store, value):
    if not store.jev_status_context:
        return {}
    for row in reversed(store.records("compaction")):
        if row["payload"].get("summary") == value:
            return compaction_checks(store, row)
    return {}


def note_source_checks(store, note_rows, character_budget=2000):
    """Report exact cited source claims, never certify or taint a rewritten note.

    Metadata has a separate bounded envelope so it cannot evict retained text.
    Only explicitly cited compactions are read; no semantic search or recursive
    trust propagation is performed.
    """
    if not store.jev_status_context:
        return {}
    from agent.revisable.recall import resolve_source, source_id

    items, omitted = [], 0
    for note in note_rows:
        for ref in dict.fromkeys(note["payload"].get("source_refs", [])):
            if not ref.startswith("compaction:"):
                continue
            try:
                kind, row, _ = resolve_source(store, ref)
                summary = parse_summary(row["payload"].get("summary", ""))
                checks = compaction_checks(store, row)
            except (OSError, ValueError, TypeError):
                continue
            if kind != "compaction" or summary is None:
                continue
            for section, _ in SECTIONS:
                for index, item in enumerate(getattr(summary, section)):
                    check = visible_check(checks.get(item_key(item.text, item.sources, item.note_source)))
                    if check is None:
                        continue
                    entry = {"note_source": source_id("note", note), "source": source_id(kind, row),
                             "item": f"/{section}/{index}", "source_claim": item.text,
                             "text_check": check}
                    candidate = {"policy": SOURCE_CHECK_POLICY, "items": [*items, entry],
                                 "omitted_count": omitted}
                    if len(json.dumps(candidate, ensure_ascii=False)) <= character_budget - 20:
                        items.append(entry)
                    else:
                        omitted += 1
    return {"policy": SOURCE_CHECK_POLICY, "items": items, "omitted_count": omitted} if items or omitted else {}
