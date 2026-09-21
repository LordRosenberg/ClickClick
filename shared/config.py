"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

JsonContainer = TypeVar("JsonContainer", dict, list)

# LiteLLM ChatGPT OAuth reads this env var for the token storage directory.
CHATGPT_TOKEN_DIR_ENV = "CHATGPT_TOKEN_DIR"

# .env JSON blobs commonly grow a trailing comma after the last property.
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _loads_json(raw: str) -> Any:
    """Parse JSON, retrying once after stripping trailing commas."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = _TRAILING_COMMA.sub(r"\1", raw)
        if repaired == raw:
            raise
        return json.loads(repaired)


def _parse_json_container(raw: str, expected: type[JsonContainer]) -> JsonContainer | None:
    """Return one JSON object/array of the expected shape, or ``None``."""
    if not raw:
        return None
    try:
        value: Any = _loads_json(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, expected) else None


class Settings(BaseSettings):
    """Operator-controlled deployment and integration settings.

    Runtime policies and algorithm thresholds intentionally do not live here:
    once a rule is established, its owning module provides the canonical
    default.  This keeps environment configuration focused on values an
    operator may legitimately need to change between deployments.
    """

    model_config = SettingsConfigDict(env_prefix="CLICKCLICK_", env_file=".env", extra="ignore")

    agent_architecture: Literal["plan_reviewer", "plan_executor"] = "plan_executor"
    executor_context_tokens: int = Field(default=16000, ge=1)

    # Storage and process bindings.
    data_dir: Path = Path("./data")
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    # Optional TLS for the console. Both must be set to enable HTTPS.
    # LAN access over plain http:// is NOT a secure context, so browser
    # WebCodecs (Live mirror decoding) stays unavailable for remote viewers.
    api_ssl_certfile: str = ""
    api_ssl_keyfile: str = ""

    # Device topology. Empty driver_url(s) means local, in-process ADB.
    driver_url: str = ""
    driver_urls_json: str = ""
    platform: str = "android"
    use_fixture_driver: bool = False

    # Model routing and credentials.
    default_model: str = "openai/MiniMax-M3"
    manager_model: str = ""
    executor_model: str = ""
    skill_learner_model: str = ""
    models_json: str = ""
    gateway_user_agent: str = ""
    # Empty → LiteLLM default (~/.config/litellm/chatgpt). When set, exported
    # to CHATGPT_TOKEN_DIR before ChatGPT OAuth / chatgpt/* completions.
    chatgpt_token_dir: str = ""

    # Optional device provisioning artifacts / operational switches.
    ime_auto_setup: bool = True
    ime_apk_path: str = ""
    accessibility_collector_enabled: bool = True
    accessibility_collector_apk_path: str = ""
    device_stay_awake_while_plugged: bool = False

    # Operational timeout and data locations that vary by deployment.
    task_cancel_hard_timeout_s: float = 5.0
    app_resolver_cache_path: str = ""  # empty -> data_dir/app_aliases_cache.json

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path under the data directory."""
        return self.data_dir / "clickclick.db"

    @property
    def artifacts_dir(self) -> Path:
        """Return the directory for screenshots, trees, and SoM frames."""
        return self.data_dir / "artifacts"

    @property
    def app_resolver_cache_path_resolved(self) -> Path:
        """Return the learned-cache path, defaulting under the data dir."""
        p = self.app_resolver_cache_path
        return Path(p) if p else (self.data_dir / "app_aliases_cache.json")

    def model_providers(self) -> dict[str, dict]:
        """Parse `models_json` into a {model_id: provider_config} map.

        Empty / unparseable → empty dict (callers fall back to env-driven
        LiteLLM defaults).
        """
        data = _parse_json_container(self.models_json, dict)
        if data is None:
            return {}
        out: dict[str, dict] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[str(k)] = v
        return out

    def provider_for(self, model_id: str) -> dict:
        """Return the provider config dict for a model id (empty if absent)."""
        return self.model_providers().get(model_id, {})

    def tool_choice_for(self, model_id: str) -> Literal["required", "auto"]:
        """Resolve the model's Agent tool policy without weakening submit validation."""
        choice = self.provider_for(model_id).get("tool_choice", "required")
        if choice not in ("required", "auto"):
            raise ValueError("Model tool_choice must be 'required' or 'auto'")
        return choice

    def context_policy(self, model_id: str, role: str) -> dict[str, int]:
        """Resolve explicit model/role limits; never infer a model's window."""
        raw = self.provider_for(model_id).get("context", {})
        if not isinstance(raw, dict):
            raise ValueError("Model context configuration must be an object")
        role_policy = raw.get(role, {})
        if not isinstance(role_policy, dict):
            raise ValueError("Role context configuration must be an object")
        values = {key: value for key, value in raw.items() if key in {"history_tokens", "max_input_tokens"}}
        values.update(role_policy)
        if role == "executor":
            values.setdefault("history_tokens", self.executor_context_tokens)
        for key, value in values.items():
            if key not in {"history_tokens", "max_input_tokens"} or type(value) is not int or value < 1:
                raise ValueError(f"Invalid context setting: {key}")
        return values

    def apply_chatgpt_token_dir(self) -> str | None:
        """Export ``CHATGPT_TOKEN_DIR`` when ``chatgpt_token_dir`` is set.

        Returns the resolved directory string when applied, otherwise ``None``
        (LiteLLM keeps its default token path).
        """
        raw = (self.chatgpt_token_dir or "").strip()
        if not raw:
            return None
        resolved = str(Path(raw).expanduser())
        os.environ[CHATGPT_TOKEN_DIR_ENV] = resolved
        return resolved

    def driver_hubs(self) -> list[dict[str, str]]:
        """Return remote driver hubs as ``[{"id": "...", "url": "..."}, ...]``.

        Sources (merged, URL-deduped, order preserved):

        1. ``CLICKCLICK_DRIVER_URLS_JSON`` — list of objects with ``id`` + ``url``
        2. ``CLICKCLICK_DRIVER_URL`` — appended as ``id="default"`` when its URL
           is not already present

        Empty list → in-process ADB / fixture (no remote transport).
        """
        hubs: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()

        def _add(hub_id: str, url: str) -> None:
            hub_id = (hub_id or "").strip()
            url = (url or "").strip().rstrip("/")
            if not hub_id or not url:
                return
            if url in seen_urls or hub_id in seen_ids:
                return
            seen_urls.add(url)
            seen_ids.add(hub_id)
            hubs.append({"id": hub_id, "url": url})

        data = _parse_json_container(self.driver_urls_json, list) or []
        for item in data:
            if not isinstance(item, dict):
                continue
            _add(str(item.get("id", "")), str(item.get("url", "")))

        if self.driver_url:
            _add("default", self.driver_url)

        return hubs


def get_settings() -> Settings:
    """Load and return singleton-style settings (new instance each call is fine)."""
    return Settings()
