"""Validated core settings, with explicit secret preservation and safe summaries."""

import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from desktop.configuration import console_config_path
from desktop.files import read_json, write_json
from shared.model_catalog import is_chatgpt_subscription_model

ROLE_FIELDS = ("default_model", "manager_model", "executor_model", "skill_learner_model", "skill_reviewer_model")
RUNTIME_FIELDS = ("agent_architecture", "executor_context_tokens", "chatgpt_history_tokens", "default_task_model_calls", "default_task_device_actions",
                  "default_task_seconds", "learning_default_calls", "learning_default_actions", "learning_default_seconds")
JEV_FIELDS = ("jev_mode", "jev_allow_external", "jev_api_key", "jev_base_url", "jev_model", "jev_timeout_s", "jev_failure_policy")


class JevSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "shadow", "enforce"] = "off"
    base_url: str = Field(default="https://api.typesafe.ai", max_length=2000)
    model: str = Field(default="jev-1.13.0", min_length=1, max_length=200)
    api_key: SecretStr = SecretStr("")
    timeout_s: float = Field(default=8.0, gt=0, le=60)
    failure_policy: Literal["bypass", "rollback"] = "bypass"

    @model_validator(mode="after")
    def valid_api(self):
        parts = urlsplit(self.base_url.strip())
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError("Jev API 地址需为不带凭据、查询参数或片段的 HTTP/HTTPS 地址")
        if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 Jev API 地址需要 HTTPS")
        if any(c.isspace() or ord(c) < 32 for c in self.model):
            raise ValueError("Jev 模型 ID 无效")
        key = self.api_key.get_secret_value()
        if len(key) > 8192 or any(c in key for c in "\r\n\x00"):
            raise ValueError("Jev API Key 格式无效")
        return self


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2000)
    api_key: SecretStr = SecretStr("")
    # Explicit local copy for a new/renamed model on the same API endpoint.
    credentials_from: str = Field(default="", max_length=200)
    user_agent: str = Field(default="", max_length=2000)
    stream: bool = False
    reasoning_supported: bool = False
    reasoning_effort: Literal["", "minimal", "low", "medium", "high", "xhigh"] = ""
    reasoning_summary: Literal["", "auto", "concise", "detailed"] = ""
    max_tokens: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    history_tokens: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    max_input_tokens: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    tool_choice: Literal["required", "auto"] = "required"
    allowed_openai_params: list[str] = Field(default_factory=list, max_length=30)
    # None retains the saved advanced body; '{}' explicitly clears it.
    extra_body: SecretStr | None = None

    @model_validator(mode="after")
    def valid_fields(self):
        if "/" not in self.id or any(c.isspace() or ord(c) < 32 for c in self.id):
            raise ValueError("模型 ID 需要服务商前缀，例如 openai/gpt-4.1 或 chatgpt/gpt-5.4")
        if not self.user_agent.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in self.user_agent):
            raise ValueError("HTTP User-Agent 需使用可打印的 ASCII 字符")
        key = self.api_key.get_secret_value()
        if len(key) > 8192 or any(c in key for c in "\r\n\x00"):
            raise ValueError("API Key 格式无效")
        if self.extra_body is not None:
            try:
                raw = self.extra_body.get_secret_value()
                if len(raw) > 65536 or not isinstance(json.loads(raw), dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("额外请求参数必须是 JSON 对象") from None
        if any(not v or len(v) > 80 or not v.replace("_", "").isalnum() for v in self.allowed_openai_params):
            raise ValueError("额外能力参数名称无效")
        return self


class CoreSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["api", "subscription"]
    # Optional for older CLI/API clients; omission retains existing Jev settings.
    jev: JevSettingsBody | None = None
    models: list[ModelConfig] = Field(min_length=1, max_length=32)
    default_model: str = Field(min_length=1, max_length=200)
    manager_model: str = ""
    executor_model: str = ""
    skill_learner_model: str = ""
    skill_reviewer_model: str = ""
    executor_context_tokens: int = Field(default=16000, ge=1, le=1000000, strict=True)
    agent_architecture: Literal["plan_executor", "plan_reviewer"] = "plan_executor"
    chatgpt_history_tokens: int | None = Field(default=None, ge=1, le=1000000, strict=True)
    default_task_model_calls: int | None = Field(default=None, ge=1, le=200, strict=True)
    default_task_device_actions: int | None = Field(default=None, ge=1, le=10000, strict=True)
    default_task_seconds: int | None = Field(default=None, ge=1, le=86400, strict=True)
    learning_default_calls: int = Field(default=24, ge=12, le=64, strict=True)
    learning_default_actions: int = Field(default=30, ge=1, le=100, strict=True)
    learning_default_seconds: int = Field(default=900, ge=30, le=1800, strict=True)

    @model_validator(mode="after")
    def valid_roles(self):
        ids = {m.id for m in self.models}
        if len(ids) != len(self.models):
            raise ValueError("模型 ID 不能重复")
        if any(getattr(self, role) and getattr(self, role) not in ids for role in ROLE_FIELDS):
            raise ValueError("各角色必须选择当前通道中已配置的模型")
        for model in self.models:
            subscription = model.id.startswith("chatgpt/")
            if subscription != (self.mode == "subscription") or model.id.startswith("subscription/"):
                raise ValueError("模型前缀与选择的接入方式不一致")
            if subscription and (model.base_url or model.api_key.get_secret_value() or model.user_agent
                                 or model.max_tokens is not None or model.extra_body is not None or model.credentials_from):
                raise ValueError("订阅通道不接受 API 地址、Key、UA、输出长度或额外请求参数")
            if not subscription:
                parts = urlsplit(model.base_url.strip())
                if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                        or parts.password or parts.query or parts.fragment):
                    raise ValueError("API 地址需为不带凭据、查询参数或片段的 HTTP/HTTPS 地址")
        return self


