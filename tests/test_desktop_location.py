from pathlib import Path

import pytest

from desktop import location
from desktop.install import install_payload
from tests.test_desktop_onboarding import sample_payload


def test_first_install_can_choose_another_disk_and_cancel_without_writes(tmp_path, monkeypatch):
    standard = tmp_path / "system disk/ClickClick"
    chosen = tmp_path / "other disk"
    monkeypatch.setattr(location, "default_home", lambda: standard)
    monkeypatch.setattr("desktop.dialogs.choose_install_parent", lambda _: chosen)
    assert location.choose_home() == chosen / "ClickClick"
    assert not standard.exists() and not chosen.exists()
    monkeypatch.setattr("desktop.dialogs.choose_install_parent", lambda _: None)
    assert location.choose_home() is None
    assert not standard.exists()
    assert location.choose_home(unattended=True) == standard


def test_upgrade_remembers_custom_home_and_does_not_open_folder_picker(tmp_path, monkeypatch):
    standard = tmp_path / "system disk/ClickClick"
    custom = tmp_path / "other disk/ClickClick"
    monkeypatch.setattr(location, "default_home", lambda: standard)
    install_payload(sample_payload(tmp_path), custom, platform_check=False)
    location.remember_home(custom)
    monkeypatch.setattr("desktop.dialogs.choose_install_parent", lambda _: pytest.fail("Upgrade must retain home"))
    assert location.choose_home() == custom
    assert location.choose_home(unattended=True) == custom
    explicit = tmp_path / "explicit home"
    assert location.choose_home(explicit) == explicit
    (custom / "current.json").unlink()
    with pytest.raises(ValueError, match="unavailable"):
        location.choose_home()
    assert location.choose_home(explicit) == explicit


def test_existing_default_install_stays_in_place(tmp_path, monkeypatch):
    standard = tmp_path / "ClickClick"
    monkeypatch.setattr(location, "default_home", lambda: standard)
    install_payload(sample_payload(tmp_path), standard, platform_check=False)
    monkeypatch.setattr("desktop.dialogs.choose_install_parent", lambda _: pytest.fail("Already installed"))
    assert location.choose_home() == standard
