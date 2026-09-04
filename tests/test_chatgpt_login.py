"""ChatGPT login CLI unit tests (no live OAuth)."""

from __future__ import annotations

import types

from shared.chatgpt_login import run_login
from shared.config import Settings


def test_run_login_applies_token_dir_and_succeeds(monkeypatch, tmp_path, capsys):
    token_dir = tmp_path / "tokens"
    auth_file = token_dir / "auth.json"

    class _FakeAuth:
        def __init__(self) -> None:
            self.auth_file = str(auth_file)

        def get_access_token(self) -> str:
            return "access-token-xyz"

        def get_account_id(self) -> str:
            return "acct-1"

    fake_mod = types.ModuleType("litellm.llms.chatgpt.authenticator")
    fake_mod.Authenticator = _FakeAuth  # type: ignore[attr-defined]

    # Ensure nested import path resolves.
    import sys

    monkeypatch.setitem(sys.modules, "litellm.llms.chatgpt.authenticator", fake_mod)
    # Also stub parent packages if missing.
    for name in ("litellm", "litellm.llms", "litellm.llms.chatgpt"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    settings = Settings(chatgpt_token_dir=str(token_dir))
    code = run_login(settings=settings)
    assert code == 0
    import os

    assert os.environ["CHATGPT_TOKEN_DIR"] == str(token_dir)
    out = capsys.readouterr().out
    assert "ChatGPT login OK" in out
    assert str(auth_file) in out


def test_run_login_cli_token_dir_override(monkeypatch, tmp_path):
    override = tmp_path / "override"

    class _FakeAuth:
        def __init__(self) -> None:
            self.auth_file = str(override / "auth.json")

        def get_access_token(self) -> str:
            return "tok"

        def get_account_id(self) -> None:
            return None

    fake_mod = types.ModuleType("litellm.llms.chatgpt.authenticator")
    fake_mod.Authenticator = _FakeAuth  # type: ignore[attr-defined]
    import sys

    monkeypatch.setitem(sys.modules, "litellm.llms.chatgpt.authenticator", fake_mod)
    for name in ("litellm", "litellm.llms", "litellm.llms.chatgpt"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    code = run_login(settings=Settings(chatgpt_token_dir=""), token_dir=str(override))
    assert code == 0
    import os

    assert os.environ["CHATGPT_TOKEN_DIR"] == str(override)


def test_run_login_failure_returns_nonzero(monkeypatch, capsys):
    class _BoomAuth:
        auth_file = "/tmp/unused.json"

        def get_access_token(self) -> str:
            raise RuntimeError("device code failed")

    fake_mod = types.ModuleType("litellm.llms.chatgpt.authenticator")
    fake_mod.Authenticator = _BoomAuth  # type: ignore[attr-defined]
    import sys

    monkeypatch.setitem(sys.modules, "litellm.llms.chatgpt.authenticator", fake_mod)
    for name in ("litellm", "litellm.llms", "litellm.llms.chatgpt"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))

    code = run_login(settings=Settings())
    assert code == 1
    err = capsys.readouterr().err
    assert "ChatGPT login failed" in err
