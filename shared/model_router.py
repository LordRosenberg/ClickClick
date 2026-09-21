"""Model routing for Planner, Reviewer and Executor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from shared.config import Settings, get_settings


class ModelRole(str, Enum):
    PLANNER = "planner"
    REVIEWER = "reviewer"
    EXECUTOR = "executor"


@dataclass(frozen=True)
class ModelRouter:
    """Resolve model identifiers for the plan-driven roles.

    Planner and Reviewer intentionally share the existing decision-model
    configuration. Each resolved id is passed verbatim to ``litellm.completion`` via the
    gateway, so it must carry a litellm routing prefix and match a key in
    `CLICKCLICK_MODELS_JSON`.
    """

    decision: str
    executor: str

    @property
    def planner(self) -> str:
        return self.decision

    @property
    def reviewer(self) -> str:
        return self.decision


    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ModelRouter:
        """Build a router from settings.

        The existing ``manager_model`` setting supplies both focused decision
        roles; ``executor_model`` remains independent. Empty overrides fall
        back to ``default_model``.
        """
        s = settings or get_settings()
        m = s.default_model
        return cls(
            decision=s.manager_model or m,
            executor=s.executor_model or m,
        )

    def for_role(self, role: ModelRole) -> str:
        """Return the model id/alias for a role."""
        if role is ModelRole.EXECUTOR:
            return self.executor
        return self.decision
