"""Forecast cleanup without reserving an unstarted research question."""
from types import SimpleNamespace

import pytest

from evaluation.androidworld.long_run_health import preventive_restart_check
from evaluation.skill_learning import AndroidWorldExperiment


def experiment(stage, seconds=2400):
    value = AndroidWorldExperiment.__new__(AndroidWorldExperiment)
    value.args = SimpleNamespace(learn_seconds=seconds)
    value.learning_backend = SimpleNamespace(environment=SimpleNamespace(stage=stage))
    return value


def health(horizon, uptime):
    return preventive_restart_check(lambda *args: f"{uptime} 0".encode(),
        {"task": "fixture", "max_seconds": horizon})


def test_cleanup_does_not_forecast_next_probe_but_next_prepare_still_does():
    value = experiment("cleanup")
    # Use the recorded uptime as a counterfactual cleanup-only boundary.
    # A separately dispatched 2400-second probe still needs the old restart.
    assert health(value.research_preparation_horizon(2400), 1082.43)["restart_due"] is False
    value.learning_backend.environment.stage = "prepare"
    assert value.research_preparation_horizon(2400) == 2400
    assert health(value.research_preparation_horizon(2400), 1082.43)["restart_due"] is True


@pytest.mark.parametrize("remaining", [45, 120, 2400])
def test_cleanup_forecast_uses_existing_initializer_deadline(remaining):
    value = experiment("cleanup", remaining)
    horizon = value.research_preparation_horizon(2400)
    assert horizon == value.source_fixture_initialization_seconds() == min(120, remaining)
    assert health(horizon, 3590)["restart_due"] is True


@pytest.mark.parametrize("stage", ["prepare", "probe"])
def test_research_forecast_and_unknown_environment_preserve_caller_allowance(stage):
    value = experiment(stage)
    assert value.research_preparation_horizon(1800) == 1800
    value.learning_backend.environment = None
    assert value.research_preparation_horizon(1800) == 1800
    del value.learning_backend
    assert value.research_preparation_horizon(1800) == 1800
