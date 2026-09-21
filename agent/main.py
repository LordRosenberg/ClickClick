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


async def run_once(instruction: str) -> str:
    """Run a single instruction through the closed loop; return task id."""
    settings = get_settings()
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
    return task_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ClickClick Agent (plan-driven execution loop)",
    )
    parser.add_argument("instruction", nargs="?", help="Natural language task")
    args = parser.parse_args()
    if not args.instruction:
        parser.error("instruction required")
    asyncio.run(run_once(args.instruction))

if __name__ == "__main__":
    main()
