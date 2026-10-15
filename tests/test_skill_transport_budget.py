"""Native transient retries stay inside the same call ledger and never act."""
import sys
import types

import pytest

from agent.skills.learning import LearningBudget
from shared.llm_gateway import GatewayTransientError
from test_llm_gateway import _FakeExc, _FakeResponse, _settings


@pytest.mark.asyncio
@pytest.mark.parametrize('cap,expected_attempts', [(2, 2), (1, 1)])
async def test_native_overload_retry_charges_attempts_without_renewing(cap, expected_attempts, monkeypatch):
    calls=[]
    async def acompletion(**kwargs):
        calls.append(kwargs)
        if len(calls)==1:raise _FakeExc('overloaded',status_code=503)
        return _FakeResponse('{"decision":"skip"}')
    fake=types.ModuleType('litellm');fake.acompletion=acompletion
    monkeypatch.setitem(sys.modules,'litellm',fake)
    monkeypatch.setattr('shared.llm_gateway.GATEWAY_RETRY_BASE_DELAY_S',0)
    budget=LearningBudget(max_calls=cap,reserve_calls=0)
    if cap==1:
        with pytest.raises(GatewayTransientError):
            await budget.ask('kimi-k3','Read-only native role',{'source':'sealed'},_settings())
    else:
        result=await budget.ask('kimi-k3','Read-only native role',{'source':'sealed'},_settings())
        assert result['decision']=='skip'
        assert calls[0]['messages']==calls[1]['messages']
    assert budget.calls==len(calls)==expected_attempts
    assert budget.remaining_calls()==0 and budget.actions==0
    # The failed attempt never fabricates returned token/cache telemetry.
    assert budget.snapshot()['provider_usage']['unknown_responses']==1


@pytest.mark.asyncio
async def test_native_retry_preserves_final_call_reserve(monkeypatch):
    calls=[]
    async def acompletion(**kwargs):
        calls.append(kwargs);raise _FakeExc('overloaded',status_code=503)
    fake=types.ModuleType('litellm');fake.acompletion=acompletion
    monkeypatch.setitem(sys.modules,'litellm',fake)
    budget=LearningBudget(max_calls=2,reserve_calls=1)
    with pytest.raises(GatewayTransientError):
        await budget.ask('kimi-k3','Read-only native role',{},_settings())
    assert budget.calls==len(calls)==1 and budget.remaining_calls()==1
    assert budget.actions==0
