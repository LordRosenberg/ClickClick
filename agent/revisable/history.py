"""Literal indexes and bounded previews. No interpretation of task evidence."""

import json

from agent.revisable.recall import source_id


def preview(text: str, query: str = "", limit: int = 160) -> str:
    position = text.lower().find(query.lower()) if query else 0
    start = max(0, position - limit // 3)
    end = start + limit
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def page(
    items: list[dict], key: str, offset: int = 0, limit: int = 12, character_budget: int = 6000
) -> dict:
    selected = []
    for item in items[offset : offset + limit]:
        size = len(json.dumps(item, ensure_ascii=False))
        if size > character_budget:
            break
        selected.append(item)
        character_budget -= size
    end = offset + len(selected)
    return {key: selected, "total": len(items), "next_offset": end if end < len(items) else None}


def event_sources(event: dict) -> set[str]:
    receipt = event["action_result"]["receipt"] or {}
    return {
        value
        for value in (
            event["observation_id"],
            receipt.get("observation_id"),
            receipt.get("effect_observation_id"),
        )
        if value
    }


def associated_events(store, observation_id: str) -> list[dict]:
    return [
        row["payload"]
        for row in store.records("event")
        if observation_id in event_sources(row["payload"])
    ]


def stage_directory(store, query: str = "") -> list[dict]:
    return [
        {
            **row["payload"],
            "stage_id": row["key"],
            "source": source_id("stage", row),
            "goal": preview(row["payload"]["goal"], query, 600),
            "goal_truncated": len(row["payload"]["goal"]) > 600,
        }
        for row in reversed(store.records("stage"))
        if query.lower() in row["payload"]["goal"].lower()
    ]


def note_directory(store, query: str = "") -> list[dict]:
    notes = []
    for row in reversed(store.records("note")):
        note = row["payload"]
        if query.lower() not in "\n".join((row["key"], note["title"], note["content"])).lower():
            continue
        sources = note["observation_ids"]
        notes.append(
            {
                "note_key": row["key"],
                "source": source_id("note", row),
                "version": row["version"],
                "title": note["title"],
                "preview": preview(note["content"], query),
                "observation_ids": sources[-2:],
                "omitted_source_count": max(0, len(sources) - 2),
                **{key: note[key] for key in ("written_step", "stage_id") if key in note},
            }
        )
    return notes


def observation_directory(
    store, *, stage_id: str | None, start_step: int | None, end_step: int | None, query: str
) -> list[dict]:
    stages = {row["key"]: row["payload"] for row in store.records("stage")}
    events_by_source: dict[str, list[dict]] = {}
    for row in store.records("event"):
        for source in event_sources(row["payload"]):
            events_by_source.setdefault(source, []).append(row["payload"])
    rows = []
    for row in store.records("observation"):
        record = row["payload"]
        visits = [
            visit
            for visit in record["visits"]
            if (stage_id is None or visit["stage_id"] == stage_id)
            and (start_step is None or visit["step"] >= start_step)
            and (end_step is None or visit["step"] <= end_step)
        ]
        if not visits:
            continue
        events = events_by_source.get(row["key"], [])
        candidates = [("observation_text", record["text"])]
        candidates += [
            ("executor_report", event["executor_report"])
            for event in events
            if (stage_id is None or event["stage_id"] == stage_id)
            and (start_step is None or event["step"] >= start_step)
            and (end_step is None or event["step"] <= end_step)
        ]
        candidates += [
            ("stage_goal", stages[visit["stage_id"]]["goal"])
            for visit in visits
            if visit["stage_id"] in stages
        ]
        matches = [(source, text) for source, text in candidates if query.lower() in text.lower()]
        if not matches:
            continue
        source, text = matches[0]
        rows.append(
            {
                "observation_id": row["key"],
                "app": record["app"],
                "captured_step": record["step"],
                "matching_visits": visits[-3:],
                "omitted_visit_count": max(0, len(visits) - 3),
                "preview_source": source,
                "preview": preview(text, query),
                "event_previews": [
                    {
                        "step": event["step"],
                        "report_preview": preview(event["executor_report"], limit=120),
                    }
                    for event in events[-2:]
                ],
                "event_count": len(events),
            }
        )
    return rows
