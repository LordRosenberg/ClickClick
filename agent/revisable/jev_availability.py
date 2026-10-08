"""Process-local provider cooldown; failures never become cached validation."""
from collections import OrderedDict
import time

from agent.revisable.citation_check import digest

SERVICE_FAILURES = {
    'timeout', 'auth', 'rate_limit', 'overloaded', 'malformed', 'transport',
    'sdk_unavailable', 'model_mismatch', 'checker_internal_error', 'circuit_open', 'not_configured',
}


class JevAvailability:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.until = 0.0
        self.generation = 0
        self.probing = False

    def acquire(self):
        if self.until:
            if self.clock() < self.until or self.probing:
                return None
            self.probing = True  # One verifier probes; other tasks bypass.
        return self.generation

    def finish(self, lease, *, failed, cooldown_s):
        if lease is None:
            return
        if failed is True:
            self.generation += 1
            self.until = self.clock() + cooldown_s
            self.probing = False
        elif lease == self.generation:
            self.probing = False
            if failed is False:
                self.until = 0.0
        # An older successful concurrent call cannot undo a newer failure.


_PROVIDERS = OrderedDict()


def availability_for(provider):
    key = digest({'endpoint': provider.config.base_url, 'model': provider.config.model,
                  'credential': provider.config.api_key,
                  'allow_external': provider.config.allow_external,
                  'transport': 'live' if provider.transport is None else id(provider.transport)})
    entry = _PROVIDERS.get(key)
    if entry is None:
        value = JevAvailability()
        # Retain intercepted transport identity until eviction, preventing id reuse.
        _PROVIDERS[key] = (value, provider.transport)
        if len(_PROVIDERS) > 64:
            _PROVIDERS.popitem(last=False)
    else:
        value = entry[0]
        _PROVIDERS.move_to_end(key)
    return value
