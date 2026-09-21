"""Summarize preserved emulator episodes without accessing the device or model."""

import argparse
from collections import Counter
import json
from pathlib import Path
from result_classification import model_infrastructure_failure


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def analyze_episode(path):
    result = read(path)
    episode = path.parent
    artifacts = episode / "runtime" / "artifacts"
    requests, responses = {}, {}
    for artifact in (artifacts / "llm").glob("*.json"):
        payload = read(artifact)
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        if kind == "agent_round_request":
            requests[payload["round_id"]] = payload
        elif kind == "agent_round_response":
            responses[payload["round_id"]] = payload
    summary_ids = {key for key, payload in requests.items() if any(
        tool.get("function", {}).get("name") == "save_summary" for tool in payload.get("tool_catalog", [])
    )}
    providers, input_statuses = Counter(), Counter()
    for artifact in (artifacts / "trees").glob("*.json"):
        payload = read(artifact)
        providers[payload.get("capture", {}).get("pixel_provider", "unknown")] += 1
    steps_path = episode / "steps.json"
    steps = read(steps_path) if steps_path.exists() else []
    merged = [step for step in steps if (step.get("action") or {}).get("type") == "replace_text"
              and (step.get("action") or {}).get("index") is not None]
    for artifact in (artifacts / "dialogue").glob("*.json"):
        for message in read(artifact):
            content = message.get("content")
            if not isinstance(content, str) or not content.startswith('{"action_result":'):
                continue
            receipt = json.loads(content)["action_result"]
            if "input_steps" in receipt:
                input_statuses[receipt.get("input_status", receipt["input_steps"].get("readback", "unknown"))] += 1
    request_tokens = sum(payload.get("prompt_measurements", {}).get("whole_request", {}).get("token_count", 0)
                         for payload in requests.values())
    role_calls, role_tokens, role_images = Counter(), Counter(), Counter()
    for key, payload in requests.items():
        role = "compression" if key in summary_ids else payload.get("role", "unknown")
        role_calls[role] += 1
        role_tokens[role] += payload.get("prompt_measurements", {}).get("whole_request", {}).get("token_count", 0)
        for message in payload.get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                role_images[role] += sum(isinstance(block, dict) and block.get("type") == "image_url" for block in content)
    waits = {key: float(payload.get("latency_ms") or 0) / 1000 for key, payload in responses.items()}
    row_integrity = None
    exact_target_fields = None
    if "Add" in result.get("case", "") and (episode / "final-rows.json").exists():
        before = Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in read(episode / "initial-rows.json"))
        after = Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in read(episode / "final-rows.json"))
        row_integrity = {"initial_count": sum(before.values()), "final_count": sum(after.values()),
                         "original_rows_missing_or_changed": sum((before-after).values())}
        case = result.get("case", "")
        fields = (["title", "description", "servings", "preparationTime", "source", "ingredients", "directions", "favorite"]
                  if case.startswith("RecipeAdd") else
                  ["name", "amount", "category", "note"] if case.startswith("ExpenseAdd") else [])
        if fields:
            spec = next(row for row in read(episode.parents[2] / "matrix.json") if row["task"] == case)
            expected = Counter(tuple(row.get(key) for key in fields) for row in spec["params"]["row_objects"])
            added = Counter()
            for encoded, count in (after-before).items():
                row = json.loads(encoded)
                added[tuple(row.get(key) for key in fields)] += count
            exact_target_fields = {"fields": fields, "expected_rows": sum(expected.values()),
                                   "exact_matches": sum((expected & added).values()),
                                   "unmatched_expected_rows": sum((expected-added).values())}
    return {
        **{key: result.get(key) for key in ["case", "architecture", "valid", "score", "budgeted_success",
            "false_success", "elapsed_s", "episode_steps", "executor_decisions", "failure_reason", "stop_cause"]},
        "episode": str(episode.resolve()),
        "evaluation_valid": bool(result.get("valid")) and not model_infrastructure_failure(result),
        "infrastructure_failure": model_infrastructure_failure(result) or result.get("infrastructure_failure"),
        "existing_row_integrity": row_integrity,
        "exact_target_fields": exact_target_fields,
        "model_calls": len(requests), "model_wait_s": round(sum(waits.values()), 3),
        "compression_calls": len(summary_ids),
        "compression_wait_s": round(sum(waits.get(key, 0) for key in summary_ids), 3),
        "text_input_token_estimate": request_tokens,
        "request_images": sum(role_images.values()),
        "by_role": {role: {"calls": count, "text_input_token_estimate": role_tokens[role],
                           "request_images": role_images[role]} for role, count in sorted(role_calls.items())},
        "longest_model_response_s": max(waits.values(), default=0),
        "actual_usage_available": any((payload.get("usage") or {}).get("input_tokens") is not None for payload in responses.values()),
        "persisted_observation_sources": dict(providers),
        "merged_input_decisions": len(merged), "merged_input_results": dict(input_statuses),
        "unsuccessful_actions": sum(step.get("executor_decision") == "act" and not step.get("success") for step in steps),
        "validation_retries": sum(max(0, (step.get("action_pipeline") or {}).get("validation_attempts", 1)-1) for step in steps),
        "reused_text_references": sum(str(payload.get("messages", [])).count("Observation text repeats ") for payload in requests.values()),
    }


def analyze(root):
    episodes = [analyze_episode(path) for path in sorted((root / "episodes").glob("*/*/result.json"))]
    return {"protocol": read(root / "protocol.json"), "episodes": episodes,
            "caveats": ["Text estimates exclude image billing and cache discounts; null usage is unavailable.",
                        "Counts describe preserved episodes, including failures. No automatic retries are discarded.",
                        "Duplicate action parameters alone do not establish wasted work; inspect the associated observations."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(analyze(args.root), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
