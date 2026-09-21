"""Select an architecture at the process boundary."""

from shared.model_router import ModelRouter


def create_orchestrator(*args, settings, **kwargs):
    from agent.revisable.orchestrator import PlanOrchestrator
    from agent.revisable.roles import PlanDecisionRole, PlanExecutor

    router = ModelRouter.from_settings(settings)
    driver, artifacts = kwargs.get("driver"), kwargs.get("artifacts")
    kwargs.update(
        planner_factory=lambda: PlanDecisionRole(
            "planner", driver, artifacts, model=router.planner, settings=settings
        ),
        reviewer_factory=lambda: PlanDecisionRole(
            "reviewer", driver, artifacts, model=router.reviewer, settings=settings
        ),
        executor_factory=lambda: PlanExecutor(
            driver, artifacts, model=router.executor, settings=settings
        ),
    )
    return PlanOrchestrator(*args, settings=settings, **kwargs)
