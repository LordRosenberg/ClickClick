"""Load and render the Planner / Reviewer / Executor prompt templates.

Templates live in `agent/prompts/*.md` as plain markdown with optional
jinja2-style `{# ... #}` comments and `{{ var }}` placeholders. We render
with a tiny regex pass (no jinja2 dependency) — the templates use only
`{{ var }}` substitution.

Decision Context keeps the static role/tool prefix and exact relevant Skill
bodies stable. Dynamic role packets are assembled by the caller. The render
functions are STRICT: a placeholder in
the template that the caller did not supply raises `KeyError`; a
caller-supplied var that the template does not reference raises
`ValueError`. This surfaces template drift instead of silently dropping
content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parent

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_COMMENT_RE = re.compile(r"\{\#.*?\#\}", re.DOTALL)


def _read(name: str) -> str:
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def _render(template: str, vars_: dict[str, Any]) -> str:
    """Render `{{ var }}` placeholders with strict drift detection.

    Raises:
        KeyError: the template references a var the caller did not supply.
        ValueError: the caller supplied a var the template does not reference.
    """
    template = _COMMENT_RE.sub("", template)
    placeholders = set(_VAR_RE.findall(template))
    missing = placeholders - vars_.keys()
    if missing:
        raise KeyError(f"template references unknown vars: {sorted(missing)}")
    unused = vars_.keys() - placeholders
    if unused:
        raise ValueError(f"caller passed vars not in template: {sorted(unused)}")

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        val = vars_[key]
        if val is None:
            return ""
        return str(val)

    return _VAR_RE.sub(repl, template)


def render_planner_system() -> str:
    """Render the task-independent rolling Planner policy."""
    return _render(_read("planner_system.md"), {})


def render_reviewer_scope_system() -> str:
    """Render the UI-independent Reviewer scope policy."""
    return _render(_read("reviewer_scope_system.md"), {})


def render_reviewer_system() -> str:
    """Render the evidence-bound Reviewer boundary policy."""
    return _render(_read("reviewer_system.md"), {})


def render_executor_system() -> str:
    """Render the Executor system prompt (bucket S): static rules only."""
    return _render(_read("executor_system.md"), {})
