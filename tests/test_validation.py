"""Validation coverage for focused-role orchestration contracts."""

from pathlib import Path

import pytest

from agent.orchestrator import Orchestrator
from agent.traces import TraceWriter
from driver.fixture import FixtureDriver
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import Action, ActionReceipt, ActionResult, ExecutorStepSubmit, EffectOutcome, ObservationMode, TaskStatus
from tests.fake_agents import FakeExecutor


def test_model_switching_keeps_shared_decision_model():
    from shared.config import Settings
    from shared.model_router import ModelRole, ModelRouter

    first = ModelRouter.from_settings(Settings(default_model="kimi-k3"))
    second = ModelRouter.from_settings(Settings(default_model="glm-4-plus"))

    assert first.for_role(ModelRole.PLANNER) == "kimi-k3"
    assert first.for_role(ModelRole.REVIEWER) == "kimi-k3"
    assert second.for_role(ModelRole.PLANNER) == "glm-4-plus"
    assert second.for_role(ModelRole.REVIEWER) == "glm-4-plus"
