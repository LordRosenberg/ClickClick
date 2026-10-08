"""Bounded historical memory; note de-duplication is a reversible projection."""

import json
from pydantic import BaseModel, ConfigDict, Field, model_validator


SECTIONS = (
    ("results", "Historical findings"),
    ("decisions_and_attempts", "Past attempts and observed outcomes"),
    ("critical_context", "Other necessary historical facts"),
)
FORMAT = "clickclick.summary.v2"
TARGET_CHARACTERS = 3000
MAX_CHARACTERS = 4000
MAX_CHECK_CONTEXT_CHARACTERS = 1500
SUBMISSION_INSTRUCTION = (
    "Submit the historical summary with save_summary now. Correct rejected fields, preserve material "
    "uncertainty, and stay within the length limit. Do not add next steps or verification requirements."
)


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_CHARACTERS,
                      description="Concise historical information. Keep each retained claim together with its material attribution, scope and evidence limitations; shortening must not strengthen it or add doubt absent from the records.")
    note_source: str = Field(default="", max_length=512,
                            description="Omit unless copying a supplied note_source value (note:...@version) and its retained text verbatim. R/P/N source labels belong in sources, never note_source.")
    sources: list[str] = Field(min_length=1,
                              description="Required: copy supplied source labels. Labels enable tracing, not truth verification, and do not replace attribution or evidence limitations needed in text.")


class StructuredSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SummaryItem] = Field(description="Consequential recorded outcomes, artifacts or reported findings. Preserve whether each is an observation, report or inference; this section does not certify it as observed.")
    decisions_and_attempts: list[SummaryItem] = Field(description="Only consequential past attempts: what was actually tried, observed outcome and scope. No inferred causes, advice, new requirements or copied action rationale.")
    critical_context: list[SummaryItem] = Field(description="Other historical details needed to continue. Keep a claim's material qualifications with that claim, not solely in this optional section. No new plan or question checklist.")

    @model_validator(mode="after")
    def bounded(self):
        if len(self.render()) > MAX_CHARACTERS:
            sizes = {key: sum(len(item.text) for item in getattr(self, key)) for key, _ in SECTIONS}
            raise ValueError(f"Rendered summary has {len(self.render())} characters; maximum {MAX_CHARACTERS} characters including headings. Section text lengths: {sizes}. Rewrite toward 3200 characters, rather than shaving only the excess. Merge repetition and omit resolved incidental steps; preserve unique facts, scope and uncertainty.")
        return self

    def render(self, supplied_notes: dict[str, str] | None = None, *, text_checks=None) -> str:
        sections = []
        annotation_budget = 1200
        for key, heading in SECTIONS:
            lines = []
            for item in getattr(self, key):
                # Equal values from different observations must remain distinct.
                # Only the exact version AND full retained text authorize hiding.
                if item.note_source and (supplied_notes or {}).get(item.note_source) == item.text:
                    continue
                suffix = ""
                if text_checks:
                    from agent.revisable.memory_checks import item_key, visible_check
                    check = visible_check(text_checks.get(item_key(item.text, item.sources, item.note_source)))
                    if check:
                        suffix = " [text check: " + check["status"] + (
                            "; " + check["reason"] if check.get("reason") else "") + "]"
                        if len(suffix) > annotation_budget:
                            suffix = ""
                        annotation_budget -= len(suffix)
                lines.append("- " + item.text + suffix)
            if lines:
                sections.append(heading + ":\n" + "\n".join(lines))
        return "\n\n".join(sections)

    def encode(self) -> str:
        return json.dumps({"format": FORMAT, **self.model_dump(include={key for key, _ in SECTIONS})},
                          ensure_ascii=False, separators=(",", ":"))

    def bind_sources(self, labels: dict[str, list[str]]) -> None:
        """Validate model references before committing; preserve original provenance."""
        for key, _ in SECTIONS:
            for item in getattr(self, key):
                unknown = [ref for ref in item.sources if not labels.get(ref)]
                if not item.sources or unknown:
                    raise ValueError(
                        "Each summary item needs supplied source labels; copy them from records or previous items. "
                        f"Unknown labels: {unknown}. Current state supplied separately is not historical evidence."
                    )
        for key, _ in SECTIONS:
            for item in getattr(self, key):
                item.sources = list(dict.fromkeys(ref for label in item.sources for ref in labels[label]))


def parse_summary(value: str) -> StructuredSummary | None:
    """Legacy strings remain valid persisted state; only our tagged format parses."""
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict) or payload.pop("format", None) != FORMAT:
            return None
        return StructuredSummary.model_validate(payload)
    except (ValueError, TypeError):
        return None


def project_summary(value: str, retained_packet: dict | None = None, *, text_checks=None) -> str:
    summary = parse_summary(value)
    if summary is None:
        return value
    supplied = {item["source"]: item["text"] for item in (retained_packet or {}).get("items", [])}
    rendered = summary.render(supplied, text_checks=text_checks)
    if rendered and text_checks:
        rendered += ("\nText checks assess fidelity to cited text, not independent visual or world truth. "
                     "Unchecked does not mean false or require another check. "
                     "Unlabelled items have no displayed check.")
    return rendered


STRUCTURED_PROMPT = (
    "Compress supplied execution history; do not solve or answer the original task. "
    "Use supplied previous summary items and records as the only sources of factual claims. "
    "Do not fill gaps with background knowledge, remembered answers, guesses or newly derived results, "
    "even if you believe them correct. Task and stage context determine relevance, not answer evidence. "
    "Findings may be empty. Preserve recorded inferences as inferences, not observations.\n\n"
    "Summarize historical records and previous items using results, decisions_and_attempts and critical_context; "
    "leave unnecessary sections empty. Keep historical information relevant to unfinished delivery, consequential attempt coverage/outcomes, "
    "active obstacles and important corrections. For a failed method that still affects unfinished work, "
    "retain its scope and observed outcome, preserving uncertainty about its cause. Returning to a usable "
    "screen alone does not establish that the method's obstacle is resolved. Omit routine actions, transient "
    "loading and errors whose consequences no longer affect unfinished work. Shorten and deduplicate; merge only "
    "within the same object and scope. Keep material attribution, negation, scope and evidence limitations with "
    "each retained claim: these qualify its meaning, even when they describe what was not observed. "
    "A shorter statement must not claim stronger support. Retain the recorded basis of earlier findings without "
    "inventing doubt or requiring fresh confirmation merely because new records do not repeat them. "
    "Describe actual attempts, observations and scope; do not infer causes or copy action rationale as fact. "
    "Preserve exact data, attribution, user-stated constraints and uncertainty. Local lookup failure is not global "
    "absence; an unverified candidate is not refuted. Plans and dispatch do not prove success. "
    "A model's preferred route is not a user requirement. Remove prescriptions inherited from model plans or "
    "previous summaries; do not add next steps, verification requirements or retry bans. Current task/stage/state "
    "are supplied separately. Preserve requirements discovered inside apps without repeating the task checklist. "
    "For supplied retained notes, use exact text with note_source when needed, not a paraphrase: runtime hides "
    "exact copies while supplied and restores them if omitted later. Do not create another unresolved checklist. "
    "Omission does not retract a relevant fact. Update or omit a past failure when later evidence supersedes "
    "its scoped outcome or it no longer affects remaining work. Superseded states and resolved detours should leave. "
    "Attach supplied short source labels to each item, outside its prose; labels do not replace necessary "
    "attribution or qualifications in the text. Aim for 3000 rendered characters; "
    "use up to 4000 only for unique necessary information, including headings. Images are omitted; do not invent contents."
)
