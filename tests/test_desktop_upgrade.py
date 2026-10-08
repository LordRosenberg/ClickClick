"""Upgrade lifecycle, stable MCP entry and bounded deferred program cleanup."""

from pathlib import Path
from contextlib import nullcontext

import pytest

from control_api.mcp_launcher import startup_lock
from desktop import cleanup, service, upgrade
from desktop.files import read_json, write_json
from desktop.install import install_payload, installed, mcp_command
from tests.test_desktop_onboarding import sample_payload


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "installed space"
    install_payload(sample_payload(tmp_path), path, platform_check=False)
    return path


def test_stable_mcp_command_survives_version_replacement(home, tmp_path):
    before = mcp_command(home)
    install_payload(sample_payload(tmp_path, version="2.0.0"), home, platform_check=False)
    assert mcp_command(home) == before
    assert "versions" not in " ".join([before[0], *before[1]])
    assert "2.0.0" in next((home / "bin").glob("clickclick*")).read_text()


def test_cleanup_defers_running_program_then_removes_without_touching_data(home, tmp_path, monkeypatch):
    secret = home / "data/console-settings.json"
    secret.write_text("private configuration")
    install_payload(sample_payload(tmp_path, version="2.0.0"), home, platform_check=False)
    unowned = home / "versions/my-notes"
    unowned.mkdir()
    (unowned / "note.txt").write_text("preserve")
    monkeypatch.setattr(cleanup, "running_versions", lambda _: {"1.0.0"})
    cleanup.queue(home)
    assert cleanup.retry(home)["deferred"] == ["1.0.0"]
    assert (home / "versions/1.0.0/runtime/python.exe").exists()
    monkeypatch.setattr(cleanup, "running_versions", lambda _: set())
    assert cleanup.retry(home)["removed"] == ["1.0.0"]
    assert not (home / "versions/1.0.0").exists()
    assert installed(home)[2]["version"] == "2.0.0"
    assert secret.read_text() == "private configuration"
    assert (unowned / "note.txt").read_text() == "preserve"
    assert not (home / "data/program-cleanup.json").exists()


def test_cleanup_waits_for_update_rollback_and_cancels_after_restore(home, tmp_path, monkeypatch):
    old = read_json(home / "current.json")
    install_payload(sample_payload(tmp_path, version="2.0.0"), home, platform_check=False)
    monkeypatch.setattr(cleanup, "running_versions", lambda _: set())
    cleanup.queue(home)
    for name in (".update-run.lock", ".install.lock"):
        with startup_lock(home / "data" / name, timeout=0):
            assert cleanup.retry(home)["deferred"] == ["retry-pending"]
        assert (home / "versions/1.0.0").exists()
    write_json(home / "current.json", old)
    cleanup.retry(home)
    assert (home / "versions/1.0.0").exists()
    assert (home / "versions/2.0.0").exists()
    assert not (home / "data/program-cleanup.json").exists()


def test_cleanup_refuses_redirected_program_root(home, tmp_path, monkeypatch):
    install_payload(sample_payload(tmp_path, version="2.0.0"), home, platform_check=False)
    monkeypatch.setattr(cleanup, "running_versions", lambda _: set())
    monkeypatch.setattr(cleanup, "linked", lambda p: p.name == ".obsolete")
    cleanup.queue(home)
    assert cleanup.retry(home)["deferred"] == ["retry-pending"]
    assert (home / "versions/1.0.0").exists()


def lifecycle(monkeypatch, home, tmp_path):
    events = []
    real_install = install_payload
    monkeypatch.setattr(upgrade, "install_payload", lambda payload, location, **kwargs:
                        real_install(payload, location, platform_check=False, **kwargs))
    monkeypatch.setattr(upgrade, "install_entries", lambda _: {})
    monkeypatch.setattr(upgrade, "task_gate", lambda *_: nullcontext())
    monkeypatch.setattr(service, "backend_status", lambda _: {"running": True, "backend": {"pid": 123}})
    monkeypatch.setattr(service, "require_idle", lambda _: events.append("idle"))
    monkeypatch.setattr(service, "stop_tray", lambda _: events.append("tray-exited"))
    monkeypatch.setattr(service, "native", lambda action, _, **kwargs: events.append((action, kwargs.get("tray", False))))
    monkeypatch.setattr(service, "wait_stopped", lambda _: events.append("port-closed"))
    monkeypatch.setattr(service, "wait_process_exited", lambda _, pid: events.append(("process-exited", pid)))
    monkeypatch.setattr(service, "wait_ready", lambda _: events.append("new-ready") or {})
    def tray(_):
        with startup_lock(home / "data/.lifecycle.lock", timeout=0):
            events.append("new-tray")
        return {"tray_visible": True}
    monkeypatch.setattr(service, "start_tray", tray)
    monkeypatch.setattr(cleanup, "running_versions", lambda _: set())
    return events, sample_payload(tmp_path, version="2.0.0")


def test_installer_stops_old_processes_before_activation_and_prunes_after_ready(home, tmp_path, monkeypatch):
    events, payload = lifecycle(monkeypatch, home, tmp_path)
    result = upgrade.install(home, payload)
    assert events.index("tray-exited") < events.index(("stop", False))
    assert events.index(("process-exited", 123)) < events.index(("start", False))
    assert events.index("new-ready") < events.index("new-tray")
    assert result["program_cleanup"]["removed"] == ["1.0.0"]
    assert result["tray_visible"]


def test_failed_manual_upgrade_restores_old_program_and_preserves_config(home, tmp_path, monkeypatch):
    _, payload = lifecycle(monkeypatch, home, tmp_path)
    old = read_json(home / "current.json")
    secret = home / "data/api-model.json"
    secret.write_text("keep me")
    monkeypatch.setattr(service, "wait_ready", lambda _: (_ for _ in ()).throw(RuntimeError("startup failed")))
    restored = []
    def restore(location, value):
        restored.append(value)
        write_json(location / "current.json", value)
    monkeypatch.setattr("desktop.updates.restore", restore)
    with pytest.raises(RuntimeError, match="startup failed"):
        upgrade.install(home, payload)
    assert restored == [old] and read_json(home / "current.json") == old
    assert secret.read_text() == "keep me"
    assert (home / "versions/1.0.0").exists()
    assert not (home / "data/program-cleanup.json").exists()


def test_busy_or_corrupt_upgrade_does_not_stop_manager(home, tmp_path, monkeypatch):
    events, payload = lifecycle(monkeypatch, home, tmp_path)
    monkeypatch.setattr(service, "require_idle", lambda _: (_ for _ in ()).throw(RuntimeError("Tasks are active")))
    with pytest.raises(RuntimeError, match="Tasks are active"):
        upgrade.install(home, payload)
    assert not events and installed(home)[2]["version"] == "1.0.0"
    with pytest.raises(ValueError, match="integrity"):
        upgrade.install(home, sample_payload(tmp_path, version="3.0.0", corrupt=True))
    assert not events


def test_tray_waits_for_process_exit_after_lock_release(home, monkeypatch):
    write_json(home / "data/tray-state.json", {"pid": 123})
    events = []
    monkeypatch.setattr(service, "finish_tray_exit", lambda location, pid: events.append(pid))
    monkeypatch.setattr(service.sys, "platform", "win32")
    service.stop_tray(home)
    assert events == [123]
