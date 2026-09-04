"""ChatGPT Pro/Max OAuth device-code login for LiteLLM ``chatgpt/*`` models.

Run before starting the Control API when using subscription-backed models::

    python -m shared.chatgpt_login

Optional: set ``CLICKCLICK_CHATGPT_TOKEN_DIR`` (or pass ``--token-dir``) so
tokens are stored under a project-owned path instead of LiteLLM's default
``~/.config/litellm/chatgpt``.

The Control API also exposes the same OAuth flow under ``/api/chatgpt/login/*``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from shared.config import Settings, get_settings


def run_login(*, settings: Settings | None = None, token_dir: str | None = None) -> int:
    """Run LiteLLM ChatGPT OAuth and persist tokens. Returns process exit code."""
    s = settings or get_settings()
    if token_dir is not None:
        s.chatgpt_token_dir = token_dir
    applied = s.apply_chatgpt_token_dir()
    if applied:
        print(f"Using ChatGPT token directory: {applied}", flush=True)
    else:
        print(
            "Using LiteLLM default ChatGPT token directory "
            "(~/.config/litellm/chatgpt unless CHATGPT_TOKEN_DIR is already set).",
            flush=True,
        )

    try:
        from litellm.llms.chatgpt.authenticator import Authenticator
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to import LiteLLM ChatGPT authenticator: {exc}", file=sys.stderr)
        return 1

    auth = Authenticator()
    try:
        token = auth.get_access_token()
    except Exception as exc:  # noqa: BLE001
        print(f"ChatGPT login failed: {exc}", file=sys.stderr)
        return 1

    if not token:
        print("ChatGPT login failed: empty access token", file=sys.stderr)
        return 1

    auth_path = Path(auth.auth_file)
    print(f"ChatGPT login OK. Auth file: {auth_path}", flush=True)
    account_id = auth.get_account_id()
    if account_id:
        print(f"Account id: {account_id}", flush=True)
    print(
        "You can now point CLICKCLICK_*_MODEL at chatgpt/* entries in "
        "CLICKCLICK_MODELS_JSON (alongside relay openai/* models), or pick "
        "them in the Console submit form.",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sign in to ChatGPT Pro/Max for LiteLLM chatgpt/* models.",
    )
    parser.add_argument(
        "--token-dir",
        default=None,
        help="Override CLICKCLICK_CHATGPT_TOKEN_DIR for this run.",
    )
    args = parser.parse_args(argv)
    return run_login(token_dir=args.token_dir)


if __name__ == "__main__":
    raise SystemExit(main())
