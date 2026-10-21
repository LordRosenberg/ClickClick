"""Agent process entrypoint for the plan-driven execution loop."""

from __future__ import annotations

import argparse
import asyncio

from agent.runtime import create_orchestrator
from agent.traces import TraceWriter
from driver.factory import get_driver
from shared.artifacts import ArtifactStore
from shared.config import get_settings
from shared.db import Database


async def run_once(instruction: str, *, hitl: bool | None = None, answer_surface="terminal") -> str:
    """Run a single instruction through the closed loop; return task id."""
    from desktop.configuration import console_values, apply_saved_settings
    settings = apply_saved_settings(get_settings())
    enabled = hitl if hitl is not None else console_values(settings.data_dir).get("hitl_enabled", settings.hitl_enabled)
    if enabled:
        from agent.hitl_cli import run_shared
        return await run_shared(instruction, apply_saved_settings(settings), answer_surface=answer_surface)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    traces = TraceWriter(db, artifacts)
    driver = get_driver(settings)

    orch = create_orchestrator(
        db,
        traces,
        driver=driver,
        artifacts=artifacts,
        settings=settings,
    )
    task_id = await orch.start_task(instruction)
    print(task_id)
    task = db.get_task(task_id)
    if task and task.state and task.state.revisable.stop_reason:
        print(f"{task.state.revisable.stop_reason}: {task.state.revisable.budget_kind}; checkpoint retained")
    return task_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ClickClick Agent (plan-driven execution loop)",
    )
    parser.add_argument("instruction", nargs="?", help="Natural language task")
    parser.add_argument("--hitl", action="store_true", default=None, help="Enable human questions on the shared Console backend")
    parser.add_argument("--answers", choices=("terminal", "console"), default="terminal", help="Where to answer HITL questions")
    args = parser.parse_args()
    if not args.instruction:
        parser.error("instruction required")
    asyncio.run(run_once(args.instruction, hitl=args.hitl, answer_surface=args.answers))

if __name__ == "__main__":
    main()
