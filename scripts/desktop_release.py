"""Validate desktop tags, assemble a complete release feed, and publish in CI."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.versions import Version, TARGETS, MAX_INSTALLER_BYTES, artifact_name, tag_version


def assemble(directory, version):
    Version(version)
    platforms = {}
    for target in sorted(TARGETS):
        name = artifact_name(version, target)
        matches = list(directory.rglob(name))
        if len(matches) != 1:
            raise ValueError("Missing/duplicate required installer: " + name)
        path = matches[0]
        inventory = json.loads(path.with_name(path.stem + "-inventory.json").read_text(encoding="utf-8"))
        if inventory["version"] != version or target != inventory["platform"] + "-" + inventory["architecture"]:
            raise ValueError("Installer inventory version/target mismatch")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        expected = path.with_suffix(".sha256").read_text(encoding="utf-8").strip().split()
        if expected != [digest, name] or not 0 < path.stat().st_size <= MAX_INSTALLER_BYTES:
            raise ValueError("Installer checksum/size mismatch")
        platforms[target] = {"name": name, "size": path.stat().st_size, "sha256": digest}
    result = {"schema": 1, "version": version, "platforms": platforms}
    (directory / "desktop-update.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def publish(directory, tag):
    version = tag_version(tag)
    assemble(directory, version)
    repository = os.environ["GITHUB_REPOSITORY"]
    base = ["gh", "release"]
    viewed = subprocess.run([*base, "view", tag, "--repo", repository, "--json", "isDraft"], capture_output=True, text=True)
    if viewed.returncode == 0:
        if not json.loads(viewed.stdout)["isDraft"]:
            raise ValueError("Release already published; published desktop versions must be immutable")
    else:
        command = [*base, "create", tag, "--repo", repository, "--draft", "--title", "ClickClick " + version,
                   "--generate-notes", "--verify-tag"]
        if Version(version).pre:
            command.append("--prerelease")
        subprocess.run(command, check=True)
    files = [p for p in directory.rglob("*") if p.is_file() and
             (p.name == "desktop-update.json" or p.name.startswith("ClickClick-"))]
    subprocess.run([*base, "upload", tag, "--repo", repository, "--clobber", *map(str, files)], check=True)
    subprocess.run([*base, "edit", tag, "--repo", repository, "--draft=false", "--latest=false"], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tag")
    p.add_argument("--version")
    p.add_argument("--directory", type=Path)
    p.add_argument("--publish", action="store_true")
    args = p.parse_args()
    version = tag_version(args.tag) if args.tag else tag_version("desktop-v" + (args.version or ""))
    Version(version)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("version=" + version + "\n")
    if args.publish:
        if not args.tag:
            p.error("Publishing requires a desktop tag")
        publish(args.directory, args.tag)
    elif args.directory:
        assemble(args.directory, version)
    print(json.dumps({"version": version, "prerelease": bool(Version(version).pre)}))


if __name__ == "__main__":
    main()
