"""Filesystem skill packs: directory + SKILL.md with YAML frontmatter.

Runtime source of truth for filesystem skills (loaded by ``AgentSession``).
Pending Learner patches under ``_pending/`` are reviewed through the pending
patch API and are not loaded into the runtime Skill index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

Role = Literal["decision", "planner", "reviewer", "executor"]
RuleCategory = Literal["constraint", "hint", "fallback", "anti_pattern"]
SkillKind = Literal["generic", "app_core", "workflow", "candidate"]
VerifiedActionTemplate = Literal["tap_capture_key", "tap_then_key"]

_SKILL_FILENAMES = frozenset({"skill.md", "SKILL.md"})
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_ROLE_NOTE_HEADINGS = {
    "executor notes": "executor",
    "executor note": "executor",
    "decision notes": "decision",
    "decision note": "decision",
}
_RULE_HEADINGS: dict[str, RuleCategory] = {
    "constraints": "constraint",
    "hints": "hint",
    "fallbacks": "fallback",
    "anti-patterns": "anti_pattern",
    "anti patterns": "anti_pattern",
}


@dataclass(frozen=True)
class SkillRule:
    category: RuleCategory = "hint"
    text: str = ""
    scope: str = ""
    evidence: str = ""
    trigger: str = ""


@dataclass(frozen=True)
class VerifiedAction:
    """Validated App-owned recipe metadata, never rendered as Skill prose."""

    id: str
    template: VerifiedActionTemplate
    key: Literal["back", "media_pause"]
    purpose: str
    delay_ms: int = 0


class SkillConflictError(ValueError):
    """Raised when a skill name/id already exists (HTTP 409)."""


class SkillPathJailError(ValueError):
    """Raised when a write would escape the skills root."""


class SkillNotFoundError(LookupError):
    """Raised when a skill is missing on disk."""


@dataclass
class SkillPack:
    """One loaded skill directory / SKILL.md."""

    name: str
    path: Path
    description: str = ""
    version: str = "0.1.0"
    app: str | None = None
    kind: SkillKind = "generic"
    capability: str = ""
    tags: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    source: str = "authored"  # authored | mined | promoted | learner
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""  # full markdown after frontmatter
    rules: list[SkillRule] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Alias of ``name`` for call sites that still say skill_id."""
        return self.name

    def scope_key(self) -> str:
        """Side-store / allow-dir key: ``generic`` or ``apps/<pkg>``."""
        if self.app:
            return f"apps/{self.app}"
        try:
            # Infer from path when under a library root layout.
            parts = self.path.resolve().parts
            if "apps" in parts:
                i = parts.index("apps")
                if i + 1 < len(parts):
                    return f"apps/{parts[i + 1]}"
            if "generic" in parts:
                return "generic"
        except Exception:  # noqa: BLE001
            pass
        return "generic"

    def section_for(self, role: Role) -> str:
        """Shared body with the other role's notes section stripped."""
        body = filter_body_for_role(
            self.body, role, partitioned=self.frontmatter.get("role_sections") is True,
        )
        return body

    def rule_categories(self) -> list[str]:
        return list(dict.fromkeys(rule.category for rule in self.rules))

    @property
    def active(self) -> bool:
        return self.kind != "candidate"

    @property
    def device_profiles(self) -> list[str]:
        return list(self.frontmatter.get("device_profiles") or [])

    @property
    def verified_actions(self) -> list[VerifiedAction]:
        return _parse_verified_actions(self.frontmatter.get("verified_actions", []))

    def summary_line(self) -> str:
        desc = (self.description or "").strip().replace("\n", " ")
        if len(desc) > 160:
            desc = desc[:157] + "..."
        bits = [self.name]
        bits.append(f"kind={self.kind}")
        if desc:
            bits.append(desc)
        elif self.app:
            bits.append(f"app={self.app}")
        return " — ".join(bits) if desc else " | ".join(bits)


