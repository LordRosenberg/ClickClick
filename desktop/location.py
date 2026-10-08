"""Remember a user-selected install location independently of program versions."""

from pathlib import Path

from desktop.files import read_json, write_json
from desktop.install import default_home, installed


def remembered_home():
    standard = default_home().resolve()
    value = read_json(standard / "install-location.json")
    if value:
        path = Path(value.get("home", ""))
        if not path.is_absolute():
            raise ValueError("Recorded ClickClick install location is invalid; specify --home")
        try:
            installed(path)
        except (ValueError, OSError, KeyError) as exc:
            raise ValueError("Recorded ClickClick installation is unavailable; reconnect its disk or specify --home") from exc
        return path.resolve()
    if (standard / "current.json").exists():
        installed(standard)
        return standard
    return None


def remember_home(home):
    home = Path(home).resolve()
    installed(home)
    # A few bytes in per-user application data locate programs on any disk.
    write_json(default_home().resolve() / "install-location.json", {"home": str(home)})


def choose_home(requested=None, *, unattended=False):
    if requested is not None:
        return Path(requested).resolve()
    previous = remembered_home()
    if previous is not None:
        return previous
    if unattended:
        return default_home().resolve()
    from desktop.dialogs import choose_install_parent
    parent = choose_install_parent(default_home().parent)
    if parent is None:
        return None
    parent = Path(parent).resolve()
    return parent if parent.name.casefold() == "clickclick" else parent / "ClickClick"
