"""Deterministic alias resolution and model-visible installed-app discovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from shared.app_resolver import NameResolver, flatten_alias_seed


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


def test_default_seed_resolves_core_system_app_localized_names(tmp_path: Path):
    async def list_packages(_serial):
        return ["com.android.settings", "com.miui.calculator", "com.android.browser"]

    resolver = NameResolver(
        list_packages=list_packages,
        seed_path=Path("shared/app_aliases.json"),
        cache_path=tmp_path / "cache.json",
    )
    assert asyncio.run(resolver.resolve("系统设置", "A")) == "com.android.settings"
    assert asyncio.run(resolver.resolve("计算器", "A")) == "com.miui.calculator"
    assert asyncio.run(resolver.resolve("浏览器", "A")) == "com.android.browser"


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
