"""SkillLearner: post-task / on-demand skill authoring into pending patches."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.skills.library import SkillLibrary, default_skills_root, serialize_skill_markdown
from agent.skills.pending import bump_patch_version, stage_pending_patch
from shared.config import Settings, get_settings
from shared.llm_gateway import complete
from shared.schemas import TaskRecord, TaskStatus

logger = logging.getLogger(__name__)

_LEARNER_SYSTEM = """\
You update mobile-app skill documents from a finished task.

Output ONLY a JSON object with keys:
- skip (bool): true if nothing worth writing
- gist (string): one-line summary of the change
- name (string): skill name
- description (string)
- version (string): bumped patch version
- app (string or null): android package or null for generic
- destination (app_core_merge|workflow_merge|workflow_new|candidate): keep as candidate when evidence or scope is uncertain
- target_workflow_id (string): required for workflow_merge
- capability (string): stable intent name for workflow destinations
- category (constraint|hint|fallback|anti_pattern): default hint when uncertain
- evidence (string): concise trace evidence supporting the proposed category
- scope (string): required only for constraint
- trigger (string): required only for fallback
- body (string): full markdown body. Workflows require non-empty Procedure and Verification sections. No YAML frontmatter in body.

Rules:
- Refine an existing workflow when the trace is a variant of that intent.
- Use app_core_merge only for stable app-wide knowledge, never for task-specific steps.
- Use workflow_new only for a distinct stable user intent. Otherwise keep an inactive candidate.
- Keep the skill concise. Do not invent UI that the trace did not show.
- Skills are fallible priors, not step scripts.
- Never promote a rule directly to an active constraint; constraint proposals require operator approval, narrow scope, and observable evidence.
- If category is uncertain, use hint.
"""


def resolve_learner_model(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    override = (getattr(s, "skill_learner_model", None) or "").strip()
    if override:
        return override
    mgr = (s.manager_model or "").strip()
    return mgr or s.default_model


def _compact_trace(task: TaskRecord, *, settings: Settings, max_steps: int = 24) -> list[dict[str, Any]]:
    from shared.db import Database

    db = Database(settings.db_path, read_only=True)
    try:
        rows = db.list_agent_records(task.id, "event")[-max_steps:]
        return [row["payload"] for row in rows]
    finally:
        db.close()


def _pick_target_app(task: TaskRecord, library: SkillLibrary) -> str | None:
    """Select only runtime-grounded App scope; never infer it from task prose."""
    state = task.state
    if state is None:
        return None
    for d in (task.state.frozen_skill_dirs if task.state else []) or []:
        if d.startswith("apps/") and "/" in d:
            return d.split("/", 1)[1]
    return None


def _extract_json(content: str) -> dict[str, Any] | None:
    text = (content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def run_skill_learner(
    task: TaskRecord,
    *,
    settings: Settings | None = None,
    library: SkillLibrary | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run SkillLearner for a task; stage pending patch(es). Returns a result dict."""
    settings = settings or get_settings()
    library = library or SkillLibrary(default_skills_root())

    if not force and not getattr(task.state, "skill_learn", False):
        return {"ok": True, "skipped": True, "reason": "skill_learn=false"}

    outcome = "success" if task.status == TaskStatus.SUCCEEDED else (
        "failure" if task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) else task.status.value
    )
    app = _pick_target_app(task, library)
    app_packs = [
        pack for pack in library.load_all()
        if app and pack.app == app
    ]
    core = next((pack for pack in app_packs if pack.kind == "app_core"), None)
    pack = core
    if pack is None and not app:
        generics = library.list_scoped(allow_dirs=["generic"])
        pack = generics[0] if generics else None

    if pack is None:
        # Create a new app skill draft body from scratch
        name = (app or "generic_learned").replace(".", "_")
        old_text = ""
        target_rel = f"apps/{app}/SKILL.md" if app else f"generic/{name}/SKILL.md"
        current_body = ""
        description = f"Learned from task {task.id[:8]}"
        version = "0.1.0"
    else:
        name = pack.name
        old_text = pack.path.read_text(encoding="utf-8") if pack.path.is_file() else ""
        try:
            target_rel = str(pack.path.resolve().relative_to(library.root))
        except ValueError:
            target_rel = pack.path.name
        current_body = pack.body
        description = pack.description
        version = bump_patch_version(pack.version)
        app = pack.app or app

    user_payload = {
        "instruction": task.instruction,
        "outcome": outcome,
        "failure_reason": task.failure_reason or "",
        "plan": task.plan,
        "steps": _compact_trace(task, settings=settings),
        "current_skill": {
            "name": name,
            "description": description,
            "version": version,
            "app": app,
            "body": current_body,
        },
        "available_modules": [
            {
                "id": module.id,
                "kind": module.kind,
                "capability": module.capability,
                "description": module.description,
                "version": module.version,
            }
            for module in app_packs
        ],
        "guidance": (
            "Bias toward Pitfalls." if outcome == "failure"
            else "Enrich operations/workflows when clearly supported."
        ),
    }
    model = resolve_learner_model(settings)
    try:
        resp = await complete(
            model,
            [
                {"role": "system", "content": _LEARNER_SYSTEM},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("SkillLearner LLM failed for task %s", task.id)
        return {"ok": False, "error": str(exc)}

    data = _extract_json(resp.content or "")
    if not data:
        return {"ok": False, "error": "learner returned non-JSON", "raw": (resp.content or "")[:500]}
    if data.get("skip"):
        return {"ok": True, "skipped": True, "reason": "model_skip", "gist": data.get("gist")}

    new_name = str(data.get("name") or name).strip() or name
    new_desc = str(data.get("description") or description).strip()
    new_ver = str(data.get("version") or version).strip() or version
    new_app = data.get("app", app)
    if new_app in ("", "null"):
        new_app = None
    body = str(data.get("body") or "").strip()
    if not body:
        return {"ok": True, "skipped": True, "reason": "empty_body"}

    category = str(data.get("category") or "hint").strip().lower()
    if category not in {"constraint", "hint", "fallback", "anti_pattern"}:
        category = "hint"
    evidence = str(data.get("evidence") or "").strip()
    scope = str(data.get("scope") or "").strip()
    trigger = str(data.get("trigger") or "").strip()
    destination = str(data.get("destination") or "candidate").strip().lower()
    if destination not in {
        "app_core_merge", "workflow_merge", "workflow_new", "candidate",
    }:
        destination = "candidate"
    if category == "constraint" and (not evidence or not scope):
        return {
            "ok": True, "skipped": True,
            "reason": "constraint_requires_scope_and_evidence",
        }
    if category == "fallback" and not trigger:
        return {"ok": True, "skipped": True, "reason": "fallback_requires_trigger"}

    target_pack = None
    module_kind = "candidate"
    capability = str(data.get("capability") or "").strip()
    if destination == "app_core_merge":
        target_pack = core
        if target_pack is None:
            destination = "candidate"
        else:
            module_kind = "app_core"
    elif destination == "workflow_merge":
        target_id = str(data.get("target_workflow_id") or "").strip()
        target_pack = next(
            (module for module in app_packs if module.id == target_id and module.kind == "workflow"),
            None,
        )
        if target_pack is None:
            destination = "candidate"
        else:
            module_kind = "workflow"
    elif destination == "workflow_new":
        module_kind = "workflow"

    if target_pack is not None:
        new_name = target_pack.id
        new_desc = str(data.get("description") or target_pack.description).strip()
        new_ver = str(data.get("version") or bump_patch_version(target_pack.version)).strip()
        capability = capability or target_pack.capability
        old_text = target_pack.path.read_text(encoding="utf-8")
        target_rel = str(target_pack.path.resolve().relative_to(library.root))
    elif destination == "workflow_new":
        target_rel = f"apps/{new_app or app}/workflows/{new_name}/SKILL.md"
    else:
        module_kind = "candidate"
        target_rel = f"_candidates/{new_app or app or 'generic'}/{new_name}/SKILL.md"

    new_text = serialize_skill_markdown(
        name=new_name,
        description=new_desc,
        version=new_ver,
        app=new_app if isinstance(new_app, str) else app,
        kind=module_kind,  # type: ignore[arg-type]
        capability=capability,
        body=body,
        extra_frontmatter={
            "source": "learner",
            "learner_rule_category": category,
            "learner_rule_evidence": evidence,
            "learner_rule_scope": scope or None,
            "learner_rule_trigger": trigger or None,
            "operator_approval_required": category == "constraint",
            "learner_destination": destination,
        },
    )
    try:
        from agent.skills.library import parse_skill_markdown

        parse_skill_markdown(new_text)
    except ValueError as exc:
        return {"ok": True, "skipped": True, "reason": f"invalid_module:{exc}"}
    pending = stage_pending_patch(
        target_rel=target_rel,
        new_text=new_text,
        gist=str(data.get("gist") or f"learn from {task.id[:8]}")[:240],
        source_task_id=task.id,
        outcome=outcome,
        app=new_app if isinstance(new_app, str) else app,
        root=library.root,
        old_text=old_text,
        review_metadata={
            "destination": destination,
            "module_kind": module_kind,
            "capability": capability,
            "category": category,
            "evidence": evidence,
        },
    )
    return {
        "ok": True,
        "skipped": False,
        "pending_id": pending.id,
        "gist": pending.gist,
        "target": pending.target_rel,
        "category": category,
        "destination": destination,
        "operator_approval_required": category == "constraint",
    }
