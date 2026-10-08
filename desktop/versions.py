"""Desktop release naming and SemVer ordering, without build/runtime dependencies."""

from functools import total_ordering
import re

OFFICIAL_REPO = "LordRosenberg/ClickClick"
TAG_PREFIX = "desktop-v"
TARGETS = {"windows-x86_64", "macos-x86_64", "macos-aarch64"}
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")


@total_ordering
class Version:
    def __init__(self, value):
        match = _VERSION.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError("Desktop version must be SemVer, e.g. 0.2.0 or 0.2.0-rc.1")
        self.core = tuple(int(p) for p in match.groups()[:3])
        self.pre = tuple((match[4] or "").split(".")) if match[4] else ()
        if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in self.pre):
            raise ValueError("Numeric prerelease identifiers must not have leading zeros")

    def __eq__(self, other):
        return self.core == other.core and self.pre == other.pre

    def __lt__(self, other):
        if self.core != other.core:
            return self.core < other.core
        if not self.pre or not other.pre:
            return bool(self.pre) and not other.pre
        for a, b in zip(self.pre, other.pre):
            if a == b:
                continue
            if a.isdigit() and b.isdigit():
                return int(a) < int(b)
            if a.isdigit() != b.isdigit():
                return a.isdigit()
            return a < b
        return len(self.pre) < len(other.pre)


def tag_version(tag):
    if not tag.startswith(TAG_PREFIX):
        raise ValueError("Desktop tags must start with desktop-v")
    value = tag[len(TAG_PREFIX):]
    Version(value)
    if "+" in value:
        raise ValueError("Desktop release tags must omit build metadata")
    return value


def artifact_name(version, target):
    Version(version)
    if target not in TARGETS or "+" in version:
        raise ValueError("Unsupported desktop release target/version")
    return f"ClickClick-{version}-{target}" + (".exe" if target.startswith("windows-") else ".zip")
