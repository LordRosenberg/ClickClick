"""Deterministic app alias/cache resolution and installed-app discovery."""

from __future__ import annotations

import json
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from shared.config import Settings, get_settings
from shared.schemas import (
    AppResolutionProvenance,
    AppResolutionResult,
    AppResolutionStatus,
    ResolverMissTicket,
    ResolverTicketValidation,
)

# A callable that returns the installed package list for a serial. Injected
# so this module does not import the driver (avoids a circular dependency).
ListPackagesFn = Callable[[str | None], Awaitable[list[str]]]
DescribeDeviceFn = Callable[[str | None], Awaitable[dict[str, str]]]

_LOGGER = logging.getLogger(__name__)
DEFAULT_APP_ALIAS_SEED_PATH = Path("shared/app_aliases.json")
DEFAULT_APP_ALIAS_PROFILE_INDEX_PATH = Path("shared/app_alias_profiles.json")


def normalize_app_query(value: str) -> str:
    """Normalize a display-name query for resolution and ticket binding."""
    return " ".join((value or "").strip().casefold().split())


def _looks_like_package(app: str) -> bool:
    """True if `app` looks like a fully-qualified package name."""
    return "." in app and len(app.split(".")) >= 2


def flatten_alias_candidates(data: dict[str, Any]) -> dict[str, list[str]]:
    """Flatten curated seed into casefold(alias) → ordered packages.

    Accepts:
    - Legacy flat: ``{"小红书": "com.xingin.xhs"}``
    - Package-centric: ``{"com.xingin.xhs": {"aliases": ["小红书", "红书"]}}``
      or ``{"com.xingin.xhs": ["小红书", "红书"]}``

    Repeating an alias under multiple package keys is intentional: AndroidWorld
    and OEM devices often ship the same human-facing App under different
    packages.  Declaration order is the deterministic fallback order; the
    resolver prefers the first candidate actually installed on the device.
    """
    seed: dict[str, list[str]] = {}

    def add(alias: str, package: str) -> None:
        normalized = alias.strip().casefold()
        candidate = package.strip()
        if not normalized or not candidate:
            return
        packages = seed.setdefault(normalized, [])
        if candidate not in packages:
            packages.append(candidate)

    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, str) and value.strip():
            # Legacy flat OR package key mistaken as alias — treat as alias→pkg.
            add(key, value)
            continue
        aliases: list[str] = []
        if isinstance(value, list):
            aliases = [str(a) for a in value if str(a).strip()]
        elif isinstance(value, dict):
            raw = value.get("aliases") or value.get("names") or []
            if isinstance(raw, str):
                aliases = [raw]
            elif isinstance(raw, list):
                aliases = [str(a) for a in raw if str(a).strip()]
        pkg = key.strip()
        if not _looks_like_package(pkg):
            # Non-package key with nested aliases is ambiguous; skip.
            continue
        for alias in aliases:
            add(alias, pkg)
        # Also allow resolving by the package string itself via passthrough,
        # not via seed — no need to insert pkg as an alias.
    return seed


def flatten_alias_seed(data: dict[str, Any]) -> dict[str, str]:
    """Return the first declared package per alias for legacy callers."""
    return {
        alias: packages[0]
        for alias, packages in flatten_alias_candidates(data).items()
        if packages
    }


def load_alias_seed_file(path: Path | str) -> dict[str, str]:
    """Load and flatten a curated seed JSON file."""
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return flatten_alias_seed(data)


