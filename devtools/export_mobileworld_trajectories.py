"""Export public role decisions and screenshots, never raw databases or model logs."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from PIL import Image


def public_text(value):
    text = str(value or "")
    # Host identities and credentials are not part of a public task explanation.
    text = re.sub(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:[\\/]|/(?:Users|home)/)[^\s\"'<>]+", "[private path omitted]", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,})", "[credential omitted]", text)
    text = re.sub(r"(?i)(password\s*(?:is|:|=)\s*)[^\s,;]+", r"\1[omitted]", text)
    return text


def public_exchange(question, answer):
    """Preserve the exchange while withholding authentication answers."""
    sensitive = re.search(r"password|passcode|verification code|one.time code|验证码|密码|口令", question, re.I)
    return public_text(question), "[Authentication answer omitted]" if sensitive else public_text(answer)


def export_episode(episode, destination, task, variant):
    artifacts = (episode / "artifacts").resolve()
    db = episode / "clickclick.db"
    result = json.loads((episode / "mobileworld-result.json").read_text(encoding="utf-8"))
    steps = {step["seq"] + 1: step for step in result.get("steps", [])}
    events, screens, screen_ids = [], {}, {}
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        observations = {key: json.loads(payload) for key, payload in connection.execute(
            "select record_key,payload_json from agent_records where kind='observation'")}
        traces = [(kind, json.loads(payload), seq) for kind, payload, seq in connection.execute(
            "select kind,payload_json,step_seq from traces where kind in "
            "('planner_decision','reviewer_decision','executor_tick','agent_tool_started') order by id")
            if kind != "agent_tool_started" or json.loads(payload).get("name") == "ask_user"]
        question_images = {}
        for payload, rowid in connection.execute("select payload_json,min(rowid) from agent_records where kind='human_input' group by record_key"):
            item = json.loads(payload)
            before = connection.execute("select payload_json from agent_records where kind='observation' and rowid < ? order by rowid desc limit 1", (rowid,)).fetchone()
            question_images[item["call_id"]] = json.loads(before[0]).get("image_ref") if before else None

    receipts = {item["call_id"]: item for item in result.get("human_interaction", {}).get("dispatches", [])}
    exported_questions = set()

    def image(ref):
        if not ref:
            return None, None
        source = (artifacts / ref).resolve()
        if not source.is_relative_to(artifacts):
            raise ValueError("Artifact outside episode")
        if not source.is_file():
            return None, None
        if ref not in screens:
            name = hashlib.sha256(source.read_bytes()).hexdigest() + ".webp"
            target = destination / "screens" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with Image.open(source) as picture:
                    picture = picture.convert("RGB")
                    picture.thumbnail((432, 960), Image.Resampling.LANCZOS)
                    picture.save(target, quality=80)
            screens[ref] = "screens/" + name
        if ref not in screen_ids:
            screen_ids[ref] = len(screen_ids)
        return screens[ref], screen_ids[ref]

    def observation_image(refs):
        for ref in refs:
            observation = observations.get(ref)
            if observation and observation.get("image_ref"):
                return observation["image_ref"]
        return None

    previous = None
    for index, (kind, payload, seq) in enumerate(traces):
        if kind == "agent_tool_started":
            receipt = receipts.get(payload["call_id"])
            if receipt is None or payload["call_id"] in exported_questions:
                raise ValueError("Question has no unique matched official answer")
            question, answer = public_exchange(receipt["action"]["text"], receipt["answer"])
            screenshot, number = image(question_images.get(payload["call_id"]))
            events.append({"role": payload["role"].title(), "kind": "Question", "summary": question,
                           "question": question, "answer": answer, "round": receipt["round"],
                           "answerSource": "Official MobileWorld simulated user", "action": "ask_user",
                           "reason": "", "screenshot": screenshot, "screenNumber": number})
            exported_questions.add(payload["call_id"])
            continue
        role = {"planner_decision": "Planner", "reviewer_decision": "Reviewer", "executor_tick": "Executor"}[kind]
        if role == "Executor":
            decision = payload.get("decision", "act")
            summary = payload.get("summary") or decision
            action = payload.get("action") or {}
            # Text entry values, internal identifiers and model messages stay private.
            fields = [f"{key}={action[key]}" for key in ("index", "app", "direction", "key") if action.get(key) is not None]
            act = action.get("type", decision) + ("(" + ", ".join(fields) + ")" if fields else "")
            ref = payload.get("som_ref") or observation_image([payload.get("observation_id")])
            reason = ""
        else:
            submission = payload["decision"]
            decision = submission["decision"]
            plan = submission.get("plan") or {}
            stage = plan.get("current_stage") or {}
            summary = stage.get("goal") or submission.get("reason") or decision
            reason = submission.get("reason", "")
            act = decision
            # Citations may point at older evidence, not the current device state.
            # Like AndroidWorld, show the screen at this execution boundary.
            following = next((entry for entry_kind, entry, _ in traces[index + 1:] if entry_kind == "executor_tick"), None)
            ref = following.get("som_ref") if following else previous
        if not ref:
            following = next((entry for entry_kind, entry, _ in traces[index + 1:] if entry_kind == "executor_tick"), None)
            ref = previous or (following.get("som_ref") if following else None)
        screenshot, screen_number = image(ref)
        events.append({"role": role, "kind": "Action" if role == "Executor" and decision == "act" else "Decision",
                       "summary": public_text(summary), "action": public_text(act), "reason": public_text(reason),
                       "screenshot": screenshot, "screenNumber": screen_number})
        post = steps.get(seq, {}) if role == "Executor" else {}
        previous = post.get("post_annotated_ref") or observation_image([post.get("post_observation_id")]) or ref or previous

    # Keep the actual final observation as evidence, including budget-ended runs.
    last_step = (result.get("steps") or [{}])[-1]
    final_ref = last_step.get("post_annotated_ref") or observation_image([last_step.get("post_observation_id")])
    if final_ref:
        screenshot, number = image(final_ref)
        events.append({"role": "", "kind": "Observation", "summary": "Final device state", "action": "", "reason": "",
                       "screenshot": screenshot, "screenNumber": number})
    counts = dict(Counter(event["role"] for event in events if event["role"]))
    if len(exported_questions) != len(receipts):
        raise ValueError("Public trajectory lost an official question")
    output = {"task": task, "variant": variant, "screenCount": len(screen_ids), "roleCounts": counts, "events": events}
    name = f"{task}-{variant}.json"
    (destination / name).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return "media/trajectories/" + name, counts


def prune_images(destination):
    """Remove only generated hash-named images no longer used by any trajectory."""
    used = {event["screenshot"] for path in destination.glob("*.json")
            for event in json.loads(path.read_text(encoding="utf-8"))["events"] if event["screenshot"]}
    directory = (destination / "screens").resolve()
    for path in directory.glob("*.webp"):
        if re.fullmatch(r"[0-9a-f]{64}\.webp", path.name) and "screens/" + path.name not in used:
            assert path.resolve().is_relative_to(directory)
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    originals = json.loads((args.run / "original/results.json").read_text(encoding="utf-8"))
    variants = {row["task"]: (arm, row) for arm in ("clarified", "clarified-extra")
                for row in json.loads((args.run / arm / "results.json").read_text(encoding="utf-8"))}
    args.output.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    for row in originals:
        case = next(item for item in data["cases"] if item["id"] == row["task"])
        episode = args.run / "original" / f"{row['ordinal']:03d}-{row['task']}"
        case["original"]["trajectory"], roles = export_episode(episode, args.output, row["task"], "original")
        counts.update(roles)
        if row["task"] in variants:
            arm, changed = variants[row["task"]]
            episode = args.run / arm / f"{changed['ordinal']:03d}-{row['task']}"
            case["clarified"]["trajectory"], roles = export_episode(episode, args.output, row["task"], "clarified")
            counts.update(roles)
        else:
            case["clarified"]["trajectory"] = case["original"]["trajectory"]
        print(f"Exported {row['ordinal']:03d} {row['task']}", flush=True)
    # Demo cards only require their static covers. Existing recordings stay in README.
    for demo in data["demos"]:
        for key in ("steps", "video", "preview", "output_sha256", "playback_speed", "sequence", "preview_disclosure"):
            demo.pop(key, None)
    args.data.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prune_images(args.output)
    print("Published role counts:", dict(counts), flush=True)


if __name__ == "__main__":
    main()
