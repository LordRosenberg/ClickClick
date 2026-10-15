"""Export the complete recorded User-Int category; never publish anything."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from evaluation.mobileworld.export_submission import copy_recording, trajectory_dir_name
from evaluation.mobileworld.long_run_health import atomic_json


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def artifact(case, reference):
    root = (case / "artifacts").resolve()
    path = (root / reference).resolve()
    if Path(reference).is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Missing or unsafe screenshot")
    return path


def observation_images(case):
    """Link each question to the observation preceding its first persisted record."""
    images, questions, current = {}, {}, None
    uri = (case / "clickclick.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        for kind, key, raw in db.execute(
            "SELECT kind,record_key,payload_json FROM agent_records "
            "WHERE kind IN ('observation','human_input') ORDER BY rowid"
        ):
            value = json.loads(raw)
            if kind == "observation":
                current = value.get("image_ref")
            else:
                images.setdefault(key, current)
                questions[key] = value
    return images, questions, current


def trajectory(row, case):
    human = row.get("human_interaction") or {}
    if human.get("enabled") is not True:
        raise ValueError("HITL result required")
    dispatches = human.get("dispatches") or []
    if human.get("query_count") != len(dispatches):
        raise ValueError("Question count differs from receipts")
    images, questions, last_image = observation_images(case)
    asks, identities = {}, set()
    for receipt in dispatches:
        identity, round_ = receipt["interaction_id"], receipt["round"]
        source = questions.get(identity, {})
        if (receipt.get("status") != "returned" or not receipt.get("answer")
                or receipt.get("provenance") != "official_simulated_user"
                or receipt.get("action", {}).get("action_type") != "ask_user"
                or source.get("call_id") != receipt.get("call_id")
                or source.get("invocation_id") != receipt.get("invocation_id")
                or identity in identities or round_ in asks or type(round_) is not int or round_ < 1):
            raise ValueError("Invalid or duplicate official question receipt")
        identities.add(identity)
        asks[round_] = {"action": receipt["action"], "ask_user_response": receipt["answer"],
                        "prediction": receipt["action"]["text"], "_image": images.get(identity)}
    # Role handoffs and finish reports are internal records, not GUI predictions.
    gui = [step for step in row.get("steps", []) if step.get("action")]
    used = len(gui) + len(asks)
    rounds = row["mobileworld_rounds"]
    if rounds not in (used, used + 1) or any(n > used for n in asks):
        raise ValueError("Prediction accounting cannot be reconstructed exactly")
    gui = iter(gui)
    result = []
    for round_ in range(1, used + 1):
        if round_ in asks:
            item = dict(asks[round_])
        else:
            step = next(gui)
            action = {k: v for k, v in step["action"].items() if v is not None and k != "type"}
            action["action_type"] = step["action"]["type"]
            item = {"action": action, "prediction": step.get("summary") or "",
                    "_image": step.get("annotated_ref") or step.get("screenshot_ref"),
                    "clickclick": {"executor_decision": step.get("executor_decision"),
                                   "action_receipt": step.get("action_receipt"),
                                   "step_seq": step["seq"]}}
        result.append(item)
    answer = row.get("answer_submission")
    if answer and rounds != used + 1:
        raise ValueError("Submitted answer has no terminal prediction round")
    if rounds == used + 1:
        action = ({"action_type": answer["official_action"], "text": answer["text"]}
                  if answer else {"action_type": "finished", "status": row["clickclick_status"]})
        result.append({"action": action, "prediction": action.get("text", row.get("failure_reason") or ""),
                       "_image": last_image, "clickclick": {"adapter_terminal_result": True,
                       "submitted_to_environment": bool(answer)}})
    for i, item in enumerate(result, 1):
        item.update(step=i, task_goal=row["goal"], tool_call=None)
        item.setdefault("ask_user_response", None)
        if not item.get("_image"):
            raise ValueError("Missing observed screenshot for prediction")
        artifact(case, item["_image"])
    return result


def validate(run_root):
    for folder in (run_root, run_root.parent):
        experiment = folder / "experiment.json"
        if experiment.exists() and read(experiment).get("submission_eligible") is False:
            raise ValueError("Diagnostic experiment is not submission eligible")
    manifest, rows = read(run_root / "manifest.json"), read(run_root / "results.json")
    metadata = {r["name"]: r for r in read(run_root.parent / "official-task-list.json")}
    expected = {r["name"] for r in metadata.values()
                if "agent-user-interaction" in r.get("tags", []) and "agent-mcp" not in r.get("tags", [])}
    goals = read(run_root.parent / "official-goals.json")
    actual = [row["task"] for row in rows]
    if (len(expected) != 44 or len(actual) != 44 or set(actual) != expected
            or {r["task"] for r in manifest["cases"]} != expected):
        raise ValueError("Exactly all 44 original User-Int results required")
    for row in rows:
        score = row.get("score")
        if (row.get("environment_status") != "ready" or not isinstance(score, dict)
                or type(score.get("score")) not in (int, float) or not 0 <= score["score"] <= 1
                or row.get("adjudication") or row.get("score_provenance") == "user_authorized_audit"
                or row["goal"] != goals[row["task"]]):
            raise ValueError("Original goal and official score required for every task")
        video = row.get("recording") or {}
        if (video.get("error") or video.get("mode") != "continuous" or not video.get("segments")
                or any(s.get("validation") != "decoded_to_eof" for s in video["segments"])):
            raise ValueError("Complete decoded continuous recording required")
    return manifest, rows


def fixture_disclosure(row, case):
    context = row.get("fixture_context") or {}
    if not context.get("required"):
        return None
    delivery = read(case / "fixture-context-delivery.json")
    if (context.get("ready") is not True or
            hashlib.sha256(delivery["context"].encode()).hexdigest() != context.get("context_sha256")):
        raise ValueError("Fixture context delivery differs from audit")
    public = {key: context[key] for key in (
        "version", "source", "document_sha256", "context_sha256", "model_visible_context_changed",
        "original_goal_unchanged", "automatic_login", "browser_authenticated") if key in context}
    return {**public, "context": delivery["context"], "prompt_hashes": delivery["prompt_hashes"]}


def copy_frame(source, target, dimensions):
    """Keep native action coordinates valid when history images were downsampled."""
    from PIL import Image

    with Image.open(source) as image:
        original = list(image.size)
        width, height = dimensions
        if width <= 0 or height <= 0 or abs(image.width / image.height - width / height) > 0.01:
            raise ValueError("Screenshot aspect ratio differs from recorded device")
        if original != dimensions:
            image = image.resize((width, height), Image.Resampling.LANCZOS)
        image.save(target, format="PNG")
    return {"source_dimensions": original, "viewer_dimensions": dimensions,
            "resized": original != dimensions,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def export(run_root, output_root):
    manifest, rows = validate(run_root)
    if output_root.exists():
        raise ValueError("Use a fresh output directory; do not mix exports")
    prepared = []
    for row in rows:
        case = run_root / f"{row['ordinal']:03d}-{row['task']}"
        prepared.append((row, case, trajectory(row, case), fixture_disclosure(row, case)))
    name = trajectory_dir_name(str(manifest["model"])) + "-hitl"
    root = output_root / "traj_logs" / name
    disclosures = {}
    for row, case, steps, context in prepared:
        target = root / row["task"]
        pictures = target / "screenshots"
        pictures.mkdir(parents=True)
        copy_recording(case, target, row["recording"])
        for step in steps:
            source = artifact(case, step.pop("_image"))
            frame = copy_frame(source, pictures / f"{row['task']}-0-{step['step']}.png",
                               row["recording"]["segments"][0]["dimensions"])
            step.setdefault("clickclick", {})["screenshot_geometry"] = frame
        atomic_json(target / "traj.json", {"0": {"tools": None, "traj": steps, "token_usage": {}}})
        score = row["score"]
        (target / "result.txt").write_text(
            f"score: {float(score['score'])}\nreason: {score.get('reason', '')}\n", encoding="utf8")
        atomic_json(target / "clickclick-evidence.json", {
            "official_score": score, "human_interaction": row["human_interaction"],
            "prediction_rounds": row["mobileworld_rounds"], "limits": row["limits"],
            "adapter_terminal_status": row["clickclick_status"],
            "fixture_context": context, "answer_submission": row.get("answer_submission")})
        if context:
            disclosures[row["task"]] = context
    if disclosures:
        atomic_json(output_root / "fixture-context-disclosure.json", disclosures)
    summary = {"local_review_complete": True, "official_acceptance": False,
               "category": "User-Int.", "cases": 44,
               "passed": sum(r["score"]["score"] == 1 for r in rows),
               "traj_root": root.relative_to(output_root).as_posix(),
               "token_usage_status": "unavailable; empty objects do not mean zero",
               "screenshot_geometry": "Persisted model-view frames may be upscaled to native device geometry; original dimensions and hashes retained. Continuous recordings unchanged.",
               "method_disclosures": read(run_root.parent / "submission-protocol.json")}
    atomic_json(output_root / "submission-summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.run_root, args.output_root), ensure_ascii=False, indent=2))
