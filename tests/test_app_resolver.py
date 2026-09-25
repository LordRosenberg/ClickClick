"""Deterministic alias resolution and model-visible installed-app discovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from shared.app_resolver import (
    NameResolver,
    flatten_alias_candidates,
    flatten_alias_seed,
)

PROFILE_INDEX = Path("shared/app_alias_profiles.json")


def _androidworld_profile() -> dict[str, str]:
    return {
        "manufacturer": "Google",
        "model": "sdk_gphone64_x86_64",
        "sdk": "33",
        "release": "13",
    }


def _xiaomi_profile() -> dict[str, str]:
    return {
        "manufacturer": "Xiaomi",
        "model": "24129PN74C",
        "sdk": "35",
        "release": "15",
    }


def _mobileworld_profile() -> dict[str, str]:
    return {
        "manufacturer": "Google",
        "model": "sdk_gphone64_x86_64",
        "sdk": "34",
        "release": "14",
        "build": "12077443",
    }


def _resolver(tmp_path: Path, packages: list[str], *, seed: dict | None = None) -> NameResolver:
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed or {}, ensure_ascii=False), encoding="utf-8")

    async def list_packages(_serial):
        return list(packages)

    return NameResolver(
        list_packages=list_packages,
        seed_path=seed_path,
        cache_path=tmp_path / "cache.json",
    )


def test_package_passthrough_is_local(tmp_path: Path):
    resolver = _resolver(tmp_path, [])
    assert asyncio.run(resolver.resolve("com.xingin.xhs", "A")) == "com.xingin.xhs"


def test_curated_alias_and_casefold_are_exact(tmp_path: Path):
    resolver = _resolver(
        tmp_path, ["tv.danmaku.bili"],
        seed={"tv.danmaku.bili": {"aliases": ["B站", "bilibili"]}},
    )
    assert asyncio.run(resolver.resolve("BILIBILI", "A")) == "tv.danmaku.bili"
    assert flatten_alias_seed({"小红书": "com.xingin.xhs"})["小红书"] == "com.xingin.xhs"


def test_duplicate_aliases_preserve_ordered_device_variants(tmp_path: Path):
    seed = {
        "com.google.android.deskclock": {"aliases": ["Clock", "时钟"]},
        "com.android.deskclock": {"aliases": ["Clock", "时钟"]},
    }
    assert flatten_alias_candidates(seed)["clock"] == [
        "com.google.android.deskclock",
        "com.android.deskclock",
    ]
    resolver = _resolver(tmp_path, ["com.android.deskclock"], seed=seed)
    assert asyncio.run(resolver.resolve("Clock", "A")) == "com.android.deskclock"
    resolved = asyncio.run(resolver.resolve_with_status("时钟", "A"))
    assert resolved.package == "com.android.deskclock"
    assert resolved.provenance == "curated_alias"


def test_curated_variant_absence_falls_back_to_learned_device_alias(tmp_path: Path):
    resolver = _resolver(
        tmp_path,
        ["vendor.clock"],
        seed={"com.google.android.deskclock": {"aliases": ["Clock"]}},
    )
    assert asyncio.run(resolver.record_explicit_selection("Clock", "vendor.clock", "A"))
    resolved = asyncio.run(resolver.resolve_with_status("Clock", "A"))
    assert resolved.package == "vendor.clock"
    assert resolved.provenance == "learned_alias"


def test_default_seed_resolves_core_system_app_localized_names(tmp_path: Path):
    async def list_packages(_serial):
        return ["com.android.settings", "com.miui.calculator", "com.android.browser"]

    async def describe_device(_serial):
        return _xiaomi_profile()

    resolver = NameResolver(
        list_packages=list_packages,
        seed_path=Path("shared/app_aliases.json"),
        cache_path=tmp_path / "cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_device,
    )
    assert asyncio.run(resolver.resolve("系统设置", "A")) == "com.android.settings"
    assert asyncio.run(resolver.resolve("计算器", "A")) == "com.miui.calculator"
    assert asyncio.run(resolver.resolve("浏览器", "A")) == "com.android.browser"


def test_default_seed_has_no_case_only_duplicate_aliases():
    paths = [Path("shared/app_aliases.json")]
    index = json.loads(PROFILE_INDEX.read_text(encoding="utf-8"))
    paths.extend(PROFILE_INDEX.parent / item["aliases"] for item in index["profiles"])
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for package, entry in data.items():
            aliases = entry.get("aliases", [])
            normalized = [" ".join(alias.strip().casefold().split()) for alias in aliases]
            assert len(normalized) == len(set(normalized)), f"{path}:{package}"


def test_default_seed_selects_androidworld_or_miui_smoke_app_packages(tmp_path: Path):
    aliases = Path("shared/app_aliases.json")

    async def androidworld_packages(_serial):
        return [
            "com.google.android.deskclock",
            "com.google.android.contacts",
            "com.android.camera2",
        ]

    async def describe_androidworld(_serial):
        return _androidworld_profile()

    androidworld = NameResolver(
        list_packages=androidworld_packages,
        seed_path=aliases,
        cache_path=tmp_path / "androidworld-cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_androidworld,
    )
    assert asyncio.run(androidworld.resolve("Clock", "emulator")) == "com.google.android.deskclock"
    assert asyncio.run(androidworld.resolve("联系人", "emulator")) == "com.google.android.contacts"
    assert asyncio.run(androidworld.resolve("相机", "emulator")) == "com.android.camera2"

    async def miui_packages(_serial):
        return ["com.android.deskclock", "com.android.contacts", "com.android.camera"]

    async def describe_miui(_serial):
        return _xiaomi_profile()

    miui = NameResolver(
        list_packages=miui_packages,
        seed_path=aliases,
        cache_path=tmp_path / "miui-cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_miui,
    )
    assert asyncio.run(miui.resolve("时钟", "phone")) == "com.android.deskclock"
    assert asyncio.run(miui.resolve("Contacts", "phone")) == "com.android.contacts"
    assert asyncio.run(miui.resolve("Camera", "phone")) == "com.android.camera"


def test_default_seed_covers_all_androidworld_environment_apps(tmp_path: Path):
    expected = {
        "Settings": "com.android.settings",
        "Clock": "com.google.android.deskclock",
        "Contacts": "com.google.android.contacts",
        "Camera": "com.android.camera2",
        "Phone": "com.google.android.dialer",
        "Chrome": "com.android.chrome",
        "Simple Calendar Pro": "com.simplemobiletools.calendar.pro",
        "Markor": "net.gsantner.markor",
        "AndroidWorld": "com.example.androidworld",
        "Clipper": "ca.zgrs.clipper",
        "Broccoli": "com.flauschcode.broccoli",
        "Pro Expense": "com.arduia.expense",
        "Simple SMS Messenger": "com.simplemobiletools.smsmessenger",
        "OpenTracks": "de.dennisguse.opentracks",
        "Tasks": "org.tasks",
        "Joplin": "net.cozic.joplin",
        "Retro Music": "code.name.monkey.retromusic",
        "Simple Gallery Pro": "com.simplemobiletools.gallery.pro",
        "OsmAnd": "net.osmand",
        "VLC": "org.videolan.vlc",
        "Audio Recorder": "com.dimowner.audiorecorder",
        "Files": "com.google.android.documentsui",
        "MiniWoB++": "com.google.androidenv.miniwob",
        "Simple Draw Pro": "com.simplemobiletools.draw.pro",
    }

    async def packages(_serial):
        return list(expected.values())

    async def describe_device(_serial):
        return _androidworld_profile()

    resolver = NameResolver(
        list_packages=packages,
        seed_path=Path("shared/app_aliases.json"),
        cache_path=tmp_path / "androidworld-all-apps-cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_device,
    )
    for alias, package in expected.items():
        assert asyncio.run(resolver.resolve(alias.swapcase(), "emulator")) == package
    assert asyncio.run(resolver.selected_profile_ids("emulator")) == ["androidworld_api33"]


def test_mobileworld_profile_covers_all_gui_task_apps(tmp_path: Path):
    expected = {
        "Settings": "com.android.settings",
        "Clock": "com.google.android.deskclock",
        "Contacts": "com.google.android.contacts",
        "Camera": "com.android.camera2",
        "Chrome": "com.android.chrome",
        "Calendar": "org.fossify.calendar",
        "Files": "com.google.android.documentsui",
        "Gallery": "gallery.photomanager.picturegalleryapp.imagegallery",
        "Taodian": "com.testmall.app",
        "Mattermost": "com.mattermost.rnbeta",
        "Mastodon": "org.joinmastodon.android.mastodon",
        "Mail": "com.gmailclone",
        "Messages": "com.google.android.apps.messaging",
        "Maps": "com.google.android.apps.maps",
        "Docreader": "at.tomtasche.reader",
    }

    async def packages(_serial):
        return list(expected.values())

    async def describe_device(_serial):
        return _mobileworld_profile()

    resolver = NameResolver(
        list_packages=packages,
        seed_path=Path("shared/app_aliases.json"),
        cache_path=tmp_path / "mobileworld-all-apps-cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_device,
    )
    for alias, package in expected.items():
        assert asyncio.run(resolver.resolve(alias.swapcase(), "mobileworld")) == package
    assert asyncio.run(resolver.selected_profile_ids("mobileworld")) == ["mobileworld_api34"]


def test_unmatched_device_does_not_receive_system_profile_aliases(tmp_path: Path):
    async def packages(_serial):
        return ["com.android.deskclock", "com.google.android.deskclock"]

    async def describe_device(_serial):
        return {
            "manufacturer": "Other",
            "model": "Unknown",
            "sdk": "35",
            "release": "15",
        }

    resolver = NameResolver(
        list_packages=packages,
        seed_path=Path("shared/app_aliases.json"),
        cache_path=tmp_path / "unmatched-cache.json",
        profile_index_path=PROFILE_INDEX,
        describe_device=describe_device,
    )
    assert asyncio.run(resolver.resolve_with_status("Clock", "other")).status == "miss"
    assert asyncio.run(resolver.selected_profile_ids("other")) == []


def test_alias_miss_does_not_start_hidden_llm_or_choose(tmp_path: Path):
    resolver = _resolver(tmp_path, ["com.xingin.xhs"])
    assert asyncio.run(resolver.resolve("小红书", "A")) is None


def test_search_is_bounded_deterministic_and_has_provenance(tmp_path: Path):
    resolver = _resolver(
        tmp_path,
        ["com.other.app", "com.xingin.xhs", "com.xingin.creator"],
        seed={"小红书": "com.xingin.xhs"},
    )
    hits = asyncio.run(resolver.search_installed_apps("xingin", "A", limit=2))
    assert [hit["package"] for hit in hits] == ["com.xingin.xhs", "com.xingin.creator"]
    assert len(hits) == 2
    assert all(hit["provenance"] for hit in hits)


def test_search_no_match_is_empty(tmp_path: Path):
    resolver = _resolver(tmp_path, ["com.xingin.xhs"])
    assert asyncio.run(resolver.search_installed_apps("wechat", "A")) == []


def test_invalid_selection_is_not_recorded(tmp_path: Path):
    resolver = _resolver(tmp_path, ["com.xingin.xhs"])
    assert not asyncio.run(
        resolver.record_explicit_selection("小红书", "com.not.installed", "A")
    )
    assert not (tmp_path / "cache.json").exists()


def test_valid_explicit_selection_writes_per_device_cache(tmp_path: Path):
    resolver = _resolver(tmp_path, ["com.xingin.xhs"])
    assert asyncio.run(
        resolver.record_explicit_selection("小红书", "com.xingin.xhs", "A")
    )
    assert asyncio.run(resolver.resolve("小红书", "A")) == "com.xingin.xhs"
    assert asyncio.run(resolver.resolve("小红书", "B")) is None
    data = json.loads((tmp_path / "cache.json").read_text(encoding="utf-8"))
    assert data == {"A": {"小红书": "com.xingin.xhs"}}


def test_validate_installed_membership(tmp_path: Path):
    resolver = _resolver(tmp_path, ["com.xingin.xhs"])
    assert asyncio.run(resolver.validate_installed("com.xingin.xhs", "A"))
    assert not asyncio.run(resolver.validate_installed("com.bad", "A"))


def test_typed_resolution_validates_packages_and_reports_provenance(tmp_path: Path):
    resolver = _resolver(
        tmp_path, ["com.xingin.xhs"], seed={"小红书": "com.xingin.xhs"},
    )
    curated = asyncio.run(resolver.resolve_with_status("小红书", "A"))
    assert curated.status == "resolved"
    assert curated.provenance == "curated_alias"
    assert curated.package == "com.xingin.xhs"
    invalid = asyncio.run(resolver.resolve_with_status("com.not.installed", "A"))
    assert invalid.status == "miss" and invalid.package is None

    assert asyncio.run(
        resolver.record_explicit_selection("Red Book", "com.xingin.xhs", "A")
    )
    learned = asyncio.run(resolver.resolve_with_status("red book", "A"))
    assert learned.status == "resolved"
    assert learned.provenance == "learned_alias"
