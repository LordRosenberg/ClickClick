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
SUBMISSION_INSTRUCTION = (
    "Submit the historical summary with save_summary now. Correct rejected fields, preserve material "
    "uncertainty, and stay within the length limit. Do not add next steps or verification requirements."
)


class SummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_CHARACTERS,
                      description="Concise historical information, retaining scope, attribution and uncertainty.")
    note_source: str = Field(default="", max_length=512,
                            description="Optional exact versioned note source copied from history, only when text copies that note's retained text verbatim. Otherwise omit.")
    sources: list[str] = Field(default_factory=list,
                              description="Copy short source labels from the supplied records or previous items. Sources identify history, not proof that an inference is true.")


class StructuredSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[SummaryItem] = Field(description="Consequential observed results or artifacts; distinguish reports and inferences from observations.")
    decisions_and_attempts: list[SummaryItem] = Field(description="Only consequential past attempts: what was actually tried, observed outcome and scope. No inferred causes, advice, new requirements or copied action rationale.")
    critical_context: list[SummaryItem] = Field(description="Other exact historical details needed to continue. Preserve important uncertainty not already covered above; no new plan or question checklist.")

    @model_validator(mode="after")
    def bounded(self):
        if len(self.render()) > MAX_CHARACTERS:
            sizes = {key: sum(len(item.text) for item in getattr(self, key)) for key, _ in SECTIONS}
            raise ValueError(f"Rendered summary has {len(self.render())} characters; maximum {MAX_CHARACTERS} characters including headings. Section text lengths: {sizes}. Rewrite toward 3200 characters, rather than shaving only the excess. Merge repetition and omit resolved incidental steps; preserve unique facts, scope and uncertainty.")
        return self

    def render(self, supplied_notes: dict[str, str] | None = None) -> str:
        sections = []
        for key, heading in SECTIONS:
            lines = []
            for item in getattr(self, key):
                # Equal values from different observations must remain distinct.
                # Only the exact version AND full retained text authorize hiding.
                if item.note_source and (supplied_notes or {}).get(item.note_source) == item.text:
                    continue
                lines.append("- " + item.text)
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
                if not item.sources or any(not labels.get(ref) for ref in item.sources):
                    raise ValueError("Each summary item needs supplied source labels; copy them from records or previous items.")
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


def project_summary(value: str, retained_packet: dict | None = None) -> str:
    summary = parse_summary(value)
    if summary is None:
        return value
    supplied = {item["source"]: item["text"] for item in (retained_packet or {}).get("items", [])}
    return summary.render(supplied)


STRUCTURED_PROMPT = (
    "Summarize historical records and previous items using results, decisions_and_attempts and critical_context; "
    "leave unnecessary sections empty. Keep only unfinished-delivery facts, consequential attempt coverage/outcomes, "
    "active obstacles and important corrections. Omit routine actions, transient loading and recovered errors unless "
    "their effects still matter. Say each fact once. "
    "Describe actual attempts, observations and scope; do not infer causes or copy action rationale as fact. "
    "Preserve exact data, attribution, user-stated constraints and uncertainty. Local lookup failure is not global "
    "absence; an unverified candidate is not refuted. Plans and dispatch do not prove success. "
    "A model's preferred route is not a user requirement. Remove prescriptions inherited from model plans or "
    "previous summaries; do not add next steps, verification requirements or retry bans. Current task/stage/state "
    "are supplied separately. Preserve requirements discovered inside apps without repeating the task checklist. "
    "For supplied retained notes, use exact text with note_source when needed, not a paraphrase: runtime hides "
    "exact copies while supplied and restores them if omitted later. Do not create another unresolved checklist. "
    "Omission does not retract a relevant fact; superseded states and resolved detours should leave. "
    "Attach supplied short source labels to each item, outside its prose. Aim for 3000 rendered characters; "
    "use up to 4000 only for unique necessary information, including headings. Images are omitted; do not invent contents."
)
