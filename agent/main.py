"""Agent process entrypoint for the Reviewer→Planner→Executor loop."""

from __future__ import annotations

import argparse
import asyncio

from agent.executor import Executor
from agent.orchestrator import Orchestrator
from agent.planner import Planner
from agent.reviewer import Reviewer
from agent.traces import TraceWriter
from driver.factory import get_driver
from shared.artifacts import ArtifactStore
from shared.config import get_settings
from shared.db import Database
from shared.model_router import ModelRouter


async def run_once(instruction: str) -> str:
    """Run a single instruction through the closed loop; return task id."""
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    traces = TraceWriter(db, artifacts)
    driver = get_driver(settings)
    router = ModelRouter.from_settings(settings)

    def planner_factory() -> Planner:
        return Planner(driver, artifacts, model=router.planner, settings=settings)

    def reviewer_factory() -> Reviewer:
        return Reviewer(driver, artifacts, model=router.reviewer, settings=settings)

    def executor_factory() -> Executor:
        return Executor(driver, artifacts, model=router.executor, settings=settings)

    orch = Orchestrator(
        db,
        traces,
        planner_factory=planner_factory,
        reviewer_factory=reviewer_factory,
        executor_factory=executor_factory,
        driver=driver,
        artifacts=artifacts,
    )
    task_id = await orch.start_task(instruction)
    print(task_id)
    return task_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ClickClick Agent (Reviewer→Planner→Executor loop)",
    )
    parser.add_argument("instruction", nargs="?", help="Natural language task")
    args = parser.parse_args()
    if not args.instruction:
        parser.error("instruction required")
    asyncio.run(run_once(args.instruction))

if __name__ == "__main__":
    main()
