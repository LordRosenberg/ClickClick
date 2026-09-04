"""ChatGPT Pro OAuth helpers for CLI and Control API (device-code flow)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from shared.config import Settings

PollStatus = Literal["pending", "authenticated", "error"]


@dataclass
class PendingDeviceLogin:
    device_auth_id: str
    user_code: str
    interval: int
    started_at: float


def _authenticator():
    from litellm.llms.chatgpt.authenticator import Authenticator

    return Authenticator()


def _verify_url() -> str:
    from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

    return CHATGPT_DEVICE_VERIFY_URL


def prepare_chatgpt_env(settings: Settings) -> str | None:
    """Apply token-dir setting; return resolved dir or None."""
    return settings.apply_chatgpt_token_dir()


def chatgpt_status(settings: Settings) -> dict[str, Any]:
    """Return auth status without exposing access/refresh tokens.

    Intentionally avoids importing LiteLLM: the Authenticator package pull is
    multi-second and must not block Control API request handlers on Console load.
    """
    prepare_chatgpt_env(settings)
    token_dir = os.environ.get(
        "CHATGPT_TOKEN_DIR",
        os.path.expanduser("~/.config/litellm/chatgpt"),
    )
    auth_file = os.path.join(token_dir, os.environ.get("CHATGPT_AUTH_FILE", "auth.json"))
    data: dict[str, Any] = {}
    try:
        with open(auth_file, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if isinstance(raw, dict):
                data = raw
    except (OSError, json.JSONDecodeError):
        data = {}

    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_at = data.get("expires_at")
    authenticated = False
    if isinstance(access, str) and access:
        if expires_at is None:
            authenticated = True
        else:
            try:
                authenticated = time.time() < float(expires_at) - 60
            except (TypeError, ValueError):
                authenticated = True
        if not authenticated and isinstance(refresh, str) and refresh:
            # Refreshable credential still counts as signed-in for the Console.
            authenticated = True
    elif isinstance(refresh, str) and refresh:
        authenticated = True

    account_id = data.get("account_id")
    if not account_id:
        account_id = _account_id_from_jwt(data.get("id_token") or access)

    return {
        "authenticated": bool(authenticated),
        "account_id": account_id,
        "expires_at": expires_at,
        "auth_file": auth_file,
        "token_dir": token_dir,
    }


def _account_id_from_jwt(token: Any) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    try:
        import base64

        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            account_id = auth_claims.get("chatgpt_account_id")
            if isinstance(account_id, str) and account_id:
                return account_id
    except Exception:  # noqa: BLE001
        return None
    return None


def start_device_login(settings: Settings) -> tuple[dict[str, Any], PendingDeviceLogin]:
    """Request a device code; return public payload + pending state."""
    prepare_chatgpt_env(settings)
    auth = _authenticator()
    device = auth._request_device_code()
    auth._record_device_code_request()
    pending = PendingDeviceLogin(
        device_auth_id=str(device["device_auth_id"]),
        user_code=str(device["user_code"]),
        interval=int(device.get("interval") or 5),
        started_at=time.time(),
    )
    payload = {
        "user_code": pending.user_code,
        "verify_url": _verify_url(),
        "interval_s": pending.interval,
        "message": (
            "Visit the verify URL, sign in with ChatGPT Pro, and enter the user code. "
            "Never share this code."
        ),
    }
    return payload, pending


def poll_device_login(
    settings: Settings,
    pending: PendingDeviceLogin,
    *,
    timeout_s: float = 15 * 60,
) -> tuple[PollStatus, dict[str, Any], PendingDeviceLogin | None]:
    """One poll step against the device-token endpoint.

    Returns (status, body, next_pending). ``next_pending`` is None when the
    flow finished (authenticated or terminal error).
    """
    prepare_chatgpt_env(settings)
    if time.time() - pending.started_at > timeout_s:
        return "error", {"status": "error", "error": "device authorization timed out"}, None

    from litellm.llms.chatgpt.common_utils import (
        CHATGPT_DEVICE_TOKEN_URL,
        GetAccessTokenError,
    )

    auth = _authenticator()
    # Use plain httpx: LiteLLM's shared client raise_for_status()s on 403/404,
    # but those codes mean "still waiting for browser authorization".
    pending_body = {
        "status": "pending",
        "user_code": pending.user_code,
        "verify_url": _verify_url(),
        "interval_s": pending.interval,
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                CHATGPT_DEVICE_TOKEN_URL,
                json={
                    "device_auth_id": pending.device_auth_id,
                    "user_code": pending.user_code,
                },
            )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else None
        if code in (403, 404):
            return "pending", pending_body, pending
        return "error", {"status": "error", "error": f"poll failed: {exc}"}, None
    except Exception as exc:  # noqa: BLE001
        return "error", {"status": "error", "error": f"poll failed: {exc}"}, None

    if resp.status_code in (403, 404):
        return "pending", pending_body, pending

    if resp.status_code != 200:
        return (
            "error",
            {"status": "error", "error": f"poll failed: HTTP {resp.status_code}"},
            None,
        )

    data = resp.json()
    needed = ("authorization_code", "code_challenge", "code_verifier")
    if not all(k in data for k in needed):
        return (
            "pending",
            {
                "status": "pending",
                "user_code": pending.user_code,
                "verify_url": _verify_url(),
                "interval_s": pending.interval,
            },
            pending,
        )

    try:
        tokens = auth._exchange_code_for_tokens(data)
        auth_data = auth._build_auth_record(tokens)
        auth._write_auth_file(auth_data)
    except GetAccessTokenError as exc:
        return "error", {"status": "error", "error": str(exc)}, None
    except Exception as exc:  # noqa: BLE001
        return "error", {"status": "error", "error": f"token exchange failed: {exc}"}, None

    status = chatgpt_status(settings)
    return (
        "authenticated",
        {"status": "authenticated", **{k: status[k] for k in ("account_id", "expires_at", "auth_file")}},
        None,
    )
