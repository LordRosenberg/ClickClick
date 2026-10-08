"""Private desktop configuration; deliberately stdlib-only for installers."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from desktop.files import read_json, write_json


def model_config_path(data_dir):
    return Path(data_dir) / "api-model.json"


def save_model(data_dir, *, model, base_url, api_key):
    model = model.strip()
    parts = urlsplit(base_url.strip())
    if (not model or "/" not in model or model.lower().startswith(("chatgpt/", "subscription/")) or
            len(model) > 200 or any(c.isspace() for c in model)):
        raise ValueError("Provide an API model ID with its provider prefix, such as openai/gpt-4.1")
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError("Provide a model API base URL without credentials, query or fragment")
    if not api_key.strip() or len(api_key) > 8192 or any(c in api_key for c in "\r\n\x00"):
        raise ValueError("Provide a model API key")
    path = model_config_path(data_dir)
    write_json(path, {"model": model, "base_url": base_url.strip().rstrip("/"), "api_key": api_key.strip()})
    # Keep the legacy CLI useful after a Console profile has been saved.
    console = read_json(console_config_path(data_dir))
    if console:
        console["mode"] = "api"
        console.setdefault("profiles", {})["api"] = {"default_model": model,
            "manager_model": model, "executor_model": model, "skill_learner_model": model,
            "skill_reviewer_model": model, "models_json": json.dumps({model: {
                "base_url": base_url.strip().rstrip("/"), "api_key": api_key.strip()}})}
        write_json(console_config_path(data_dir), console)
    return {"saved": True, "restart_required": True, "credentials_validated": False,
            "model": model, "base_url": base_url.strip().rstrip("/"), "path": str(path)}


def model_environment(data_dir):
    console = console_values(data_dir)
    if console:
        return {"CLICKCLICK_" + key.upper(): value if isinstance(value, str) else json.dumps(value)
                for key, value in console.items() if value is not None}
    value = read_json(model_config_path(data_dir))
    if not value:
        return {}
    model = value["model"]
    return {"CLICKCLICK_DEFAULT_MODEL": model, "CLICKCLICK_MANAGER_MODEL": model,
            "CLICKCLICK_EXECUTOR_MODEL": model, "CLICKCLICK_SKILL_LEARNER_MODEL": model,
            "CLICKCLICK_MODELS_JSON": json.dumps({model: {"base_url": value["base_url"],
                                                         "api_key": value["api_key"]}})}


def model_summary(data_dir):
    console = console_values(data_dir)
    if console:
        model = console["default_model"]
        provider = json.loads(console["models_json"]).get(model, {})
        return {"saved": True, "model": model, "base_url": provider.get("base_url"),
                "credentials_validated": False}
    value = read_json(model_config_path(data_dir))
    return {"saved": bool(value), "model": value.get("model"), "base_url": value.get("base_url"),
            "credentials_validated": False}


def console_config_path(data_dir):
    return Path(data_dir) / "console-settings.json"


def console_values(data_dir):
    value = read_json(console_config_path(data_dir))
    if not value:
        return {}
    if value.get("version") != 1 or value.get("mode") not in {"api", "subscription"}:
        raise ValueError("Unsupported Console settings format")
    return {**value["profiles"][value["mode"]], **value.get("runtime", {})}


def apply_saved_settings(settings):
    """Load durable desktop values without changing the parent process env."""
    values = console_values(settings.data_dir)
    if not values:
        values = {key.removeprefix("CLICKCLICK_").lower(): value
                  for key, value in model_environment(settings.data_dir).items()}
    if values:
        checked = type(settings)(**{**settings.model_dump(), **values})
        for key in values:
            setattr(settings, key, getattr(checked, key))
    return settings