def default_skills_root() -> Path:
    """Skills directory: ``CLICKCLICK_SKILLS_DIR`` or repo-root ``skills/``."""
    import os

    override = (os.environ.get("CLICKCLICK_SKILLS_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "skills"


def _is_skill_filename(path: Path) -> bool:
    return path.name in _SKILL_FILENAMES or path.name.lower() == "skill.md"


def filter_body_for_role(body: str, role: Role, *, partitioned: bool = False) -> str:
    """Drop decision/Executor-only note sections for the other role."""
    parts = re.split(r"(?m)^(##\s+.+)$", body or "")
    if len(parts) <= 1:
        return (body or "").strip()
    out: list[str] = []
    if parts[0].strip():
        out.append(parts[0].rstrip())
    i = 1
    while i < len(parts) - 1:
        heading = parts[i].strip()
        content = parts[i + 1]
        key = heading.lstrip("#").strip().lower()
        note_role = _ROLE_NOTE_HEADINGS.get(key)
        allowed = note_role is None or note_role == role or (
            note_role == "decision" and role in {"planner", "reviewer"}
        )
        if partitioned and key == "execution":
            allowed = role == "executor"
        elif partitioned and key == "verification":
            allowed = role != "planner"
        if allowed:
            out.append(heading)
            if content.strip():
                out.append(content.rstrip())
        # else: omit other role's notes
        i += 2
    return "\n\n".join(out).strip()


def _metadata_from_rule_text(text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("scope", "evidence", "trigger"):
        match = re.search(rf"(?:^|[|;])\s*{key}\s*[:=]\s*([^|;]+)", text, re.IGNORECASE)
        if match:
            metadata[key] = match.group(1).strip()
    return metadata


def classify_skill_rules(body: str, frontmatter: dict[str, Any] | None = None) -> list[SkillRule]:
    """Classify only explicit rule sections or structured rule metadata."""
    rules: list[SkillRule] = []
    structured = (frontmatter or {}).get("rules")
    if isinstance(structured, list):
        for raw in structured:
            if not isinstance(raw, dict):
                continue
            category = str(raw.get("category") or "hint").strip().lower()
            if category not in {"constraint", "hint", "fallback", "anti_pattern"}:
                category = "hint"
            rule = SkillRule(
                category=category,  # type: ignore[arg-type]
                text=str(raw.get("text") or "").strip(),
                scope=str(raw.get("scope") or "").strip(),
                evidence=str(raw.get("evidence") or "").strip(),
                trigger=str(raw.get("trigger") or "").strip(),
            )
            _validate_rule(rule)
            rules.append(rule)

    parts = re.split(r"(?m)^#{2,3}\s+(.+?)\s*$", body or "")
    for index in range(1, len(parts), 2):
        heading = parts[index].strip().casefold()
        category = _RULE_HEADINGS.get(heading)
        if category is None:
            continue
        content = parts[index + 1] if index + 1 < len(parts) else ""
        entries = [
            re.sub(r"^[-*]\s+", "", line).strip()
            for line in content.splitlines()
            if re.match(r"^\s*[-*]\s+\S", line)
        ]
        for entry in entries:
            meta = _metadata_from_rule_text(entry)
            rule = SkillRule(category=category, text=entry, **meta)
            _validate_rule(rule)
            rules.append(rule)
    return rules


def _validate_rule(rule: SkillRule) -> None:
    if not rule.text:
        raise ValueError("skill rule text is required")
    if rule.category == "constraint" and (not rule.scope or not rule.evidence):
        raise ValueError("constraint rules require scope and evidence metadata")
    if rule.category == "fallback" and not rule.trigger:
        raise ValueError("fallback rules require trigger metadata")


_VERIFIED_ACTION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,127}$")
_VERIFIED_ACTION_FIELDS = frozenset({
    "id", "template", "key", "purpose", "delay_ms",
})


def _parse_verified_actions(raw_actions: Any) -> list[VerifiedAction]:
    if raw_actions in (None, []):
        return []
    if not isinstance(raw_actions, list):
        raise ValueError("verified_actions must be a list")
    parsed: list[VerifiedAction] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(raw_actions):
        if not isinstance(raw, dict):
            raise ValueError(f"verified_actions[{position}] must be a mapping")
        extra = set(raw) - _VERIFIED_ACTION_FIELDS
        if extra:
            raise ValueError(
                f"verified_actions[{position}] contains unsupported fields: "
                + ", ".join(sorted(str(value) for value in extra))
            )
        action_id = str(raw.get("id") or "").strip()
        template = str(raw.get("template") or "").strip()
        key = str(raw.get("key") or "").strip()
        purpose = " ".join(str(raw.get("purpose") or "").split())
        raw_delay = raw.get("delay_ms", 0)
        if not _VERIFIED_ACTION_ID_RE.fullmatch(action_id):
            raise ValueError(f"verified_actions[{position}] has invalid id")
        if action_id in seen_ids:
            raise ValueError(f"duplicate verified action id: {action_id}")
        seen_ids.add(action_id)
        if template not in {"tap_capture_key", "tap_then_key"}:
            raise ValueError(f"unknown verified action template: {template or '<missing>'}")
        if template == "tap_capture_key" and key != "back":
            raise ValueError("tap_capture_key verified actions require key: back")
        if template == "tap_then_key" and key != "media_pause":
            raise ValueError("tap_then_key verified actions require key: media_pause")
        if isinstance(raw_delay, bool) or not isinstance(raw_delay, int):
            raise ValueError(f"verified_actions[{position}] delay_ms must be an integer")
        if not 0 <= raw_delay <= 1000:
            raise ValueError(f"verified_actions[{position}] delay_ms must be between 0 and 1000")
        if template == "tap_capture_key" and raw_delay:
            raise ValueError("tap_capture_key does not accept delay_ms")
        if not purpose:
            raise ValueError(f"verified_actions[{position}] requires purpose")
        parsed.append(VerifiedAction(
            id=action_id,
            template=template,  # type: ignore[arg-type]
            key=key,  # type: ignore[arg-type]
            purpose=purpose,
            delay_ms=raw_delay,
        ))
    return parsed


def _section_body(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        body or "",
    )
    return (match.group(1) if match else "").strip()


def _validate_module(pack: SkillPack) -> None:
    verified_actions = _parse_verified_actions(
        pack.frontmatter.get("verified_actions", [])
    )
    if verified_actions and (not pack.app or pack.kind not in {"app_core", "workflow"}):
        raise ValueError(
            "verified_actions may only be declared by an active App core or workflow skill"
        )
    if pack.kind == "candidate":
        return
    if pack.kind == "app_core" and not pack.app:
        raise ValueError("app_core skills require app metadata")
    if pack.kind != "workflow":
        return
    missing: list[str] = []
    if not pack.app:
        missing.append("app")
    if not pack.description:
        missing.append("description")
    if not str(pack.frontmatter.get("version") or "").strip():
        missing.append("version")
    if not pack.capability:
        missing.append("capability")
    if missing:
        raise ValueError("workflow metadata missing: " + ", ".join(missing))
    deprecated = {"surface", "surfaces", "triggers"} & pack.frontmatter.keys()
    if deprecated:
        raise ValueError(
            "workflow metadata contains removed fields: " + ", ".join(sorted(deprecated))
        )
    if not _section_body(pack.body, "Procedure"):
        raise ValueError("workflow requires a non-empty Procedure section")
    if not _section_body(pack.body, "Verification"):
        raise ValueError("workflow requires a non-empty Verification section")


def parse_skill_markdown(text: str, *, path: Path | None = None) -> SkillPack:
    """Parse frontmatter + Markdown body into a ``SkillPack``."""
    path = path or Path("<memory>")
    fm: dict[str, Any] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            raise ValueError(f"skill frontmatter must be a mapping: {path}")
        body = m.group(2) or ""

    # Name comes from frontmatter, with the SKILL.md parent directory as fallback.
    name = str(fm.get("name") or "").strip()
    if not name:
        if _is_skill_filename(path) and path.parent.name not in ("", ".", "/"):
            name = path.parent.name
        else:
            name = path.stem
    if not name:
        raise ValueError(f"skill missing name: {path}")

    app_raw = fm.get("app", None)
    app = None if app_raw in (None, "", "null") else str(app_raw)

    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    triggers = fm.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]

    raw_kind = str(fm.get("kind") or "").strip().lower()
    if raw_kind not in {"generic", "app_core", "workflow", "candidate"}:
        raise ValueError(f"skill requires explicit current kind: {path}")
    kind: SkillKind = raw_kind  # type: ignore[assignment]

    description = str(fm.get("description") or "").strip()
    profiles = fm.get("device_profiles", [])
    if not isinstance(profiles, list) or any(
        not isinstance(value, str) or not value.strip() for value in profiles
    ):
        raise ValueError("device_profiles must be a list of nonempty profile IDs")
    version = str(fm.get("version") or "0.1.0").strip() or "0.1.0"

    pack = SkillPack(
        name=name,
        path=path,
        description=description,
        version=version,
        app=app,
        kind=kind,
        capability=str(fm.get("capability") or "").strip(),
        tags=[str(t) for t in tags],
        triggers=[str(t) for t in triggers],
        source=str(fm.get("source") or "authored"),
        frontmatter=fm,
        body=body,
        rules=classify_skill_rules(body, fm),
    )
    _validate_module(pack)
    return pack


def serialize_skill_markdown(
    *,
    name: str,
    description: str = "",
    version: str = "0.1.0",
    app: str | None = None,
    kind: SkillKind = "generic",
    capability: str = "",
    tags: list[str] | None = None,
    triggers: list[str] | None = None,
    body: str = "",
    extra_frontmatter: dict[str, Any] | None = None,
) -> str:
    """Serialize structured fields to YAML frontmatter + Markdown body."""
    sid = (name or "").strip()
    if not sid:
        raise ValueError("skill name is required")
    if "/" in sid or "\\" in sid or ".." in sid:
        raise SkillPathJailError(f"invalid skill name: {sid!r}")

    fm: dict[str, Any] = {
        "name": sid,
        "description": (description or "").strip(),
        "version": (version or "0.1.0").strip() or "0.1.0",
    }
    if app not in (None, "", "null"):
        fm["app"] = str(app)
    fm["kind"] = kind
    if capability:
        fm["capability"] = str(capability).strip()
    if tags:
        fm["tags"] = [str(t) for t in tags]
    if triggers:
        fm["triggers"] = [str(t) for t in triggers]
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            if k in ("name", "id"):
                continue
            fm[k] = v

    return (
        "---\n"
        + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + (body or "").rstrip()
        + "\n"
    )


def _safe_dirname(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_").replace("..", "_")


class SkillLibrary:
    """Load and query filesystem skill packs."""

    def __init__(self, root: Path | None = None, *, device_profiles: Iterable[str] | None = None) -> None:
        self.root = (root or default_skills_root()).resolve()
        # None is the authoring/admin view; an empty set is an unknown device.
        self.device_profiles = None if device_profiles is None else frozenset(device_profiles)

    def for_device(self, profiles: Iterable[str]) -> SkillLibrary:
        return SkillLibrary(self.root, device_profiles=profiles)

    def iter_files(self) -> Iterable[Path]:
        if not self.root.is_dir():
            return []
        seen: set[Path] = set()
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            if path.name.upper() == "README.MD" or path.name == "README.md":
                continue
            is_skill_md = _is_skill_filename(path)
            if not is_skill_md:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path

    def load_all(self, *, include_candidates: bool = False) -> list[SkillPack]:
        packs: list[SkillPack] = []
        seen_ids: dict[str, Path] = {}
        for path in self.iter_files():
            try:
                pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)
            except Exception:  # noqa: BLE001
                continue
            if not (pack.active or include_candidates):
                continue
            if (self.device_profiles is not None and pack.device_profiles
                    and not self.device_profiles.intersection(pack.device_profiles)):
                continue
            previous = seen_ids.setdefault(pack.id, path)
            if previous != path:
                raise ValueError(
                    f"duplicate active skill id {pack.id!r}: {previous} and {path}"
                )
            packs.append(pack)
        return packs

    def lint_authored_modules(self) -> list[str]:
        """Return actionable authoring errors without hiding parse failures."""
        errors: list[str] = []
        seen_ids: dict[str, Path] = {}
        app_cores: dict[str, Path] = {}
        for path in self.iter_files():
            try:
                pack = parse_skill_markdown(path.read_text(encoding="utf-8"), path=path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
                continue
            if pack.source != "authored" or not pack.active:
                continue
            previous_path = seen_ids.setdefault(pack.id, path)
            if previous_path != path:
                errors.append(f"{path}: duplicate skill id also used by {previous_path}")
            if not pack.rules:
                errors.append(f"{path}: authored active skill has no classified rules")
            if pack.kind == "app_core" and _section_body(pack.body, "Procedure"):
                errors.append(f"{path}: app_core must move task procedures into workflows")
            if pack.kind == "app_core" and pack.app:
                previous_core = app_cores.setdefault(pack.app, path)
                if previous_core != path:
                    errors.append(
                        f"{path}: app {pack.app} already has app_core at {previous_core}"
                    )
            relative = path.resolve().relative_to(self.root)
            if pack.kind == "app_core":
                expected = Path("apps") / str(pack.app) / "core" / "SKILL.md"
                if relative != expected:
                    errors.append(f"{path}: app_core must live at {expected}")
            elif pack.kind == "workflow":
                expected_prefix = Path("apps") / str(pack.app) / "workflows"
                if relative.parent.parent != expected_prefix:
                    errors.append(
                        f"{path}: workflow must live under {expected_prefix}/<workflow-id>"
                    )
        return errors

    def get(self, skill_id: str, *, include_candidates: bool = False) -> SkillPack | None:
        key = (skill_id or "").strip()
        for pack in self.load_all(include_candidates=include_candidates):
            if pack.name == key or pack.id == key:
                return pack
        return None

    def packages_with_skills(self) -> set[str]:
        """Package names that have at least one non-draft skill under ``apps/<pkg>/``."""
        found: set[str] = set()
        for pack in self.load_all():
            rel = self._rel(pack)
            parts = rel.parts
            if parts and parts[0] == "apps" and len(parts) >= 2:
                found.add(parts[1])
            elif pack.app:
                found.add(pack.app)
        return found

    def list_scoped(
        self,
        *,
        app: str | None = None,
        allow_dirs: list[str] | None = None,
    ) -> list[SkillPack]:
        packs = self.load_all()
        scoped: list[SkillPack] = []
        for pack in packs:
            if allow_dirs is not None:
                if not self._in_allow_dirs(pack, allow_dirs):
                    continue
            elif not self._in_default_scope(pack, app):
                continue
            scoped.append(pack)
        return scoped

    def search(
        self,
        query: str = "",
        *,
        app: str | None = None,
        limit: int = 20,
    ) -> list[SkillPack]:
        q = (query or "").strip().casefold()
        app_f = (app or "").strip()
        packs = self.load_all()
        hits: list[SkillPack] = []
        for pack in packs:
            pack_app = pack.app or ""
            rel = self._rel(pack)
            parts = rel.parts
            if parts and parts[0] == "apps" and len(parts) >= 2:
                pack_app = parts[1]
            if app_f and pack_app != app_f and (pack.app or "") != app_f:
                continue
            if q:
                blob = " ".join(
                    [
                        pack.name,
                        pack.description,
                        pack_app or "",
                        pack.app or "",
                        " ".join(pack.tags),
                        " ".join(pack.triggers),
                        pack.summary_line(),
                    ]
                ).casefold()
                if q not in blob and not any(
                    t.casefold() in q or q in t.casefold() for t in pack.triggers
                ):
                    continue
            hits.append(pack)
            if len(hits) >= limit:
                break
        return hits

    def app_workflows(self, app: str) -> list[SkillPack]:
        """Return every active workflow owned by one exact package."""
        exact_app = (app or "").strip()
        return sorted(
            (
                pack
                for pack in self.load_all()
                if pack.kind == "workflow" and pack.app == exact_app
            ),
            key=lambda pack: pack.id,
        )

    def planner_catalog_apps(self, preferred: Iterable[str] = ()) -> list[str]:
        """Prioritize known Apps without hiding discoverable workflow metadata.

        Instructions can name a capability without naming its App. Exact App
        aliases therefore rank the index; they cannot be its visibility gate.
        Workflow bodies still require an explicit owned selection.
        """
        return list(dict.fromkeys([
            *(app.strip() for app in preferred if app.strip()),
            *sorted(self.packages_with_skills()),
        ]))

    def workflow_catalog(self, apps: Iterable[str]) -> list[dict[str, str]]:
        """Return compact Planner cards for the requested Apps."""
        cards: list[dict[str, str]] = []
        seen: set[str] = set()
        for app in apps:
            package = (app or "").strip()
            if not package or package in seen:
                continue
            seen.add(package)
            for workflow in self.app_workflows(package):
                description = " ".join(workflow.description.split())
                if len(description) > 96:
                    description = description[:93] + "..."
                cards.append({
                    "id": workflow.id,
                    "app": package,
                    "capability": workflow.capability,
                    "description": description,
                })
        return cards

    def app_core(self, app: str) -> SkillPack | None:
        matches = [
            pack for pack in self.load_all()
            if pack.kind == "app_core" and pack.app == (app or "").strip()
        ]
        return matches[0] if len(matches) == 1 else None

    def index_summaries(
        self,
        *,
        app: str | None = None,
        allow_dirs: list[str] | None = None,
    ) -> str:
        lines = [
            p.summary_line()
            for p in self.list_scoped(app=app, allow_dirs=allow_dirs)
            if p.kind != "workflow"
        ]
        return "\n".join(lines) if lines else "(no skills in scope)"

    def _rel(self, pack: SkillPack) -> Path:
        try:
            return pack.path.resolve().relative_to(self.root)
        except ValueError:
            return Path(pack.path.name)

    def _in_default_scope(self, pack: SkillPack, app: str | None) -> bool:
        rel = self._rel(pack)
        parts = rel.parts
        if not parts:
            return False
        if parts[0] == "generic":
            return True
        if parts[0] == "apps" and len(parts) >= 2:
            return bool(app) and parts[1] == app
        if pack.app and app and pack.app == app:
            return True
        if pack.app is None and app is None:
            return True
        return False

    def _in_allow_dirs(self, pack: SkillPack, allow_dirs: list[str]) -> bool:
        if not allow_dirs:
            return False
        rel = self._rel(pack).as_posix()
        scope = pack.scope_key()
        for d in allow_dirs:
            prefix = d.strip().strip("/")
            if not prefix:
                continue
            if rel == prefix or rel.startswith(prefix + "/"):
                return True
            if scope == prefix or scope.startswith(prefix + "/"):
                return True
        return False

    def _assert_under_root(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SkillPathJailError(f"path escapes skills root: {path}") from exc
        return resolved

    def _canonical_dest(
        self, name: str, app: str | None, kind: SkillKind = "generic",
    ) -> Path:
        if kind == "candidate":
            candidate_scope = _safe_dirname(str(app or "generic"))
            dest_dir = self.root / "_candidates" / candidate_scope / _safe_dirname(name)
        elif app:
            app_clean = str(app).strip()
            if not app_clean or "/" in app_clean or "\\" in app_clean or ".." in app_clean:
                raise SkillPathJailError(f"invalid app id: {app!r}")
            if kind == "app_core":
                dest_dir = self.root / "apps" / app_clean / "core"
            elif kind == "workflow":
                dest_dir = self.root / "apps" / app_clean / "workflows" / _safe_dirname(name)
            else:
                dest_dir = self.root / "apps" / app_clean
        else:
            dest_dir = self.root / "generic" / _safe_dirname(name)
        dest = dest_dir / "SKILL.md"
        return self._assert_under_root(dest)

    def list_for_api(
        self,
        *,
        app: str | None = None,
        q: str = "",
    ) -> list[SkillPack]:
        packs = self.load_all(include_candidates=True)
        out: list[SkillPack] = []
        q_cf = (q or "").strip().casefold()
        app_f = (app or "").strip()
        for pack in packs:
            if app_f:
                pack_app = pack.app or ""
                rel = self._rel(pack)
                parts = rel.parts
                if parts and parts[0] == "apps" and len(parts) >= 2:
                    pack_app = parts[1]
                if pack_app != app_f and (pack.app or "") != app_f:
                    continue
            if q_cf:
                blob = " ".join(
                    [
                        pack.name,
                        pack.description,
                        pack.app or "",
                        " ".join(pack.tags),
                        " ".join(pack.triggers if pack.kind != "workflow" else []),
                    ]
                ).casefold()
                if q_cf not in blob:
                    continue
            out.append(pack)
        return out

    def create_canonical(
        self,
        *,
        skill_id: str = "",
        name: str = "",
        description: str = "",
        version: str = "0.1.0",
        app: str | None = None,
        tags: list[str] | None = None,
        triggers: list[str] | None = None,
        body: str = "",
        source: str = "authored",
        kind: SkillKind = "generic",
        capability: str = "",
    ) -> SkillPack:
        sid = (name or skill_id or "").strip()
        if not sid:
            raise ValueError("skill name is required")
        if self.get(sid, include_candidates=True) is not None:
            raise SkillConflictError(f"skill already exists: {sid}")
        dest = self._canonical_dest(sid, app, kind)
        if dest.exists():
            raise SkillConflictError(f"destination occupied: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = serialize_skill_markdown(
            name=sid,
            description=description,
            version=version,
            app=app,
            kind=kind,
            capability=capability,
            tags=tags,
            triggers=triggers,
            body=body,
            extra_frontmatter={"source": source},
        )
        dest.write_text(text, encoding="utf-8")
        return parse_skill_markdown(text, path=dest)

    def update_skill(
        self,
        skill_id: str,
        *,
        app: str | None = None,
        tags: list[str] | None = None,
        triggers: list[str] | None = None,
        description: str | None = None,
        version: str | None = None,
        body: str | None = None,
        kind: SkillKind | None = None,
        capability: str | None = None,
        extra_frontmatter: dict[str, Any] | None = None,
    ) -> SkillPack:
        pack = self.get(skill_id, include_candidates=True)
        if pack is None:
            raise SkillNotFoundError(skill_id)
        path = self._assert_under_root(pack.path)
        fm = dict(pack.frontmatter)
        if app is not None:
            if app in ("", "null"):
                fm.pop("app", None)
            else:
                fm["app"] = app
        if tags is not None:
            fm["tags"] = list(tags)
        if triggers is not None:
            fm["triggers"] = list(triggers)
        if description is not None:
            fm["description"] = description
        if version is not None:
            fm["version"] = version
        if kind is not None:
            fm["kind"] = kind
        if capability is not None:
            fm["capability"] = capability
        if extra_frontmatter:
            fm.update(extra_frontmatter)
        fm["name"] = pack.name
        new_body = pack.body if body is None else body
        text = serialize_skill_markdown(
            name=pack.name,
            description=str(fm.get("description") or pack.description),
            version=str(fm.get("version") or pack.version),
            app=fm.get("app"),
            kind=str(fm.get("kind") or "generic"),  # type: ignore[arg-type]
            capability=str(fm.get("capability") or ""),
            tags=fm.get("tags") or [],
            triggers=(fm.get("triggers") or []) if str(fm.get("kind") or "generic") != "workflow" else [],
            body=new_body,
            extra_frontmatter={
                k: v
                for k, v in fm.items()
                if k not in (
                    "name", "id", "app", "kind", "capability",
                    "tags", "triggers", "description", "version",
                )
            },
        )
        new_app = None if fm.get("app") in (None, "", "null") else str(fm.get("app"))
        new_kind = str(fm.get("kind") or "generic")
        new_dest = self._canonical_dest(pack.name, new_app, new_kind)  # type: ignore[arg-type]
        if new_dest != path:
            if new_dest.exists() and new_dest.resolve() != path.resolve():
                raise SkillConflictError(f"destination occupied: {new_dest}")
            new_dest.parent.mkdir(parents=True, exist_ok=True)
            new_dest.write_text(text, encoding="utf-8")
            if path.exists() and path.resolve() != new_dest.resolve():
                path.unlink()
                try:
                    if path.parent != self.root and not any(path.parent.iterdir()):
                        path.parent.rmdir()
                except OSError:
                    pass
            return parse_skill_markdown(text, path=new_dest)
        path.write_text(text, encoding="utf-8")
        return parse_skill_markdown(text, path=path)

    def delete_canonical(self, skill_id: str) -> None:
        pack = self.get(skill_id, include_candidates=True)
        if pack is None:
            raise SkillNotFoundError(skill_id)
        path = self._assert_under_root(pack.path)
        parent = path.parent
        path.unlink()
        if _is_skill_filename(path):
            try:
                if parent != self.root and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