def prepare_settings(settings, body):
    """Return fully validated persistence/runtime values; does not mutate state."""
    saved = read_json(console_config_path(settings.data_dir))
    old_profile = saved.get("profiles", {}).get(body.mode, {})
    old_providers = json.loads(old_profile.get("models_json", "{}"))
    old_providers = {**settings.model_providers(), **old_providers}
    providers = {}
    for model in body.models:
        previous = old_providers.get(model.id, {})
        if model.credentials_from:
            source = old_providers.get(model.credentials_from, {})
            if (not source.get("api_key") or is_chatgpt_subscription_model(model.credentials_from, source)
                    or source.get("base_url", "").strip().rstrip("/") != model.base_url.strip().rstrip("/")):
                raise ValueError("只能复用已保存的同一 API 地址的凭据；新服务请填写 API Key")
            previous = {**source, **previous}
        provider = {"provider": model.id.split("/", 1)[0], "stream": model.stream,
                    "reasoning_supported": model.reasoning_supported, "tool_choice": model.tool_choice}
        if model.reasoning_supported:
            provider["reasoning"] = {key: value for key, value in {
                "effort": model.reasoning_effort, "summary": model.reasoning_summary}.items() if value}
        provider["context"] = {key: value for key, value in {
            "history_tokens": model.history_tokens, "max_input_tokens": model.max_input_tokens}.items() if value}
        if model.allowed_openai_params:
            provider["allowed_openai_params"] = model.allowed_openai_params
        if body.mode == "subscription":
            provider["api_mode"] = "responses"
        else:
            key = model.api_key.get_secret_value().strip() or previous.get("api_key", "")
            if not key:
                raise ValueError("新 API 模型需要填写 API Key；已有模型留空可保留原 Key")
            provider.update(api_key=key, base_url=model.base_url.strip().rstrip("/"), user_agent=model.user_agent)
            if model.max_tokens is not None:
                provider["max_tokens"] = model.max_tokens
            if model.extra_body is None:
                provider["extra_body"] = previous.get("extra_body", {})
            else:
                provider["extra_body"] = json.loads(model.extra_body.get_secret_value())
        providers[model.id] = provider
    profile = {role: getattr(body, role) for role in ROLE_FIELDS}
    profile["models_json"] = json.dumps(providers)
    runtime = {field: getattr(body, field) for field in RUNTIME_FIELDS}
    jev = {field: getattr(settings, field) for field in JEV_FIELDS}
    jev["jev_api_key"] = settings.jev_api_key.get_secret_value()
    if body.jev is not None:
        config = body.jev
        key = config.api_key.get_secret_value().strip()
        base_url = config.base_url.strip().rstrip("/")
        if not key and jev["jev_api_key"] and base_url != settings.jev_base_url.rstrip("/"):
            raise ValueError("更换 Jev API 地址需要重新填写 API Key")
        key = key or jev["jev_api_key"]
        if config.mode != "off" and not key:
            raise ValueError("启用 Jev 需要填写 API Key")
        jev.update(jev_mode=config.mode, jev_allow_external=config.mode != "off", jev_api_key=key,
                   jev_base_url=base_url, jev_model=config.model, jev_timeout_s=config.timeout_s,
                   jev_failure_policy=config.failure_policy)
    runtime.update(jev)
    runtime["chatgpt_token_dir"] = settings.chatgpt_token_dir or str(settings.data_dir.resolve() / "chatgpt-auth")
    checked = type(settings)(**{**settings.model_dump(), **profile, **runtime})
    document = {"version": 1, "mode": body.mode, "profiles": {**saved.get("profiles", {}), body.mode: profile},
                "runtime": runtime}
    # Retain the currently configured alternate channel on the first switch.
    if not saved:
        alternate_mode = "api" if body.mode == "subscription" else "subscription"
        alternate_models = {mid: config for mid, config in settings.model_providers().items()
                            if is_chatgpt_subscription_model(mid, config) == (alternate_mode == "subscription")}
        if alternate_models:
            alternate = {role: getattr(settings, role) if getattr(settings, role) in alternate_models else ""
                         for role in ROLE_FIELDS}
            alternate["default_model"] = alternate["default_model"] or next(iter(alternate_models))
            alternate["models_json"] = json.dumps(alternate_models)
            document["profiles"][alternate_mode] = alternate
    return document, checked


