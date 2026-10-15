from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from evaluation.mobileworld import user_agent_gateway as gateway


def test_gateway_auth_model_and_hidden_user_prompt_isolation(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_USER_AGENT_GATEWAY_TOKEN", "fixture-secret")
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(jev_mode="off"))
    complete = AsyncMock(return_value=SimpleNamespace(content="A real simulated answer", model=gateway.MODEL))
    monkeypatch.setattr(gateway, "complete", complete)
    client = TestClient(gateway.app)
    body = {"model": gateway.MODEL,"messages": [{"role":"system","content":"synthetic hidden context"},
           {"role":"user","content":"Question?"}],"seed":42,"temperature":0}
    assert client.post("/v1/chat/completions",json=body).status_code == 401
    headers = {"Authorization":"Bearer fixture-secret"}
    assert client.post("/v1/chat/completions",json={**body,"model":"other"},headers=headers).status_code == 400
    result = client.post("/v1/chat/completions",json=body,headers=headers)
    assert result.status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == "A real simulated answer"
    assert complete.call_count == 1
    assert complete.call_args.args[1] == body["messages"]


def test_gateway_error_does_not_echo_secret_or_user_system_prompt(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_USER_AGENT_GATEWAY_TOKEN", "fixture-secret")
    monkeypatch.setattr(gateway, "get_settings", lambda: SimpleNamespace(jev_mode="off"))
    monkeypatch.setattr(gateway, "complete", AsyncMock(side_effect=RuntimeError("private provider credential")))
    result = TestClient(gateway.app).post("/v1/chat/completions",
        headers={"Authorization":"Bearer fixture-secret"},
        json={"model":gateway.MODEL,"messages":[{"role":"system","content":"hidden task answer"}]})
    assert result.status_code == 502
    assert "private" not in result.text and "hidden task answer" not in result.text
