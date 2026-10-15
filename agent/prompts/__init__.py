"""Load and render the Planner / Reviewer / Executor prompts.

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


def render_role_system(role: str, *, hitl_enabled: bool = False, hitl_mode: str = "managed") -> str:
    if role not in {"planner", "reviewer", "executor"}:
        raise ValueError("unknown role")
    from agent.interaction_policy import interaction_profile
    profile = interaction_profile(enabled=hitl_enabled, mode=hitl_mode)
    mode = "enabled" if profile.enabled else "disabled"
    block = _read(f"revisable_{role}_{mode}.md").rstrip("\n")
    if profile.enabled and role != "reviewer":
        block = _render(block, {"tool_form": profile.tool_form})
    role_text = _render(_read(f"revisable_{role}.md"), {"decision_policy": block})
    return role_text + "\n\n" + _render(_read("revisable_common.md"), {})


def render_planner_system(*, hitl_enabled: bool = False, hitl_mode: str = "managed") -> str:
    return render_role_system("planner", hitl_enabled=hitl_enabled, hitl_mode=hitl_mode)


def render_reviewer_system(*, hitl_enabled: bool = False, hitl_mode: str = "managed") -> str:
    return render_role_system("reviewer", hitl_enabled=hitl_enabled, hitl_mode=hitl_mode)


def render_executor_system(*, hitl_enabled: bool = False, hitl_mode: str = "managed") -> str:
    return render_role_system("executor", hitl_enabled=hitl_enabled, hitl_mode=hitl_mode)


def render_skill_index(skill_index: str) -> str:
    """Render the stable, discoverable Skill-id index."""
    body = (skill_index or "").strip() or "(no skills in scope)"
    return (
        "GENERIC SKILL INDEX (optional; load only an exact listed id).\n"
        "A Skill may be irrelevant. Current evidence wins. Empty is valid.\n\n"
        f"{body}\n"
    )
