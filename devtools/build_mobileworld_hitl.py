"""Publish the complete User-Int cohort as a separate gallery dataset."""
import argparse
import json
from pathlib import Path

from export_mobileworld_trajectories import export_episode


def build(run, site, leaderboard):
    rows = json.loads((run / "interaction/results.json").read_text("utf8"))
    manifest = json.loads((run / "interaction/manifest.json").read_text("utf8"))
    metadata = {row["task"]: row["metadata"] for row in manifest["cases"]}
    expected = {row["name"] for row in json.loads((run / "official-task-list.json").read_text("utf8"))
                if "agent-user-interaction" in row.get("tags", []) and "agent-mcp" not in row.get("tags", [])}
    assert len(rows) == len(expected) == 44 and {row["task"] for row in rows} == expected
    assert all(row["environment_status"] == "ready" and row["score"]["score"] in (0, 1) for row in rows)
    destination = site / "mobileworld/media/hitl-trajectories"
    destination.mkdir(parents=True, exist_ok=True)
    cases = []
    for row in rows:
        task = row["task"]
        result = {"score": row["score"]["score"], "goal": row["goal"],
                  "actions": row["logical_action_steps"], "rounds": row["mobileworld_rounds"],
                  "seconds": round(row["elapsed_s"], 1), "budget": row["max_steps_budget"],
                  "apps": metadata[task]["apps"]}
        episode = run / "interaction" / f"{row['ordinal']:03d}-{task}"
        url, _ = export_episode(episode, destination, task, "hitl")
        result.update(trajectory=url.replace("media/trajectories/", "media/hitl-trajectories/"),
                      queries=row["human_interaction"]["query_count"])
        cases.append({"id": task, "ordinal": row["ordinal"], "original": result, "changed": False})
    board = [row for row in json.loads(leaderboard.read_text("utf8"))["results"]
             if isinstance(row.get("user_int"), (int, float)) and row.get("max_steps") == 50]
    data = {"category": "hitl", "date": "2026-10-15", "comparison_date": "2026-10-16",
            "model": "GPT-5.6-SOL", "reasoning": "high", "jev": "off", "tasks": 44,
            "original_passed": sum(row["score"]["score"] == 1 for row in rows),
            "questions": sum(row["human_interaction"]["query_count"] for row in rows),
            "leaderboard_source": "https://tongyi-mai.github.io/MobileWorld/",
            "leaderboard": [{"model": row["model"], "score": row["user_int"],
                             "category": row["category"], "runs": row["runs"]}
                            for row in sorted(board, key=lambda row: -row["user_int"])],
            "cases": cases, "clarifications": []}
    (site / "mobileworld/hitl.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf8")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--leaderboard", type=Path, required=True)
    args = parser.parse_args()
    data = build(args.run, args.site, args.leaderboard)
    print(json.dumps({"cases": data["tasks"], "passed": data["original_passed"], "questions": data["questions"]}))
