import json
from types import SimpleNamespace

import pytest

from evaluation.mobileworld import fixture_context as fc
from evaluation.mobileworld.long_run_health import MobileWorldTarget


def probe_result(**overrides):
    accounts = {name: dict(documented=True, email_matches=True, password_matches=True, enabled=True)
                for name in fc.ACCOUNTS}
    accounts["owner"].update(overrides)
    return SimpleNamespace(returncode=0, stdout=json.dumps({"document_sha256": "abc", "accounts": accounts}))


def test_unrelated_task_does_not_probe_or_inject(monkeypatch, tmp_path):
    monkeypatch.setattr(fc.subprocess, "run", lambda *a, **k: pytest.fail("unexpected Docker access"))
    report = fc.prepare_fixture_context(MobileWorldTarget(), ["Maps", "Mail"], tmp_path)
    assert report == {"required": False, "ready": True}
    with fc.fixture_prompt_context(report, tmp_path):
        assert not (tmp_path / "environment-prompts").exists()


@pytest.mark.parametrize("field", ["documented", "email_matches", "password_matches", "enabled"])
def test_incomplete_login_prerequisite_blocks_model(monkeypatch, tmp_path, field):
    monkeypatch.setattr(fc.subprocess, "run", lambda *a, **k: probe_result(**{field: False}))
    report = fc.prepare_fixture_context(MobileWorldTarget(), ["Mastodon"], tmp_path)
    assert report["ready"] is False
    with pytest.raises(RuntimeError, match="verified fixture context"):
        with fc.fixture_prompt_context(report, tmp_path):
            pytest.fail("model should not run")


def test_rechecks_every_episode_and_pins_container(monkeypatch, tmp_path):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return probe_result()
    monkeypatch.setattr(fc.subprocess, "run", run)
    for n in range(2):
        report = fc.prepare_fixture_context(MobileWorldTarget(container="fixture-only"), ["Mastodon"], tmp_path / str(n))
        assert report["ready"] and report["automatic_login"] is False
        assert report["browser_authenticated"] == "not_asserted"
    assert len(calls) == 2
    assert all(c[:3] == ["docker", "exec", "fixture-only"] for c in calls)
    assert "SET TRANSACTION READ ONLY" in fc.PROBE


