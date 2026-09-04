"""Tests for filesystem skill library (SKILL.md layout)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.library import SkillLibrary, classify_skill_rules, parse_skill_markdown

SAMPLE = """---
name: demo_hello
description: Demo skill for hello flows.
version: 0.1.0
app: com.example.app
kind: app_core
tags: [a, b]
triggers:
  - hello
---

Shared ops.

## Executor notes
Don't tap blind
"""


def test_parse_frontmatter_and_role_filter():
    pack = parse_skill_markdown(SAMPLE, path=Path("SKILL.md"))
    assert pack.name == "demo_hello"
    assert pack.id == "demo_hello"
    assert pack.description.startswith("Demo skill")
    assert pack.version == "0.1.0"
    assert pack.app == "com.example.app"
    assert "Shared ops" in pack.section_for("manager")
    assert "Don't tap blind" not in pack.section_for("manager")
    assert "Don't tap blind" in pack.section_for("executor")
    assert "Shared ops" in pack.section_for("executor")


def test_miss_get_returns_none(tmp_path: Path):
    lib = SkillLibrary(tmp_path)
    dest = tmp_path / "generic" / "x" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text(
        "---\nname: generic_x\ndescription: x\nversion: 0.1.0\nkind: generic\n---\n\nok\n",
        encoding="utf-8",
    )
    assert lib.get("nope") is None
    assert lib.get("generic_x") is not None


def test_app_scoping(tmp_path: Path):
    generic = tmp_path / "generic" / "g"
    apps = tmp_path / "apps" / "tv.danmaku.bili"
    generic.mkdir(parents=True)
    apps.mkdir(parents=True)
    (generic / "SKILL.md").write_text(
        "---\nname: generic_g\ndescription: g\nversion: 0.1.0\nkind: generic\n---\n\ng\n",
        encoding="utf-8",
    )
    (apps / "SKILL.md").write_text(
        "---\nname: bilibili\ndescription: bili\nversion: 0.1.0\n"
        "app: tv.danmaku.bili\nkind: app_core\n---\n\na\n",
        encoding="utf-8",
    )
    other = tmp_path / "apps" / "other.app"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(
        "---\nname: other\ndescription: o\nversion: 0.1.0\napp: other.app\nkind: app_core\n---\n\no\n",
        encoding="utf-8",
    )

    lib = SkillLibrary(tmp_path)
    scoped = lib.list_scoped(app="tv.danmaku.bili")
    names = {p.name for p in scoped}
    assert names == {"generic_g", "bilibili"}
    assert "other" not in names

    narrowed = lib.list_scoped(app="tv.danmaku.bili", allow_dirs=["apps/tv.danmaku.bili"])
    assert {p.name for p in narrowed} == {"bilibili"}

    index = lib.index_summaries(allow_dirs=["generic", "apps/tv.danmaku.bili"])
    assert "bilibili" in index
    assert "bili" in index


def test_unclassified_skill_body_has_no_inferred_rules():
    rules = classify_skill_rules("## Common operations\n- open search")
    assert rules == []


def test_explicit_constraint_requires_scope_and_evidence():
    with pytest.raises(ValueError, match="scope and evidence"):
        classify_skill_rules("## Constraints\n- Never use a suggestion")
    rules = classify_skill_rules(
        "## Constraints\n- Use explicit search | scope: search input | evidence: trace-1"
    )
    assert rules[0].category == "constraint"
    assert rules[0].scope == "search input"


def test_fallback_requires_trigger():
    with pytest.raises(ValueError, match="trigger"):
        classify_skill_rules("## Fallbacks\n- use screenshot")
    rules = classify_skill_rules(
        "## Fallbacks\n- use screenshot | trigger: accessibility tree is sparse"
    )
    assert rules[0].category == "fallback"
