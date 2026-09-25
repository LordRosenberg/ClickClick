"""Shared Chat Completions session owner for Planner / Reviewer / Executor.

Owns ``messages[]`` assembly (system, exact skill bodies, task/history,
observation), skill allowlists, and a bounded tool loop. Gateway
``complete()`` stays sessionless; this module repeatedly calls it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from agent.context_projection import estimate_context
from agent.image_region_policy import (
    MAX_PAIRS_PER_CALL, MAX_COMPARISON_REGIONS, MAX_COMPARISON_REFERENCES,
    MAX_COMPARISON_TOP_K, region_limit,
)
from agent.observation_space import COORDINATE_ROUNDING_EPSILON, ObservationRegistry, action_uses_coordinates, action_uses_index, transform_action, validate_action_bounds
from agent.prompt_measurement import measure_json_component, measure_text_component
from agent.prompts import render_skill_index
from agent.skills.library import (
    SkillLibrary,
    SkillPack,
    VerifiedAction,
    default_skills_root,
)
from agent.tool_registry import AgentRole, AgentToolRegistry, AgentToolResult, AgentToolSpec, LLMRoundRecord, NormalizedUsage, ToolAttachment, ToolCallRecord, ToolCategory, ToolExecutionContext, ToolHandler, ToolStatus, count_message_images, monotonic_ms, redact_exact_values, redact_value, stable_hash, stable_json
from perception.observation import decoded_image_size
from shared.artifacts import ArtifactStore
from shared.config import Settings, get_settings
from shared.llm_gateway import GatewayError, GatewayInputSafetyError, GatewayResponse, complete
from shared.schemas import (
    SUPPORTED_ANDROID_KEY_ACTIONS,
    Action,
    ExecutorDecisionKind,
    ExecutorStep,
    ExecutorStepSubmit,
)

logger = logging.getLogger(__name__)

Role = Literal["planner", "reviewer", "executor"]
TerminalValue = Any

_OPAQUE_EVIDENCE_FIELDS = (
    "action_observation_id",
    "observation_id",
    "coordinate_space_id",
    "transform_id",
    "frame_id",
    "timestamp_ms",
    "captured_monotonic_ms",
    "evidence_ref",
    "artifact_ref",
    "resolution_ticket",
)
_ATTACHMENT_TIMESTAMP_RE = re.compile(r" @ -?\d+(?:\.\d+)?ms$")
_TRANSPORT_LINE_RE = re.compile(
    r"^(observation_id|coordinate_space_id|transform_id|frame_id|"
    r"timestamp_ms|captured_monotonic_ms|evidence_ref|artifact_ref|"
    r"resolution_ticket):\s*.*$"
)


def _canonical_evidence_value(value: Any) -> Any:
    """Remove transport-only identity while preserving delivered content bytes.

    This is deliberately a small exact canonicalizer, not a semantic or
    perceptual comparison.  Any non-whitelisted text/image change remains in
    the digest and is therefore left to the model.
    """
    if isinstance(value, dict):
        return {
            key: _canonical_evidence_value(item)
            for key, item in sorted(value.items())
            if key not in _OPAQUE_EVIDENCE_FIELDS
        }
    if isinstance(value, list):
        return [_canonical_evidence_value(item) for item in value]
    if isinstance(value, str):
        text = value
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed, consumed = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if consumed == len(stripped):
                    return stable_json(_canonical_evidence_value(parsed))
        marker = text.find("INPUT:")
        json_start = text.find("{", marker) if marker >= 0 else -1
        if json_start >= 0:
            try:
                parsed, consumed = json.JSONDecoder().raw_decode(text[json_start:])
            except json.JSONDecodeError:
                pass
            else:
                canonical_json = stable_json(_canonical_evidence_value(parsed))
                text = (
                    text[:json_start]
                    + canonical_json
                    + text[json_start + consumed:]
                )
        return text
    return value


def _canonicalize_transport_blocks(
    message: dict[str, Any], *, message_name: str,
) -> dict[str, Any]:
    """Strip opaque values only from known protocol metadata blocks."""
    canonical = _canonical_evidence_value(message)
    content = canonical.get("content")
    if not isinstance(content, list):
        return canonical
    if message_name.startswith("observation"):
        candidate_indexes = range(1, len(content))
    elif message_name.startswith(("next_observation", "attachment_message[")):
        candidate_indexes = range(1, len(content), 2)
    else:
        candidate_indexes = ()
    for index in candidate_indexes:
        block = content[index]
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        lines = str(block.get("text") or "").splitlines()
        if not lines:
            continue
        if message_name.startswith("observation"):
            keys = {line.partition(":")[0] for line in lines if ":" in line}
            if not keys or not keys.issubset({
                "observation_id", "actionable", "indexed_targets_available",
                "image_size", "coordinate_space",
            }):
                continue
        lines[0] = _ATTACHMENT_TIMESTAMP_RE.sub(" @ <opaque-ms>", lines[0])
        lines = [
            f"{line.partition(':')[0]}: <opaque>"
            if _TRANSPORT_LINE_RE.fullmatch(line) else line
            for line in lines
        ]
        block["text"] = "\n".join(lines)
    return canonical


def delivered_evidence_digest(
    messages: list[dict[str, Any]],
    message_names: list[str] | None = None,
) -> str:
    """Hash exact model-visible evidence after transport-id canonicalization."""
    names = list(message_names or ["observation"] * len(messages))
    canonical_messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, message in enumerate(messages):
        name = names[index] if index < len(names) else "observation"
        canonical = _canonicalize_transport_blocks(message, message_name=name)
        serialized = stable_json(canonical)
        if serialized in seen:
            continue
        seen.add(serialized)
        canonical_messages.append(canonical)
    payload = stable_json(canonical_messages).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def delivered_history_digest(
    messages: list[dict[str, Any]],
    message_names: list[str] | None = None,
) -> str:
    """Hash the exact model-visible messages explicitly named ``history``."""
    names = list(message_names or [])
    delivered = [
        message
        for index, message in enumerate(messages)
        if index < len(names) and names[index] == "history"
    ]
    if not delivered:
        return ""
    return hashlib.sha256(stable_json(delivered).encode("utf-8")).hexdigest()


def _is_evidence_message_name(name: str) -> bool:
    return (
        name.startswith("observation")
        or name.startswith("next_observation")
        or name.startswith("attachment_message[observe_screen]")
        or name.startswith("model_result[observe_screen]")
        or name.startswith("model_result[search_installed_apps]")
    )

DEFAULT_MAX_TOTAL_ROUNDS = 8

_TOOL_CALL_REQUIRED_NUDGE = {
    "planner": (
        "Your previous response was not a Planner tool call. Call "
        "submit_planner_decision now without a preface."
    ),
    "reviewer": (
        "Your previous response was not a Reviewer tool call. Call the available "
        "submit_reviewer_* tool now without a preface."
    ),
    "executor": (
        "Your previous response contained only text and no tool call, so it was not a "
        "valid Executor step. Call an available tool now. If you are ready to act or "
        "finish, call submit_executor_step. Do not preface the tool call with an explanation."
    ),
}

LOAD_SKILL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "Load one optional generic Skill by an exact id from the index. "
            "Do not reload a supplied body. Current evidence overrides guidance."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "Exact id from the supplied generic Skill index"
                    ),
                },
            },
            "required": ["skill_id"],
        },
    },
}

def _function_tool(
    *, name: str, description: str, parameters: dict[str, Any],
) -> dict[str, Any]:
    """Build one static model-visible tool from the portable schema subset."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def _submit_executor_tool() -> dict[str, Any]:
    return _function_tool(
        name="submit_executor_step",
        description=(
            "Submit act with one device action, or request_review/request_replan "
            "without an action. Act requires action; boundary requests must omit it. "
            "Keep act summary to a very short operation intent; state established facts "
            "only for request_review. Runtime derives evidence, receipts, and routing."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [decision.value for decision in ExecutorDecisionKind],
                },
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "For act, one very short action intent with no reasoning or "
                        "completion claim. For request_review, concise facts established "
                        "by delivered evidence. For request_replan, the concise blocker."
                    ),
                },
                "action": _executor_action_schema(),
            },
            "required": ["decision", "summary"],
        },
    )


_EXECUTOR_SUBMIT_REQUIRED_FIELDS = tuple(
    ExecutorStepSubmit.model_json_schema().get("required") or ()
)


def _delivered_text(content: Any) -> str:
    """Read exact delivered text, including JSON tool results, for skill checks."""
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except (ValueError, TypeError):
            return content
        return _delivered_text(decoded) if isinstance(decoded, (dict, list)) else content
    if isinstance(content, dict):
        return "\n".join(_delivered_text(value) for key, value in content.items() if key != "image_url")
    if isinstance(content, list):
        return "\n".join(_delivered_text(value) for value in content)
    return ""


_ACTION_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "tap": ("index",),
    "tap_xy": ("x", "y"),
    "double_tap": ("x", "y"),
    "skill_authorized_action": ("skill_action_id",),
    "type": ("text",),
    "replace_text": ("text",),
    "swipe": ("x", "y", "x2", "y2"),
    "long_press": (),
    "scroll": ("direction",),
    "drag": ("x", "y", "x2", "y2"),
    "key": ("key",),
    "launch": ("app",),
    "back": (),
    "home": (),
    "sleep": (),
}

_ACTION_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "index": {"type": "integer"},
    "surface_index": {"type": "integer", "minimum": 0,
                      "description": "For a drag confined to an indexed surface, supply its current index. Both endpoints must stay inside that surface."},
    "x": {"type": "number"},
    "y": {"type": "number"},
    "x2": {"type": "number"},
    "y2": {"type": "number"},
    "text": {"type": "string", "minLength": 1},
    "key": {
        "type": "string",
        "enum": list(SUPPORTED_ANDROID_KEY_ACTIONS),
        "description": "One supported Android key event. Key chords are unsupported.",
    },
    "app": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Launch the target App directly by its human-facing name or exact "
            "package; do not navigate launcher UI to find it. A miss dispatches "
            "nothing; use its ticket with search_installed_apps in this "
            "invocation. Search results are bounded hints, not authorization; "
            "launch the intended exact package, or request_replan when no "
            "plausible installed target is known."
        ),
    },
    "direction": {
        "type": "string",
        "enum": ["up", "down", "left", "right"],
        "description": (
            "Content direction: down reveals content below; up above; right "
            "to the right; left to the left. Scroll uses a fixed short gesture; "
            "duration changes its speed, not distance. Use swipe to specify finger travel."
        ),
    },
    "duration_ms": {"type": "integer", "minimum": 0},
    "skill_action_id": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Exact ID from the current stage-authorized actions. Unauthorized "
            "invocation is prohibited; execution also requires the owning App "
            "to be the verified foreground App."
        ),
    },
}

_ACTION_OPTIONAL_PARAMS: dict[str, tuple[str, ...]] = {
    "skill_authorized_action": ("index", "x", "y"),
    "replace_text": ("index",),
    "swipe": ("duration_ms",),
    "long_press": ("index", "x", "y", "duration_ms"),
    "scroll": ("duration_ms",),
    "drag": ("duration_ms", "surface_index"),
    "key": ("duration_ms",),
    "sleep": ("duration_ms",),
}


def _executor_action_schema() -> dict[str, Any]:
    requirements = "; ".join(
        f"{action_type}="
        + (
            ",".join(required) if required else "no additional required fields"
        )
        for action_type, required in _ACTION_REQUIRED_PARAMS.items() if action_type != "double_tap"
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "Use index for indexed taps, skill_authorized_action, long presses and supported targeted text replacement. Coordinate clicks "
            "are only for targets without a current usable index. "
            "One action request. Required fields by type: " + requirements + ". "
            "long_press and skill_authorized_action require index or x,y. "
            "skill_authorized_action also requires an exact current stage-authorized "
            "skill_action_id; unauthorized invocation is prohibited and execution "
            "requires its owning App to be foreground. type inserts once at the cursor; "
            "replace_text clears and enters final text, optionally focusing its indexed field first; key sends "
            "one Android key event and chords such as CTRL+A are unsupported; sleep "
            "waits for a required elapsed duration, consumes a device-action unit and acquires no evidence. "
            "Use observe_screen to determine whether loading or a UI transition has completed."
        ),
        "properties": {
            "type": {
                "type": "string",
                "enum": [kind for kind in _ACTION_REQUIRED_PARAMS if kind != "double_tap"],
            },
            **{name: dict(schema) for name, schema in _ACTION_FIELD_SCHEMAS.items()},
        },
        "required": ["type"],
    }


