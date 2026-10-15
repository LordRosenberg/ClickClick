"""One immutable profile for the human tool and all its model-facing surfaces."""

from dataclasses import dataclass


POLICY_VERSION = "role-decision-v19"


@dataclass(frozen=True)
class InteractionProfile:
    name: str
    wire: dict | None = None

    @property
    def enabled(self):
        return self.name != "disabled"

    @property
    def delegated(self):
        return self.name == "delegated"

    @property
    def tool_form(self):
        if self.wire is not None:
            return self.wire["tool_form"]
        if self.delegated:
            return (
                "ask_user accepts one clarification question: id, question, and optional reason or "
                "two or three genuine choices. Omit unused fields. Batch questions and confirmation "
                "are unavailable; a necessary confirmation remains a blocker."
            )
        if self.enabled:
            return (
                "Prefer one question; group at most three related clarification questions in questions. "
                "Use stable IDs and two or three genuine choices when useful. Use purpose=confirmation "
                "for one concrete action; only the explicit confirmed outcome grants that consent. "
                "Omit unused optional fields."
            )
        return ""

    @property
    def description(self):
        if self.wire is not None:
            return self.wire["description"]
        return (
            "Learn what the user means or requires before dependent work when material inputs are "
            "missing. Do not turn undefined user conditions into lookup goals without a supplied "
            "or evidenced source for that gap. Investigate app facts normally. A candidate's actual "
            "value does not establish missing target relationships or conditions. For a supported "
            "target, obtain the actual values needed to act; typed labels in different required fields "
            "or an app acknowledgement cannot supply missing values. An accepted target reference "
            "or success message does not establish its actual destination. If a scoped check leaves "
            "that destination unresolved, ask before the "
            "dependent operation; finding no alternative is not confirmation. Supplied destinations "
            "and supported app resolutions need no extra proof or question. Use sufficient context "
            "without extra proof or approval of an already requested ordinary action. Do not disguise "
            "unsupported confirmation as clarification. A reply resolves "
            "only what it actually states; clarify remaining necessary parts rather than fill "
            "omissions with guesses. Preserve requested relationships, properties and selection "
            "rules; do not invent latest/best/first or a one-item limit. Clarify material scope "
            "ambiguity and obtain acceptance before a material substitution. Respect delegated "
            "choices, sufficient context and ordinary defaults for unspecified incidental details. "
            "Ask neutrally, leaving unresolved information open; do not present an unsupported "
            "guess as an established premise in a question or its choices. Do not repeat resolved "
            "questions, granted permission, declines or "
            "unavailable answers, or ask about navigation and recoverable errors. "
            "Never collect secret credentials. " + self.tool_form + " Call alone; its matched result "
            "continues this invocation before normal submit_* completion."
        )

    @property
    def recovery_nudge(self):
        if self.wire is not None:
            return self.wire["recovery_nudge"]
        return (
            "Your previous response contained only text. Call an advertised tool without a preface. "
            "If necessary user information or a material choice is unresolved, use ask_user alone; otherwise continue "
            "toward the normal terminal decision. " + self.tool_form
        )

    @property
    def submission_hint(self):
        if self.wire is not None:
            return self.wire["submission_hint"]
        return (
            " Necessary unresolved user information or a material choice may use ask_user alone before submission only "
            "if its result fits the remaining rounds. " + self.tool_form
        )

    @property
    def schema(self):
        if self.wire is not None:
            return self.wire["schema"]
        if not self.enabled:
            return {}
        from shared.hitl import AskUser, Question, human_tool_schema
        return human_tool_schema(Question if self.delegated else AskUser)

    def snapshot(self):
        if not self.enabled:
            return {}
        return {key: getattr(self, key) for key in (
            "tool_form", "description", "recovery_nudge", "submission_hint", "schema")}


def interaction_profile(*, enabled=False, mode="managed"):
    if not enabled:
        return InteractionProfile("disabled")
    if mode not in {"managed", "delegated"}:
        raise ValueError("Unknown human interaction profile")
    return InteractionProfile(mode)


def session_interaction_profile(session, config):
    profile = interaction_profile(enabled=config["enabled"], mode=config["mode"])
    store = getattr(session, "store", None)
    row = store.db.get_agent_record(store.task_id, "prompt_policy", "roles") if store else None
    if row and profile.enabled:
        if row["payload"]["profile"] != profile.name:
            raise ValueError("Task interaction profile changed after prompt freeze")
        return InteractionProfile(profile.name, row["payload"]["wire"])
    return profile