def test_transport_error_has_no_raw_output_leak(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError("sensitive backend body")
    monkeypatch.setattr(fc.subprocess, "run", fail)
    report = fc.prepare_fixture_context(MobileWorldTarget(), ["Mastodon"], tmp_path)
    assert report["ready"] is False
    assert "sensitive backend body" not in (tmp_path / "fixture-context.json").read_text()


def test_all_roles_keep_private_prompts_and_restore_after_error(monkeypatch, tmp_path):
    import agent.prompts as prompts
    from agent.revisable import roles
    private = tmp_path / "private"
    private.mkdir()
    for name in ["common", "planner", "reviewer", "executor"]:
        (private / f"revisable_{name}.md").write_text("private-" + name + (" {{ decision_policy }}" if name != "common" else ""), encoding="utf8")
    for role in ("planner", "reviewer", "executor"):
        for mode in ("enabled", "disabled"):
            body = "hitl" if mode == "enabled" else "autonomous"
            if mode == "enabled" and role != "reviewer":
                body += " {{ tool_form }}"
            (private / f"revisable_{role}_{mode}.md").write_text(body, encoding="utf8")
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", private)
    with pytest.raises(ValueError, match="intentional"):
        with fc.fixture_prompt_context({"required": True, "ready": True}, tmp_path / "episode"):
            for role, render in [("planner", prompts.render_planner_system),
                                 ("reviewer", prompts.render_reviewer_system),
                                 ("executor", prompts.render_executor_system)]:
                assert "private-" + role in render()
                assert fc.CONTEXT in render() and fc.CONTEXT in roles.prompt(role)
                assert render().count(fc.HEADER) == 1
                assert "hitl" in roles.prompt(role, hitl_enabled=True)
                assert fc.CONTEXT in roles.prompt(role, hitl_enabled=True)
            raise ValueError("intentional")
    assert prompts._PROMPTS_DIR == private
    assert (private / "revisable_common.md").read_text() == "private-common"


def test_conflicting_existing_context_is_not_silently_duplicated(monkeypatch, tmp_path):
    import agent.prompts as prompts
    from agent.revisable import roles
    for name in ["common", "planner", "reviewer", "executor"]:
        (tmp_path / f"revisable_{name}.md").write_text(fc.HEADER if name == "common" else name, encoding="utf8")
    monkeypatch.setattr(prompts, "_PROMPTS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="already present"):
        with fc.fixture_prompt_context({"required": True, "ready": True}, tmp_path / "episode"):
            pass
    assert prompts._PROMPTS_DIR == tmp_path


@pytest.mark.parametrize("ready", [True, False])
def test_episode_checks_after_snapshot_and_delivers_before_model(monkeypatch, tmp_path, ready):
    import sys
    import agent.prompts as prompts
    from evaluation.mobileworld import run_single as single
    events, results = [], []
    monkeypatch.setattr(sys, "argv", ["run_single", "--task", "MastodonTask", "--collector-apk", "a.apk",
        "--ime-apk", "b.apk", "--data-dir", str(tmp_path), "--no-recording"])
    for name in ("_verify_device", "verify_official_session_fix", "verify_official_task_fix",
                 "prepare_clickclick_device", "restore_task_clock"):
        monkeypatch.setattr(single, name, lambda *a: None)
    for name in ("inspect_task_sessions", "ensure_task_clock"):
        monkeypatch.setattr(single, name, lambda *a: {"ready": True})
    def request(method, url, **kwargs):
        op = url.rsplit("/", 1)[-1]
        events.append(op)
        return {"metadata": {"apps": ["Mastodon"]}, "goal": "Original task exactly",
                "eval": {"score": 1}}.get(op, "OK")
    def probe(*args):
        assert events[-1] == "init"
        events.append("fixture_check")
        return {"required": True, "ready": ready}
    async def execute(args, goal):
        events.append("model")
        assert goal == "Original task exactly"
        assert fc.CONTEXT in prompts.render_executor_system()
        return {"clickclick_status": "succeeded"}
    monkeypatch.setattr(single, "_request", request)
    monkeypatch.setattr(single, "prepare_fixture_context", probe)
    monkeypatch.setattr(single, "_run_clickclick", execute)
    monkeypatch.setattr(single, "_write_result", lambda args, row: results.append(row))
    single.main()
    assert events[-1] == "tear_down"
    assert results[0]["goal"] == "Original task exactly"
    if ready:
        assert events.index("fixture_check") < events.index("model") < events.index("eval")
        assert results[0]["fixture_context"]["ready"]
    else:
        assert "model" not in events and "eval" not in events
        assert results[0]["score"] is None
        assert results[0]["environment_status"] == "invalid_initialization"


def test_preflight_rejects_unavailable_accounts_before_scoring(monkeypatch, tmp_path):
    from evaluation.mobileworld import preflight_all as pf
    events = []
    for name in ("verify_identity", "verify_official_session_fix", "verify_official_task_fix",
                 "verify_task_apps", "ensure_task_asset_route", "verify_mattermost_readiness_fix"):
        monkeypatch.setattr(pf, name, lambda *a: {})
    for name in ("inspect_initialization_errors", "inspect_task_asset_access"):
        monkeypatch.setattr(pf, name, lambda *a: {"ready": True})
    def request(method, url, **kwargs):
        events.append(url.rsplit("/", 1)[-1])
        return "goal" if url.endswith("/goal") else "OK"
    def probe(*a):
        assert events[-1] == "init"
        return {"required": True, "ready": False}
    monkeypatch.setattr(pf, "request", request)
    monkeypatch.setattr(pf, "prepare_fixture_context", probe)
    row = pf.preflight_one(MobileWorldTarget(), {"name": "MastodonTask", "apps": ["Mastodon"]},
                           tmp_path, tmp_path / "a.apk", tmp_path / "b.apk")
    assert row["failure_phase"] == "fixture_account_context"
    assert events == ["goal", "init", "tear_down"]
