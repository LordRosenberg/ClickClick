"""Source-typed current focus and editable evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from shared.schemas import (
    CanonicalUI,
    EvidenceSource,
    FocusedElementEvidence,
    InteractionStateEvidence,
    UIElement,
)


Editability = Literal["editable", "not_editable", "unknown", "conflict"]


def editability_evidence(el: UIElement) -> Editability:
    """Resolve only structural platform evidence, never text semantics.

    An explicit accessibility state and the platform role are independent
    sources.  A disagreement is preserved for the model instead of being
    collapsed with a permissive boolean OR.
    """
    role = (el.role or "").lower()
    role_editable = "edittext" in role or "textfield" in role
    states = el.states or {}
    if "editable" in states:
        state_editable = bool(states.get("editable"))
        if role_editable and not state_editable:
            return "conflict"
        return "editable" if state_editable else "not_editable"
    return "editable" if role_editable else "unknown"


def is_editable(el: UIElement) -> bool:
    return editability_evidence(el) == "editable"


def focused_target_evidence(
    interaction: InteractionStateEvidence | None,
) -> FocusedElementEvidence | None:
    """Return the one exact focused target without interpreting editability."""
    if interaction is None:
        return None
    return interaction.focused_editable or interaction.focused_element


def element_identity(el: UIElement) -> str:
    """Stable composite that excludes action index and editable values."""
    b = el.bounds if len(el.bounds) == 4 else [0, 0, 0, 0]
    cx = ((b[0] + b[2]) // 2) // 64
    cy = ((b[1] + b[3]) // 2) // 64
    width = max(0, b[2] - b[0]) // 64
    height = max(0, b[3] - b[1]) // 64
    rid = (el.resource_id or "").strip()
    # Editable labels commonly contain the current value and therefore mutate
    # after typing. Geometry, hierarchy context, role, and resource id retain
    # collision separation without binding identity to that mutable value.
    stable_label = (
        ""
        if is_editable(el) or bool((el.states or {}).get("password"))
        else (el.text or el.desc or "").strip()
    )
    return "|".join(
        (
            rid,
            (el.role or "").lower(),
            stable_label,
            el.ctx or "",
            str(el.depth),
            f"{cx},{cy},{width},{height}",
        )
    )


def _focused_evidence(el: UIElement) -> FocusedElementEvidence:
    """Project one platform-focused node without choosing its semantics."""
    password = bool((el.states or {}).get("password"))
    return FocusedElementEvidence(
        index=el.index if el.interactable and el.index >= 0 else None,
        identity=element_identity(el),
        bounds=list(el.bounds),
        role=el.role,
        text="" if password else el.text,
        desc="" if password else el.desc,
        hint="" if password else el.hint,
        password=password,
        editability=editability_evidence(el),
        value_available=False if password else True,
    )


def _focus_scope(elements: list[UIElement]) -> list[UIElement]:
    """Keep focus evidence in active/focused windows when metadata exists."""
    active_window_ids = {
        element.window_id
        for element in elements
        if element.window_wrapper
        and element.window_id is not None
        and any(
            bool((element.states or {}).get(state))
            for state in ("active", "focused")
        )
    }
    if not active_window_ids:
        return elements
    return [
        element
        for element in elements
        if element.window_id in active_window_ids
    ]


def _focus_projection_signature(el: UIElement) -> tuple[Any, ...]:
    """Fields that must agree before duplicate a11y nodes may be collapsed."""
    return (
        editability_evidence(el),
        bool((el.states or {}).get("password")),
        el.text,
        el.desc,
        el.hint,
    )


def build_interaction_state(ui: CanonicalUI) -> InteractionStateEvidence:
    candidates = list(ui.semantic_tree or ui.elements or [])
    focused: list[tuple[UIElement, FocusedElementEvidence]] = []
    for el in _focus_scope(candidates):
        # AccessibilityWindow.focused identifies the active OS window, not the
        # focused UI control inside it.  Treating the wrapper as a control
        # masks the descendant EditText and drops its exact value/hint.
        if el.window_wrapper:
            continue
        if (el.states or {}).get("focused"):
            focused.append((el, _focused_evidence(el)))

    if not focused:
        return InteractionStateEvidence()

    focused_by_identity: dict[
        str, list[tuple[tuple[Any, ...], FocusedElementEvidence]]
    ] = {}
    non_editable: list[FocusedElementEvidence] = []
    for element, evidence in focused:
        focused_by_identity.setdefault(evidence.identity, []).append(
            (_focus_projection_signature(element), evidence)
        )
        if evidence.editability != "editable":
            non_editable.append(evidence)

    focused_editable = None
    editable_groups = [
        duplicates
        for duplicates in focused_by_identity.values()
        if any(
            evidence.editability == "editable"
            for _signature, evidence in duplicates
        )
    ]
    if len(editable_groups) == 1:
        duplicates = editable_groups[0]
        if (
            all(
                evidence.editability == "editable"
                for _signature, evidence in duplicates
            )
            and len({signature for signature, _evidence in duplicates}) == 1
        ):
            focused_editable = duplicates[0][1]
    focused_element = non_editable[0] if non_editable else focused_editable
    return InteractionStateEvidence(
        focused_element=focused_element,
        focused_editable=focused_editable,
        keyboard_visible=None,
        confidence=0.9,
        sources=[EvidenceSource.A11Y.value],
        age_ms=0,
    )


@dataclass
class ProviderResult:
    keyboard_visible: bool | None = None
    source: str = ""
    confidence: float = 0.0


class EvidenceProvider(Protocol):
    """Optional providers must be bounded by the caller and soft-degrade."""

    async def collect(self) -> ProviderResult | None: ...


class AdbImeEvidenceProvider:
    """Bounded adapter over an optional local/remote driver diagnostic."""

    def __init__(self, driver: object, *, timeout_ms: int = 350) -> None:
        self.driver = driver
        self.timeout_ms = max(1, int(timeout_ms))

    async def collect(self) -> ProviderResult | None:
        import asyncio

        fn = getattr(self.driver, "get_input_diagnostics", None)
        if not callable(fn):
            return None
        try:
            raw = await asyncio.wait_for(
                fn(timeout_s=self.timeout_ms / 1000.0),
                timeout=self.timeout_ms / 1000.0 + 0.05,
            )
        except Exception:  # noqa: BLE001 - optional evidence must soft-degrade
            return None
        if not isinstance(raw, dict):
            return None
        visible = raw.get("keyboard_visible")
        if visible not in (True, False, None):
            visible = None
        return ProviderResult(
            keyboard_visible=visible,
            source=str(raw.get("source") or EvidenceSource.ADB_IME.value),
            confidence=float(raw.get("confidence") or 0.0),
        )


def fuse_interaction_state(
    baseline: InteractionStateEvidence, provider_results: list[ProviderResult | None]
) -> InteractionStateEvidence:
    out = baseline.model_copy(deep=True)
    known_keyboard: list[bool] = []
    for result in provider_results:
        if result is None:
            continue
        if result.keyboard_visible is not None:
            known_keyboard.append(result.keyboard_visible)
            out.confidence = max(out.confidence, result.confidence)
        if result.source:
            out.sources = list(dict.fromkeys([*out.sources, result.source]))
    if known_keyboard:
        out.keyboard_visible = known_keyboard[0] if len(set(known_keyboard)) == 1 else None
    return out


def _value_state(focus: FocusedElementEvidence) -> str:
    if focus.password:
        return "redacted"
    if focus.value_available is False:
        return "unavailable"
    if focus.text:
        return "present"
    return "empty"


def interaction_envelope(interaction: InteractionStateEvidence | None) -> dict[str, Any]:
    """Project current focus without fusing text, label, and hint semantics."""
    envelope: dict[str, Any] = {
        "focused_element": None,
        "focused_editable": None,
        "keyboard": None if interaction is None else interaction.keyboard_visible,
    }
    if interaction is None:
        return envelope

    def project(focus: FocusedElementEvidence, *, editable: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": focus.index,
            "role": focus.role,
            "password": bool(focus.password),
        }
        if editable:
            payload["value_state"] = _value_state(focus)
        else:
            payload["editability"] = focus.editability
        if not focus.password:
            payload["raw_text"] = focus.text
            payload["raw_a11y_label"] = focus.desc
            payload["raw_hint"] = focus.hint
        return payload

    if interaction.focused_element is not None and interaction.focused_editable is None:
        envelope["focused_element"] = project(
            interaction.focused_element,
            editable=False,
        )
    if interaction.focused_editable is not None:
        envelope["focused_editable"] = project(
            interaction.focused_editable,
            editable=True,
        )
    return envelope