def persist_settings(settings, document, checked):
    write_json(console_config_path(settings.data_dir), document)
    for field in (*ROLE_FIELDS, *RUNTIME_FIELDS, *JEV_FIELDS, "models_json", "chatgpt_token_dir"):
        setattr(settings, field, getattr(checked, field))


def profile_summary(values, mode):
    models = []
    providers = json.loads(values.get("models_json") or "{}")
    for model_id, config in providers.items():
        if is_chatgpt_subscription_model(model_id, config) != (mode == "subscription"):
            continue
        reasoning = config.get("reasoning") or {}
        context = config.get("context") or {}
        # Allowlist: arbitrary provider metadata may contain secret headers/body.
        models.append({"id": model_id, "base_url": config.get("base_url", ""),
            "api_key_configured": bool(config.get("api_key")), "user_agent": config.get("user_agent", ""),
            "stream": config.get("stream", False), "reasoning_supported": config.get("reasoning_supported", bool(reasoning)),
            "reasoning_effort": reasoning.get("effort", ""), "reasoning_summary": reasoning.get("summary", ""),
            "max_tokens": config.get("max_tokens"), "history_tokens": context.get("history_tokens"),
            "max_input_tokens": context.get("max_input_tokens"), "tool_choice": config.get("tool_choice", "required"),
            "allowed_openai_params": config.get("allowed_openai_params", []),
            "extra_body_configured": bool(config.get("extra_body")), "extra_body": None})
    ids = {model["id"] for model in models}
    roles = {role: values.get(role, "") if values.get(role) in ids else "" for role in ROLE_FIELDS}
    roles["default_model"] = roles["default_model"] or (models[0]["id"] if models else "")
    return {"models": models, **roles}


def settings_summary(settings):
    saved = read_json(console_config_path(settings.data_dir))
    mode = saved.get("mode", "subscription" if settings.default_model.startswith("chatgpt/") else "api")
    profiles = {key: profile_summary(value, key) for key, value in saved.get("profiles", {}).items()}
    current = {role: getattr(settings, role) for role in ROLE_FIELDS}
    current["models_json"] = settings.models_json
    profiles[mode] = profile_summary(current, mode)
    other = "api" if mode == "subscription" else "subscription"
    if other not in profiles:
        alternate = profile_summary(current, other)
        if alternate["models"]:
            profiles[other] = alternate
    return {"mode": mode, "profiles": profiles, **{key: getattr(settings, key) for key in RUNTIME_FIELDS},
            "jev": {"mode": settings.jev_mode, "base_url": settings.jev_base_url, "model": settings.jev_model,
                    "api_key_configured": bool(settings.jev_api_key.get_secret_value()),
                    "timeout_s": settings.jev_timeout_s, "failure_policy": settings.jev_failure_policy},
            "credentials_validated": False, "restart_required": False}
