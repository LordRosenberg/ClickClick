from dataclasses import replace

from agent.revisable.jev_availability import JevAvailability, availability_for
from shared.jev_gateway import JevConfig, JevProvider
from shared.config import Settings


def test_defaults_and_single_probe_recovery():
    settings=Settings(_env_file=None)
    assert settings.jev_failure_policy=='bypass' and settings.jev_failure_cooldown_s==60
    now=[10.]
    circuit=JevAvailability(clock=lambda:now[0])
    first=circuit.acquire();concurrent=circuit.acquire()
    circuit.finish(first,failed=True,cooldown_s=60)
    circuit.finish(concurrent,failed=False,cooldown_s=60)
    assert circuit.acquire() is None  # An older success cannot undo the failure.
    now[0]=69.9
    assert circuit.acquire() is None
    now[0]=70.
    probe=circuit.acquire()
    assert probe is not None and circuit.acquire() is None
    circuit.finish(probe,failed=False,cooldown_s=60)
    assert circuit.acquire() is not None


def test_cancelled_probe_releases_lease_and_failed_probe_reopens():
    now=[1.]
    circuit=JevAvailability(clock=lambda:now[0])
    circuit.finish(circuit.acquire(),failed=True,cooldown_s=60)
    now[0]=61.
    probe=circuit.acquire()
    circuit.finish(probe,failed=None,cooldown_s=60)
    retry=circuit.acquire()
    assert retry is not None
    circuit.finish(retry,failed=True,cooldown_s=60)
    assert circuit.acquire() is None
    now[0]=121.
    assert circuit.acquire() is not None


def test_shared_across_tasks_but_isolated_by_credentials_endpoint_and_transport():
    config=JevConfig(api_key='availability-test-key',allow_external=True)
    first=availability_for(JevProvider(config))
    assert availability_for(JevProvider(config)) is first
    assert availability_for(JevProvider(replace(config,api_key='another-key'))) is not first
    assert availability_for(JevProvider(replace(config,base_url='https://example.org'))) is not first
    transport=object()
    intercepted=availability_for(JevProvider(config,transport=transport))
    assert availability_for(JevProvider(config,transport=transport)) is intercepted
    assert intercepted is not first
    assert availability_for(JevProvider(config,transport=object())) is not intercepted