def executor_action_variants_schema(*, double_tap=False) -> dict[str, Any]:
    """Publish the same per-action field sets used by dispatch validation."""
    variants = []
    for kind, required in _ACTION_REQUIRED_PARAMS.items():
        if kind == "double_tap" and not double_tap:
            continue
        optional = _ACTION_OPTIONAL_PARAMS.get(kind, ())
        variant = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string", "enum": [kind]},
                **{name: dict(_ACTION_FIELD_SCHEMAS[name]) for name in (*required, *optional)},
            },
            "required": ["type", *required],
        }
        if kind == "double_tap":
            variant["description"] = "Double-tap current image coordinates, e.g. to toggle zoom on a document/image. Changes the device and consumes one action; app support varies. Use the resulting fresh observation. Do not use for repeated button activation."
        if kind == "tap_xy":
            variant["description"] = "Only for a target without a current usable index. Otherwise use tap(index)."
        if kind == "skill_authorized_action":
            variant["description"] = (
                "Invoke one verified compound action supplied by current stage guidance. "
                "Use an exact stage-authorized skill_action_id; unauthorized invocation "
                "is prohibited and execution requires its owning App to be the verified "
                "foreground App. Target a current index when available, otherwise current-image "
                "x,y coordinates. Internal recipe parameters cannot be supplied."
            )
            variant["oneOf"] = [
                {"required": ["index"], "not": {"anyOf": [{"required": ["x"]}, {"required": ["y"]}]}},
                {"required": ["x", "y"], "not": {"required": ["index"]}},
            ]
        if kind == "long_press":
            variant["description"] = "Use index for an indexed target; x,y only when it has no usable current index."
            variant["anyOf"] = [{"required": ["index"]}, {"required": ["x", "y"]}]
        if kind == "type":
            variant["description"] = "Enter text in the focused editable; focus it with a separate tap first."
        if kind == "replace_text":
            variant["description"] = "Replace the full field value. With index, requires an editable with a unique resource id that remains the same after focus. Without index, replaces the already-focused field. If indexed replacement is unsupported, focus the intended field separately, confirm focus, then omit index; tapping does not make an unsupported index valid."
        if kind == "sleep":
            variant["description"] = "Wait for a required elapsed duration, such as recording time. Consumes a device-action unit and acquires no evidence. Use observe_screen to determine whether loading or a UI transition has completed."
        variants.append(variant)
    return {
        "type": "object",
        "properties": {"type": {"type": "string", "enum": [
            kind for kind in _ACTION_REQUIRED_PARAMS if kind != "double_tap" or double_tap
        ]}},
        "required": ["type"],
        "anyOf": variants,
    }


OBSERVE_SCREEN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "observe_screen",
        "description": (
            "Acquire snapshot or sequence screen evidence inside this invocation. "
            "Window and coordinate checks do not guarantee that tree and pixels show the same content during transitions. "
            "Use snapshot to recheck conflicting evidence and sequence when change over time matters."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["snapshot", "sequence"],
                },
                "frames": {
                    "type": "integer",
                    "enum": [2, 3],
                    "description": "Sequence only; defaults to the runtime frame count.",
                },
                "duration_ms": {
                    "type": "integer",
                    "minimum": 300,
                    "maximum": 1500,
                    "description": "Sequence only; total capture duration.",
                },
            },
            "required": ["mode"],
        },
    },
}

SEARCH_INSTALLED_APPS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_installed_apps",
        "description": (
            "Search installed packages only after a deterministic resolver miss. Requires the "
            "same-task/device/subgoal one-use resolution_ticket. You may translate the App "
            "name or use a package keyword. Results are bounded hints; explicitly choose the "
            "intended exact installed package."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "resolution_ticket": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One-use ticket from app_resolution_miss context/result",
                },
            },
            "required": ["query", "resolution_ticket"],
        },
    },
}

INSPECT_IMAGE_REGIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "inspect_image_regions",
        "description": (
            "Sample exact pixels or compare a visible group of color candidates. Put indexed controls in targets; "
            "put unindexed reference/candidate areas in regions using current model-image bounds. Results identify index:N or "
            "region:N (1-based input order); a free region never establishes a control index. "
            "One call per response: at most 12 total samples, or 32 with compare (at most 8 references). "
            "Supply metrics for sampling; compare alone returns compact rankings, adding metrics returns both. "
            "Group comparison expands pairs internally; do not enumerate unrelated page regions."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "observation_id": {"type": "string", "minLength": 1},
                "targets": {
                    "type": "array", "maxItems": MAX_COMPARISON_REGIONS,
                    "items": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "index": {"type": "integer", "minimum": 0},
                            "inset_ratio": {"type": "number", "minimum": 0, "maximum": 0.45},
                        }, "required": ["index"]},
                },
                "regions": {
                    "type": "array", "maxItems": MAX_COMPARISON_REGIONS,
                    "items": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "bounds": {"type": "array", "minItems": 4, "maxItems": 4,
                                       "items": {"type": "number"}},
                            "inset_ratio": {"type": "number", "minimum": 0, "maximum": 0.45},
                        }, "required": ["bounds"]},
                },
                "metrics": {"type": "array", "minItems": 1,
                    "items": {"type": "string", "enum": ["median_rgb", "dominant_rgb", "lab"]}},
                "pairs": {
                    "description": "Optional pairs of generated IDs, e.g. [region:1, index:7].",
                    "type": "array", "maxItems": MAX_PAIRS_PER_CALL,
                    "items": {"type": "array", "minItems": 2, "maxItems": 2,
                              "items": {"type": "string", "minLength": 1}},
                },
                "compare": {
                    "type": "object", "additionalProperties": False,
                    "description": "Compare disjoint groups of sampled IDs. Omit metrics for rankings only. "
                        "Each top row is [candidate ID, delta E 2000]; ties_truncated flags ties cut by top_k. "
                        "Nearest means closest among supplied candidates, not proof of an exact match.",
                    "properties": {
                        key: {"anyOf": [
                            {"type": "string", "enum": ["all_regions", "all_targets"]},
                            {"type": "array", "minItems": 1, "maxItems": maximum,
                             "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                        ]} for key, maximum in (
                            ("references", MAX_COMPARISON_REFERENCES),
                            ("candidates", MAX_COMPARISON_REGIONS),
                        )
                    } | {"top_k": {"type": "integer", "minimum": 1,
                                    "maximum": MAX_COMPARISON_TOP_K, "default": 3}},
                    "required": ["references", "candidates"],
                },
            }, "required": ["observation_id"],
            "anyOf": [{"required": ["metrics"]}, {"required": ["compare"]}],
        },
    },
}


def _spec_from_tool(
    tool: dict[str, Any], category: ToolCategory, roles: tuple[AgentRole, ...],
    *, timeout_ms: int = 10_000, updates_action_context: bool = False,
) -> AgentToolSpec:
    fn = tool["function"]
    return AgentToolSpec(
        name=fn["name"], description=fn["description"], category=category,
        roles=roles,
        parameters=fn["parameters"],
        timeout_ms=timeout_ms,
        updates_action_context=updates_action_context,
    )


def _catalog_specs(
    role: Role,
) -> list[AgentToolSpec]:
    try:
        role = AgentRole(role).value
    except ValueError as exc:
        raise ValueError(f"unsupported agent role: {role}") from exc
    specs: list[AgentToolSpec] = []
    if role == "planner":
        interactive_roles = (AgentRole.PLANNER,)
        specs.extend([
            _spec_from_tool(LOAD_SKILL_TOOL, ToolCategory.KNOWLEDGE, interactive_roles),
            _spec_from_tool(
                OBSERVE_SCREEN_TOOL,
                ToolCategory.OBSERVATION,
                interactive_roles,
                timeout_ms=10_000,
                updates_action_context=True,
            ),
        ])
    elif role in {"reviewer", "executor"}:
        role_value = AgentRole(role)
        specs.append(_spec_from_tool(
            OBSERVE_SCREEN_TOOL,
            ToolCategory.OBSERVATION,
            (role_value,),
            timeout_ms=10_000,
            updates_action_context=True,
        ))
    if role == "executor":
        specs.extend([
            _spec_from_tool(LOAD_SKILL_TOOL, ToolCategory.KNOWLEDGE, (AgentRole.EXECUTOR,)),
            _spec_from_tool(INSPECT_IMAGE_REGIONS_TOOL, ToolCategory.OBSERVATION,
                            (AgentRole.EXECUTOR,)),
            _spec_from_tool(SEARCH_INSTALLED_APPS_TOOL, ToolCategory.DEVICE_DISCOVERY,
                            (AgentRole.EXECUTOR,)),
            _spec_from_tool(_submit_executor_tool(), ToolCategory.TERMINAL, (AgentRole.EXECUTOR,)),
        ])
    else:
        from agent.revisable.tools import tool_schema
        from shared.revisable import PlannerDecision, Review

        schema = PlannerDecision if role == "planner" else Review
        specs.append(_spec_from_tool(
            _function_tool(
                name=f"submit_{role}_decision",
                description="Submit the next plan-driven decision from current evidence.",
                parameters=tool_schema(schema),
            ), ToolCategory.TERMINAL, (AgentRole(role),),
        ))
    return specs


def tools_for_role(
    role: Role,
) -> list[dict[str, Any]]:
    """Return the byte-stable role catalog in registry order."""
    specs = _catalog_specs(role)
    return [
        s.chat_completion_schema() for s in sorted(specs, key=lambda s: s.name)
    ]


@dataclass
class SessionResult:
    decision: TerminalValue
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    llm_rounds: list[LLMRoundRecord] = field(default_factory=list)
    raw_messages: list[dict[str, Any]] = field(default_factory=list)
    dialogue_messages: list[dict[str, Any]] = field(default_factory=list)
    context_state: dict[str, Any] = field(default_factory=dict)
    request_snapshot: dict[str, Any] = field(default_factory=dict)


_CONTENT_SAFETY_DEGRADED_NOTICE = (
    "CONTENT-SAFETY DEGRADED MODE: Upstream rejected one or more image "
    "attachments, so all images are omitted for the rest of this invocation. "
    "Use the remaining accessibility tree, text, focus, and structured "
    "observations. Do not submit tap_xy, swipe, drag, or coordinate-based "
    "long_press actions because the image coordinate reference is unavailable."
)


