"""Verified versioned payload installation. No network or system Python needed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
from zipfile import ZipFile

from desktop.files import private_write, read_json, write_json


def default_home():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ClickClick"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ClickClick"
    return Path.home() / ".local/share/clickclick"


def runtime_architecture():
    import platform
    import sysconfig
    # Some restricted Windows hosts omit the processor environment variables.
    name = platform.machine() or sysconfig.get_platform().rsplit("-", 1)[-1]
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(name.lower())
    if arch is None:
        raise ValueError("Unsupported desktop architecture")
    return arch


def payload_member(name):
    value = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or value.is_absolute() or
            any(p in {"", ".", ".."} for p in name.split("/"))):
        raise ValueError("Unsafe payload path")
    return value


def installed(home):
    home = Path(home).resolve()
    current = read_json(home / "current.json")
    version = current.get("version", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", version):
        raise ValueError("ClickClick is not installed at this location")
    root = home / "versions" / version
    if root.is_symlink() or (home / "versions").is_symlink():
        raise ValueError("Installed version must not be a symbolic link")
    python = root / Path(*payload_member(current["python"]).parts)
    if not python.is_file() or not (root / "app/desktop/main.py").is_file():
        raise ValueError("Installed ClickClick runtime is incomplete")
    return root, python, current


def installed_command(home, operation):
    _, python, _ = installed(home)
    # The private runtime's .pth loads app + bundled dependencies automatically.
    return str(python), ["-I", "-m", "desktop.main", "--home", str(Path(home).resolve()), operation]


def mcp_command(home):
    """Assistant configuration survives removal of obsolete version directories."""
    home = Path(home).resolve()
    installed(home)
    if sys.platform == "win32":
        command = str(Path(os.environ.get("SystemRoot", "C:" + "/Windows")) / "System32/cmd.exe")
        return command, ["/d", "/c", "call", str(home / "bin/clickclick.cmd"), "mcp"]
    return str(home / "bin/clickclick"), ["mcp"]


def install_payload(archive, home=None, *, platform_check=True, activate=True):
    home = Path(home or default_home()).resolve()
    home.mkdir(parents=True, exist_ok=True)
    if (home / "versions").is_symlink():
        raise ValueError("Version directory must not be a symbolic link")
    with ZipFile(archive) as zipped:
        entries = {i.filename: i for i in zipped.infolist() if not i.is_dir()}
        if len(entries) != sum(not i.is_dir() for i in zipped.infolist()):
            raise ValueError("Duplicate payload path")
        if len({name.casefold() for name in entries}) != len(entries):
            raise ValueError("Payload paths collide on a case-insensitive filesystem")
        for name, item in entries.items():
            payload_member(name)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Payload links are not accepted")
        manifest = json.loads(zipped.read("manifest.json"))
        version = manifest["version"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", version):
            raise ValueError("Invalid payload version")
        target = {"win32": "windows", "darwin": "macos", "linux": "linux"}.get(sys.platform)
        arch = runtime_architecture()
        if platform_check and (manifest["platform"] != target or manifest["architecture"] != arch):
            raise ValueError("Installer does not match this operating system/architecture")
        hashes = manifest["files"]
        if set(entries) != {"manifest.json", *hashes}:
            raise ValueError("Payload inventory mismatch")
        required = {manifest["python"], "app/desktop/main.py", "app/web/dist/index.html",
                    "app/driver/vendor/scrcpy-server-v3.3.1.jar", "tools/collector.apk",
                    "tools/ADBKeyboard.apk", "licenses/ADBKeyboard-source.zip",
                    "tools/platform-tools/" + ("adb.exe" if manifest["platform"] == "windows" else "adb")}
        if not required <= set(hashes):
            raise ValueError("Required runtime assets missing")
        versions = home / "versions"
        versions.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=versions) as raw:
            staging = Path(raw)
            for name, digest in hashes.items():
                target_path = staging / Path(*payload_member(name).parts)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                actual = hashlib.sha256()
                with zipped.open(name) as source, target_path.open("wb") as dest:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        actual.update(chunk)
                        dest.write(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError("Payload integrity check failed")
                mode = entries[name].external_attr >> 16
                target_path.chmod(0o755 if mode & 0o111 else 0o644)
            write_json(staging / "manifest.json", manifest)
            final = versions / version
            if final.is_symlink():
                raise ValueError("Installed version must not be a symbolic link")
            if final.exists():
                if read_json(final / "manifest.json") != manifest:
                    raise ValueError("Installed version differs; use a new version rather than overwrite")
                # Verify existing files too: repeated installation must not trust corruption.
                if any(not (final / name).is_file() or hashlib.sha256((final / name).read_bytes()).hexdigest() != digest
                       for name, digest in hashes.items()):
                    raise ValueError("Existing version is damaged; install a new version")
            else:
                staging.rename(final)
            if activate:
                write_json(home / "current.json", {"version": version, "python": manifest["python"]})
    if not activate:
        return {"staged": True, "version": version, "root": str(final),
                "python": str(final / manifest["python"])}
    (home / "data").mkdir(exist_ok=True)
    seed_skills(home)
    if not (home / "installation.json").exists():
        write_json(home / "installation.json", {"port": 18080})
    make_launcher(home)
    return {"installed": True, "home": str(home), "version": version,
            "launcher": str(home / "bin" / ("clickclick.cmd" if os.name == "nt" else "clickclick"))}


def seed_skills(home):
    """Seed bundled skills while preserving subsequent user edits and additions."""
    root, _, _ = installed(home)
    destination = Path(home) / "data/skills"
    for source in (root / "app/skills").rglob("SKILL.md"):
        target = destination / source.relative_to(root / "app/skills")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def make_launcher(home):
    _, python, _ = installed(home)
    if os.name == "nt":
        # Regenerated atomically after activation, retaining the same command path.
        command = str(python).replace("%", "%%")
        location = str(home).replace("%", "%%")
        private_write(home / "bin/clickclick.cmd", f'@echo off\r\n"{command}" -I -m desktop.main --home "{location}" %*\r\n')
    else:
        import shlex
        path = home / "bin/clickclick"
        private_write(path, "#!/bin/sh\nexec " + shlex.quote(str(python)) + " -I -m desktop.main --home " + shlex.quote(str(home)) + ' "$@"\n')
        path.chmod(0o755)


def installed_environment(home):
    root, _, _ = installed(home)
    from desktop.configuration import model_environment
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLICKCLICK_", "_PYI_"))
           and k not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    data = Path(home).resolve() / "data"
    config = read_json(Path(home) / "installation.json")
    adb = root / "tools/platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    env.update(CLICKCLICK_INSTALL_HOME=str(Path(home).resolve()), CLICKCLICK_DATA_DIR=str(data),
        CLICKCLICK_API_HOST="127.0.0.1", CLICKCLICK_API_PORT=str(config.get("port", 18080)),
        CLICKCLICK_MCP_HTTP_ENABLED="true", CLICKCLICK_DRIVER_URL="", CLICKCLICK_DRIVER_URLS_JSON="",
        CLICKCLICK_MCP_HTTP_PORT=str(config.get("mcp_port", 18081)),
        CLICKCLICK_SKILLS_DIR=str(data / "skills"),
        CLICKCLICK_USE_FIXTURE_DRIVER="false", CLICKCLICK_ADB_PATH=str(adb),
        CLICKCLICK_ADB_AUTO_DOWNLOAD="0", CLICKCLICK_DEVICE_APK_CACHE=str(data / "device-apks"),
        CLICKCLICK_ACCESSIBILITY_COLLECTOR_APK_PATH=str(root / "tools/collector.apk"),
        CLICKCLICK_IME_APK_PATH=str(root / "tools/ADBKeyboard.apk"),
        CLICKCLICK_API_SSL_CERTFILE="", CLICKCLICK_API_SSL_KEYFILE="")
    env["TIKTOKEN_CACHE_DIR"] = str(root / "tools/tiktoken")
    # Put bundled adb first only in this process, never modify machine PATH.
    env["PATH"] = str(adb.parent) + os.pathsep + env.get("PATH", "")
    env.update(model_environment(data))
    return env, root / "app"
