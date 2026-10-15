"""Evaluator continuity requires owned isolation and reaches the real entry."""
from types import SimpleNamespace

import pytest

from evaluation.skill_learning import AndroidWorldExperiment, Experiment
from test_task_skill_learning import context


@pytest.mark.parametrize("environment,disposable,no_reset", [
    ("mobileworld", True, False), ("androidworld", False, False),
    ("androidworld", True, True),
])
def test_invalid_continuity_fails_before_output_or_runtime_access(tmp_path, environment, disposable, no_reset):
    output = tmp_path / "output"
    args = SimpleNamespace(continuous_research=True, environment=environment,
        disposable_research=disposable, no_research_reset=no_reset, output=str(output))
    with pytest.raises(ValueError, match="owned AndroidWorld disposable fixture"):
        Experiment(args)
    assert not output.exists()


def test_non_boolean_continuity_fails_before_runtime_access():
    with pytest.raises(ValueError, match="must be boolean"):
        Experiment(SimpleNamespace(continuous_research="true"))


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous", [False, True])
@pytest.mark.parametrize("score,budgeted,needs_recovery", [
    (0, False, True), (1, False, True), (1, True, False),
    (1, None, False), (0, None, True), (None, False, False),
])
async def test_evaluator_wires_continuity_and_reset_capability_into_learning(
        context, tmp_path, monkeypatch, continuous, score, budgeted, needs_recovery):
    settings, _, task, store, library = context
    experiment = AndroidWorldExperiment.__new__(AndroidWorldExperiment)
    experiment.root = tmp_path / "experiment"
    experiment.root.mkdir()
    experiment.settings, experiment.model, experiment.baseline = settings, "test", library.root
    experiment.args = SimpleNamespace(target="fixture", disposable_research=True,
        continuous_research=continuous, force_learning=False,
        learn_calls=12, learn_actions=5, learn_seconds=300, environment="androidworld")
    reset = object()
    experiment.disposable_adapter = lambda baseline, **kwargs: reset
    calls = []
    class Backend:
        def __init__(self, source, **kwargs):
            self.retain_probe_state = kwargs["retain_probe_state"]
            self.cancelled = lambda: False
            self.source_store, self.library, self.environment = store, library, None
            calls.append(self)
        async def diagnose(self, *args, **kwargs):
            raise AssertionError("the entry must not add diagnosis requests")
        async def explore(self, *args, **kwargs):
            raise AssertionError("the entry must not dispatch an extra probe")
        async def release_device(self):
            pass
        def close(self):
            pass
    async def close_provider():
        pass
    async def learn(source, **kwargs):
        backend = kwargs["backend"]
        assert backend.retain_probe_state is continuous and backend.fixture_reset_adapter is reset
        assert kwargs["task_success_goal"] is needs_recovery
        assert kwargs["outcome_evidence"]["success"] is (bool(score) if score is not None else None)
        assert kwargs["outcome_evidence"]["result"] == score
        assert kwargs["budget"].max_calls == 12 and kwargs["budget"].max_actions == 5
        return {"ok": True, "skipped": True}
    monkeypatch.setattr("evaluation.skill_learning.ExplorationBackend", Backend)
    monkeypatch.setattr("evaluation.skill_learning.get_driver", lambda *a, **kw: SimpleNamespace(close_observation_provider=close_provider))
    monkeypatch.setattr("evaluation.skill_learning.run_task_learning", learn)
    result = await experiment.learn({"clickclick_task_id":task.id, "score":score,
        "runtime_directory":str(settings.data_dir), "budgeted_success":budgeted})
    assert result["skipped"] and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", [False, True])
async def test_changed_plan_requires_verified_cleanup_before_configuration(monkeypatch, restored):
    from agent.skills.environment import ResearchEnvironment
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LearningBudget, LearningStopped
    from test_skill_incremental_environment import Store, plan
    from test_skill_acquisition_repairs import adapter

    backend = ExplorationBackend.__new__(ExplorationBackend)
    backend.record, backend.retain_probe_state = object(), True
    backend.fixture_reset_adapter = adapter({"teardown_success": restored})
    backend.environment = ResearchEnvironment(Store(), reset_adapter=backend.fixture_reset_adapter)
    old = plan()
    backend.environment.configure(old)
    intent = backend.environment.intent({"type": "tap"}, "observed-screen")
    backend.environment.effect(intent, {"success": True})
    backend.environment_transition = None
    released, configured = [], []
    async def release():
        released.append(True)
    backend.release_device = release
    def configure(new):
        assert backend.environment.receipt()["verified_clean"]
        configured.append(new)
        raise RuntimeError("stop after verified boundary")
    monkeypatch.setattr(backend.environment, "configure", configure)
    new = {**old, "scope": "different owned research scope"}
    budget = LearningBudget(max_actions=5)
    error = RuntimeError if restored else LearningStopped
    message = "stop after verified boundary" if restored else "research_plan_change_cleanup_unverified"
    with pytest.raises(error, match=message):
        await backend.explore("next hypothesis", 1, budget,
            context={"experiment": {"environment_plan": new}})
    assert released == [True] and budget.actions == 1
    assert configured == ([new] if restored else [])
    assert backend.environment.plan.model_dump() == old
    assert backend.environment.receipt()["verified_clean"] is restored


@pytest.mark.asyncio
async def test_plan_change_reuses_its_just_verified_reset_without_second_initialization(monkeypatch):
    from agent.skills.environment import ResearchEnvironment
    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import LearningBudget
    from test_skill_incremental_environment import Store, plan
    from test_skill_acquisition_repairs import adapter

    backend = ExplorationBackend.__new__(ExplorationBackend)
    backend.record, backend.retain_probe_state = object(), True
    backend.fixture_reset_adapter = adapter()
    backend.store = Store()
    backend.environment = ResearchEnvironment(backend.store, reset_adapter=backend.fixture_reset_adapter)
    backend.environment.configure(plan())
    intent = backend.environment.intent({'type': 'tap'}, 'original-screen')
    backend.environment.effect(intent, {'success': True})
    backend.environment_transition, backend.diagnostic_reads = None, []
    async def release():
        pass
    async def capture():
        assert backend.environment.receipt()['verified_clean']
    async def forbidden(*args):
        pytest.fail('A second fixture initialization is redundant')
    def stop(*args, **kwargs):
        raise RuntimeError('stop before GUI dispatch')
    backend.release_device = release
    backend.prepare_disposable_fixture = forbidden
    monkeypatch.setattr(backend.environment, 'capture_checkpoint', capture)
    monkeypatch.setattr('agent.skills.exploration.live_experiment_context', stop)
    new = {**plan(), 'scope': 'different owned probe scope'}
    budget = LearningBudget(max_actions=5)
    with pytest.raises(RuntimeError, match='stop before GUI dispatch'):
        await backend.explore('New hypothesis', 1, budget, context={'experiment': {'environment_plan': new}})
    assert budget.actions == 1 and backend.environment.plan.model_dump() == new
    assert backend.environment.receipt()['verified_clean']
    reused = [r for r in backend.store.records('measurement') if r['key'] == 'fixture_preparation_reused']
    assert len(reused) == 1 and reused[0]['payload']['restoration_receipt']['verified_clean']
