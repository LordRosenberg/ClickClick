#!/usr/bin/env python3
"""Print deterministic GPT-5.4/o200k prompt component measurements."""

from __future__ import annotations

import argparse
import json

from agent.prompt_measurement import build_role_component_report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Planner/Reviewer/Executor role-policy text and canonical tool-schema "
            "JSON with the frozen GPT-5.4/o200k tokenizer."
        )
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="Output format.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = build_role_component_report()
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f'model={report["model"]} tokenizer={report["tokenizer"]}')
    for role_name, role_components in report["roles"].items():
        for component_name, measurement in role_components.items():
            print(
                f"{role_name}.{component_name}: "
                f"tokens={measurement['token_count']} chars={measurement['char_count']} "
                f"bytes={measurement['byte_count']} sha256={measurement['digest_sha256']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
