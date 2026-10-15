"""Durable human-input protocol. Semantic necessity is decided by the agent."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas import TaskStatus


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def omit_optional_fields(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("omit optional fields instead of passing null")
        return value


class Choice(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=200)


class Question(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = Field(min_length=1, max_length=500)
    reason: str | None = Field(default=None, min_length=1, max_length=300)
    options: list[Choice] | None = Field(default=None, min_length=2, max_length=3)

    @model_validator(mode="after")
    def unique_choices(self):
        if self.options and len({o.id for o in self.options}) != len(self.options):
            raise ValueError("duplicate option IDs")
        return self


class AskUser(StrictModel):
    purpose: Literal["clarification", "confirmation"] = "clarification"
    questions: list[Question] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question IDs")
        if self.purpose == "confirmation" and len(self.questions) != 1:
            raise ValueError("confirm one concrete action at a time")
        return self


class Answer(StrictModel):
    question_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4096)
    option_id: str | None = Field(default=None, min_length=1, max_length=64)


class Reply(StrictModel):
    interaction_id: str = Field(min_length=1, max_length=128)
    owner_version: int = Field(ge=1, strict=True)
    response_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["answered", "confirmed", "declined", "unable_to_answer", "cancel_task"]
    answers: list[Answer] = Field(default_factory=list, max_length=3)


class HitlConfig(StrictModel):
    version: Literal[1] = 1
    enabled: bool = False
    mode: Literal["managed", "delegated"] = "managed"
    max_queries: int = Field(default=8, ge=1, le=50)
    timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    max_answer_chars: int = Field(default=4096, ge=1, le=4096)


def human_tool_schema(model):
    """Expose optional fields by omission, matching the strict human protocol."""
    def normalize(node):
        if isinstance(node, list):
            return [normalize(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = {key: normalize(value) for key, value in node.items()
                  if not (key == "default" and value is None)}
        if "anyOf" in result:
            variants = [value for value in result["anyOf"] if value != {"type": "null"}]
            if len(variants) == 1:
                result.pop("anyOf")
                result.update(variants[0])
            else:
                result["anyOf"] = variants
        if result.get("type") == "object":
            result.setdefault("required", [])
        return result
    return normalize(model.model_json_schema())


class HumanInputSuspended(BaseException):
    """Scheduler yield; deliberately not caught as a failed tool/role call."""

    def __init__(self, interaction_id: str):
        self.interaction_id = interaction_id
        super().__init__(interaction_id)


class InputConflict(ValueError):
    pass


def configured_secrets(settings):
    """Only local credential values, never persisted as part of a task snapshot."""
    if settings is None:
        return ()
    values = [settings.jev_api_key.get_secret_value()]
    for provider in settings.model_providers().values():
        for key, value in provider.items():
            if any(part in key.lower() for part in ("api_key", "token", "password", "authorization", "cookie")) and isinstance(value, str):
                values.append(value)
    return tuple(value for value in values if value)


class HumanInputBroker:
    def __init__(self, db, *, settings=None):
        self.db = db
        self.settings = settings

    @property
    def sensitive_values(self):
        return configured_secrets(self.settings)

    def sanitize(self, value):
        if isinstance(value, str):
            for secret in sorted(self.sensitive_values, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        return value

    def configure(self, task_id, config: HitlConfig, *, origin="console", binding=None):
        if self.db.get_agent_record(task_id, "hitl_config", "snapshot"):
            return
        self.db.append_agent_record(task_id, "hitl_config", "snapshot", {
            **config.model_dump(), "origin": origin, "binding": binding,
        })

    def config(self, task_id):
        row = self.db.get_agent_record(task_id, "hitl_config", "snapshot")
        return row["payload"] if row else HitlConfig().model_dump()

    def current(self, task_id):
        row = self.db.latest_agent_record(task_id, "human_input")
        return row["payload"] if row else None

    def public(self, task_id, *, owner="console", binding=None):
        item = self.current(task_id)
        if not item or item["status"] != "pending":
            return None
        result = {key: item[key] for key in (
            "interaction_id", "task_id", "purpose", "questions", "owner", "owner_version",
            "origin", "deadline_at", "created_at", "fallback_reason", "presented",
        )}
        can_answer = item["owner"] == owner and (owner == "console" or secrets.compare_digest(
            str(item.get("binding") or ""), str(binding or "")))
        result["can_answer"] = can_answer
        if not can_answer:
            # Another surface can see ownership, not a competing answer form.
            result["questions"] = []
        return result

    def begin(self, task_id, request, checkpoint, *, role, invocation_id, call_id,
              resumed_interaction_id=None):
        config = self.config(task_id)
        if not config["enabled"]:
            raise InputConflict("hitl_disabled")
        request = AskUser.model_validate(self.sanitize(request))
        if config["mode"] == "delegated" and (request.purpose != "clarification" or len(request.questions) != 1):
            raise InputConflict("delegated_host_supports_single_clarification_only")
        normalized = {"purpose": request.purpose, "questions": [
            {"question": " ".join(q.question.casefold().split()),
             "options": [" ".join(o.label.casefold().split()) for o in (q.options or [])]} for q in request.questions]}
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        with self.db.transaction():
            previous = None
            if resumed_interaction_id is not None:
                row = self.db.get_agent_record(task_id, "human_input", resumed_interaction_id)
                previous = row["payload"] if row else None
                if (not previous or previous["status"] != "resolved" or
                        previous["role"] != role or previous["invocation_id"] != invocation_id or
                        previous["call_id"] == call_id or previous["outcome"] == "cancel_task" or
                        not any(m.get("role") == "tool" and m.get("tool_call_id") == previous["call_id"]
                                for m in checkpoint.get("dialogue", []) if isinstance(m, dict))):
                    raise InputConflict("invalid_resumed_interaction")
            rows = self.db.list_agent_records(task_id, "human_input")
            if any(r["payload"]["status"] == "pending" for r in rows):
                raise InputConflict("interaction_already_pending")
            if len(rows) >= config["max_queries"]:
                raise InputConflict("human_query_budget_exhausted")
            if any(r["payload"]["question_digest"] == digest and
                   r["payload"]["outcome"] in {"answered", "confirmed", "declined"} for r in rows):
                raise InputConflict("question_already_answered: use the previous answer")
            task = self.db.get_task(task_id)
            if task is None or task.status not in {TaskStatus.RUNNING, TaskStatus.QUEUED}:
                raise InputConflict("task_not_executing")
            now = time.time()
            deadline = now + config["timeout_seconds"]
            if task.state.revisable.limits.deadline_at is not None:
                deadline = min(deadline, task.state.revisable.limits.deadline_at)
            if deadline <= now:
                raise InputConflict("task_deadline_exhausted")
            binding = config.get("binding")
            live = self.db.get_local_preference("hitl_binding:" + str(binding)) if binding else None
            origin = config.get("origin", "console")
            owner = origin if origin in {"assistant", "cli"} and live and live["owner"] == origin and live["expires_at"] > now and live.get("subscribed") else "console"
            if config["mode"] == "delegated":
                owner = "delegated"
            item = {
                **request.model_dump(exclude_none=True), "interaction_id": uuid.uuid4().hex,
                "task_id": task_id, "status": "pending", "outcome": None,
                "question_digest": digest, "origin": origin, "owner": owner,
                "owner_version": 1, "binding": binding if owner != "console" else None,
                "fallback_reason": "origin_unavailable" if origin in {"assistant", "cli"} and owner == "console" else None,
                "created_at": now, "deadline_at": deadline, "presented": False,
                "delivery_deadline_at": now + 30, "role": role, "invocation_id": invocation_id,
                "call_id": call_id, "checkpoint": self.sanitize(checkpoint), "response_id": None,
            }
            # Transfer only after the successor checkpoint is ready. Both writes
            # commit together; the new pending row must remain the latest record.
            if previous is not None:
                previous.update(status="consumed", checkpoint=None)
                self._save(previous)
            self.db.append_agent_record(task_id, "human_input", item["interaction_id"], item)
            self.db.update_task(task_id, status=TaskStatus.WAITING_FOR_INPUT)
        return item

    def bind(self, owner, token=None):
        if owner not in {"assistant", "cli"}:
            raise ValueError("invalid binding owner")
        token = token or secrets.token_urlsafe(32)
        existing = self.db.get_local_preference("hitl_binding:" + token)
        if existing and existing["owner"] != owner:
            raise InputConflict("binding owner mismatch")
        self.db.set_local_preference("hitl_binding:" + token, {"owner": owner, "expires_at": time.time() + 45,
                                                            "subscribed": bool(existing and existing.get("subscribed"))})
        return token

    def _save(self, item):
        self.db.append_agent_record(item["task_id"], "human_input", item["interaction_id"], item)

    def present(self, task_id, interaction_id, version, *, owner, binding=None):
        with self.db.transaction():
            item = self._owned(task_id, interaction_id, version, owner, binding)
            item["presented"] = True
            self._save(item)

    def _owned(self, task_id, interaction_id, version, owner, binding):
        item = self.current(task_id)
        task = self.db.get_task(task_id)
        if not task or task.status != TaskStatus.WAITING_FOR_INPUT:
            raise InputConflict("task_not_waiting")
        if (not item or item["status"] != "pending" or item["interaction_id"] != interaction_id
                or item["owner_version"] != version or item["owner"] != owner
                or (owner != "console" and not secrets.compare_digest(str(item.get("binding") or ""), str(binding or "")))):
            raise InputConflict("stale_or_wrong_answer_owner")
        if time.time() >= item["deadline_at"]:
            raise InputConflict("interaction_expired")
        if owner in {"assistant", "cli"}:
            live = self.db.get_local_preference("hitl_binding:" + str(binding))
            if not live or not live.get("subscribed") or live["expires_at"] <= time.time():
                raise InputConflict("reply_binding_unavailable")
        return item

    def takeover(self, task_id, *, interaction_id=None, owner_version=None):
        with self.db.transaction():
            item = self.current(task_id)
            if not item or item["status"] != "pending" or item["owner"] == "delegated":
                raise InputConflict("no_managed_pending_interaction")
            if ((interaction_id is not None and interaction_id != item["interaction_id"]) or
                    (owner_version is not None and owner_version != item["owner_version"])):
                raise InputConflict("stale_takeover_request")
            if item["owner"] != "console":
                item.update(owner="console", binding=None, owner_version=item["owner_version"] + 1,
                            presented=False, fallback_reason="console_takeover")
                self._save(item)
        return self.public(task_id)

    def reply(self, task_id, reply: Reply, *, owner="console", binding=None):
        digest = hashlib.sha256(reply.model_dump_json().encode()).hexdigest()
        reply = Reply.model_validate(self.sanitize(reply.model_dump(exclude_none=True)))
        with self.db.transaction():
            row = self.db.get_agent_record(task_id, "human_input", reply.interaction_id)
            if not row:
                raise InputConflict("unknown_interaction")
            item = row["payload"]
            # Retries must still come from the submitting owner and binding.
            if item.get("response_id") == reply.response_id:
                if (item.get("response_digest") != digest or item["owner"] != owner or
                        (owner != "console" and item.get("binding") != binding)):
                    raise InputConflict("response_id_conflict")
                return False
            item = self._owned(task_id, reply.interaction_id, reply.owner_version, owner, binding)
            expected = {q["id"]: q for q in item["questions"]}
            if reply.outcome == "answered":
                if item["purpose"] != "clarification" or len(reply.answers) != len(expected) or {a.question_id for a in reply.answers} != set(expected):
                    raise InputConflict("answer_question_mapping_mismatch")
                for answer in reply.answers:
                    if len(answer.text) > self.config(task_id)["max_answer_chars"]:
                        raise InputConflict("answer_too_large")
                    options = {o["id"]: o["label"] for o in expected[answer.question_id].get("options", [])}
                    if answer.option_id is not None and answer.option_id not in options:
                        raise InputConflict("unknown_option")
                    if answer.option_id is not None and answer.text != options[answer.option_id]:
                        raise InputConflict("option_label_mismatch")
            elif reply.answers or (reply.outcome in {"confirmed", "declined"} and item["purpose"] != "confirmation"):
                raise InputConflict("response_purpose_mismatch")
            item.update(status="resolved", outcome=reply.outcome, answers=[a.model_dump(exclude_none=True) for a in reply.answers],
                        response_id=reply.response_id, response_digest=digest, responded_at=time.time(),
                        provenance=owner)
            self._save(item)
            self.db.update_task(task_id, status=TaskStatus.CANCELLED if reply.outcome == "cancel_task" else TaskStatus.QUEUED)
        return True

    def reconcile(self, task_id):
        """Tick deadlines/liveness without inference. Returns whether a resume is ready."""
        with self.db.transaction():
            item = self.current(task_id)
            task = self.db.get_task(task_id)
            if not item or item["status"] != "pending":
                return False
            now = time.time()
            if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                item.update(status="invalidated", outcome="cancelled")
                self._save(item)
                return False
            if now >= item["deadline_at"]:
                item.update(status="resolved", outcome="timeout", answers=[], provenance="runtime")
                self._save(item)
                self.db.update_task(task_id, status=TaskStatus.QUEUED)
                return True
            if item["owner"] in {"assistant", "cli"}:
                live = self.db.get_local_preference("hitl_binding:" + str(item["binding"]))
                if (not live or not live.get("subscribed") or live["expires_at"] <= now or
                        (not item["presented"] and now >= item["delivery_deadline_at"])):
                    item.update(owner="console", binding=None, owner_version=item["owner_version"] + 1,
                                presented=False, fallback_reason="origin_delivery_lost")
                    self._save(item)
        return False

    def continuation(self, task_id, role):
        item = self.current(task_id)
        if item and item["role"] == role and item["status"] == "resolved" and item["outcome"] != "cancel_task":
            return item
        return None

    def complete(self, task_id):
        with self.db.transaction():
            item = self.current(task_id)
            if item and item["status"] == "resolved":
                item.update(status="consumed", checkpoint=None)
                self._save(item)
