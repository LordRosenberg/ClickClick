"""Research lifecycle receipts, independent of any app reset script."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import inspect
import time
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["observe", "isolated"] = "observe"
    scope: str = Field(min_length=1, max_length=1200)
    preparation: str = Field(default="Use current state", max_length=2000)
    expected_condition: str = Field(min_length=1, max_length=1200)
    cleanup: str = Field(default="", max_length=2000)
    cleanup_actions: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="after")
    def bounded_cleanup(self):
        if self.mode == "isolated" and (not self.cleanup.strip() or self.cleanup_actions < 1):
            raise ValueError("isolated mutations require bounded cleanup")
        return self


@dataclass(frozen=True)
class TrustedFixtureReset:
    """Only an operator harness supplies this callback and identity binding."""
    scope: str
    expected: dict
    restore: object

    def metadata(self):
        return {"available": True, "mode": "disposable_official_fixture", "scope": self.scope,
            "restores": "Official initial task fixture, not the completed source state",
            "verification": "Successful teardown and independent actual initializer hash",
            "policy": "A capability, not a completed reset or whole-device rollback claim"}


class ResearchEnvironment:
    def __init__(self, store, *, cancelled=lambda: False, verifier=None, reset_adapter=None):
        self.store, self.cancelled, self.verifier = store, cancelled, verifier
        self.reset_adapter = reset_adapter
        self.id = uuid4().hex
        self.stage = "probe"
        self.status = "open"
        self.plan = None
        self.effects = []
        self.claims = []
        self.checkpoint = None
        self.resources = {}
        self.resource_states = {}
        self._persist("begin", {"initialization": "not_requested", "reset_available": reset_adapter is not None,
            "verification_available": verifier is not None})

    @classmethod
    def resume(cls, store, session_id, *, cancelled=lambda: False, verifier=None, reset_adapter=None):
        # Recover unresolved journals without replaying device actions.
        rows = [r["payload"] for r in store.records("measurement")
                if r["payload"].get("session_id") == session_id]
        if not rows:
            raise ValueError("unknown environment session")
        obj = cls.__new__(cls)
        obj.store, obj.cancelled, obj.verifier = store, cancelled, verifier
        obj.reset_adapter = reset_adapter
        obj.id, obj.effects, obj.claims, obj.resources, obj.checkpoint = session_id, [], [], {}, None
        obj.plan = None
        obj.resource_states = {}
        for event in rows:
            obj.stage, obj.status = event["stage"], event["status"]
            kind = event["event"]
            if kind == "plan":
                obj.plan = ResearchPlan.model_validate(event["plan"])
            elif kind == "intent":
                obj.effects.append({k:v for k,v in event.items() if k not in {"event", "session_id", "time", "status"}})
            elif kind == "effect":
                effect = next(e for e in obj.effects if e["id"] == event["intent_id"])
                effect.update(result=event["result"], verified=event["verified"])
            elif kind == "fixture_reset" and event.get("verified") is True:
                obj.checkpoint = None
                for effect in obj.effects:
                    if "result" in effect:
                        effect["verified"] = True
            elif kind == "checkpoint":
                obj.checkpoint = event["value"]
            elif kind == "verify" and event["matched"]:
                for effect in obj.effects:
                    if "result" in effect:
                        effect["verified"] = True
            elif kind == "owned_resource":
                obj.resource_states[event["key"]] = {"before":event["before"],"owned_after":event["owned_after"],"verified":False}
            elif kind == "resource_verified":
                obj.resource_states[event["key"]]["verified"] = True
            elif kind == "change_claim":
                obj.claims.append({"description":event["description"], "evidence_refs":event["evidence_refs"]})
        # Interrupted/unresolved sessions remain blocked; a caller cannot open a new
        # journal and pretend the prior effects vanished.
        obj._persist("resume", {"automatic_replay": False})
        return obj

    def register_owned_resource(self, key, *, before, owned_after, reader, restorer):
        self._active()
        if not key or key in self.resources:
            raise ValueError("resource requires fresh ownership identity")
        before, owned_after = copy.deepcopy(before), copy.deepcopy(owned_after)
        self.resources[key] = (before, owned_after, reader, restorer)
        self.resource_states[key] = {"before":before,"owned_after":owned_after,"verified":False}
        self._persist("owned_resource", {"key":key, "before":before, "owned_after":owned_after})

    async def restore_owned_resource(self, key):
        self._active()
        before, owned_after, reader, restorer = self.resources[key]
        current = reader()
        current = await current if inspect.isawaitable(current) else current
        self._active()
        if current != owned_after:
            self._persist("ownership_conflict", {"key":key, "current":current})
            return {"restored":False, "reason":"resource changed outside research ownership"}
        # Adapter must implement an atomic expected-value check, not blindly write.
        self._persist("restore_intent", {"key":key, "expected":owned_after, "before":before})
        result = restorer(expected=owned_after, replacement=before)
        result = await result if inspect.isawaitable(result) else result
        self._persist("restore_effect", {"key":key, "result":result})
        return {"restored":result is True, "policy":"Still requires independent checkpoint verification"}

    def reopen_for_cleanup(self):
        if self.cancelled():
            from asyncio import CancelledError
            raise CancelledError()
        if self.status != "unresolved":
            raise ValueError("only unresolved journals may resume bounded cleanup")
        self.status, self.stage = "open", "cleanup"
        self._persist("cleanup_resumed", {"new_budget":False})

    def attach_owned_resource(self, key, *, reader, restorer):
        self._active()
        saved = self.resource_states[key]
        self.resources[key] = (saved["before"],saved["owned_after"],reader,restorer)
        self._persist("resource_adapter_attached", {"key":key})

    def _persist(self, event, detail):
        self.store.put("measurement", "environment-" + self.id + "-" + uuid4().hex,
            {"event": event, "session_id": self.id, "stage": self.stage,
             "status": self.status, "time": time.time(), **detail})

    def configure(self, plan):
        self._active()
        plan = ResearchPlan.model_validate(plan)
        if self.receipt()["unresolved_effects"]:
            raise ValueError("unresolved effects prohibit replacing the environment plan")
        self.plan = plan
        self._persist("plan", {"plan": plan.model_dump(), "policy": "Intent, not proof of isolation or readiness"})
        return self.receipt()

    async def prepare(self, initializer=None):
        self._active()
        self.stage = "prepare"
        self._persist("prepare_started", {"initializer": initializer is not None})
        if initializer:
            # Trusted adapters may partially mutate before returning or failing.
            # Preparation itself therefore has a durable unresolved intent.
            entry={"id":uuid4().hex,"action":{"type":"adapter_initialize"},
                   "observation_id":"trusted-adapter-checkpoint", "stage":"prepare",
                   "verified":False,"possible_mutation":True}
            self.effects.append(entry)
            self._persist("intent",entry)
            try:
                result = initializer()
                if inspect.isawaitable(result):
                    result = await result
                self._active()
                self.effect(entry["id"], {"success":True,"adapter_result":result})
                self._persist("prepare_result", {"result": result,
                    "policy": "Adapter result; actual readiness must still be observed"})
            except BaseException as exc:
                from asyncio import CancelledError
                if isinstance(exc,CancelledError):
                    self.interrupt("initializer_cancelled")
                self._persist("prepare_failed", {"error": type(exc).__name__})
                raise
        return self.receipt()

    def set_stage(self, stage):
        self._active()
        if stage not in {"prepare", "probe", "cleanup"}:
            raise ValueError("invalid research stage")
        self.stage = stage
        self._persist("stage", {})
        return self.receipt()

    def _active(self):
        if self.cancelled():
            self.status = "interrupted"
            self._persist("interrupted", {})
            from asyncio import CancelledError
            raise CancelledError()
        if self.status != "open":
            raise ValueError("research environment is not open")

    def intent(self, action, observation_id):
        self._active()
        if not observation_id:
            raise ValueError("environment action requires current observation")
        navigation = action.get("type") in {"back", "home", "sleep"}
        # A reviewed observe plan may inspect menus with taps. Physical dispatch
        # is permission, never a claim that a tap has no hidden effects.
        observed_navigation = action.get("type") in {"tap", "tap_xy", "double_tap", "long_press", "scroll", "swipe", "launch"}
        if not navigation and (self.plan is None or
            (self.plan.mode == "observe" and not observed_navigation)):
            raise ValueError("UI interaction requires a reviewed plan; writing requires isolated mode")
        entry = {"id": uuid4().hex, "action": action, "observation_id": observation_id,
                 "stage": self.stage, "verified": False, "possible_mutation": not navigation}
        self.effects.append(entry)
        self._persist("intent", entry)
        return entry["id"]

    def effect(self, intent_id, result):
        entry = next(e for e in self.effects if e["id"] == intent_id)
        if "result" in entry:
            raise ValueError("intent already has an effect receipt")
        entry["result"] = result
        # Rejected dispatches can be resolved; accepted/unknown effects need independent checking.
        receipt = result.get("receipt") or (result.get("detail") or {}).get("receipt") or {}
        if receipt.get("dispatch_succeeded") is False or (not entry["possible_mutation"] and result.get("success") is True):
            entry["verified"] = True
        self._persist("effect", {"intent_id": intent_id, "result": result, "verified": entry["verified"]})

    def record_claim(self, description, evidence_refs):
        self._active()
        if not description or len(description) > 1600 or not evidence_refs or len(evidence_refs) > 8:
            raise ValueError("bounded change claims require evidence")
        self.claims.append({"description": description, "evidence_refs": evidence_refs})
        self._persist("change_claim", {**self.claims[-1], "policy": "Learner claim; not independent verification"})
        return self.receipt()

    async def capture_checkpoint(self):
        self._active()
        if self.verifier:
            value = self.verifier()
            self.checkpoint = await value if inspect.isawaitable(value) else value
            self._active()
            if not isinstance(self.checkpoint, dict) or self.checkpoint.get("complete") is not True:
                self.checkpoint = None
            self.checkpoint = copy.deepcopy(self.checkpoint)
            self._persist("checkpoint", {"available": self.checkpoint is not None,
                "value": self.checkpoint, "policy": "Trusted read-only adapter; never a model claim"})
        return self.receipt()

    async def verify(self):
        self._active()
        if self.verifier and self.checkpoint is not None:
            value = self.verifier()
            value = await value if inspect.isawaitable(value) else value
            self._active()
            matched = (isinstance(value, dict) and value.get("complete") is True
                       and value == self.checkpoint)
            self._persist("verify", {"matched": matched, "value": value})
            if matched:
                for effect in self.effects:
                    if "result" in effect:
                        effect["verified"] = True
                for key, state in self.resource_states.items():
                    adapter = self.resources.get(key)
                    if adapter is None:
                        continue
                    current = adapter[2]()
                    current = await current if inspect.isawaitable(current) else current
                    self._active()
                    if current == state["before"]:
                        state["verified"] = True
                        self._persist("resource_verified", {"key":key})
        return self.receipt()

    async def reset_fixture(self):
        self._active()
        adapter = self.reset_adapter
        if adapter is None:
            raise ValueError("no trusted disposable fixture reset")
        if self.resource_states:
            raise ValueError("registered resources require their ownership adapters")
        entry = {"id":uuid4().hex,"action":{"type":"adapter_fixture_reset"},
            "observation_id":"trusted-fixture-lease", "stage":self.stage,
            "verified":False,"possible_mutation":True}
        self.effects.append(entry)
        self._persist("intent", entry)
        try:
            value = adapter.restore()
            value = await value if inspect.isawaitable(value) else value
            self._active()
            verified = (isinstance(value, dict) and value.get("complete") is True
                and value.get("teardown_success") is True and value.get("initialized") is True
                and all(value.get(k) == v for k,v in adapter.expected.items()))
            self.effect(entry["id"], {"success":verified,"adapter_result":value})
            if verified:
                for effect in self.effects:
                    if "result" in effect:
                        effect["verified"] = True
                self.checkpoint = None
            self._persist("fixture_reset", {"verified":verified, "receipt":value,
                "scope":adapter.scope,"policy":"Old UI grounding invalid; source history unchanged"})
        except BaseException as exc:
            from asyncio import CancelledError
            if isinstance(exc, CancelledError):
                self.interrupt("fixture_reset_cancelled")
            self._persist("fixture_reset_failed", {"error":type(exc).__name__})
            raise
        return self.receipt()

    def interrupt(self, reason="research_cancelled"):
        self.status="interrupted"
        self._persist("interrupted", {"reason":reason})
        return self.receipt()

    def close(self):
        if self.status == "closed":
            return self.receipt()
        if self.status == "interrupted" or self.cancelled():
            self.status = "interrupted"
        elif self.receipt()["unresolved_effects"]:
            self.status = "unresolved"
        else:
            self.status = "closed"
        self._persist("close", {"unresolved_effects": sum(not e.get("verified") for e in self.effects) + sum(not r["verified"] for r in self.resource_states.values())})
        return self.receipt()

    def receipt(self):
        unresolved = sum(not e.get("verified") for e in self.effects) + sum(not r["verified"] for r in self.resource_states.values())
        return {"session_id": self.id, "stage": self.stage, "status": self.status,
                "plan": self.plan.model_dump() if self.plan else None,
                "effect_count": len(self.effects), "unresolved_effects": unresolved,
                "change_claim_count": len(self.claims), "owned_resource_count":len(self.resource_states), "verified_clean": unresolved == 0 and self.status in {"open", "closed"},
                "reset_capability": self.reset_adapter.metadata() if self.reset_adapter else {"available":False},
                "policy": "Clean applies only to registered verification scope; no whole-app rollback claim"}
