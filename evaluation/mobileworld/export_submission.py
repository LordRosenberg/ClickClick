"""Convert a completed ClickClick run to MobileWorld's traj_logs layout.

This prepares local review material only; it never publishes a submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from evaluation.mobileworld.long_run_health import atomic_json


def trajectory_dir_name(model: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", model.lower()).strip(".-")
    return f"clickclick-{slug or 'unknown-model'}"


def copy_recording(case_dir: Path, destination: Path, recording: dict[str, Any]) -> dict[str, Any]:
    """Make the audited recording portable without exporting private host paths."""
    source_root = (case_dir / "video").resolve()
    segments = []
    for segment in recording["segments"]:
        relative = Path(segment["file"])
        source = (source_root / relative).resolve()
        if relative.is_absolute() or not source.is_relative_to(source_root) or not source.is_file():
            raise ValueError("missing or unsafe video segment")
        size = source.stat().st_size
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if not size or (segment.get("bytes") is not None and segment["bytes"] != size):
            raise ValueError("video size differs from audited recording")
        if segment.get("sha256") and segment["sha256"] != digest:
            raise ValueError("video hash differs from audited recording")
        target = destination / "video" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        segments.append({"file": relative.as_posix(), "bytes": size, "sha256": digest,
                         **{key: segment[key] for key in (
                             "duration_s", "dimensions", "validation", "decoded_frames",
                             "first_frame_pts_s", "last_frame_pts_s", "max_frame_interval_s"
                         ) if key in segment}})
    public = {"mode": recording.get("mode"), "segments": segments,
              "interruption_count": len(recording.get("interruptions") or [])}
    atomic_json(destination / "video" / "recording-manifest.json", public)
    return public


def export(run_root: Path, output_root: Path, *, allow_partial: bool = False,
           allow_adjudicated: bool = False) -> dict[str, Any]:
    experiment_file = run_root / "experiment.json"
    if experiment_file.exists():
        experiment = json.loads(experiment_file.read_text(encoding="utf-8"))
        if experiment.get("submission_eligible") is False:
            raise ValueError("diagnostic experiment is not eligible for official-score export")
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((run_root / "results.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    expected = {row["task"] for row in cases}
    actual = [row["task"] for row in results]
    if len(actual) != len(set(actual)) or not set(actual) <= expected:
        raise ValueError("results contain duplicate or unlisted tasks")
    if not allow_partial and (len(cases) != 117 or set(actual) != expected):
        raise ValueError("submission requires all 117 GUI-only cases")
    if any(row.get("environment_status") != "ready" or not isinstance(row.get("score"), dict)
           or "score" not in row["score"] for row in results):
        raise ValueError("submission has a case without an official score")
    adjudicated = [row for row in results if row.get("score_provenance") == "user_authorized_audit"]
    if adjudicated and not allow_adjudicated:
        raise ValueError("audited results require --allow-adjudicated and explicit disclosure")
    for row in adjudicated:
        if row.get("official_score") is not None or not row.get("adjudication"):
            raise ValueError("audited result must preserve missing official score and adjudication evidence")

    variant_file = run_root / "goal-variant.json"
    variant = None
    if variant_file.exists():
        audit = json.loads(variant_file.read_text(encoding="utf-8"))
        tasks = {}
        for row in results:
            task_audit = audit.get("tasks", {}).get(row["task"], {})
            if task_audit.get("delivered_goal") != row["goal"]:
                raise ValueError("instruction-variant audit does not match the scored goal")
            tasks[row["task"]] = {key: task_audit[key] for key in
                ("original_goal", "delivered_goal", "source_file_sha256") if key in task_audit}
            source = task_audit.get("source", "")
            if source.startswith(("https://", "http://")):
                tasks[row["task"]]["source"] = source
        variant = {key: audit[key] for key in
            ("variant", "spec_sha256", "fixture_changed", "scorer_changed", "automatic_login") if key in audit}
        variant["tasks"] = tasks

    traj_root = output_root / "traj_logs" / trajectory_dir_name(str(manifest.get("model") or ""))
    traj_root.mkdir(parents=True, exist_ok=True)
    total_frames = 0
    fixture_contexts = {}
    for row in results:
        task = row["task"]
        case_dir = run_root / f"{row['ordinal']:03d}-{task}"
        video = row.get("recording") or {}
        if video.get("error") or not video.get("segments"):
            raise ValueError(f"missing audited video for {task}")
        destination = traj_root / task
        copy_recording(case_dir, destination, video)
        context = row.get("fixture_context") or {}
        if context.get("required"):
            delivery = json.loads((case_dir / "fixture-context-delivery.json").read_text(encoding="utf8"))
            if (context.get("ready") is not True or
                    hashlib.sha256(delivery["context"].encode()).hexdigest() != context["context_sha256"]):
                raise ValueError(f"fixture context delivery does not match audit for {task}")
            disclosed = {key: context[key] for key in (
                "version", "source", "document_sha256", "context_sha256",
                "model_visible_context_changed", "original_goal_unchanged", "automatic_login",
                "browser_authenticated") if key in context}
            disclosed.update(context=delivery["context"], prompt_hashes=delivery["prompt_hashes"])
            fixture_contexts[task] = disclosed
            atomic_json(destination / "fixture-context.json", disclosed)
        screenshots = destination / "screenshots"
        screenshots.mkdir(parents=True, exist_ok=True)
        trajectory = []
        for step in row.get("steps", []):
            ordinal = int(step["seq"]) + 1
            action = dict(step.get("action") or {})
            action["action_type"] = action.pop("type", "unknown")
            trajectory.append({
                "task_goal": row["goal"], "step": ordinal,
                "prediction": step.get("summary") or "",
                "action": action, "ask_user_response": None, "tool_call": None,
                "clickclick": {
                    "executor_decision": step.get("executor_decision"),
                    "action_receipt": step.get("action_receipt"),
                    "observation_id": step.get("observation_id"),
                    "post_observation_id": step.get("post_observation_id"),
                },
            })
            image_ref = step.get("annotated_ref") or step.get("post_annotated_ref")
            if not image_ref:
                raise ValueError(f"missing step screenshot reference: {task} #{ordinal}")
            image = (case_dir / "artifacts" / image_ref).resolve()
            artifacts = (case_dir / "artifacts").resolve()
            if not image.is_relative_to(artifacts) or not image.is_file():
                raise ValueError(f"missing or unsafe screenshot: {task} #{ordinal}")
            shutil.copyfile(image, screenshots / f"{task}-0-{ordinal}.png")
            total_frames += 1
        atomic_json(destination / "traj.json", {"0": {
            "tools": None, "traj": trajectory, "token_usage": {},
        }})
        score = row["score"]
        adjudication = row.get("adjudication") if row in adjudicated else None
        score_source = "user_authorized_audit" if adjudication else "MobileWorld /task/eval"
        (destination / "result.txt").write_text(
            f"score: {float(score['score'])}\nreason: {score.get('reason', '')}\n"
            + (f"score_source: {score_source}\nofficial_score: unavailable\n" if adjudication else ""), encoding="utf-8"
        )
        atomic_json(destination / "clickclick-evidence.json", {
            "task": task, "official_score": None if adjudication else score,
            "reported_score": score, "score_source": score_source, "adjudication": adjudication,
            "video_manifest": "video/recording-manifest.json",
            "answer_submission": row.get("answer_submission"),
        })
    passed = sum(float(row["score"]["score"]) >= 1.0 for row in results)
    report = {
        "official_score_source": "MobileWorld /task/eval",
        "officially_scored_cases": len(results) - len(adjudicated),
        "adjudicated_cases": len(adjudicated),
        "aggregate_score_basis": "official plus disclosed audit" if adjudicated else "official",
        "traj_root": traj_root.relative_to(output_root).as_posix(),
        "cases": len(results), "expected_cases": len(cases), "passed": passed,
        "gui_only_percent": round(100 * passed / len(results), 4) if results else None,
        "screenshots": total_frames,
        "submission_ready": len(results) == 117 and set(actual) == expected and not adjudicated,
        "local_review_complete": len(results) == len(cases) and set(actual) == expected,
    }
    if adjudicated:
        atomic_json(output_root / "adjudications.json", {row["task"]: row["adjudication"] for row in adjudicated})
    if variant is not None:
        atomic_json(output_root / "instruction-variant.json", variant)
        report["instruction_variant"] = variant.get("variant")
    if fixture_contexts:
        atomic_json(output_root / "fixture-context-disclosure.json", fixture_contexts)
        report["fixture_context_tasks"] = list(fixture_contexts)
        report["pristine_upstream_context"] = False
    atomic_json(output_root / "submission-summary.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--allow-adjudicated", action="store_true",
                        help="Export separately disclosed human-authorized audits; never label them official")
    args = parser.parse_args()
    print(json.dumps(export(Path(args.run_root), Path(args.output_root), allow_partial=args.allow_partial,
                            allow_adjudicated=args.allow_adjudicated),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
