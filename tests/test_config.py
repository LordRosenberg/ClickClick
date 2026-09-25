"""The Settings surface contains deployment inputs, not runtime policy."""

from driver.android import AndroidDriver
from driver.observation_deadline import CURRENT_DEADLINE_MS
from shared.config import Settings


def test_settings_contains_only_operator_controlled_fields():
        assert set(Settings.model_fields) == {
            "data_dir", "api_host", "api_port", "api_ssl_certfile", "api_ssl_keyfile",
            "driver_url", "driver_urls_json", "platform", "use_fixture_driver",
        "default_model", "manager_model", "executor_model",
        "skill_learner_model",
        "models_json", "gateway_user_agent", "chatgpt_token_dir",
        "ime_auto_setup", "ime_apk_path",
        "accessibility_collector_enabled", "accessibility_collector_apk_path",
        "device_stay_awake_while_plugged", "task_cancel_hard_timeout_s",
        "app_resolver_cache_path", "agent_architecture", "executor_context_tokens",
        "compaction_attempt_notes", "chatgpt_history_tokens", "screen_detail", "double_tap",
    }


def test_driver_policy_defaults_are_transaction_compatible():
    driver = AndroidDriver()
    assert driver._capture_budget_ms == CURRENT_DEADLINE_MS
    assert not hasattr(driver, "_settle_ms")
    assert not hasattr(driver, "_frame_gate_budget_ms")
    assert driver._ime_id == "com.android.adbkeyboard/.AdbIME"


def test_settings_default_task_cancel_hard_timeout():
    s = Settings()
    assert s.task_cancel_hard_timeout_s == 5.0


def test_settings_task_cancel_hard_timeout_override(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_TASK_CANCEL_HARD_TIMEOUT_S", "10")
    s = Settings()
    assert s.task_cancel_hard_timeout_s == 10.0


def test_settings_observation_policy_is_not_configurable():
    s = Settings()
    policy_fields = {
        "scrcpy_shared_source_enabled",
        "scrcpy_decode_enabled",
        "scrcpy_stream_current_enabled",
        "scrcpy_temporal_queries_enabled",
        "observe_current_baseline_max_age_ms",
        "observe_baseline_max_age_ms",
        "observe_current_deadline_ms",
        "observe_temporal_deadline_ms",
        "observe_parent_guard_margin_ms",
        "observe_ui_dump_min_budget_ms",
        "observe_ui_dump_max_budget_ms",
        "observe_screencap_min_budget_ms",
        "observe_screencap_max_budget_ms",
        "observe_decode_min_budget_ms",
        "observe_alignment_min_budget_ms",
        "frame_gate_budget_ms",
        "frame_gate_backoff_ms",
        "frame_blank_std_threshold",
        "frame_blank_near_white_ratio",
        "frame_gate_pair_match_threshold",
        "detailed_visibility_ratio",
    }
    assert policy_fields.isdisjoint(type(s).model_fields)


def test_runtime_policy_environment_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_FRAME_GATE_BUDGET_MS", "2000")
    monkeypatch.setenv("CLICKCLICK_GATEWAY_MAX_RETRIES", "99")
    monkeypatch.setenv("CLICKCLICK_IME_ID", "io.github.fork/.AdbIME")
    settings = Settings()
    assert "frame_gate_budget_ms" not in settings.model_fields_set
    assert "gateway_max_retries" not in settings.model_fields_set
    assert "ime_id" not in settings.model_fields_set


def test_operator_switch_still_overrides_via_env(monkeypatch):
    monkeypatch.setenv("CLICKCLICK_IME_AUTO_SETUP", "0")
    s = Settings()
    assert s.ime_auto_setup is False


def test_apply_chatgpt_token_dir_sets_env(monkeypatch, tmp_path):
    import os

    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    target = tmp_path / "chatgpt-auth"
    s = Settings(chatgpt_token_dir=str(target))
    assert s.apply_chatgpt_token_dir() == str(target)
    assert os.environ["CHATGPT_TOKEN_DIR"] == str(target)


def test_models_json_allows_trailing_commas():
    s = Settings(
        models_json='{"openai/a": {"provider": "openai",}, "chatgpt/b": {"provider": "chatgpt"}}',
    )
    assert set(s.model_providers()) == {"openai/a", "chatgpt/b"}


def test_models_json_still_empty_when_unparseable():
    s = Settings(models_json="{not-json")
    assert s.model_providers() == {}


def test_apply_chatgpt_token_dir_noop_when_empty(monkeypatch):
    import os

    monkeypatch.delenv("CHATGPT_TOKEN_DIR", raising=False)
    s = Settings(chatgpt_token_dir="")
    assert s.apply_chatgpt_token_dir() is None
    assert "CHATGPT_TOKEN_DIR" not in os.environ
