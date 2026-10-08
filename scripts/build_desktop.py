"""Build an offline ClickClick installer on the target Windows/macOS host.

Uses a relocatable uv-managed Python, never copies the developer virtualenv.
Node/uv/PyInstaller/Android build tools are BUILD requirements only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from zipfile import ZipFile, ZIP_DEFLATED

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from desktop.files import write_json
from desktop.packaging import application_files
from desktop.install import runtime_architecture
from desktop.versions import tag_version


def run(command, **kwargs):
    return subprocess.run([str(p) for p in command], check=True, text=True, **kwargs)


def prepare_runtime(destination, work):
    uv = shutil.which("uv") or str(Path(sys.executable).parent / ("uv.exe" if os.name == "nt" else "uv"))
    cache = REPO / "data/desktop-build-cache"
    env = {**os.environ, "UV_PYTHON_INSTALL_DIR": str(cache / "managed-python"), "UV_CACHE_DIR": str(cache / "uv-cache")}
    run([uv, "python", "install", "--no-bin", "--no-registry", "3.12"], env=env)
    # uv may return a minor-version directory symlink on Unix. Resolve it before
    # comparing with sys.base_prefix and copying the physical installation.
    executable = Path(run([uv, "python", "find", "--managed-python", "3.12"], env=env, capture_output=True).stdout.strip()).resolve(strict=True)
    info = json.loads(run([executable, "-c", "import sys,json; print(json.dumps({'prefix':sys.base_prefix,'version':sys.version}))"], capture_output=True).stdout)
    prefix = Path(info["prefix"]).resolve(strict=True)
    shutil.copytree(prefix, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test", "tests", "idlelib", "tkinter", "ensurepip"))
    python = destination / executable.relative_to(prefix)
    deps = destination / "site-packages"
    # Install once during build into the payload; no pip runs during user installation.
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [*project["dependencies"], *project["optional-dependencies"]["desktop"]]
    # Resolve dependencies directly. Building a wheel from the development checkout
    # can traverse unrelated data and embed source paths; application files come
    # exclusively from the allowlist below.
    run([uv, "pip", "install", "--python", python, "--target", deps, *requirements], env=env)
    purelib = Path(run([python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], capture_output=True).stdout.strip())
    purelib.relative_to(destination)  # Fail if Python is not relocatable.
    purelib.mkdir(parents=True, exist_ok=True)
    # Process dependency .pth files too (notably pywin32's DLL/bootstrap paths).
    (purelib / "clickclick.pth").write_text(
        'import site,sys,os; site.addsitedir(os.path.join(sys.prefix,"site-packages"))\n' +
        os.path.relpath(destination.parent / "app", purelib) + "\n", encoding="utf-8")
    return python, info


def device_assets(payload, keyboard_apk, keyboard_source):
    from driver.adb import _platform_tools_archive
    from driver.collector_release import COLLECTOR_SHA256, COLLECTOR_VERSION, download_collector_release
    tools = payload / "tools"
    tools.mkdir()
    host = "windows" if sys.platform == "win32" else "darwin"
    url, sha1 = _platform_tools_archive(host=host)
    with tempfile.TemporaryDirectory(dir=payload.parent) as raw:
        archive = Path(raw) / "platform-tools.zip"
        urllib.request.urlretrieve(url, archive)
        if hashlib.sha1(archive.read_bytes()).hexdigest() != sha1:
            raise ValueError("Platform Tools digest mismatch")
        with ZipFile(archive) as zipped:
            for item in zipped.infolist():
                target = (tools / item.filename).resolve()
                target.relative_to(tools.resolve())
            zipped.extractall(tools)
        for executable in ("adb", "fastboot"):
            path = tools / "platform-tools" / executable
            if path.exists():
                path.chmod(0o755)
    # Keep only adb and its runtime libraries/notices; fastboot is unnecessary.
    for item in (tools / "platform-tools").iterdir():
        if item.is_file() and item.name not in {"adb", "adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "source.properties"}:
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)
    asyncio.run(download_collector_release(tools / "collector.apk", timeout_s=180))
    if not keyboard_apk.is_file() or not keyboard_source.is_file():
        raise ValueError("Provide built ADBKeyboard APK and corresponding pinned source archive")
    shutil.copy2(keyboard_apk, tools / "ADBKeyboard.apk")
    licenses = payload / "licenses"
    licenses.mkdir()
    # GPL dependency distributed as a separate APK with its corresponding source.
    shutil.copy2(keyboard_source, licenses / "ADBKeyboard-source.zip")
    (licenses / "device-assets.txt").write_text(
        "Platform Tools: " + url + "\nSHA1: " + sha1 + "\n" +
        f"Collector {COLLECTOR_VERSION}: https://github.com/LordRosenberg/ClickClick\nSHA256: {COLLECTOR_SHA256}\n" +
        "ADBKeyboard: https://github.com/senzhk/ADBKeyBoard\nSeparate APK, GPL-2.0; corresponding source included.\n", encoding="utf-8")
    return {"platform_tools": {"url": url, "sha1": sha1}, "collector": {"version": COLLECTOR_VERSION, "sha256": COLLECTOR_SHA256},
            "keyboard_sha256": hashlib.sha256(keyboard_apk.read_bytes()).hexdigest()}


def build_payload(destination, *, version, keyboard_apk, keyboard_source):
    if destination.exists():
        raise ValueError("Build destination must be new; do not overwrite an existing payload")
    destination.mkdir(parents=True)
    python, info = prepare_runtime(destination / "runtime", destination.parent)
    for relative in application_files(REPO):
        target = destination / "app" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    # Remove benchmark device-profile entries along with their excluded assets.
    profiles = destination / "app/shared/app_alias_profiles.json"
    profile_data = json.loads(profiles.read_text(encoding="utf-8"))
    profile_data["profiles"] = [p for p in profile_data["profiles"]
                                if not p["id"].startswith(("androidworld", "mobileworld"))]
    write_json(profiles, profile_data)
    assets = device_assets(destination, keyboard_apk, keyboard_source)
    # Private runtime smoke-check: imports use only payload code/dependencies.
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLICKCLICK_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
    run([python, "-I", "-c", "import desktop.main,control_api.main,mcp,av,tomlkit,pystray; print('payload imports OK')"], cwd=destination, env=env)
    env["TIKTOKEN_CACHE_DIR"] = str(destination / "tools/tiktoken")
    run([python, "-I", "-c", "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"], env=env)
    versions = run([python, "-c", "import importlib.metadata,json; print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))"], env=env, capture_output=True).stdout
    write_json(destination / "licenses/dependency-versions.json", json.loads(versions))
    hashes = {}
    for path in destination.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            with path.open("rb") as source:
                hashes[path.relative_to(destination).as_posix()] = hashlib.file_digest(source, "sha256").hexdigest()
    arch = runtime_architecture()
    manifest = {"version": version, "platform": "windows" if sys.platform == "win32" else "macos", "architecture": arch,
                "python": python.relative_to(destination).as_posix(), "python_version": info["version"],
                "files": hashes, "device_assets": assets}
    write_json(destination / "manifest.json", manifest)
    archive = destination.parent / "payload.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=6) as zipped:
        for name in [*sorted(hashes), "manifest.json"]:
            zipped.write(destination / name, name)
    write_json(destination.parent / "payload-inventory.json", manifest)
    return archive, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", required=True)
    p.add_argument("--keyboard-apk", type=Path, required=True)
    p.add_argument("--keyboard-source", type=Path, required=True)
    p.add_argument("--output", type=Path, default=REPO / "dist/desktop")
    args = p.parse_args()
    tag_version("desktop-v" + args.version)
    if sys.platform not in {"win32", "darwin"}:
        raise SystemExit("Build installers on Windows/macOS respectively")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="clickclick-build-", dir=args.output) as raw:
        work = Path(raw)
        archive, manifest = build_payload(work / "payload", version=args.version,
            keyboard_apk=args.keyboard_apk.resolve(), keyboard_source=args.keyboard_source.resolve())
        name = f"ClickClick-{args.version}-{manifest['platform']}-{manifest['architecture']}"
        from desktop.icons import write_app_icon
        icon = write_app_icon(work / ("ClickClick.ico" if sys.platform == "win32" else "ClickClick.icns"))
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", name,
                   "--distpath", args.output, "--workpath", work / "freeze", "--specpath", work,
                   "--paths", REPO, "--icon", icon, "--add-data", str(archive) + os.pathsep + "."]
        if sys.platform == "win32":
            command += ["--onefile", "--console"]
        else:
            command += ["--onedir", "--windowed", "--osx-bundle-identifier", "com.clickclick.installer"]
        run([*command, REPO / "desktop/bootstrap.py"],
            env={**os.environ, "PYINSTALLER_CONFIG_DIR": str(work / "pyinstaller-cache")})
        shutil.copy2(work / "payload-inventory.json", args.output / (name + "-inventory.json"))
        if sys.platform == "darwin":
            run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", args.output / (name + ".app"), args.output / (name + ".zip")])
        output = args.output / (name + (".exe" if sys.platform == "win32" else ".zip"))
        with output.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        (args.output / (name + ".sha256")).write_text(checksum + "  " + output.name + "\n", encoding="utf-8")
        print(json.dumps({"installer": str(output), "sha256": checksum}, indent=2))


if __name__ == "__main__":
    main()
