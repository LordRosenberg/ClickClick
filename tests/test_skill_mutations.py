"""Filesystem skill mutation / draft lifecycle tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.library import (
    SkillConflictError,
    SkillLibrary,
    SkillNotFoundError,
    SkillPathJailError,
    parse_skill_markdown,
    serialize_skill_markdown,
)


def test_serialize_parse_round_trip():
    text = serialize_skill_markdown(
        name="demo_round",
        description="Round trip",
        version="0.2.0",
        app="com.demo",
        tags=["a"],
        triggers=["go"],
        body="Shared\n\n## Executor notes\nHint B\n",
        extra_frontmatter={"source": "authored"},
    )
    pack = parse_skill_markdown(text)
    assert pack.name == "demo_round"
    assert pack.app == "com.demo"
    assert "Shared" in pack.section_for("manager")
    assert "Hint B" in pack.section_for("executor")


def test_create_update_delete_canonical(tmp_path: Path):
    lib = SkillLibrary(tmp_path)
    pack = lib.create_canonical(
        name="generic_hello",
        description="hello",
        body="p1\n\n## Executor notes\ne1\n",
    )
    assert pack.path.exists()
    assert (tmp_path / "generic" / "generic_hello" / "SKILL.md").exists()

    with pytest.raises(SkillConflictError):
        lib.create_canonical(name="generic_hello", body="x")

    updated = lib.update_skill("generic_hello", body="p2\n\n## Executor notes\ne1\n")
    assert "p2" in updated.body
    assert "e1" in updated.section_for("executor")

    lib.delete_canonical("generic_hello")
    assert lib.get("generic_hello") is None
    with pytest.raises(SkillNotFoundError):
        lib.delete_canonical("generic_hello")


def test_path_jail_rejects_bad_id(tmp_path: Path):
    lib = SkillLibrary(tmp_path)
    with pytest.raises(SkillPathJailError):
        lib.create_canonical(name="../escape", body="x")
    with pytest.raises(SkillPathJailError):
        serialize_skill_markdown(name="foo/../bar", body="x")