def _project_messages_without_images(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return a non-mutating text/structure-only projection of ``messages``."""
    projected: list[dict[str, Any]] = []
    removed = 0
    for message in messages:
        item = dict(message)
        content = item.get("content")
        if isinstance(content, list):
            blocks: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") in {"image", "image_url"}:
                    removed += 1
                    continue
                blocks.append(block)
            item["content"] = blocks
        projected.append(item)
    if removed:
        projected.append({"role": "user", "content": _CONTENT_SAFETY_DEGRADED_NOTICE})
    return projected, removed


class AgentSession:
    """Per-role session with S → K → T/H → O/X message buckets."""

    def __init__(
        self,
        role: Role,
        model: str,
        *,
        library: SkillLibrary | None = None,
        settings: Settings | None = None,
        max_total_rounds: int = DEFAULT_MAX_TOTAL_ROUNDS,
    ) -> None:
        try:
            self.role = AgentRole(role).value
        except ValueError as exc:
            raise ValueError(f"unsupported agent role: {role}") from exc
        self.model = model
        self.settings = settings or get_settings()
        self.library = library or SkillLibrary(default_skills_root(), device_profiles=[])
        self.max_total_rounds = max_total_rounds

        self._lifecycle_key: str | None = None
        self._stable: list[dict[str, Any]] = []
        self._index: list[dict[str, Any]] = []
        self._workflow_catalog: list[dict[str, Any]] = []
        self._workflow_catalog_ids: frozenset[str] = frozenset()
        # Side-store of skill body messages keyed by scope (generic | apps/<pkg>).
        self._k_store: dict[str, list[dict[str, Any]]] = {}
        self._loaded_ids: set[str] = set()
        self._foreground_app: str = ""
        self._target_app: str = ""
        self._selected_workflow_ids: list[str] = []
        self._stage_generic_ids: set[str] | None = None
        self._allow_dirs: list[str] | None = None
        self._frozen_dirs: list[str] = []
        self._missing_skill_ids: set[str] = set()
        self._skill_activation_metadata: dict[str, dict[str, Any]] = {}

    async def bind_device_skills(self, driver: Any) -> None:
        """Filter discovery and exact-id loading using the bound device profile."""
        provider = getattr(driver, "skill_profile_ids", None)
        try:
            profiles = await provider() if callable(provider) else []
        except Exception:  # Missing identity must never enable another system's skills.
            profiles = []
        profiles = profiles if isinstance(profiles, (list, tuple)) else []
        selected = frozenset(value for value in profiles if isinstance(value, str))
        if self.library.device_profiles == selected:
            return
        self.library = self.library.for_device(selected)
        # A device switch must not retain previously delivered scoped bodies.
        self._lifecycle_key = None
        self.reset_lifecycle("device-profile-change")

    @property
    def frozen_allow_dirs(self) -> list[str]:
        return list(self._frozen_dirs)

    def reset_lifecycle(self, key: str) -> bool:
        """Reset skill side-store when the task lifecycle key changes.

        All roles use a task-scoped key. Subgoal changes MUST
        NOT call this with a new key.

        Returns True when a new lifecycle started (caller should freeze scope).
        """
        if key == self._lifecycle_key:
            return False
        self._lifecycle_key = key
        self._k_store = {}
        self._loaded_ids = set()
        self._foreground_app = ""
        self._target_app = ""
        self._selected_workflow_ids = []
        self._stage_generic_ids = None
        self._missing_skill_ids = set()
        self._skill_activation_metadata = {}
        self._index = []
        self._workflow_catalog = []
        self._workflow_catalog_ids = frozenset()
        self._frozen_dirs = []
        self._allow_dirs = None
        return True

    def set_foreground_app(
        self,
        app_id: str | None,
    ) -> None:
        """Record observed foreground; it does not replace a selected target."""
        self._foreground_app = (app_id or "").strip()
        if self._foreground_app:
            pack = self.library.app_core(self._foreground_app)
            if pack is not None:
                self._store_skill_message(pack, label="foreground_app_core")

    def set_workflow_catalog(self, apps: list[str], *, core_apps: list[str] | None = None) -> None:
        """Expose compact workflow cards to Planner without loading bodies."""
        cards = self.library.workflow_catalog(apps)
        self._workflow_catalog_ids = frozenset(card["id"] for card in cards)
        self._workflow_catalog = [] if not cards else [{
            "role": "user",
            "content": (
                "WORKFLOW CATALOG (optional; select only exact ids from one App):\n"
                + json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
            ),
        }]
        for app in dict.fromkeys(core_apps or []):
            core = self.library.app_core(app)
            if core is not None and app != (self._target_app or self._foreground_app):
                self._workflow_catalog.append(self.skill_message(core, label="planning_app_core"))

    def skill_message(self, pack: SkillPack, *, label: str) -> dict[str, str]:
        return {
            "role": "user",
            "content": f"[{label} skill:{pack.id}@{pack.version}]\n"
            + pack.section_for(self._skill_projection_role()),
        }

    @property
    def workflow_catalog_ids(self) -> frozenset[str]:
        return self._workflow_catalog_ids

    def set_target_app(self, app_id: str | None, workflow_ids: list[str] | None = None) -> None:
        """Select one App core and zero to two owned workflow bodies."""
        target = (app_id or "").strip()
        selected = [skill_id.strip() for skill_id in (workflow_ids or []) if skill_id.strip()]
        if len(selected) > 2 or len(set(selected)) != len(selected):
            raise ValueError("workflow selection must contain zero to two unique ids")
        if selected and not target:
            raise ValueError("workflow selection requires a target App")
        packs: list[SkillPack] = []
        for skill_id in selected:
            pack = self.library.get(skill_id)
            if pack is None or pack.kind != "workflow" or pack.app != target:
                raise ValueError(f"workflow {skill_id!r} is not owned by {target!r}")
            packs.append(pack)
        self._target_app = target
        self._selected_workflow_ids = selected
        if target:
            core = self.library.app_core(target)
            if core is not None:
                self._store_skill_message(core, label="target_app_core")
        for pack in packs:
            self._store_skill_message(pack, label="selected_workflow")

    def validate_stage_skills(self, app_id: str, skill_ids: list[str]) -> list[SkillPack]:
        if len(skill_ids) > 4 or len(set(skill_ids)) != len(skill_ids):
            raise ValueError("Select at most four unique catalog skill IDs")
        packs = []
        for sid in skill_ids:
            pack = self.library.get(sid)
            if pack is None or not (pack.kind == "generic" or
                    (pack.kind == "workflow" and pack.app == app_id and app_id)):
                raise ValueError(f"Unknown or out-of-scope stage skill: {sid}; copy an exact catalog ID")
            if not pack.section_for("executor").strip():
                raise ValueError(f"Stage skill has no execution guidance: {sid}")
            packs.append(pack)
        if sum(p.kind == "workflow" for p in packs) > 2:
            raise ValueError("Select at most two target-owned workflows")
        return packs

    def set_stage_skills(self, app_id: str, skill_ids: list[str]) -> None:
        packs = self.validate_stage_skills(app_id, skill_ids)
        self.set_target_app(app_id, [p.id for p in packs if p.kind == "workflow"])
        self._stage_generic_ids = {p.id for p in packs if p.kind == "generic"}
        for pack in packs:
            if pack.kind == "generic":
                self._store_skill_message(pack, label="selected_stage_skill")

    @property
    def loaded_skill_ids(self) -> list[str]:
        return sorted(self._loaded_ids)

    def _k_wire(self) -> list[dict[str, Any]]:
        """Compose generic knowledge and the selected App handoff."""
        candidates: list[dict[str, Any]] = []
        candidates.extend(
            message for message in (self._k_store.get("generic") or [])
            if self._stage_generic_ids is None or message.get("_skill_id") in self._stage_generic_ids
        )
        for app in dict.fromkeys(filter(None, (self._target_app, self._foreground_app))):
            allowed_ids = set(self._selected_workflow_ids) if app == self._target_app else set()
            core = self.library.app_core(app)
            if core is not None:
                allowed_ids.add(core.id)
            candidates.extend(
                message
                for message in (self._k_store.get(f"apps/{app}") or [])
                if str(message.get("_skill_id") or "") in allowed_ids
            )
        projected = self._project_skill_messages(candidates)
        availability = self._authorized_actions_message()
        if availability is not None:
            projected.append(availability)
        return projected

    def available_authorized_actions(self, app_id: str | None = None) -> list[dict[str, str]]:
        """Return the model-safe catalog frozen by the current target stage."""
        if self.role != "executor":
            return []
        app = (app_id if app_id is not None else self._target_app).strip()
        if not app or app != self._target_app:
            return []
        active_ids = set(self._selected_workflow_ids)
        core = self.library.app_core(app)
        if core is not None:
            active_ids.add(core.id)
        summaries: list[dict[str, str]] = []
        seen: set[str] = set()
        for skill_id in sorted(active_ids):
            pack = self.library.get(skill_id)
            if pack is None or pack.app != app or pack.kind not in {"app_core", "workflow"}:
                continue
            for action in pack.verified_actions:
                if action.id in seen:
                    continue
                seen.add(action.id)
                inspection = action.template == "tap_capture_key"
                summaries.append({
                    "id": action.id,
                    "purpose": action.purpose,
                    "target": "current index, or current-image x,y when no usable index exists",
                    "result": (
                        "historical intermediate evidence is persisted with the action result and "
                        "restored before the latest actionable state; do not call read_history to "
                        "retrieve that intermediate evidence"
                        if inspection else
                        "the latest actionable state after the verified time-sensitive operation"
                    ),
                    "budget": (
                        "Use for a needed detail-page inspection when separate open and return "
                        "actions would exceed or unnecessarily consume the remaining action budget; "
                        "the complete inspection-and-return consumes one submitted action unit."
                        if inspection else
                        "Use only for its stated time-sensitive App scenario; the verified compound "
                        "operation consumes one submitted action unit."
                    ),
                })
        return summaries

    def authorized_action(
        self, action_id: str, app_id: str,
    ) -> tuple[SkillPack, VerifiedAction] | None:
        """Resolve one stage-authorized recipe only for the exact foreground App."""
        wanted = (action_id or "").strip()
        app = (app_id or "").strip()
        if (
            not wanted
            or not app
            or app != self._foreground_app
            or app != self._target_app
        ):
            return None
        visible = {item["id"] for item in self.available_authorized_actions(app)}
        if wanted not in visible:
            return None
        active_ids = set(self._selected_workflow_ids)
        core = self.library.app_core(app)
        if core is not None:
            active_ids.add(core.id)
        for skill_id in sorted(active_ids):
            pack = self.library.get(skill_id)
            if pack is None or pack.app != app or pack.kind not in {"app_core", "workflow"}:
                continue
            for action in pack.verified_actions:
                if action.id == wanted:
                    return pack, action
        return None

    def _authorized_actions_message(self) -> dict[str, Any] | None:
        actions = self.available_authorized_actions()
        if not actions:
            return None
        return {
            "role": "user",
            "content": (
                "STAGE-AUTHORIZED ACTIONS\n"
                "Use only an exact listed id; unauthorized invocation is prohibited. "
                "A listed action is executable only while its owning App is the verified "
                "foreground App.\n"
                + json.dumps(actions, ensure_ascii=False, separators=(",", ":"))
            ),
        }

    @staticmethod
    def _project_skill_messages(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        for message in candidates:
            content = str(message.get("content") or "")
            sid = str(message.get("_skill_id") or "")
            content_hash = str(message.get("_content_hash") or stable_hash(content))
            if (sid and sid in seen_ids) or content_hash in seen_hashes:
                continue
            if sid:
                seen_ids.add(sid)
            seen_hashes.add(content_hash)
            out.append({"role": str(message.get("role") or "user"), "content": content})
        return out

    def _store_skill_message(self, pack: SkillPack, *, label: str) -> None:
        sid = pack.name
        body = pack.section_for(self._skill_projection_role())
        previous = self._skill_activation_metadata.get(sid, {})
        if previous.get("version") == pack.version and previous.get("content_hash") == stable_hash(body):
            return
        for messages in self._k_store.values():
            messages[:] = [message for message in messages if message.get("_skill_id") != sid]
        scope = pack.scope_key()
        msg = {
            **self.skill_message(pack, label=label),
            "_skill_id": sid,
            "_content_hash": stable_hash(body),
        }
        self._k_store.setdefault(scope, []).append(msg)
        self._loaded_ids.add(sid)
        self._skill_activation_metadata[sid] = {
            "skill_id": sid,
            "version": pack.version,
            "content_hash": stable_hash(body),
            "scope": scope,
            "activation_source": label,
            "rule_categories": pack.rule_categories(),
            "verified_action_ids": [action.id for action in pack.verified_actions],
            "device_profiles": pack.device_profiles,
            "interface_scope": pack.interface_scope,
            "selected_device_profiles": sorted(self.library.device_profiles or []),
        }

    def freeze_allow_dirs(self, dirs: list[str]) -> list[str]:
        """Freeze generic discovery scope and its stable exact-id index."""
        clean = [d.strip().strip("/") for d in dirs if d and d.strip()]
        if not clean:
            clean = ["generic"]
        self._frozen_dirs = clean
        self._allow_dirs = list(clean)
        generic_dirs = [
            directory for directory in self._allow_dirs
            if directory == "generic" or directory.startswith("generic/")
        ]
        self._index = [{
            "role": "user",
            "content": render_skill_index(
                self.library.index_summaries(allow_dirs=generic_dirs),
            ),
        }]
        return list(self._frozen_dirs)

    def _skill_projection_role(self) -> Literal["decision", "planner", "reviewer", "executor"]:
        return self.role if self.role in {"planner", "reviewer", "executor"} else "decision"

    def set_stable_system(self, system_text: str) -> None:
        """System message: static role rules (no skill index / subgoal)."""
        self._stable = [{"role": "system", "content": system_text.rstrip() + "\n"}]

    @property
    def active_skill_metadata(self) -> list[dict[str, Any]]:
        allowed_app_ids = set(self._selected_workflow_ids)
        apps = set(filter(None, (self._target_app, self._foreground_app)))
        for app in apps:
            core = self.library.app_core(app)
            if core is not None:
                allowed_app_ids.add(core.id)
        active_ids: set[str] = set()
        for skill_id, item in self._skill_activation_metadata.items():
            scope = str(item.get("scope") or "")
            if (scope == "generic" and (self._stage_generic_ids is None or skill_id in self._stage_generic_ids)) or (
                scope in {f"apps/{app}" for app in apps} and skill_id in allowed_app_ids
            ):
                active_ids.add(skill_id)
        return [
            dict(self._skill_activation_metadata[skill_id])
            for skill_id in sorted(active_ids)
            if skill_id in self._skill_activation_metadata
        ]


    def scoped_packs(self) -> list[SkillPack]:
        return self.library.list_scoped(
            allow_dirs=self._allow_dirs or self._frozen_dirs or None,
        )

    def _resolve_allowed(self, skill_id: str) -> SkillPack | None:
        pack = self.library.get(skill_id)
        if pack is None:
            return None
        scoped_ids = {p.id for p in self.scoped_packs()}
        if skill_id not in scoped_ids:
            return None
        return pack

    async def run(
        self,
        observation_messages: list[dict[str, Any]],
        *,
        history_messages: list[dict[str, Any]] | None = None,
        final_messages: list[dict[str, Any]] | None = None,
        observation_message_names: list[str] | None = None,
        history_message_names: list[str] | None = None,
        final_message_names: list[str] | None = None,
        cache_system_prefix: bool = True,
        include_skill_context: bool = True,
        handlers: dict[str, ToolHandler] | None = None,
        tool_registry: AgentToolRegistry | None = None,
        retain_observations: bool = False,
        reserve_terminal_round: bool = False,
        terminal_submission_instruction: str | None = None,
        context_state: dict[str, Any] | None = None,
        event_sink: Callable[[str, dict[str, Any]], Any] | None = None,
        model_call_meter: Callable[[str, dict[str, Any]], Any] | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> SessionResult:
        """Run one bounded invocation inside the task-scoped session lifecycle.

        A terminal submit ends this invocation's tool loop, not the
        ``AgentSession`` lifecycle; retained relevant-Skill state remains available
        to later ``run()`` calls for the same lifecycle key.
        """
        if not self._stable:
            raise RuntimeError("AgentSession.set_stable_system() required before run()")
        if self._allow_dirs is None:
            # Ensure tool scope exists even if caller forgot to freeze.
            self.freeze_allow_dirs(["generic"])

        index_messages = (
            list(self._index)
            if include_skill_context and self.role == "planner"
            else []
        )
        catalog_messages = (
            list(self._workflow_catalog)
            if include_skill_context and self.role == "planner"
            else []
        )
        k_wire_messages = self._k_wire() if include_skill_context else []
        prefix_entries: list[tuple[str, dict[str, Any]]] = [
            *[("system", message) for message in self._stable],
            *[("skill_index", message) for message in index_messages],
            *[("workflow_catalog", message) for message in catalog_messages],
            *[(f"k_wire_messages[{index}]", message) for index, message in enumerate(k_wire_messages)],
            *[
                (
                    history_message_names[index]
                    if history_message_names is not None and index < len(history_message_names)
                    else f"history_messages[{index}]",
                    message,
                )
                for index, message in enumerate(history_messages or [])
            ],
        ]
        prefix_messages = [message for _, message in prefix_entries]
        prefix_message_names = [name for name, _ in prefix_entries]
        observation_bucket = list(observation_messages)
        observation_bucket_names = [
            observation_message_names[index]
            if observation_message_names is not None and index < len(observation_message_names)
            else f"observation_messages[{index}]"
            for index in range(len(observation_bucket))
        ]
        final_bucket = list(final_messages or [])
        final_bucket_names = [
            final_message_names[index]
            if final_message_names is not None and index < len(final_message_names)
            else f"final_messages[{index}]"
            for index in range(len(final_bucket))
        ]
        # Keep tool dialogue on the side of the current observation where it
        # actually occurred. Knowledge/discovery tools append after the
        # observation they used. A successful observe_screen promotes that
        # dialogue before the replacement observation and drops the stale one.
        transcript_before_observation: list[dict[str, Any]] = []
        transcript_before_observation_names: list[str] = []
        transcript_after_observation: list[dict[str, Any]] = []
        transcript_after_observation_names: list[str] = []
        messages: list[dict[str, Any]] = [
            *prefix_messages,
            *observation_bucket,
            *final_bucket,
        ]
        invocation_id = uuid.uuid4().hex
        context = ToolExecutionContext(
            role=AgentRole(self.role), invocation_id=invocation_id,
            state={**dict(context_state or {}), "double_tap_enabled": self.settings.double_tap},
        )
        registry = tool_registry or self._build_registry(handlers or {})
        tools = registry.catalog_for_role(context.role)
        terminal_names = [
            spec.name
            for spec in registry.specs_for_role(context.role)
            if spec.category == ToolCategory.TERMINAL
        ]
        required_tool_nudge = _TOOL_CALL_REQUIRED_NUDGE[self.role]
        if len(terminal_names) == 1:
            required_tool_nudge = (
                "Your previous response contained only text and no tool call. "
                f"Call {terminal_names[0]} now without a preface."
            )
        catalog_hash = registry.catalog_hash(context.role)
        stable_prefix_hash = stable_hash([
            *self._stable, *index_messages, *catalog_messages, *k_wire_messages,
        ])
        tool_records: list[ToolCallRecord] = []
        llm_rounds: list[LLMRoundRecord] = []
        request_rounds: list[dict[str, Any]] = []
        content_safety_degraded = False
        content_safety_original_image_count = 0
        content_safety_removed_image_count = 0
        content_safety_offending_locations: list[tuple[int, int]] = []
        content_only_retries = 0
        submission_start = None

        for round_index in range(1, self.max_total_rounds + 1):
            submitting = (
                reserve_terminal_round
                and (context.state.get("evidence_saturated")
                     or round_index >= min(self.max_total_rounds, max(2, self.max_total_rounds - 1)))
                and bool(terminal_names)
            )
            if submitting:
                submission_start = submission_start or round_index
                if round_index >= submission_start + 2:
                    raise GatewayError("Decision submission did not converge after evidence gathering", category="budget")
            context.state["terminal_submission_only"] = submitting
            if submitting:
                tools = [tool for tool in tools if tool["function"]["name"] in terminal_names]
                catalog_hash = stable_hash(tools)
            # Rebuild the dynamic suffix before every gateway call. Tool
            # transition text remains bounded by max_total_rounds, while the
            # observation bucket is replaced wholesale after observation tools.
            messages = [
                *prefix_messages,
                *transcript_before_observation,
                *observation_bucket,
                *transcript_after_observation,
                *final_bucket,
            ]
            message_names = [
                *prefix_message_names,
                *transcript_before_observation_names,
                *observation_bucket_names,
                *transcript_after_observation_names,
                *final_bucket_names,
            ]
            if submitting:
                messages.append({
                    "role": "user",
                    "content": terminal_submission_instruction or (
                        "Evidence gathering for this invocation has ended. Submit your decision "
                        "now using the available terminal tool and the evidence already supplied. "
                        "If facts remain unknown, state the gap and the supported next step in "
                        "your decision; do not invent certainty. Correct any rejected fields."
                    ),
                })
                message_names.append("submission_boundary")
            wire_messages = messages
            round_original_image_count = count_message_images(messages)
            round_removed_image_count = 0
            evidence_entries = [
                (name, message)
                for name, message in zip(message_names, messages)
                if _is_evidence_message_name(name)
            ]
            evidence_names = [name for name, _ in evidence_entries]
            evidence_messages = [message for _, message in evidence_entries]
            if content_safety_degraded:
                wire_messages, round_removed_image_count = _project_messages_without_images(messages)
                evidence_messages, _ = _project_messages_without_images(evidence_messages)
            context.state["delivered_history_digest"] = delivered_history_digest(
                wire_messages,
                message_names,
            )
            context.state["delivered_evidence_digest"] = delivered_evidence_digest(
                evidence_messages,
                evidence_names,
            )
            current_visual = context.state.get("visual_evidence_metadata")
            input_visual = None
            if isinstance(current_visual, dict) and count_message_images(wire_messages) > 0:
                input_visual = {
                    "observation_id": str(current_visual.get("observation_id") or ""),
                    "model_image_ref": current_visual.get("image_artifact_ref"),
                    "visual_kind": current_visual.get("visual_kind"),
                    "captured_monotonic_ms": current_visual.get("captured_monotonic_ms"),
                }
            message_snapshot = _snapshot_messages(wire_messages, context)
            context.state["delivered_skill_text"] = "\n".join(
                _delivered_text(message.get("content")) for message in wire_messages
            )
            terminal_tool_choice = self.settings.tool_choice_for(self.model)
            context_policy = self.settings.context_policy(self.model, self.role)
            request_estimate = estimate_context(wire_messages) + estimate_context([
                {"role": "system", "content": json.dumps(tools, ensure_ascii=False)},
            ])
            input_limit = context_policy.get("max_input_tokens")
            if input_limit is not None and request_estimate > input_limit:
                raise GatewayError(
                    f"Estimated request size {request_estimate} exceeds configured input limit {input_limit}",
                    category="context_capacity",
                )
            request_payload = {
                "schema_version": 2,
                "decision_context_version": "v2",
                "kind": "agent_round_request",
                "invocation_id": invocation_id,
                "round_id": f"{invocation_id}:round:{round_index}",
                "order": round_index,
                "role": self.role,
                "model": self.model,
                "messages": message_snapshot,
                "message_sections": _snapshot_message_sections(message_snapshot, message_names),
                "tool_catalog": redact_value(tools, max_string=None, max_items=None),
                "tool_choice": terminal_tool_choice,
                "prompt_measurements": _prompt_measurements(
                    role=self.role,
                    model=self.model,
                    tools=tools,
                    tool_choice=terminal_tool_choice,
                    wire_messages=wire_messages,
                    message_names=message_names,
                    context_state=context.state,
                ),
                "attachment_policy": "metadata_only_binary_omitted",
                "input_visual": input_visual,
                "active_skills": self.active_skill_metadata if include_skill_context else [],
                "context_capacity": {"estimated_input_tokens": request_estimate, "configured_input_limit": input_limit},
            }
            round_record_base = {
                "round_id": f"{invocation_id}:round:{round_index}",
                "order": round_index,
                "role": context.role,
                "invocation_id": invocation_id,
                "message_count": len(messages),
                "image_count": count_message_images(messages),
                "stable_prefix_hash": stable_prefix_hash,
                "tool_catalog_hash": catalog_hash,
                "attachment_count": count_message_images(messages),
                "prompt_measurements": request_payload.get("prompt_measurements") or {},
                "input_observation_id": (
                    str(input_visual.get("observation_id") or "") if input_visual else ""
                ),
                "input_model_image_ref": (
                    input_visual.get("model_image_ref") if input_visual else None
                ),
                "input_visual_kind": (
                    input_visual.get("visual_kind") if input_visual else None
                ),
                "input_captured_monotonic_ms": (
                    input_visual.get("captured_monotonic_ms") if input_visual else None
                ),
            }
            initial_request_ref = (
                artifacts.save_json("llm", request_payload) if artifacts is not None
                else "sha256:" + stable_hash(request_payload)
            )
            await _emit(
                event_sink,
                "agent_llm_round_started",
                LLMRoundRecord(
                    **round_record_base,
                    request_ref=initial_request_ref,
                    stop_reason="pending",
                    model=self.model,
                ).model_dump(),
            )

            def provider_meter(
                request_kind: str,
            ) -> Callable[[str, dict[str, Any]], Any] | None:
                if model_call_meter is None:
                    return None

                def meter(kind: str, payload: dict[str, Any]) -> Any:
                    return model_call_meter(kind, {
                        **payload,
                        "role": self.role,
                        "round": round_index,
                        "kind": request_kind,
                    })

                return meter

            async def stream_update(update: dict[str, Any]) -> None:
                text = _scrub_sensitive_values(
                    redact_value(update.get("text", ""), max_string=None, max_items=None),
                    context,
                )
                summary = _scrub_sensitive_values(
                    redact_value(update.get("summary", ""), max_string=None, max_items=None),
                    context,
                )
                tool_count = int(update.get("tool_call_count") or 0)
                display_text = text or summary
                if not display_text and tool_count:
                    display_text = (
                        "Preparing tool call…" if tool_count == 1
                        else f"Preparing {tool_count} tool calls…"
                    )
                await _emit(event_sink, "agent_llm_stream", {
                    "round_id": f"{invocation_id}:round:{round_index}",
                    "order": round_index,
                    "role": self.role,
                    "invocation_id": invocation_id,
                    "model": self.model,
                    "attempt": int(update.get("attempt") or 1),
                    "sequence": int(update.get("sequence") or 0),
                    "status": str(update.get("status") or "streaming"),
                    "text": display_text,
                    "tool_call_count": tool_count,
                })

            try:
                resp = await complete(
                    self.model,
                    wire_messages,
                    tools=tools,
                    tool_choice=terminal_tool_choice,
                    cache_system_prefix=cache_system_prefix,
                    cache_skill_index=(
                        cache_system_prefix
                        and include_skill_context
                        and self.role in {"planner", "reviewer", "executor"}
                    ),
                    settings=self.settings,
                    attempt_meter=provider_meter("primary"),
                    stream_sink=stream_update,
                )
            except GatewayInputSafetyError as exc:
                if content_safety_degraded:
                    raise
                original_image_count = count_message_images(messages)
                fallback_messages, removed_image_count = _project_messages_without_images(messages)
                if not original_image_count or not removed_image_count:
                    raise
                content_safety_degraded = True
                content_safety_original_image_count = original_image_count
                content_safety_removed_image_count = removed_image_count
                round_removed_image_count = removed_image_count
                content_safety_offending_locations = list(exc.offending_locations)
                fallback_evidence_messages, _ = _project_messages_without_images(
                    evidence_messages,
                )
                context.state["delivered_history_digest"] = delivered_history_digest(
                    fallback_messages,
                    message_names,
                )
                context.state["delivered_evidence_digest"] = delivered_evidence_digest(
                    fallback_evidence_messages,
                    evidence_names,
                )
                context.state.update({
                    "content_safety_degraded": True,
                    "content_safety_original_image_count": original_image_count,
                    "content_safety_removed_image_count": removed_image_count,
                    "content_safety_offending_locations": [
                        {"message_index": message_index, "content_index": content_index}
                        for message_index, content_index in exc.offending_locations
                    ],
                })
                await _emit(event_sink, "agent_input_safety_degraded", {
                    "invocation_id": invocation_id,
                    "round_id": f"{invocation_id}:round:{round_index}",
                    "role": self.role,
                    "model": self.model,
                    "original_image_count": original_image_count,
                    "removed_image_count": removed_image_count,
                    "offending_locations": context.state["content_safety_offending_locations"],
                })
                wire_messages = fallback_messages
                message_snapshot = _snapshot_messages(wire_messages, context)
                request_payload["messages"] = message_snapshot
                request_payload["message_sections"] = _snapshot_message_sections(
                    message_snapshot, message_names,
                )
                request_payload["prompt_measurements"] = _prompt_measurements(
                    role=self.role,
                    model=self.model,
                    tools=tools,
                    tool_choice=terminal_tool_choice,
                    wire_messages=wire_messages,
                    message_names=message_names,
                    context_state=context.state,
                )
                resp = await complete(
                    self.model,
                    wire_messages,
                    tools=tools,
                    tool_choice=terminal_tool_choice,
                    cache_system_prefix=cache_system_prefix,
                    cache_skill_index=(
                        cache_system_prefix
                        and include_skill_context
                        and self.role in {"planner", "reviewer", "executor"}
                    ),
                    settings=self.settings,
                    attempt_meter=provider_meter("input_safety_fallback"),
                    stream_sink=stream_update,
                )
            if content_safety_degraded:
                request_payload["content_safety"] = {
                    "degraded": True,
                    "original_image_count": round_original_image_count,
                    "removed_image_count": round_removed_image_count,
                    "trigger_original_image_count": content_safety_original_image_count,
                    "trigger_removed_image_count": content_safety_removed_image_count,
                    "offending_locations": [
                        {"message_index": message_index, "content_index": content_index}
                        for message_index, content_index in content_safety_offending_locations
                    ],
                }
            request_rounds.append(request_payload)
            messages = wire_messages
            delivered_image_count = count_message_images(messages)
            round_record_base["image_count"] = delivered_image_count
            round_record_base["attachment_count"] = delivered_image_count
            round_record_base["prompt_measurements"] = (
                request_payload.get("prompt_measurements") or {}
            )
            if delivered_image_count == 0:
                round_record_base.update({
                    "input_observation_id": "",
                    "input_model_image_ref": None,
                    "input_visual_kind": None,
                    "input_captured_monotonic_ms": None,
                })
            for tool_call in resp.tool_calls:
                _register_sensitive_tool_arguments(
                    tool_call.name,
                    _safe_json(tool_call.arguments),
                    context,
                )
            response_payload = {
                "schema_version": 2,
                "kind": "agent_round_response",
                "invocation_id": invocation_id,
                "round_id": f"{invocation_id}:round:{round_index}",
                "order": round_index,
                "role": self.role,
                "model": resp.model or self.model,
                "content": _scrub_sensitive_values(
                    redact_value(resp.content, max_string=None, max_items=None), context,
                ),
                "content_blocks": ([{"type": "text", "text": _scrub_sensitive_values(
                    redact_value(resp.content, max_string=None, max_items=None), context,
                )}]
                                   if resp.content else []),
                "tool_calls": [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": _redact_tool_arguments(
                            tc.name,
                            _safe_json(tc.arguments),
                            context,
                        ),
                    }
                    for tc in resp.tool_calls
                ],
                "stop_reason": resp.stop_reason,
                "latency_ms": resp.latency_ms,
                "usage": NormalizedUsage.model_validate(resp.usage or {}).model_dump(),
                "private_reasoning": "not_persisted",
                "reasoning": {
                    "status": resp.reasoning.status,
                    "effort": resp.reasoning.effort,
                    "summary_preference": resp.reasoning.summary_preference,
                    "summary": _scrub_sensitive_values(resp.reasoning.summary, context),
                    "diagnostic_only": True,
                },
            }
            if content_safety_degraded:
                response_payload["content_safety_degraded"] = True
            request_ref = (
                artifacts.save_json("llm", request_payload) if artifacts is not None
                else "sha256:" + stable_hash(request_payload)
            )
            response_ref = (
                artifacts.save_json("llm", response_payload) if artifacts is not None
                else "sha256:" + stable_hash(response_payload)
            )
            round_record = LLMRoundRecord(
                **round_record_base,
                request_ref=request_ref,
                response_ref=response_ref,
                stop_reason=resp.stop_reason,
                latency_ms=resp.latency_ms,
                usage=NormalizedUsage.model_validate(resp.usage or {}),
                model=resp.model or self.model,
                response_content_count=1 if resp.content else 0,
                reasoning_status=resp.reasoning.status,
                reasoning_effort=resp.reasoning.effort,
                reasoning_summary_preference=resp.reasoning.summary_preference,
                reasoning_summary=_scrub_sensitive_values(resp.reasoning.summary, context),
            )
            llm_rounds.append(round_record)
            await _emit(event_sink, "agent_llm_round_finished", round_record.model_dump())
            if resp.tool_calls:
                request_message_count = len(messages)
                decision, done = await self._execute_tool_calls(
                    resp, messages, message_names, registry, context, tool_records,
                    event_sink, round_index,
                )
                appended = messages[request_message_count:]
                appended_names = message_names[request_message_count:]
                next_observations = context.state.pop("_next_observation_messages", [])
                if isinstance(next_observations, list) and next_observations:
                    observation_message_ids = {id(message) for message in next_observations}
                    retained = [
                        (name, message) for name, message in zip(appended_names, appended)
                        if id(message) not in observation_message_ids
                    ]
                    if retain_observations:
                        transcript_before_observation.extend(observation_bucket)
                        transcript_before_observation_names.extend(observation_bucket_names)
                    transcript_before_observation.extend(transcript_after_observation)
                    transcript_before_observation_names.extend(
                        transcript_after_observation_names
                    )
                    transcript_after_observation.clear()
                    transcript_after_observation_names.clear()
                    transcript_before_observation.extend(message for _, message in retained)
                    transcript_before_observation_names.extend(name for name, _ in retained)
                    observation_bucket = list(next_observations)
                    observation_bucket_names = [
                        next(
                            (
                                name
                                for name, message in zip(appended_names, appended)
                                if id(message) == id(observation_message)
                            ),
                            f"next_observation[{index}]",
                        )
                        for index, observation_message in enumerate(next_observations)
                    ]
                    next_k_wire_messages = (
                        self._k_wire() if include_skill_context else []
                    )
                    if next_k_wire_messages != k_wire_messages:
                        previous_prefix_hash = stable_prefix_hash
                        k_wire_messages = next_k_wire_messages
                        prefix_entries = [
                            *[("system", message) for message in self._stable],
                            *[("skill_index", message) for message in index_messages],
                            *[("workflow_catalog", message) for message in catalog_messages],
                            *[
                                (f"k_wire_messages[{index}]", message)
                                for index, message in enumerate(k_wire_messages)
                            ],
                            *[
                                (
                                    history_message_names[index]
                                    if history_message_names is not None
                                    and index < len(history_message_names)
                                    else f"history_messages[{index}]",
                                    message,
                                )
                                for index, message in enumerate(history_messages or [])
                            ],
                        ]
                        prefix_messages = [message for _, message in prefix_entries]
                        prefix_message_names = [name for name, _ in prefix_entries]
                        stable_prefix_hash = stable_hash([
                            *self._stable,
                            *index_messages,
                            *catalog_messages,
                            *k_wire_messages,
                        ])
                        context.state.setdefault("stable_prefix_transitions", []).append({
                            "from": previous_prefix_hash,
                            "to": stable_prefix_hash,
                            "reason": "foreground_app_core_refresh",
                        })
                else:
                    transcript_after_observation.extend(appended)
                    transcript_after_observation_names.extend(appended_names)
                if done and decision is not None:
                    context.state.pop("_ephemeral_sensitive_values", None)
                    return SessionResult(
                        decision=decision, tool_calls=tool_records,
                        llm_rounds=llm_rounds, raw_messages=messages,
                        dialogue_messages=[
                            *transcript_before_observation, *observation_bucket,
                            *transcript_after_observation, *final_bucket,
                        ],
                        context_state=context.state,
                        request_snapshot={
                            "schema_version": 2,
                            "decision_context_version": "v2",
                            "representation": "redacted_semantic_request",
                            "role": self.role,
                            "model": self.model,
                            "tool_catalog": redact_value(
                                tools, max_string=None, max_items=None,
                            ),
                            "tool_catalog_hash": catalog_hash,
                            "rounds": request_rounds,
                            "compaction": {
                                "image_data_urls": "omitted",
                                "assistant_content": "preserved_redacted",
                                "semantic_truncation": "none",
                            },
                        },
                    )
                continue
            if round_index < self.max_total_rounds:
                content_only_retries += 1
                context.state["content_only_retry_count"] = content_only_retries
                transcript_after_observation.extend([
                    {"role": "assistant", "content": resp.content or ""},
                    {"role": "user", "content": required_tool_nudge},
                ])
                transcript_after_observation_names.extend([
                    "content_only_response", "tool_call_required_nudge",
                ])
                await _emit(event_sink, "agent_tool_call_recovery", {
                    "invocation_id": invocation_id,
                    "round_id": f"{invocation_id}:round:{round_index}",
                    "role": self.role,
                    "reason": "content_without_tool_calls",
                    "retry": content_only_retries,
                })
                continue
            raise GatewayError(
                "AgentSession global role rounds exhausted without required tool call",
                category="budget" if reserve_terminal_round else "malformed",
            )

        raise GatewayError(
            "AgentSession tool loop exhausted without submit",
            category="budget" if reserve_terminal_round else "malformed",
        )

    def _build_registry(self, external_handlers: dict[str, ToolHandler]) -> AgentToolRegistry:
        registry = AgentToolRegistry()

        async def unavailable(args: dict[str, Any], ctx: ToolExecutionContext) -> AgentToolResult:
            del args, ctx
            return AgentToolResult(
                status=ToolStatus.UNAVAILABLE,
                summary="tool provider is unavailable in this invocation",
                provider_status="unavailable",
            )

        async def load_skill(args: dict[str, Any], ctx: ToolExecutionContext) -> AgentToolResult:
            skill_id = str(args.get("skill_id") or "").strip()
            pack = self.library.get(skill_id)
            if self.role == "executor" and pack is not None and pack.kind != "generic":
                return AgentToolResult(
                    status=ToolStatus.INVALID_ARGUMENTS,
                    summary="Executor may load only advertised generic skills; Planner selects app workflows.",
                    error="skill_scope_denied",
                )
            already_loaded = skill_id in self._loaded_ids
            known_missing = skill_id in self._missing_skill_ids
            result_text, ok = self._exec_load_skill_args(args)
            if (
                not ok
                and skill_id
                and (known_missing or self.library.get(skill_id) is None)
            ):
                self._missing_skill_ids.add(skill_id)
            pack = self.library.get(skill_id) if ok else None
            scope = pack.scope_key() if pack is not None else ""
            active_app = self._target_app or self._foreground_app
            active = bool(
                scope == "generic"
                or (scope and scope == f"apps/{active_app}")
            )
            return AgentToolResult(
                status=ToolStatus.SUCCEEDED if ok else ToolStatus.INVALID_ARGUMENTS,
                summary=result_text,
                data={
                    "skill_id": skill_id,
                    "already_loaded": bool(ok and already_loaded),
                    "scope": scope,
                    "active_in_k_wire": active,
                    "injected_by": (
                        self._skill_activation_metadata.get(skill_id, {}).get(
                            "activation_source", ""
                        )
                    ),
                    "content_hash": (
                        stable_hash(pack.section_for(self._skill_projection_role()))
                        if pack else ""
                    ),
                    "rule_categories": pack.rule_categories() if pack else [],
                },
                error=None if ok else "load_skill_failed",
            )

        async def submit(args: dict[str, Any], ctx: ToolExecutionContext) -> AgentToolResult:
            if self.role == "executor":
                invalid = _executor_submission_error(args, ctx)
                if invalid is not None:
                    return invalid
            decision = self._parse_submit_args(args, context=ctx)
            if isinstance(decision, ExecutorStep) and decision.action is not None:
                if (
                    ctx.state.get("content_safety_degraded")
                    and action_uses_coordinates(decision.action)
                ):
                    return AgentToolResult(
                        status=ToolStatus.INVALID_ARGUMENTS,
                        summary=(
                            "content-safety degraded mode removed the image coordinate "
                            "reference; use an accessibility-index/text action or request "
                            "a structure-only observation"
                        ),
                        data={
                            "recoverable": True,
                            "reason": "image_coordinates_unavailable",
                            "content_safety_degraded": True,
                        },
                        error="image_coordinates_unavailable",
                    )
                reason, detail = _visual_submission_error(
                    decision,
                    context=ctx,
                    epsilon=COORDINATE_ROUNDING_EPSILON,
                )
                if reason:
                    ctx.state["visual_validation_nudges"] = int(
                        ctx.state.get("visual_validation_nudges", 0)
                    ) + 1
                    return AgentToolResult(
                        status=ToolStatus.INVALID_ARGUMENTS,
                        summary=(
                            f"recoverable visual action rejection: {reason}; "
                            "resubmit against the current actionable observation without guessing"
                        ),
                        data={
                            "recoverable": True,
                            "reason": reason,
                            **detail,
                        },
                        error=reason,
                    )
            preflight = ctx.state.get("terminal_preflight")
            if isinstance(decision, ExecutorStep) and callable(preflight):
                produced = preflight(decision, ctx)
                if hasattr(produced, "__await__"):
                    produced = await produced
                if isinstance(produced, AgentToolResult):
                    return produced
            return AgentToolResult(
                status=ToolStatus.SUCCEEDED,
                summary="accepted",
                data={},
                terminal_value=decision,
            )

        internal: dict[str, ToolHandler] = {
            "load_skill": load_skill,
            "submit_planner_decision": submit,
            "submit_reviewer_decision": submit,
            "submit_executor_step": submit,
        }
        for spec in _catalog_specs(self.role):
            if spec.name == "submit_executor_step":
                import copy
                parameters = copy.deepcopy(spec.parameters)
                action_schema = parameters.get("properties", {}).get("action", {})
                if self.settings.double_tap:
                    action_schema["properties"]["type"]["enum"].append("double_tap")
                    action_schema["description"] += " double_tap requires x,y; changes document/image zoom when supported and consumes one action. Use fresh image coordinates."
                spec = spec.model_copy(update={"parameters": parameters})
            if spec.name == "observe_screen" and self.role == "executor" and self.settings.screen_detail:
                from agent.screen_detail import detail_parameters
                spec = spec.model_copy(update={
                    "parameters": detail_parameters(spec.parameters),
                    "description": spec.description + " Use detail only for unreadable small text: fresh native-resolution crops plus a global screen; optional region, otherwise four overlapping tiles. Crops are read-only; act using the fresh global observation. It cannot recover off-screen or unrendered detail.",
                })
            registry.register(spec, external_handlers.get(spec.name) or internal.get(spec.name) or unavailable)
        return registry

    async def _execute_tool_calls(
        self,
        resp: GatewayResponse,
        messages: list[dict[str, Any]],
        message_names: list[str],
        registry: AgentToolRegistry,
        context: ToolExecutionContext,
        records: list[ToolCallRecord],
        event_sink: Callable[[str, dict[str, Any]], Any] | None,
        llm_round_order: int,
    ) -> tuple[TerminalValue | None, bool]:
        terminal_call_ids: set[str] = set()
        for tool_call in resp.tool_calls:
            try:
                if registry.spec(tool_call.name, context.role).category == ToolCategory.TERMINAL:
                    terminal_call_ids.add(tool_call.id)
            except KeyError:
                continue
        mixed_terminal_response = bool(terminal_call_ids) and len(resp.tool_calls) > 1
        write_before_submit = (
            context.role == AgentRole.EXECUTOR
            and context.state.get("allow_note_before_submit", False)
            and len(terminal_call_ids) == 1
            and resp.tool_calls[-1].id in terminal_call_ids
            and all(call.name == "write_note" for call in resp.tool_calls[:-1])
        )
        write_failed = False
        inspection_calls = [
            call for call in resp.tool_calls if call.name == "inspect_image_regions"
        ]
        inspection_scope_error = ""
        if len(inspection_calls) > 1:
            inspection_scope_error = (
                "submit one shortlisted image-region verification call per response; "
                "do not partition a broad screen search"
            )
        elif inspection_calls:
            inspection_args = _safe_json(inspection_calls[0].arguments)
            regions = inspection_args.get("regions") or []
            targets = inspection_args.get("targets") or []
            if isinstance(regions, list) and isinstance(targets, list):
                regions = regions + targets
            pairs = inspection_args.get("pairs") or []
            limit = region_limit(inspection_args)
            if isinstance(regions, list) and len(regions) > limit:
                inspection_scope_error = (
                    f"shortlist at most {limit} relevant regions from the current SoM; "
                    "do not enumerate the page"
                )
            elif isinstance(pairs, list) and len(pairs) > MAX_PAIRS_PER_CALL:
                inspection_scope_error = (
                    f"compare at most {MAX_PAIRS_PER_CALL} shortlisted pairs; "
                    "use compare for a visible group's cross-product"
                )
        repeated_inspection_scope_error = False
        if inspection_scope_error:
            rejection_count = int(context.state.get("image_region_scope_rejections", 0)) + 1
            context.state["image_region_scope_rejections"] = rejection_count
            repeated_inspection_scope_error = rejection_count > 1
            if repeated_inspection_scope_error:
                context.state["terminal_submission_only"] = True
        inspection_feedback_emitted = False
        assistant_tools = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": stable_json(_redact_tool_arguments(
                        tc.name,
                        _safe_json(tc.arguments),
                        context,
                    )),
                },
            }
            for tc in resp.tool_calls
        ]
        messages.append({
            "role": "assistant",
            "content": _scrub_sensitive_values(resp.content, context) or None,
            "tool_calls": assistant_tools,
        })
        message_names.append("assistant_tools")

        submit_decision: TerminalValue | None = None
        attachment_entries: list[tuple[str, dict[str, Any]]] = []
        for tc in resp.tool_calls:
            args = _safe_json(tc.arguments)
            try:
                spec = registry.spec(tc.name, context.role)
                category = spec.category
            except KeyError:
                category = ToolCategory.KNOWLEDGE
                spec = None
            started = monotonic_ms()
            previous_observation_id = str(context.state.get("active_observation_id") or "")
            previous_visual = context.state.get("visual_evidence_metadata")
            previous_visual = dict(previous_visual) if isinstance(previous_visual, dict) else {}
            record = ToolCallRecord(
                call_id=tc.id, order=len(records) + 1, name=tc.name,
                role=context.role, invocation_id=context.invocation_id,
                llm_round_order=llm_round_order,
                category=category, status=ToolStatus.STARTED,
                arguments=_redact_tool_arguments(tc.name, args, context), started_at_ms=started,
            )
            records.append(record)
            await _emit(event_sink, "agent_tool_started", record.model_dump())

            if tc.name == "inspect_image_regions" and inspection_scope_error:
                first_scope_feedback = not inspection_feedback_emitted
                inspection_feedback_emitted = True
                result = AgentToolResult(
                    status=ToolStatus.PRECONDITION_NOT_MET,
                    summary=(
                        (
                            "Repeated broad image inspection rejected; evidence gathering is closed. "
                            "Submit a terminal decision using current evidence or report the grounding gap."
                            if repeated_inspection_scope_error
                            else "Broad image inspection rejected. " + inspection_scope_error
                        )
                        if first_scope_feedback
                        else "Rejected as part of the same aggregate inspection scope."
                    ),
                    data=(
                        {"recoverable": not repeated_inspection_scope_error}
                        if first_scope_feedback else {}
                    ),
                    error=(
                        "repeated_image_region_scope_violation"
                        if repeated_inspection_scope_error
                        else "image_region_scope_too_broad"
                    ),
                )
            elif context.state.get("terminal_submission_only") and tc.id not in terminal_call_ids:
                result = AgentToolResult(
                    status=ToolStatus.PRECONDITION_NOT_MET,
                    summary="Evidence gathering has ended. Submit the decision using available evidence.",
                    error="submission_only",
                )
            elif tc.id in terminal_call_ids and write_before_submit and write_failed:
                result = AgentToolResult(
                    status=ToolStatus.PRECONDITION_NOT_MET,
                    summary="Note was not saved. Read the error before submitting an action.",
                    error="note_write_failed",
                )
            elif mixed_terminal_response and not write_before_submit and tc.id in terminal_call_ids:
                result = AgentToolResult(
                    status=ToolStatus.PRECONDITION_NOT_MET,
                    summary=(
                        "A terminal submit must be the only tool call in its model response. "
                        "Read the other tool results, then submit alone in the next round."
                    ),
                    error="terminal_submit_must_be_alone",
                )
            else:
                if (
                    tc.name == "observe_screen"
                    and context.state.get("observe_screen_succeeded")
                ):
                    result = AgentToolResult(
                        status=ToolStatus.PRECONDITION_NOT_MET,
                        summary=(
                            "observe_screen already succeeded in this invocation and "
                            "cannot be repeated; submit the role decision now"
                        ),
                        data={
                            "recoverable": True,
                            "reason": "observe_screen_already_succeeded",
                        },
                        error="observe_screen_already_succeeded",
                    )
                else:
                    result = await registry.execute(tc.name, args, context)
            if tc.name == "write_note" and result.status != ToolStatus.SUCCEEDED:
                write_failed = True
            record.status = result.status
            record.elapsed_ms = max(0.0, monotonic_ms() - started)
            record.result_summary = str(
                _scrub_sensitive_values(result.summary or "", context)
            )[:1000]
            record.result = _scrub_sensitive_values(
                redact_value(result.data), context,
            )
            record.local_result = _scrub_sensitive_values(
                result.metadata(), context,
            )
            model_result = _scrub_sensitive_values(
                result.model_metadata(tc.name), context,
            )
            if tc.name == "observe_screen" and result.status == ToolStatus.SUCCEEDED:
                context.state["observe_screen_succeeded"] = True
                # The following Decision Context v2 observation is the sole
                # model-visible source of current screen facts. Tool-local
                # ids and capture metadata remain available in the trace.
                model_result = {
                    "status": "succeeded",
                    "summary": "Current screen evidence is replaced by the following observation.",
                }
            record.model_result = model_result
            record.attachments = [attachment.model_dump() for attachment in result.attachments]
            record.evidence_refs = list(result.evidence_refs)
            if result.evidence is not None and result.evidence.evidence_ref not in record.evidence_refs:
                record.evidence_refs.append(result.evidence.evidence_ref)
            if record.evidence_refs:
                runtime_refs = context.state.setdefault("runtime_evidence_refs", [])
                for evidence_ref in record.evidence_refs:
                    if evidence_ref not in runtime_refs:
                        runtime_refs.append(evidence_ref)
                if (
                    tc.name == "observe_screen"
                    and result.status == ToolStatus.SUCCEEDED
                    and result.data.get("mode") == "sequence"
                ):
                    sequence_refs = context.state.setdefault(
                        "reviewer_sequence_evidence_handles", []
                    )
                    if "screen:sequence" not in sequence_refs:
                        sequence_refs.append("screen:sequence")
            record.artifact_refs = list(result.artifact_refs)
            record.provider_status = result.provider_status
            record.error = result.error
            next_observation_id = str(result.actionable_observation_id or previous_observation_id)
            if next_observation_id and next_observation_id != previous_observation_id:
                next_visual = context.state.get("visual_evidence_metadata")
                next_visual = dict(next_visual) if isinstance(next_visual, dict) else {}
                registry_state = context.state.get("observation_registry")
                old_entry = registry_state.get(previous_observation_id) if hasattr(registry_state, "get") else None
                new_entry = registry_state.get(next_observation_id) if hasattr(registry_state, "get") else None
                record.observation_transition = {
                    "from_observation_id": previous_observation_id,
                    "to_observation_id": next_observation_id,
                    "from_mode": (
                        "image-only"
                        if getattr(old_entry, "model_image_size", None)
                        and not getattr(old_entry, "index_actionable", True)
                        else "tree+image" if getattr(old_entry, "model_image_size", None)
                        else "tree-only"
                    ),
                    "to_mode": (
                        "image-only"
                        if getattr(new_entry, "model_image_size", None)
                        and not getattr(new_entry, "index_actionable", True)
                        else "tree+image" if getattr(new_entry, "model_image_size", None)
                        else "tree-only"
                    ),
                    "attachment_refs": [a.get("artifact_ref") for a in record.attachments if a.get("artifact_ref")],
                    "from_model_image_ref": previous_visual.get("image_artifact_ref"),
                    "to_model_image_ref": next_visual.get("image_artifact_ref"),
                    "from_captured_monotonic_ms": previous_visual.get("captured_monotonic_ms"),
                    "to_captured_monotonic_ms": next_visual.get("captured_monotonic_ms"),
                    "from_visual_kind": previous_visual.get("visual_kind"),
                    "to_visual_kind": next_visual.get("visual_kind"),
                    "next_active_basis": next_observation_id,
                }
                context.state["active_observation_id"] = next_observation_id
            event = "agent_tool_finished" if result.status == ToolStatus.SUCCEEDED else "agent_tool_failed"
            await _emit(event_sink, event, record.model_dump())
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": stable_json(model_result),
            })
            message_names.append(f"model_result[{tc.name}]")
            replacement_attachments = (
                result.replacement_attachments
                if result.replacement_attachments is not None
                else result.attachments
            )
            projected_observation = None
            if tc.name == "observe_screen" and result.status == ToolStatus.SUCCEEDED:
                projector = context.state.get("render_observation_bucket")
                active_package = context.state.get("active_package")
                if not callable(projector) or active_package is None:
                    raise RuntimeError(
                        "successful observe_screen requires a Decision Context v2 projector"
                    )
                projected_observation = projector(active_package)
                if not projected_observation:
                    raise RuntimeError(
                        "observe_screen projector returned an empty observation bucket"
                    )
            if projected_observation is not None:
                projected_messages, projected_names = projected_observation
                projected_messages = list(projected_messages)
                projected_names = list(projected_names)
                if not projected_messages:
                    raise RuntimeError(
                        "observe_screen projector returned an empty observation bucket"
                    )
                sequence_message = None
                if result.data.get("mode") == "sequence":
                    sequence_message = _attachments_message(
                        tc.name,
                        replacement_attachments,
                        contact_sheet=False,
                    )
                if sequence_message is not None:
                    sequence_message["content"].append({
                        "type": "text",
                        "text": (
                            "The ordered sequence continues with the canonical "
                            "ending observation below. Prior frames are "
                            "visual history only and are not action bases."
                        ),
                    })
                    attachment_entries.append(("observation_sequence_history", sequence_message))
                if result.data.get("mode") == "detail":
                    detail_message = _attachments_message(tc.name, replacement_attachments, contact_sheet=False)
                    if detail_message is not None:
                        projected_messages.append(detail_message)
                        projected_names.append("observation_detail")
                for index, projected_message in enumerate(projected_messages):
                    projected_name = (
                        projected_names[index]
                        if index < len(projected_names)
                        else f"next_observation[{index}]"
                    )
                    attachment_entries.append((projected_name, projected_message))
                context.state["_next_observation_messages"] = (
                    ([sequence_message] if sequence_message is not None else [])
                    + projected_messages
                )
            elif tc.name != "observe_screen" or result.status != ToolStatus.SUCCEEDED:
                attachment_message = _attachments_message(
                    tc.name, replacement_attachments,
                    contact_sheet=False,
                )
                if attachment_message is not None:
                    attachment_entries.append((f"attachment_message[{tc.name}]", attachment_message))
                    if tc.name == "observe_screen":
                        context.state["_next_observation_messages"] = [attachment_message]
            if tc.name == "observe_screen" and result.status == ToolStatus.SUCCEEDED:
                context.state["active_observation_id"] = result.actionable_observation_id or ""
            elif result.actionable_observation_id:
                context.state["active_observation_id"] = result.actionable_observation_id
            if result.terminal_value is not None:
                submit_decision = result.terminal_value

        # Every tool call needs its reply before any user/image message can follow.
        for name, message in attachment_entries:
            messages.append(message)
            message_names.append(name)
        if submit_decision is not None:
            return submit_decision, True
        return None, False

    def _exec_load_skill_args(self, args: dict[str, Any]) -> tuple[str, bool]:
        skill_id = str(args.get("skill_id") or "").strip()
        if not skill_id:
            return "load_skill error: missing skill_id", False
        if skill_id in self._missing_skill_ids:
            return (
                f"load_skill error: '{skill_id}' was already confirmed unavailable "
                "for this task",
                False,
            )
        # Disclosure is task-scoped. Once this role has the body, later
        # allowlist narrowing or an invocation-local search-grant reset must
        # not turn the same request into a fresh load (or an error).
        stored_pack = self.library.get(skill_id)
        if skill_id in self._loaded_ids:
            scope = stored_pack.scope_key() if stored_pack is not None else ""
            active_app = self._target_app or self._foreground_app
            active = scope == "generic" or scope == f"apps/{active_app}"
            state = "active in K_wire" if active else "stored but inactive for current foreground"
            return (
                f"already_loaded:{skill_id} ({state}); "
                "use the existing skill context and submit",
                True,
            )
        pack = self._resolve_allowed(skill_id)
        if pack is None:
            return (
                f"load_skill error: '{skill_id}' not in allowlist or missing on disk",
                False,
            )
        body = pack.section_for(self._skill_projection_role())
        self._store_skill_message(pack, label="loaded")
        return f"skill:{skill_id}\n{body}", True

    def _parse_submit_args(
        self,
        args: dict[str, Any],
        *,
        context: ToolExecutionContext | None = None,
    ) -> TerminalValue:
        try:
            if self.role in {"planner", "reviewer"}:
                from shared.revisable import PlannerDecision, Review
                schema = PlannerDecision if self.role == "planner" else Review
                return schema.model_validate(args)
            active_observation_id = ""
            evidence_refs: list[str] = []
            if context is not None:
                active_observation_id = str(
                    context.state.get("active_observation_id") or ""
                )
                evidence_refs = [
                    str(ref) for ref in context.state.get("runtime_evidence_refs", [])
                ]
            current = dict(args)
            if context is not None:
                _register_sensitive_tool_arguments(
                    "submit_executor_step",
                    current,
                    context,
                )
            step = ExecutorStepSubmit.model_validate(current).to_step(
                basis_observation_id=active_observation_id,
                evidence_refs=evidence_refs,
            )
            if context is not None:
                step.summary = str(_scrub_sensitive_values(step.summary, context))
            return step
        except ValidationError:
            raise

def _safe_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _executor_submission_error(
    args: dict[str, Any], context: ToolExecutionContext | None = None,
) -> AgentToolResult | None:
    """Return a recoverable terminal error before permissive model defaults apply."""
    missing_fields = [name for name in _EXECUTOR_SUBMIT_REQUIRED_FIELDS if name not in args]
    if missing_fields:
        return AgentToolResult(
            status=ToolStatus.INVALID_ARGUMENTS,
            summary=(
                "recoverable Executor submit validation error: resubmit in this "
                f"invocation with required field(s): {', '.join(missing_fields)}"
            ),
            data={"recoverable": True, "missing_fields": missing_fields},
            error="invalid_executor_submission",
        )

    raw_decision = args.get("decision")
    current = dict(args)
    try:
        submission = ExecutorStepSubmit.model_validate(current)
    except ValidationError as exc:
        invalid_fields = sorted({str(item["loc"][-1]) for item in exc.errors() if item["loc"]})
        return _invalid_executor_action(
            invalid_fields or ["decision"],
            "Executor decision contains an invalid field value",
        )
    if submission.decision != ExecutorDecisionKind.ACT:
        return None
    raw_action = args.get("action")
    if not isinstance(raw_action, dict):
        return _invalid_executor_action(["action"], "action must be an object")
    action = submission.action
    action_type = raw_action.get("type")
    if isinstance(action_type, str) and action_type in _ACTION_REQUIRED_PARAMS:
        allowed_fields = {
            "type",
            *_ACTION_REQUIRED_PARAMS[action_type],
            *_ACTION_OPTIONAL_PARAMS.get(action_type, ()),
        }
        extra_fields = sorted(set(raw_action) - allowed_fields)
        if extra_fields:
            return _invalid_executor_action(
                extra_fields,
                f"{action_type}: remove {', '.join(extra_fields)}; allowed fields: "
                + ", ".join(sorted(allowed_fields)),
            )

    if action.type == "double_tap" and context is not None and not context.state.get("double_tap_enabled"):
        return _invalid_executor_action(["type"], "double_tap is unavailable in this session")
    missing_params = _missing_action_params(action)
    if missing_params:
        return _invalid_executor_action(
            missing_params,
            f"{action.type} is missing required action parameter(s)",
        )
    return None


def _missing_action_params(action: Action) -> list[str]:
    if action.type == "long_press":
        if action.index is None and (action.x is None or action.y is None):
            return ["index or (x,y)"]
        return []
    if action.type == "skill_authorized_action":
        missing = []
        if not action.skill_action_id or not action.skill_action_id.strip():
            missing.append("skill_action_id")
        has_index = action.index is not None
        has_coordinates = action.x is not None and action.y is not None
        if has_index == has_coordinates or ((action.x is None) != (action.y is None)):
            missing.append("exactly one target: index or (x,y)")
        return missing
    return [
        name
        for name in _ACTION_REQUIRED_PARAMS.get(action.type, ())
        if getattr(action, name, None) is None
        or (isinstance(getattr(action, name, None), str) and not getattr(action, name).strip())
    ]


def _invalid_executor_action(fields: list[str], detail: str) -> AgentToolResult:
    return AgentToolResult(
        status=ToolStatus.INVALID_ARGUMENTS,
        summary=(
            f"recoverable Executor submit validation error: {detail}; "
            "correct submit_executor_step and resubmit in this invocation"
        ),
        data={"recoverable": True, "invalid_fields": fields},
        error="invalid_executor_submission",
    )


def _visual_submission_error(
    step: ExecutorStep,
    *,
    context: ToolExecutionContext,
    epsilon: float,
) -> tuple[str, dict[str, Any]]:
    """Preflight visual submissions so one retry stays in this invocation."""
    action = step.action
    if action is None or not (action_uses_coordinates(action) or action_uses_index(action)):
        return "", {}
    if (
        (action.type == "tap" and action.index is None)
        or (action.type == "tap_xy" and (action.x is None or action.y is None))
        or (
            action.type in {"swipe", "drag"}
            and None in (action.x, action.y, action.x2, action.y2)
        )
        or (
            action.type == "long_press"
            and action.index is None
            and (action.x is None or action.y is None)
        )
    ):
        # Executor's existing malformed-action nudge owns this case.
        return "", {}
    registry = context.state.get("observation_registry")
    active_id = str(context.state.get("active_observation_id") or "")
    if not isinstance(registry, ObservationRegistry):
        return "", {}
    if not step.basis_observation_id:
        return "ambiguous_observation_basis", {"active_observation_id": active_id}
    entry = registry.get(step.basis_observation_id)
    if entry is None:
        return "unknown_observation_basis", {"active_observation_id": active_id}
    if not entry.actionable:
        return "non_actionable_observation_basis", {"active_observation_id": active_id}
    if entry.observation_id != active_id:
        return "stale_observation_basis", {"active_observation_id": active_id}
    if action_uses_index(action):
        if not entry.index_actionable:
            return "index_unavailable_for_observation", {
                "active_observation_id": active_id,
            }
        if not any(
            element.index == action.index and len(element.bounds) == 4
            for element in entry.elements
        ):
            return "mismatched_observation_basis", {"index_set_id": entry.element_set_id}
        return "", {}
    if entry.model_image_size is None or entry.transform is None or entry.frame_geometry is None:
        return "unknown_coordinate_geometry", {}
    image_action, _, evidence = validate_action_bounds(
        action,
        width=entry.model_image_size[0],
        height=entry.model_image_size[1],
        epsilon=epsilon,
    )
    if image_action is None:
        return "coordinate_out_of_bounds", {"image_space": evidence}
    transformed = transform_action(image_action, entry.transform)
    driver_action, _, evidence = validate_action_bounds(
        transformed,
        width=entry.frame_geometry[0],
        height=entry.frame_geometry[1],
        epsilon=epsilon,
    )
    if driver_action is None:
        return "coordinate_out_of_bounds", {"driver_space": evidence}
    return "", {}


def _snapshot_message_sections(
    messages: list[dict[str, Any]], names: list[str],
) -> list[dict[str, Any]]:
    """Index each wire message by its prompt-construction variable."""
    return [
        {
            "name": names[index] if index < len(names) else f"messages[{index}]",
            "message_index": index,
        }
        for index, _message in enumerate(messages)
    ]


def _snapshot_messages(
    messages: list[dict[str, Any]], context: ToolExecutionContext,
) -> list[dict[str, Any]]:
    """Return an inspectable request copy without image bytes or typed secrets.

    Harness-owned user/system/tool messages retain their semantic content and
    ordering. Assistant content and tool calls are both retained so the
    artifact is a complete redacted record of the provider-wire request.
    """
    snapshot: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "")
        if role == "assistant":
            item: dict[str, Any] = {
                "role": role,
                "content": redact_value(
                    message.get("content"), max_string=None, max_items=None,
                ),
            }
            calls: list[dict[str, Any]] = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name") or "")
                args = _safe_json(fn.get("arguments") or {})
                calls.append({
                    "id": str(call.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": _redact_tool_arguments(name, args, context),
                    },
                })
            if calls:
                item["tool_calls"] = calls
            snapshot.append(item)
            continue
        source = dict(message)
        content = source.get("content")
        if isinstance(content, list):
            safe_blocks: list[Any] = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "image_url":
                    safe_blocks.append(block)
                    continue
                image = block.get("image_url") or {}
                url = str(image.get("url") or "") if isinstance(image, dict) else ""
                header, _, body = url.partition(",")
                mime_type = header[5:].split(";", 1)[0] if header.startswith("data:") else "application/octet-stream"
                safe_blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": "[sent to model; binary omitted from persisted trace]",
                    },
                    "attachment_metadata": {
                        "mime_type": mime_type,
                        "encoded_bytes": (len(body) * 3) // 4 if body else None,
                        "binary": "omitted",
                    },
                })
            source["content"] = safe_blocks
        item = redact_value(source, max_string=None, max_items=None)
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            item["content"] = re.sub(
                r'("resolution_ticket"\s*:\s*")[^"]+("?)',
                r'\1[REDACTED]\2',
                item["content"],
            )
        snapshot.append(item)
    return snapshot


def _measurement_summary(component: Any | None) -> dict[str, Any] | None:
    if component is None:
        return None
    summary = getattr(component, "summary_dict", None)
    if callable(summary):
        return summary()
    return component


def _project_content_without_images(content: Any) -> Any:
    if isinstance(content, list):
        return [
            block for block in content
            if not (isinstance(block, dict) and block.get("type") == "image_url")
        ]
    return content


def _project_messages_without_images_for_measurement(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for message in messages:
        item = dict(message)
        item["content"] = _project_content_without_images(message.get("content"))
        projected.append(item)
    return projected


def _collect_named_content_projection(
    messages: list[dict[str, Any]],
    names: list[str],
    *,
    include: Callable[[str], bool],
) -> list[Any]:
    projected: list[Any] = []
    for index, message in enumerate(messages):
        name = names[index] if index < len(names) else f"messages[{index}]"
        if include(name):
            projected.append(_project_content_without_images(message.get("content")))
    return projected


def _prompt_measurements(
    *,
    role: str,
    model: str,
    tools: list[dict[str, Any]],
    tool_choice: str,
    wire_messages: list[dict[str, Any]],
    message_names: list[str],
    context_state: dict[str, Any],
) -> dict[str, Any]:
    state = context_state.get("agent_state")
    instruction = (
        str(getattr(state, "instruction", "") or "")
        if state is not None
        else str(context_state.get("task_instruction") or "")
    )
    visual_evidence = context_state.get("visual_evidence_metadata")
    image_estimate = None
    if isinstance(visual_evidence, dict) and count_message_images(wire_messages) > 0:
        estimated = visual_evidence.get("estimated_image_tokens")
        if estimated is not None:
            try:
                image_estimate = {"model": model, "token_count": int(estimated)}
            except (TypeError, ValueError):
                image_estimate = None

    component_names = {
        "role_policy": lambda name: name == "system",
        "skill_context": lambda name: (
            name == "skill_index" or name.startswith("k_wire_messages[")
        ),
        "task_anchor": lambda name: name in {"goal", "task_anchor"},
        "history": lambda name: name == "history",
        "observation_text": lambda name: name.startswith("observation"),
        "conditional": lambda name: name == "conditional",
    }
    projections = {
        component: _collect_named_content_projection(
            wire_messages,
            message_names,
            include=predicate,
        )
        for component, predicate in component_names.items()
    }
    whole_request_projection = {
        "role": role,
        "model": model,
        "messages": _project_messages_without_images_for_measurement(wire_messages),
        "tool_catalog": tools,
        "tool_choice": tool_choice,
    }
    return {
        "task_instruction": _measurement_summary(
            measure_text_component("task_instruction", instruction) if instruction else None
        ),
        "tool_schema": _measurement_summary(
            measure_json_component("tool_schema", tools)
        ),
        **{
            component: _measurement_summary(
                measure_json_component(component, projection)
                if projection else None
            )
            for component, projection in projections.items()
        },
        "image_estimate": image_estimate,
        "whole_request": _measurement_summary(
            measure_json_component("whole_request", whole_request_projection)
        ),
    }


def _attachments_message(
    name: str, attachments: list[ToolAttachment], *, contact_sheet: bool = False,
) -> dict[str, Any] | None:
    if not attachments:
        return None
    import base64

    blocks: list[dict[str, Any]] = [{
        "type": "text",
        "text": f"Evidence attachments from {name}; labels are ordered and only the marked ending frame is actionable.",
    }]
    image_attachments = [
        attachment for attachment in attachments
        if (
            attachment.kind == "image"
            and isinstance(attachment.content, bytes)
            and all(value > 0 for value in decoded_image_size(attachment.content))
        )
    ]
    sheet: bytes | None = None
    if (
        contact_sheet and len(image_attachments) > 1
        and not any(a.actionable_coordinate_reference for a in image_attachments)
    ):
        try:
            import io
            from PIL import Image, ImageDraw

            images = [Image.open(io.BytesIO(a.content)).convert("RGB") for a in image_attachments]
            width = max(image.width for image in images)
            height = max(image.height for image in images)
            canvas = Image.new("RGB", (width * len(images), height + 24), "white")
            draw = ImageDraw.Draw(canvas)
            for index, (attachment, parsed) in enumerate(zip(image_attachments, images)):
                canvas.paste(parsed, (index * width, 24))
                draw.text((index * width + 4, 4), attachment.label, fill="black")
            output = io.BytesIO()
            canvas.save(output, format="JPEG", quality=80)
            sheet = output.getvalue()
        except Exception:  # noqa: BLE001
            sheet = None
    for attachment in attachments:
        if (
            attachment.kind == "image"
            and (
                not isinstance(attachment.content, bytes)
                or not all(
                    value > 0
                    for value in decoded_image_size(attachment.content)
                )
            )
        ):
            continue
        ts = f" @ {attachment.timestamp_ms:.1f}ms" if attachment.timestamp_ms is not None else ""
        if attachment.kind == "image":
            metadata = [
                f"actionable: {str(attachment.actionable_coordinate_reference).lower()}",
                f"indexed_targets_available: {str(attachment.indexed_targets_available).lower()}",
            ]
            if attachment.actionable_coordinate_reference:
                metadata.insert(
                    0, f"observation_id: {attachment.observation_id or '(unknown)'}",
                )
            if attachment.image_size is not None:
                metadata.append(f"image_size: {attachment.image_size[0]}x{attachment.image_size[1]}")
            blocks.append({
                "type": "text",
                "text": f"{attachment.label}{ts}\n" + "\n".join(metadata),
            })
        else:
            # Text/tree evidence shares the paired image's observation basis;
            # repeating coordinate transforms and artifact metadata is noise.
            blocks.append({"type": "text", "text": f"{attachment.label}{ts}"})
        if sheet is not None and attachment.kind == "image":
            continue
        if attachment.kind == "image" and isinstance(attachment.content, bytes):
            data_url = (
                f"data:{attachment.mime_type};base64," +
                base64.b64encode(attachment.content).decode("ascii")
            )
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        elif attachment.content is not None:
            blocks.append({"type": "text", "text": str(attachment.content)})
    if sheet is not None:
        data_url = "data:image/jpeg;base64," + base64.b64encode(sheet).decode("ascii")
        blocks.append({"type": "text", "text": "contact sheet fallback (ordered left to right)"})
        blocks.append({"type": "image_url", "image_url": {"url": data_url}})
    return {"role": "user", "content": blocks}


async def _emit(
    sink: Callable[[str, dict[str, Any]], Any] | None,
    kind: str,
    payload: dict[str, Any],
) -> None:
    if sink is None:
        return
    value = sink(kind, payload)
    if hasattr(value, "__await__"):
        await value


def _redact_tool_arguments(
    name: str, args: dict[str, Any], context: ToolExecutionContext,
) -> dict[str, Any]:
    _register_sensitive_tool_arguments(name, args, context)
    redacted = redact_value(args)
    if name != "submit_executor_step" or not isinstance(redacted, dict):
        return _scrub_sensitive_values(redacted, context)
    raw_action = args.get("action")
    action = redacted.get("action")
    action_type = raw_action.get("type") if isinstance(raw_action, dict) else None
    if (
        not isinstance(raw_action, dict)
        or not isinstance(action, dict)
        or not isinstance(action_type, str)
        or action_type not in {"type", "replace_text"}
    ):
        return _scrub_sensitive_values(redacted, context)
    if _focused_target_is_password(context, action.get("index")):
        action["text"] = "[REDACTED]"
        action["text_redacted"] = True
    return _scrub_sensitive_values(redacted, context)


def _register_sensitive_tool_arguments(
    name: str,
    args: dict[str, Any],
    context: ToolExecutionContext,
) -> None:
    """Retain password action values only in an invocation-local scrub set."""
    if name != "submit_executor_step":
        return
    raw_action = args.get("action")
    action_type = raw_action.get("type") if isinstance(raw_action, dict) else None
    if (
        not isinstance(action_type, str)
        or action_type not in {"type", "replace_text"}
    ):
        return
    raw_text = raw_action.get("text")
    if (
        not _focused_target_is_password(context, raw_action.get("index"))
        or not isinstance(raw_text, str)
        or not raw_text
    ):
        return
    values = context.state.setdefault("_ephemeral_sensitive_values", [])
    if raw_text not in values:
        values.append(raw_text)


def _focused_target_is_password(context: ToolExecutionContext, index: int | None = None) -> bool:
    """Fail closed when exact focused sources disagree about password state."""
    package = context.state.get("active_package")
    if index is not None:
        return any(
            element.index == index and bool(element.states.get("password"))
            for element in getattr(getattr(package, "ui", None), "elements", [])
        )
    interaction = getattr(package, "interaction_state", None)
    from perception.input_evidence import focused_target_evidence

    target = focused_target_evidence(interaction)
    if target is None:
        return False
    if target.password:
        return True
    ui = getattr(package, "ui", None)
    candidates = list(
        getattr(ui, "semantic_tree", None)
        or getattr(ui, "elements", None)
        or []
    )
    for element in candidates:
        same_index = target.index is not None and element.index == target.index
        exact_focused_node = bool((element.states or {}).get("focused"))
        if (same_index or exact_focused_node) and bool(
            (element.states or {}).get("password")
        ):
            return True
    return False


def _scrub_sensitive_values(value: Any, context: ToolExecutionContext) -> Any:
    """Remove exact invocation-local password values from persistent projections."""
    secrets = [
        item
        for item in context.state.get("_ephemeral_sensitive_values", [])
        if isinstance(item, str) and item
    ]
    return redact_exact_values(value, secrets)