def _load_alias_candidate_file(path: Path | str) -> dict[str, list[str]]:
    """Load all curated package candidates for each alias."""
    p = Path(path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return flatten_alias_candidates(data)


def _merge_candidate_seeds(
    *seeds: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Merge alias candidates in priority order without duplicates."""
    merged: dict[str, list[str]] = {}
    for seed in seeds:
        for alias, candidates in seed.items():
            packages = merged.setdefault(alias, [])
            for package in candidates:
                if package not in packages:
                    packages.append(package)
    return merged


def _profile_matches(match: dict[str, Any], device: dict[str, str]) -> bool:
    """Return whether all exact/regex fields match one device profile."""
    for field, expected in match.items():
        if field.endswith("_regex"):
            key = field.removesuffix("_regex")
            try:
                if re.fullmatch(
                    str(expected), str(device.get(key, "")), re.IGNORECASE,
                ) is None:
                    return False
            except re.error:
                return False
            continue
        actual = str(device.get(field, "")).strip().casefold()
        if actual != str(expected).strip().casefold():
            return False
    return bool(match)


class NameResolver:
    """Resolve exact local hits and expose bounded installed candidates."""

    def __init__(
        self,
        *,
        list_packages: ListPackagesFn,
        seed_path: str | Path,
        cache_path: str | Path,
        profile_index_path: str | Path | None = None,
        describe_device: DescribeDeviceFn | None = None,
    ) -> None:
        self._list_packages = list_packages
        self._seed_path = Path(seed_path)
        self._cache_path = Path(cache_path)
        self._profile_index_path = (
            Path(profile_index_path) if profile_index_path is not None else None
        )
        self._describe_device = describe_device

        # Lazy-loaded.
        self._seed: dict[str, list[str]] | None = None
        self._profiles: list[dict[str, Any]] | None = None
        self._device_seeds: dict[str, dict[str, list[str]]] = {}
        self._device_profile_ids: dict[str, list[str]] = {}
        # {serial: {display_name: package}}. Key "" holds serial-less entries.
        self._cache: dict[str, dict[str, str]] | None = None

    async def resolve(self, app: str, serial: str | None) -> str | None:
        """Return the package for `app` (display name or package), or None.

        - Package-looking input is returned as-is (passthrough).
        - Curated seed and learned cache hits resolve locally.
        - Misses return ``None`` so Executor can call ``search_installed_apps``.
        """
        if not app:
            return None
        if _looks_like_package(app):
            return app

        key = (serial or "").strip()
        target = app.strip()
        if not target:
            return None

        # Stage 1: curated seed (global, casefold), then learned cache (per-serial).
        seed = await self._seed_for(serial)
        folded = target.casefold()
        candidates = seed.get(folded, [])
        if candidates:
            installed = set(await self.installed_packages(serial))
            for package in candidates:
                if package in installed:
                    return package
        cache = self._load_cache()
        serial_cache = cache.get(key, {})
        if target in serial_cache:
            return serial_cache[target]
        # Casefold learned-cache keys for display-name hits.
        for cached_name, pkg in serial_cache.items():
            if cached_name.casefold() == folded:
                return pkg

        # Preserve the original local-resolution behavior when installed-app
        # enumeration is temporarily unavailable. The driver still validates
        # package membership before dispatch.
        if candidates:
            return candidates[0]

        return None

    async def resolve_with_status(
        self, app: str, serial: str | None,
    ) -> AppResolutionResult:
        """Resolve and validate an app with explicit provenance and generation."""
        requested = (app or "").strip()
        normalized = normalize_app_query(requested)
        generation = self.resolver_generation()
        if not requested:
            return AppResolutionResult(
                requested_name=requested,
                normalized_query=normalized,
                status=AppResolutionStatus.MISS,
                resolver_generation=generation,
            )

        packages = set(await self.installed_packages(serial))
        if _looks_like_package(requested):
            if requested in packages:
                return AppResolutionResult(
                    requested_name=requested,
                    normalized_query=normalized,
                    status=AppResolutionStatus.RESOLVED,
                    resolver_generation=generation,
                    package=requested,
                    provenance=AppResolutionProvenance.EXACT_PACKAGE,
                )
            return AppResolutionResult(
                requested_name=requested,
                normalized_query=normalized,
                status=AppResolutionStatus.MISS,
                resolver_generation=generation,
            )

        seed = await self._seed_for(serial)
        candidates = seed.get(normalized, [])
        package = next((item for item in candidates if item in packages), None)
        provenance: AppResolutionProvenance | None = None
        if package:
            provenance = AppResolutionProvenance.CURATED_ALIAS
        else:
            cache = self._load_cache().get((serial or "").strip(), {})
            for cached_name, cached_package in cache.items():
                if normalize_app_query(cached_name) == normalized:
                    package = cached_package
                    provenance = AppResolutionProvenance.LEARNED_ALIAS
                    break
        if package and package in packages:
            return AppResolutionResult(
                requested_name=requested,
                normalized_query=normalized,
                status=AppResolutionStatus.RESOLVED,
                resolver_generation=generation,
                package=package,
                provenance=provenance,
            )
        return AppResolutionResult(
            requested_name=requested,
            normalized_query=normalized,
            status=AppResolutionStatus.MISS,
            resolver_generation=generation,
        )

    def resolver_generation(self) -> str:
        """Return a compact generation that changes with curated/learned mappings."""
        payload = {
            "seed": self._load_seed(),
            "profiles": self._load_profiles(),
            "cache": self._load_cache(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()[:16]

    async def installed_packages(self, serial: str | None) -> list[str]:
        try:
            return sorted(set(await self._list_packages(serial)))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("app-resolver: list_packages failed: %s", exc)
            return []

    async def search_installed_apps(
        self, query: str, serial: str | None, *, limit: int = 12,
    ) -> list[dict[str, str]]:
        """Return deterministic bounded candidates without choosing one."""
        packages = await self.installed_packages(serial)
        folded = (query or "").strip().casefold()
        seed = await self._seed_for(serial)
        cache = self._load_cache().get((serial or "").strip(), {})
        aliases_by_package: dict[str, list[tuple[str, str]]] = {}
        for alias, candidates in sorted(seed.items()):
            for package in candidates:
                aliases_by_package.setdefault(package, []).append((alias, "curated_alias"))
        for alias, package in sorted(cache.items(), key=lambda item: item[0].casefold()):
            aliases_by_package.setdefault(package, []).append((alias, "learned_cache"))

        ranked: list[tuple[tuple[int, int, str], dict[str, str]]] = []
        for package in packages:
            aliases = aliases_by_package.get(package, [])
            haystacks = [package.casefold(), *(alias.casefold() for alias, _ in aliases)]
            exact = folded and folded in haystacks
            contains = not folded or any(folded in value for value in haystacks)
            token_match = folded and any(part.startswith(folded) for part in package.casefold().split("."))
            if not (exact or contains or token_match):
                continue
            alias, provenance = aliases[0] if aliases else ("", "installed_package")
            score = (0 if exact else 1 if token_match else 2, len(package), package)
            ranked.append((score, {
                "package": package, "alias": alias, "provenance": provenance,
            }))
        ranked.sort(key=lambda item: item[0])
        return [item for _, item in ranked[:max(1, min(int(limit), 50))]]

    async def validate_installed(self, package: str, serial: str | None) -> bool:
        return package in set(await self.installed_packages(serial))

    async def record_explicit_selection(
        self, display_name: str, package: str, serial: str | None,
    ) -> bool:
        if not display_name or _looks_like_package(display_name):
            return False
        if not await self.validate_installed(package, serial):
            return False
        self._write_back((serial or "").strip(), display_name.strip(), package)
        return True

    # --- local mapping table ------------------------------------------------

    async def selected_profile_ids(self, serial: str | None) -> list[str]:
        """Return the deterministic device-profile overlays selected for a device."""
        await self._seed_for(serial)
        return list(self._device_profile_ids.get((serial or "").strip(), []))

    async def _seed_for(self, serial: str | None) -> dict[str, list[str]]:
        key = (serial or "").strip()
        cached = self._device_seeds.get(key)
        if cached is not None:
            return cached

        common = self._load_seed()
        profiles = self._load_profiles()
        if not profiles or self._describe_device is None:
            self._device_profile_ids[key] = []
            self._device_seeds[key] = common
            return common

        try:
            device = await self._describe_device(serial)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("app-resolver: describe_device failed: %s", exc)
            device = {}
        matched = [
            profile
            for profile in profiles
            if _profile_matches(profile["match"], device)
        ]
        overlays = [profile["seed"] for profile in matched]
        selected = _merge_candidate_seeds(*overlays, common)
        self._device_profile_ids[key] = [profile["id"] for profile in matched]
        self._device_seeds[key] = selected
        return selected

    def _load_profiles(self) -> list[dict[str, Any]]:
        if self._profiles is not None:
            return self._profiles
        profiles: list[dict[str, Any]] = []
        index_path = self._profile_index_path
        if index_path is None or not index_path.exists():
            self._profiles = profiles
            return profiles
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            raw_profiles = data.get("profiles", []) if isinstance(data, dict) else []
            root = index_path.parent.resolve()
            for raw in raw_profiles:
                if not isinstance(raw, dict):
                    continue
                profile_id = str(raw.get("id", "")).strip()
                match = raw.get("match")
                alias_ref = str(raw.get("aliases", "")).strip()
                if not profile_id or not isinstance(match, dict) or not alias_ref:
                    continue
                alias_path = (index_path.parent / alias_ref).resolve()
                if alias_path != root and root not in alias_path.parents:
                    continue
                profiles.append({
                    "id": profile_id,
                    "match": {str(k): str(v) for k, v in match.items()},
                    "seed": _load_alias_candidate_file(alias_path),
                })
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "app-resolver: failed to load profile index %s: %s",
                index_path,
                exc,
            )
            profiles = []
        self._profiles = profiles
        return profiles

    def _load_seed(self) -> dict[str, list[str]]:
        if self._seed is not None:
            return self._seed
        seed: dict[str, list[str]] = {}
        try:
            seed = _load_alias_candidate_file(self._seed_path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("app-resolver: failed to load seed %s: %s", self._seed_path, exc)
            seed = {}
        self._seed = seed
        return seed

    def _load_cache(self) -> dict[str, dict[str, str]]:
        if self._cache is not None:
            return self._cache
        cache: dict[str, dict[str, str]] = {}
        try:
            if self._cache_path.exists():
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for serial, mapping in data.items():
                        if isinstance(mapping, dict):
                            clean = {
                                str(k): str(v)
                                for k, v in mapping.items()
                                if isinstance(k, str) and isinstance(v, str)
                            }
                            cache[str(serial)] = clean
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("app-resolver: failed to load cache %s: %s", self._cache_path, exc)
        self._cache = cache
        return cache

    def _write_back(self, serial: str, display_name: str, package: str) -> None:
        cache = self._load_cache()
        mapping = cache.setdefault(serial, {})
        if mapping.get(display_name) == package:
            return
        mapping[display_name] = package
        self._flush_cache()

    def _flush_cache(self) -> None:
        assert self._cache is not None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._cache_path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("app-resolver: failed to flush cache %s: %s", self._cache_path, exc)


@dataclass
class _TicketRecord:
    token: str
    task_id: str
    device_id: str
    subgoal_id: str
    normalized_query: str
    resolver_generation: str
    expires_at_monotonic_ms: float
    consumed: bool = False


class ResolverTicketStore:
    """Harness-owned, one-use resolver-miss authorization store.

    Raw ticket values remain only in this in-memory store and model-visible
    tool messages. Diagnostics expose only a short SHA-256 fingerprint.
    """

    def __init__(
        self,
        *,
        ttl_s: float = 90.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_s = max(0.001, float(ttl_s))
        self._clock = clock or time.monotonic
        self._records: dict[str, _TicketRecord] = {}

    @staticmethod
    def fingerprint(token: str | None) -> str:
        if not token:
            return ""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]

    def issue(
        self,
        *,
        task_id: str,
        device_id: str,
        subgoal_id: str,
        query: str,
        resolver_generation: str,
    ) -> ResolverMissTicket:
        token = secrets.token_urlsafe(32)
        expires = (self._clock() + self.ttl_s) * 1000.0
        self._records[token] = _TicketRecord(
            token=token,
            task_id=str(task_id or ""),
            device_id=str(device_id or ""),
            subgoal_id=str(subgoal_id or ""),
            normalized_query=normalize_app_query(query),
            resolver_generation=str(resolver_generation or ""),
            expires_at_monotonic_ms=expires,
        )
        return ResolverMissTicket(
            resolution_ticket=token,
            expires_at_monotonic_ms=expires,
            resolver_generation=str(resolver_generation or ""),
        )

    def consume(
        self,
        ticket: str | None,
        *,
        task_id: str,
        device_id: str,
        subgoal_id: str,
    ) -> ResolverTicketValidation:
        token = str(ticket or "")
        fingerprint = self.fingerprint(token)
        if not token:
            return ResolverTicketValidation(
                accepted=False,
                reason="absent",
            )
        record = self._records.get(token)
        if record is None:
            return ResolverTicketValidation(
                accepted=False, reason="unknown", ticket_fingerprint=fingerprint,
            )
        if record.consumed:
            return ResolverTicketValidation(
                accepted=False, reason="consumed", ticket_fingerprint=fingerprint,
            )
        if self._clock() * 1000.0 >= record.expires_at_monotonic_ms:
            return ResolverTicketValidation(
                accepted=False, reason="expired", ticket_fingerprint=fingerprint,
            )
        checks = (
            (record.task_id == str(task_id or ""), "task_mismatch"),
            (record.device_id == str(device_id or ""), "device_mismatch"),
            (record.subgoal_id == str(subgoal_id or ""), "subgoal_mismatch"),
        )
        for accepted, reason in checks:
            if not accepted:
                return ResolverTicketValidation(
                    accepted=False,
                    reason=reason,  # type: ignore[arg-type]
                    ticket_fingerprint=fingerprint,
                )
        record.consumed = True
        return ResolverTicketValidation(
            accepted=True, reason="accepted", ticket_fingerprint=fingerprint,
        )

    def clear_task(self, task_id: str) -> None:
        key = str(task_id or "")
        self._records = {
            token: record for token, record in self._records.items()
            if record.task_id != key
        }

    def clear_other_subgoals(self, task_id: str, subgoal_id: str) -> None:
        task = str(task_id or "")
        subgoal = str(subgoal_id or "")
        self._records = {
            token: record for token, record in self._records.items()
            if record.task_id != task or record.subgoal_id == subgoal
        }


def build_default_resolver(settings: Settings | None = None) -> "NameResolver":
    """Construct a `NameResolver` from `Settings`, wiring `adb.list_packages_async`.

    Kept here (rather than in the driver) so the resolver stays decoupled from
    the `AndroidDriver` class; the driver receives the built resolver via DI.
    """
    s = settings or get_settings()
    from driver import adb  # local import to avoid import cycles at module load

    return NameResolver(
        list_packages=adb.list_packages_async,
        seed_path=DEFAULT_APP_ALIAS_SEED_PATH,
        cache_path=s.app_resolver_cache_path_resolved,
        profile_index_path=DEFAULT_APP_ALIAS_PROFILE_INDEX_PATH,
        describe_device=adb.device_profile_async,
    )
