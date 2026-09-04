"""Run the frozen GPT-5.4 unavailable-temporal Reviewer gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from agent.prompt_measurement import measure_text_component
from agent.prompts import render_reviewer_system
from agent.session import AgentSession
from agent.tool_registry import AgentToolResult, ToolStatus, stable_hash
from shared.config import get_settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    ROOT / "evaluation/fixtures/deferred_temporal_unavailable_reviewer_packet.json"
)
DEFAULT_FREEZE = ROOT / "evaluation/deferred_temporal_static_blocker_gate_freeze.json"
DEFAULT_OUTPUT = (
    ROOT / "evaluation/results/deferred-temporal-unavailable-gpt54.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _usage(rounds: list[Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
    for row in rounds:
        usage = row.usage
        totals["input_tokens"] += int(usage.input_tokens or 0)
        totals["cached_tokens"] += int(usage.cached_read_tokens or 0)
        totals["output_tokens"] += int(usage.output_tokens or 0)
    return totals


async def run(fixture_path: Path, freeze_path: Path, output_path: Path) -> int:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    expected_fixture_hash = freeze["injected_model_case"]["fixture_sha256"]
    policy = render_reviewer_system()
    packet = fixture["review_packet"]
    expected = fixture["expected"]
    observe_result = fixture["observe_screen_result"]
    evidence_handles = list(packet["evidence_handles"])

    settings = get_settings()
    provider = settings.provider_for(fixture["model"])
    configured_effort = (provider.get("reasoning") or {}).get("effort")
    if configured_effort != fixture["reasoning_effort"]:
        raise RuntimeError(f"reasoning effort drift: {configured_effort!r}")

    async def unavailable_observe(
        args: dict[str, Any], _context: Any,
    ) -> AgentToolResult:
        if args.get("mode") != expected["observe_mode"]:
            return AgentToolResult(
                status=ToolStatus.INVALID_ARGUMENTS,
                summary="fixture requires temporal observation",
            )
        return AgentToolResult(
            status=ToolStatus(observe_result["status"]),
            summary=observe_result["summary"],
            data=dict(observe_result["data"]),
            evidence_refs=[observe_result["data"]["evidence_ref"]],
            error="temporal_role_evidence_unavailable",
        )

    session = AgentSession("reviewer", fixture["model"], settings=settings)
    session.reset_lifecycle("gate:deferred-temporal-unavailable")
    session.freeze_allow_dirs(["generic"])
    session.set_stable_system(policy)
    result = await session.run(
        [{
            "role": "user",
            "content": "REVIEW PACKET:\n" + json.dumps(
                packet, ensure_ascii=False, separators=(",", ":"),
            ),
        }],
        include_skill_context=False,
        handlers={"observe_screen": unavailable_observe},
        context_state={
            "reviewer_packet_digest": stable_hash({
                "packet": packet,
                "observation_id": packet["current"]["observation_id"],
            }),
            "reviewer_evidence_handles": evidence_handles,
            "reviewer_criterion_sources": {
                item["criterion_ref"]: item["source"]
                for item in packet["task_contract"]["criteria"]
            },
            "reviewer_dispatched_action_handles": [],
            "reviewer_progress_bindings": [],
            "reviewer_temporal_evidence_handles": [],
        },
    )

    calls = [item.model_dump(mode="json") for item in result.tool_calls]
    submit_calls = [item for item in calls if item["name"] == "submit_reviewer_decision"]
    first_call = calls[0] if calls else {}
    first_submit = submit_calls[0] if submit_calls else {}
    first_submit_args = first_submit.get("arguments") or {}
    if isinstance(first_submit_args, str):
        first_submit_args = json.loads(first_submit_args)
    allowed_handles = set(expected["allowed_submit_evidence_handles"])
    submitted_handles = set(first_submit_args.get("evidence_handles") or [])
    checks = {
        "fixture_hash_matches_freeze": _sha256(fixture_path) == expected_fixture_hash,
        "first_tool_is_temporal_observe": (
            first_call.get("name") == expected["first_tool"]
            and (first_call.get("arguments") or {}).get("mode")
            == expected["observe_mode"]
        ),
        "first_submit_is_nonterminal": (
            first_submit_args.get("verdict")
            in expected["first_submit_verdict_after_observe"]
        ),
        "first_submit_uses_only_delivered_handles": submitted_handles <= allowed_handles,
        "forbidden_raw_ref_not_cited": not submitted_handles.intersection(
            expected["forbidden_submit_evidence_handles"]
        ),
        "invalid_submits_zero": not any(
            item["name"] == "submit_reviewer_decision"
            and item["status"] != ToolStatus.SUCCEEDED.value
            for item in calls
        ),
        "device_actions_zero": True,
    }
    report = {
        "fixture": str(fixture_path.relative_to(ROOT)),
        "fixture_sha256": _sha256(fixture_path),
        "policy": measure_text_component("reviewer.role_policy", policy).summary_dict(),
        "checks": checks,
        "passed": all(checks.values()),
        "decision": result.decision.model_dump(mode="json"),
        "tool_calls": calls,
        "usage": _usage(result.llm_rounds),
        "llm_rounds": len(result.llm_rounds),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.fixture, args.freeze, args.output)))


if __name__ == "__main__":
    main()
