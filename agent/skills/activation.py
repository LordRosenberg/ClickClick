"""Task-scoped routing facts; active prompt knowledge is not semantic adoption."""
import json

from agent.skills.library import parse_skill_markdown
from agent.tool_registry import stable_hash


def candidate_activation(store, text, *, max_requests=300):
    pack = parse_skill_markdown(text)
    expected = stable_hash(pack.section_for("executor"))
    refs = []
    for trace in store.db.list_traces(store.task_id):
        for key in ("initial_request_ref", "request_ref"):
            ref = trace.payload.get(key)
            if isinstance(ref, str) and ref not in refs:
                refs.append(ref)
    planner = executor = known_executor = errors = 0
    catalog = read = active = False
    examples = []
    for ref in refs[:max_requests]:
        try:
            request = json.loads(store.artifacts.read_text(ref))
        except (ValueError, OSError):
            errors += 1
            continue
        role = request.get("role")
        if role == "planner":
            planner += 1
            for message in request.get("messages", []):
                body = message.get("content", "")
                if not isinstance(body, str):
                    continue
                if body.startswith("WORKFLOW CATALOG") and f'"id":"{pack.id}"' in body:
                    catalog = True
                if message.get("role") == "tool" and f"[loaded skill:{pack.id}@" in body:
                    read = True
        elif role == "executor":
            executor += 1
            if isinstance(request.get("active_skills"), list):
                known_executor += 1
                matches = [s for s in request["active_skills"] if isinstance(s, dict)
                    and s.get("skill_id") == pack.id and s.get("content_hash") == expected]
                if matches:
                    active = True
                    if len(examples) < 2:
                        examples.append({"request_ref":ref,"skill_id":pack.id,"content_hash":expected})
    stages = store.records("stage")
    selections = [r for r in stages if isinstance(r["payload"].get("skill_ids"), list)]
    selected = any(pack.id in r["payload"]["skill_ids"] for r in selections)
    complete = len(refs) <= max_requests and errors == 0
    fact = lambda yes, observed: True if yes else (False if observed and complete else None)
    return {"skill_id":pack.id,"kind":pack.kind,"executor_projection_hash":expected,
        "planner_catalog_exposed":fact(catalog,planner), "planner_read":fact(read,planner),
        "stage_selected":(True if selected else (False if selections and len(selections) == len(stages) else None)) if pack.kind == "workflow" else None,
        "executor_active_exact":fact(active,known_executor and known_executor == executor),
        "requests": {"planner":planner,"executor":executor,"known_executor_context":known_executor,
            "omitted":max(0,len(refs)-max_requests),"unreadable":errors},
        "examples":examples,"mechanism_adoption":"unassessed",
        "policy":"Selection and exact prompt activation are facts, not proof of mechanism use or causal benefit; inspect native actions. Null means unknown."}
