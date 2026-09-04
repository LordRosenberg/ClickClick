"""Desensitized LLM model catalog for Control API / Console."""

from __future__ import annotations

from typing import Any

from shared.config import Settings
from shared.model_router import ModelRouter


def is_chatgpt_subscription_model(model_id: str, provider_cfg: dict[str, Any] | None = None) -> bool:
    """True for LiteLLM ChatGPT Pro/Max subscription routes."""
    cfg = provider_cfg or {}
    provider = str(cfg.get("provider", "")).lower()
    if provider:
        return provider == "chatgpt"
    return str(model_id).lower().startswith("chatgpt/")


def build_model_catalog(settings: Settings) -> dict[str, Any]:
    """Return catalog + resolved roles; never include secrets."""
    providers = settings.model_providers()
    models: list[dict[str, Any]] = []
    for model_id, cfg in providers.items():
        models.append({
            "id": model_id,
            "provider": str(cfg.get("provider") or ("chatgpt" if is_chatgpt_subscription_model(model_id, cfg) else "unknown")),
            "is_chatgpt": is_chatgpt_subscription_model(model_id, cfg),
            "reasoning_supported": bool(cfg.get("reasoning_supported", cfg.get("reasoning"))),
        })
    models.sort(key=lambda m: m["id"])
    router = ModelRouter.from_settings(settings)
    skill = (settings.skill_learner_model or "").strip() or router.decision
    return {
        "models": models,
        "roles": {
            "default": settings.default_model,
            "planner": router.planner,
            "reviewer": router.reviewer,
            "executor": router.executor,
            "skill_learner": skill,
        },
    }


def catalog_model_ids(settings: Settings) -> set[str]:
    return set(settings.model_providers().keys())
