"""Detect a changed app build and hand scoped source history to the Learner."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent.revisable.recall import source_id
from agent.revisable.store import TaskStore
from agent.skills.learning import TERMINAL, LearningBudget, LearningStopped, digest
from agent.skills.library import parse_skill_markdown
from agent.tool_registry import stable_hash
from shared.llm_gateway import GatewayError

VERSION_AUDIT_KIND = "skill_version_audit"
VERSION_AUDIT_RUN_KIND = "skill_version_audit_run"
MAX_WORKFLOWS = 3


@dataclass(frozen=True)
class TraceStep:
    event: dict
    before: dict
    after: dict | None
    receipt: dict
    executor_request_ref: str

    @property
    def number(self):
        return self.event["payload"]["step"]

    @property
    def same_app_post(self):
        return self.after is not None and self.after["payload"].get("app") == self.before["payload"].get("app")

    @property
    def paired(self):
        return self.same_app_post and self.receipt.get("dispatch_succeeded") is True


@dataclass
class WorkflowEvidence:
    key: str
    identity: dict
    build: dict
    projection: str
    delivery: dict
    target: str
    steps: dict[int, TraceStep] = field(default_factory=dict)


def _artifact(store, reference):
    if not isinstance(reference, str) or not reference or store.artifacts is None:
        return {}
    try:
        value = json.loads(store.artifacts.verify(reference))
    except (ValueError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _accepted_basis(store, tick, package, observations):
    observation_id = tick.get("observation_id")
    row = observations.get(observation_id)
    if not row or tick.get("basis_observation_id") != observation_id or row["payload"].get("app") != package:
        return None
    if tick.get("observation_accepted") is True:
        return row
    tree = _artifact(store, tick.get("tree_ref"))
    if tick.get("observation_accepted") is None and tree.get("accepted") is True and tree.get("app_id") == package:
        return row
    return None


def _delivery_requests(store, tick):
    """The tick describes active scope; a returned native request confirms delivery."""
    for round_record in reversed(tick.get("agent_rounds") or []):
        if round_record.get("role") != "executor" or not round_record.get("response_ref"):
            continue
        request_ref = round_record.get("request_ref")
        request = _artifact(store, request_ref)
        if (request.get("kind") == "agent_round_request" and request.get("role") == "executor"
                and request.get("round_id") == round_record.get("round_id")):
            yield request_ref, request


def _projections(store):
    for row in store.records("skill_snapshot"):
        snapshot = row["payload"]
        metadata = snapshot.get("frontmatter") or {}
        if not isinstance(metadata, dict) or metadata.get("kind") != "workflow" or not metadata.get("app"):
            continue
        try:
            text = "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n" + snapshot["body"]
            pack = parse_skill_markdown(text, path=Path(snapshot["relative_path"]))
        except (KeyError, TypeError, ValueError, yaml.YAMLError):
            continue
        if pack.id == row["key"] and pack.version == snapshot.get("version") and pack.interface_scope == "app":
            yield pack.id, (pack, source_id("skill_snapshot", row), digest(snapshot))


def _indexed_target(before, submitted):
    index = submitted.get("index")
    if type(index) is not int or index < 0 or submitted.get("surface_index") is not None:
        return None
    text = str(before["payload"].get("text") or "")
    matches = [line.strip() for line in text.split("\n") if line.lstrip().startswith(f"[{index}] ")]
    if len(matches) == 1:
        return matches[0][:220]
    return None


def _step_receipt(tick, event, before, observations):
    receipt = (event["payload"].get("action_result") or {}).get("receipt") or {}
    tick_receipt = (tick.get("action_result") or {}).get("receipt") or {}
    if (tick.get("decision") != "act" or not (tick.get("action") or {}).get("type")
            or not (event["payload"].get("submitted_action") or {}).get("type")
            or not receipt.get("transaction_id") or receipt["transaction_id"] != tick_receipt.get("transaction_id")):
        return {}, None
    if receipt.get("observation_accepted") is not True:
        return receipt, None
    after = observations.get(receipt.get("effect_observation_id") or receipt.get("observation_id"))
    if after is not None and after["key"] == before["key"] and receipt.get("visible_change") != "none":
        after = None
    return receipt, after


def _receipt_state(step):
    if not step.receipt:
        return "unavailable"
    if step.receipt.get("dispatch_succeeded") is not True:
        return "dispatch_failed"
    if step.after is None:
        return "post_observation_missing"
    if not step.same_app_post:
        return "left_app"
    if step.receipt.get("visible_change") == "none":
        return "no_visible_change"
    return "post_observed"


def _collect_workflows(store, app_builds):
    projections = dict(_projections(store))
    if not projections:
        return {}
    observations = {row["key"]: row for row in store.records("observation")}
    events = {row["key"]: row for row in store.records("event")}
    workflows = {}
    for trace in store.db.list_traces(store.task_id):
        if trace.kind != "executor_tick" or trace.step_seq is None:
            continue
        tick, step = trace.payload, trace.step_seq
        package = tick.get("app_id")
        build = app_builds.get(package) or {}
        code = build.get("version_code")
        active = tick.get("active_skills") or []
        if not active or build.get("package") != package or not isinstance(code, str) or not code.isdigit():
            continue
        first_step = build.get("first_step", 1)
        if (step < first_step or (step == first_step
                and build.get("observation_id") not in (None, tick.get("observation_id")))):
            continue
        before = _accepted_basis(store, tick, package, observations)
        if before is None:
            continue
        matching = [(item, projections.get(item.get("skill_id"))) for item in active
                    if item.get("scope") == f"apps/{package}"
                    and item.get("activation_source") in {"selected_workflow", "selected_stage_skill"}]
        if not any(snapshot for _, snapshot in matching):
            continue
        requests = list(_delivery_requests(store, tick))
        if not requests:
            continue
        # executor_tick is written after the event and step_number increment.
        event = events.get(str(step - 1))
        valid_event = (event is not None and event["payload"].get("step") == step - 1
                       and event["payload"].get("observation_id") == before["key"])
        for metadata, snapshot in matching:
            if snapshot is None:
                continue
            pack, snapshot_source, snapshot_hash = snapshot
            projection = pack.section_for("executor")
            profile_ids = metadata.get("selected_device_profiles")
            if (pack.app != package or pack.version != metadata.get("version")
                    or stable_hash(projection) != metadata.get("content_hash")
                    or not isinstance(profile_ids, list) or not all(isinstance(p, str) for p in profile_ids)
                    or (pack.device_profiles and not set(pack.device_profiles).intersection(profile_ids))):
                continue
            label = metadata["activation_source"]
            message = {"role": "user", "content": f"[{label} skill:{pack.id}@{pack.version}]\n{projection}"}
            request_ref = next((ref for ref, request in requests
                                if metadata in (request.get("active_skills") or [])
                                and message in (request.get("messages") or [])), None)
            if request_ref is None:
                continue
            identity = {"package": package, "skill_id": pack.id, "skill_version": pack.version,
                        "projection_hash": metadata["content_hash"], "snapshot_hash": snapshot_hash,
                        "device_profiles": sorted(set(profile_ids))}
            key = digest(identity)
            delivery = {"skill_snapshot_source": snapshot_source, "observation_source": source_id("observation", before),
                        "executor_request_ref": request_ref, "step": step,
                        "build_observation_id": build.get("observation_id"), "build_first_step": first_step}
            workflow = workflows.setdefault(key, WorkflowEvidence(key, identity,
                {name: build.get(name) for name in ("package", "version_code", "version_name")}, projection, delivery, pack.path.as_posix()))
            if valid_event:
                receipt, after = _step_receipt(tick, event, before, observations)
                workflow.steps[step - 1] = TraceStep(event, before, after, receipt, request_ref)
    return workflows


def _scoped_event(step, *, include_geometry=False):
    """Project recorded facts without forwarding raw input or cross-app receipt references."""
    payload = step.event["payload"]
    submitted = payload.get("submitted_action") or {}
    receipt = {key: step.receipt[key] for key in (
        "transaction_id", "dispatch_succeeded", "observation_accepted", "visible_change", "effect_outcome",
    ) if key in step.receipt}
    if step.same_app_post:
        receipt["effect_observation_id"] = step.after["key"]
    event = {key: payload[key] for key in (
        "step", "stage_id", "decision", "executor_report", "observation_id",
    ) if key in payload}
    if submitted:
        fields = ("type", "index", "direction", "duration_ms")
        if include_geometry:
            fields += ("x", "y", "x2", "y2")
        event["submitted_action"] = {key: submitted[key] for key in fields if submitted.get(key) is not None}
        target = _indexed_target(step.before, submitted)
        if target is not None:
            event["target_in_before"] = target
    event["receipt_state"] = _receipt_state(step)
    if receipt:
        action_result = payload.get("action_result") or {}
        event["action_result"] = {"success": action_result.get("success"), "receipt": receipt}
    return {**step.event, "payload": event}


class _VersionSourceStore(TaskStore):
    """Pin the accepted build-local input for native Learner history reads."""

    canonical_history_only = True
    history_image_max_bytes = 2_000_000

    def __init__(self, source, workflows):
        super().__init__(source.db, source.artifacts, source.task_id)
        self._rows = {"event": {}, "observation": {}, "stage": {}}
        self._steps = {}
        self.allowed_request_refs = set()
        stage_ids = set()
        for workflow in workflows:
            self.allowed_request_refs.add(workflow.delivery["executor_request_ref"])
            for step in workflow.steps.values():
                self.allowed_request_refs.add(step.executor_request_ref)
                self._rows["event"][step.event["key"]] = _scoped_event(step)
                self._steps[step.event["key"]] = step
                self._rows["observation"][step.before["key"]] = step.before
                if step.same_app_post:
                    self._rows["observation"][step.after["key"]] = step.after
                if stage := step.event["payload"].get("stage_id"):
                    stage_ids.add(stage)
        self._rows["stage"] = {row["key"]: row for row in source.records("stage") if row["key"] in stage_ids}

    def records(self, kind):
        return list(self._rows.get(kind, {}).values())

    def get(self, kind, key, version=None):
        row = self._rows.get(kind, {}).get(key)
        if row is None or (version is not None and version != row["version"]):
            raise ValueError(f"Source is outside this build's verified history: {kind}:{key}")
        if kind == "event":
            return _scoped_event(self._steps[key], include_geometry=True)
        return row

    def put(self, kind, key, payload):
        raise PermissionError("Version-triggered source history is read-only")


def _paired_sources(workflow):
    return [{"sources": sorted({source_id("observation", step.before),
                                source_id("event", step.event),
                                source_id("observation", step.after)}),
             "executor_request_ref": step.executor_request_ref}
            for step in workflow.steps.values() if step.paired]


def _claim_changes(task, store, workflows, budget):
    selected = []
    with store.db.transaction():
        records = store.db.find_agent_records(VERSION_AUDIT_KIND, list(workflows))
        already_requested = bool(store.records(VERSION_AUDIT_RUN_KIND))
        for key, workflow in workflows.items():
            history = [row for row in records if row["key"] == key]
            code = workflow.build["version_code"]
            same_build = [row for row in history if row["payload"]["build"]["version_code"] == code]
            if any(row["task_id"] == task.id or row["payload"]["status"] == "baseline"
                   or row["payload"].get("request_claimed") for row in same_build):
                continue
            previous = next((row["payload"]["build"] for row in reversed(history)
                             if row["payload"]["build"]["version_code"] != code), None)
            paired = _paired_sources(workflow)
            record = {**workflow.identity, "target": workflow.target, "build": workflow.build,
                      "previous_build": previous, "assessment": "unassessed", "delivery": workflow.delivery,
                      "request_claimed": False, "window_sources": paired}
            if previous is None:
                store.put(VERSION_AUDIT_KIND, key, {**record, "status": "baseline", "reason": "first_observed_build"})
                continue
            if not paired:
                reason = "missing_paired_evidence"
            elif already_requested or len(selected) == MAX_WORKFLOWS:
                reason = "request_budget"
            else:
                record.update(status="pending", reason="awaiting_learner", request_claimed=True)
                store.put(VERSION_AUDIT_KIND, key, record)
                selected.append((workflow, record))
                continue
            if not any(row["payload"].get("reason") == reason for row in same_build):
                store.put(VERSION_AUDIT_KIND, key, {**record, "status": "unassessed", "reason": reason})
        if selected:
            store.put(VERSION_AUDIT_RUN_KIND, "task", {
                "status": "pending", "trigger": "app_build_changed", "allocated_calls": budget.max_calls,
                "workflow_keys": [workflow.key for workflow, _ in selected],
            })
    return selected


async def audit_task_versions(task, store, settings, app_builds):
    """Record first builds and route subsequent build changes to the shared read-only Learner."""
    if task.status not in TERMINAL or not app_builds:
        return
    workflows = _collect_workflows(store, app_builds)
    if not workflows:
        return

    calls = settings.learning_default_calls
    seconds = settings.learning_default_seconds
    budget = LearningBudget(max_calls=calls, max_actions=0, max_seconds=seconds,
                            max_job_seconds=seconds, reserve_calls=min(8, calls // 3))
    selected = _claim_changes(task, store, workflows, budget)
    if not selected:
        return

    from agent.skills.exploration import ExplorationBackend
    from agent.skills.learning import run_task_learning
    from agent.skills.library import SkillLibrary

    library = SkillLibrary()
    backend = ExplorationBackend(task, db=store.db, artifacts=store.artifacts, driver=None,
                                 settings=settings, library=library)
    scoped = _VersionSourceStore(store, [workflow for workflow, _ in selected])
    backend.source_store = scoped
    changes = [{**workflow.identity, "target": workflow.target, "projection": workflow.projection,
                "build": workflow.build, "previous_build": record["previous_build"],
                "delivery": workflow.delivery, "window_sources": record["window_sources"],
                "evidence": sorted({reference for window in record["window_sources"] for reference in window["sources"]})}
               for workflow, record in selected]
    try:
        result = await run_task_learning(task, settings=settings, library=library, store=scoped,
                                         backend=backend, manual=False, version_changes=changes, budget=budget)
        status = "completed" if result.get("ok") else "stopped"
    except (GatewayError, TimeoutError):
        result, status = {"ok": False, "reason": "provider_result_unknown"}, "pending"
    except LearningStopped as exc:
        result, status = {"ok": False, "reason": str(exc)}, "stopped"
    finally:
        backend.close()

    summary = {key: result[key] for key in ("ok", "skipped", "eligible", "pending_id", "pending_ids", "accepted_targets")
               if key in result}
    reason = str(result.get("reason") or "learner_finished")[:400]
    run = {"status": status, "trigger": "app_build_changed", "allocated_calls": budget.max_calls,
           "workflow_keys": [workflow.key for workflow, _ in selected], "reason": reason,
           "budget": budget.snapshot(), **summary}
    with store.db.transaction():
        for workflow, record in selected:
            candidate = {"pending_id": summary["pending_id"]} if len(selected) == 1 and summary.get("pending_id") else {}
            store.put(VERSION_AUDIT_KIND, workflow.key, {**record, "status": status, "reason": reason, **candidate})
        store.put(VERSION_AUDIT_RUN_KIND, "task", run)
