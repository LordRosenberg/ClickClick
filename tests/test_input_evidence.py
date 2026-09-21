from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from agent.decision_context import semantic_tree_projection
from agent.executor import _focused_text_target, _target_snapshot, build_executor_prompt
from agent.observation_space import make_registry_entry
from agent.orchestrator import Orchestrator
from perception.input_evidence import (
    AdbImeEvidenceProvider,
    ProviderResult,
    build_interaction_state,
    element_identity,
    fuse_interaction_state,
    interaction_envelope,
)
from perception.observation import ObservationPackage
from perception.normalizer import normalize_a11y_tree, render_semantic_tree
from shared.schemas import (
    Action,
    ActionResult,
    CanonicalUI,
    ExecutorStep,
    FocusedElementEvidence,
    InteractionStateEvidence,
    ObservationMode,
    UIElement,
)


def _field(
    *, text: str = "", desc: str | None = None, hint: str = "",
    focused: bool = True, password: bool = False, index: int = 3,
) -> UIElement:
    return UIElement(
        index=index,
        role="android.widget.EditText",
        text=text,
        desc=("搜索" if text == "搜索" else "") if desc is None else desc,
        hint=hint,
        bounds=[10, 20, 410, 100],
        resource_id="0_resource_name_obfuscated",
        states={
            "focused": focused,
            "focusable": True,
            "selected": False,
            "password": password,
        },
    )


def _package(field: UIElement) -> ObservationPackage:
    ui = CanonicalUI(
        app_id="com.example",
        activity=".Main",
        elements=[field],
        semantic_tree=[field],
    )
    return ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_ONLY,
        text_for_llm="[3] EditText 搜索 focusable editable focused",
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        interaction_state=build_interaction_state(ui),
    )


def test_focus_known_while_keyboard_remains_unknown():
    evidence = build_interaction_state(
        CanonicalUI(elements=[_field(text="raw", desc="搜索, raw", hint="搜索")])
    )
    assert evidence.focused_element is not None
    assert evidence.focused_editable is not None
    assert evidence.focused_element.index == 3
    assert evidence.focused_editable.password is False
    assert evidence.focused_editable.value_available is True
    assert evidence.keyboard_visible is None
    assert evidence.sources == ["a11y"]


def test_selected_tab_is_not_focused_element():
    tab = UIElement(index=0, role="Tab", states={"selected": True}, clickable=True)
    evidence = build_interaction_state(CanonicalUI(elements=[tab]))
    assert evidence.focused_element is None
    assert evidence.focused_editable is None


def test_editable_identity_ignores_mutable_label_and_small_geometry_shift():
    before = _field(text="", desc="搜索, ")
    after = _field(text="", desc="搜索, 张凌赫")
    after.bounds = [16, 20, 416, 100]
    assert element_identity(before) == element_identity(after)


def test_duplicate_editable_identity_keeps_geometry_collision_safeguard():
    first = _field(text="same", index=1)
    second = _field(text="same", index=2)
    second.bounds = [10, 220, 410, 300]
    assert element_identity(first) != element_identity(second)


@pytest.mark.asyncio
async def test_ime_provider_timeout_soft_degrades():
    class SlowDriver:
        async def get_input_diagnostics(self, *, timeout_s: float):
            await asyncio.sleep(0.2)
            return {"keyboard_visible": True}

    assert await AdbImeEvidenceProvider(SlowDriver(), timeout_ms=5).collect() is None
    baseline = build_interaction_state(CanonicalUI(elements=[_field()]))
    fused = fuse_interaction_state(baseline, [None])
    assert fused.focused_editable is not None
    assert fused.keyboard_visible is None


def test_conflicting_keyboard_providers_degrade_to_unknown():
    baseline = build_interaction_state(CanonicalUI(elements=[_field()]))
    fused = fuse_interaction_state(
        baseline,
        [
            ProviderResult(keyboard_visible=True, source="one", confidence=0.8),
            ProviderResult(keyboard_visible=False, source="two", confidence=0.9),
        ],
    )
    assert fused.keyboard_visible is None
    assert {"one", "two"}.issubset(fused.sources)


def test_interaction_envelope_function_is_mechanically_scoped():
    assert list(inspect.signature(interaction_envelope).parameters) == ["interaction"]
    source = inspect.getsource(interaction_envelope).lower()
    for forbidden in (
        "instruction",
        "subgoal",
        "app_id",
        "package",
        "ui_text",
        "completion",
        "success",
        "action_history",
        "recommended_action",
        "fuzzy",
        "similarity",
        "embedding",
    ):
        assert forbidden not in source


def test_interaction_envelope_reports_empty_focused_value():
    envelope = interaction_envelope(
        build_interaction_state(CanonicalUI(elements=[_field(text="", desc="", hint="搜索")]))
    )
    assert envelope["focused_element"] is None
    assert envelope["focused_editable"]["index"] == 3
    assert envelope["focused_editable"]["value_state"] == "empty"
    assert envelope["focused_editable"]["raw_text"] == ""
    assert envelope["focused_editable"]["raw_hint"] == "搜索"
    assert envelope["keyboard"] is None


def test_interaction_envelope_reports_present_value_without_comparison():
    envelope = interaction_envelope(
        build_interaction_state(
            CanonicalUI(elements=[_field(text="Eiffel Tower", desc="", hint="搜索")])
        )
    )
    assert envelope["focused_editable"]["value_state"] == "present"
    assert envelope["focused_editable"]["raw_text"] == "Eiffel Tower"
    assert envelope["focused_editable"]["raw_hint"] == "搜索"


def test_interaction_envelope_reports_unavailable_value():
    interaction = InteractionStateEvidence(
        focused_element=FocusedElementEvidence(
            index=7,
            role="android.widget.EditText",
            text="",
            desc="搜索",
            hint="输入地点",
            value_available=False,
        ),
        focused_editable=FocusedElementEvidence(
            index=7,
            role="android.widget.EditText",
            text="",
            desc="搜索",
            hint="输入地点",
            value_available=False,
        ),
        keyboard_visible=False,
    )
    envelope = interaction_envelope(interaction)
    assert envelope["focused_editable"]["value_state"] == "unavailable"
    assert envelope["focused_editable"]["raw_text"] == ""
    assert envelope["focused_editable"]["raw_a11y_label"] == "搜索"
    assert envelope["focused_editable"]["raw_hint"] == "输入地点"
    assert envelope["keyboard"] is False


def test_interaction_envelope_redacts_password_value_desc_and_hint():
    interaction = InteractionStateEvidence(
        focused_element=FocusedElementEvidence(
            index=4,
            role="android.widget.EditText",
            text="secret",
            desc="密码",
            hint="输入密码",
            password=True,
            value_available=True,
        ),
        focused_editable=FocusedElementEvidence(
            index=4,
            role="android.widget.EditText",
            text="secret",
            desc="密码",
            hint="输入密码",
            password=True,
            value_available=True,
        ),
        keyboard_visible=True,
    )
    envelope = interaction_envelope(interaction)
    dumped = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert envelope["focused_editable"]["password"] is True
    assert envelope["focused_editable"]["value_state"] == "redacted"
    assert "raw_text" not in envelope["focused_editable"]
    assert "raw_a11y_label" not in envelope["focused_editable"]
    assert "raw_hint" not in envelope["focused_editable"]
    assert "secret" not in dumped
    assert "密码" not in dumped
    assert "输入密码" not in dumped


@pytest.mark.parametrize("keyboard", [True, False, None])
def test_interaction_envelope_preserves_keyboard_tri_state(keyboard: bool | None):
    interaction = InteractionStateEvidence(
        focused_element=FocusedElementEvidence(index=2, role="android.widget.EditText"),
        focused_editable=FocusedElementEvidence(index=2, role="android.widget.EditText"),
        keyboard_visible=keyboard,
    )
    assert interaction_envelope(interaction)["keyboard"] is keyboard


def test_interaction_state_preserves_complete_source_list():
    interaction = InteractionStateEvidence(
        focused_element=FocusedElementEvidence(index=2, role="android.widget.EditText"),
        focused_editable=FocusedElementEvidence(index=2, role="android.widget.EditText"),
        sources=["a11y", "driver", "adb_ime"],
    )
    assert interaction.sources == ["a11y", "driver", "adb_ime"]


def test_interaction_envelope_handles_no_focus():
    envelope = interaction_envelope(InteractionStateEvidence(keyboard_visible=True))
    assert envelope["focused_element"] is None
    assert envelope["focused_editable"] is None
    assert envelope["keyboard"] is True


def test_non_editable_focused_element_is_not_projected_as_focused_editable():
    button = UIElement(
        index=9,
        role="android.widget.Button",
        text="搜索",
        desc="搜索",
        bounds=[10, 20, 100, 60],
        states={"focused": True},
    )
    evidence = build_interaction_state(CanonicalUI(elements=[button]))
    envelope = interaction_envelope(evidence)
    assert evidence.focused_element is not None
    assert evidence.focused_editable is None
    assert envelope["focused_element"]["editability"] == "unknown"
    assert envelope["focused_element"]["raw_text"] == "搜索"
    assert envelope["focused_editable"] is None


@pytest.mark.parametrize("editable_first", [False, True])
def test_unique_focused_editable_survives_focused_webview_order(
    editable_first: bool,
):
    webview = UIElement(
        index=-1,
        role="android.webkit.WebView",
        text="Old results page",
        bounds=[0, 0, 1080, 2200],
        states={"focused": True},
    )
    field = _field(
        text="old query",
        desc="Address",
        hint="Search or enter URL",
        index=15,
    )
    semantic_tree = [field, webview] if editable_first else [webview, field]
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=semantic_tree,
        elements=[field],
    )

    evidence = build_interaction_state(ui)
    envelope = interaction_envelope(evidence)
    assert evidence.focused_element is not None
    assert evidence.focused_element.role == "android.webkit.WebView"
    assert evidence.focused_editable is not None
    assert evidence.focused_editable.index == 15
    assert envelope["focused_editable"]["raw_text"] == "old query"
    assert envelope["focused_editable"]["raw_hint"] == "Search or enter URL"
    assert "old query" not in render_semantic_tree(ui)


def test_duplicate_focused_editable_identity_projects_once():
    first = _field(text="same value", index=3)
    duplicate = _field(text="same value", index=4)
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[first, duplicate],
        elements=[first, duplicate],
    )

    evidence = build_interaction_state(ui)
    assert evidence.focused_editable is not None
    assert evidence.focused_editable.index == 3
    assert interaction_envelope(evidence)["focused_editable"]["raw_text"] == (
        "same value"
    )


def test_same_identity_with_conflicting_raw_channels_remains_ambiguous():
    first = _field(text="first value", desc="Address", hint="Enter URL", index=3)
    second = _field(text="second value", desc="Address", hint="Enter URL", index=4)
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[first, second],
        elements=[first, second],
    )

    evidence = build_interaction_state(ui)
    rendered = render_semantic_tree(ui)
    projected = semantic_tree_projection(ui.semantic_tree)

    assert evidence.focused_editable is None
    assert 'raw_text="first value"' in rendered
    assert 'raw_text="second value"' in rendered
    assert [row["raw_text"] for row in projected] == [
        "first value",
        "second value",
    ]


def test_same_identity_with_editability_conflict_remains_ambiguous():
    editable = _field(text="", desc="", hint="Search", index=3)
    conflicting = editable.model_copy(deep=True)
    conflicting.index = 4
    conflicting.states["editable"] = False
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[editable, conflicting],
        elements=[editable, conflicting],
    )

    assert element_identity(editable) == element_identity(conflicting)
    evidence = build_interaction_state(ui)
    rendered = render_semantic_tree(ui)
    projected = semantic_tree_projection(ui.semantic_tree)

    assert evidence.focused_editable is None
    assert rendered.count('raw_hint="Search"') == 2
    assert [row["raw_hint"] for row in projected] == ["Search", "Search"]


def test_same_identity_with_password_conflict_remains_ambiguous_and_redacted():
    editable = _field(text="visible", desc="", hint="Search", index=3)
    password = editable.model_copy(deep=True)
    password.index = 4
    password.states["password"] = True
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[editable, password],
        elements=[editable, password],
    )

    assert element_identity(editable) == element_identity(password)
    evidence = build_interaction_state(ui)
    rendered = render_semantic_tree(ui)

    assert evidence.focused_editable is None
    assert 'raw_text="visible"' in rendered
    assert rendered.count("raw_fields_redacted=true") == 1


def test_distinct_focused_editables_remain_ambiguous_and_visible_in_tree():
    first = _field(text="first value", index=3)
    second = _field(text="second value", index=4)
    second.bounds = [10, 220, 410, 300]
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[first, second],
        elements=[first, second],
    )

    evidence = build_interaction_state(ui)
    envelope = interaction_envelope(evidence)
    rendered = render_semantic_tree(ui)
    assert evidence.focused_element is None
    assert evidence.focused_editable is None
    assert envelope["focused_element"] is None
    assert envelope["focused_editable"] is None
    assert 'raw_text="first value"' in rendered
    assert 'raw_text="second value"' in rendered


def test_focus_ignores_inactive_window_but_includes_active_overlay():
    active_window = UIElement(
        index=-1,
        role="AccessibilityWindow",
        states={"active": True, "focused": True},
        interactable=False,
        window_wrapper=True,
        window_id=1,
    )
    inactive_window = active_window.model_copy(update={
        "states": {},
        "window_id": 2,
    })
    overlay_window = active_window.model_copy(update={
        "states": {"active": True},
        "window_id": 3,
    })
    active_field = _field(text="active", index=1).model_copy(
        update={"window_id": 1}
    )
    stale_field = _field(text="stale", index=2).model_copy(
        update={"window_id": 2}
    )
    ui = CanonicalUI(
        semantic_tree=[
            inactive_window,
            stale_field,
            active_window,
            active_field,
        ],
        elements=[stale_field, active_field],
    )

    assert build_interaction_state(ui).focused_editable.text == "active"

    overlay_field = _field(text="overlay", index=3).model_copy(
        update={"window_id": 3}
    )
    ui.semantic_tree.extend([overlay_window, overlay_field])
    ui.elements.append(overlay_field)
    assert build_interaction_state(ui).focused_editable is None


def test_password_focused_editable_survives_container_without_leaking():
    secret = "focused-password-secret"
    webview = UIElement(
        index=-1,
        role="android.webkit.WebView",
        states={"focused": True},
    )
    password = _field(
        text=secret,
        desc=f"desc-{secret}",
        hint=f"hint-{secret}",
        password=True,
        index=8,
    )
    ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[webview, password],
        elements=[password],
    )

    evidence = build_interaction_state(ui)
    combined = (
        json.dumps(interaction_envelope(evidence), ensure_ascii=False)
        + render_semantic_tree(ui)
    )
    assert evidence.focused_editable is not None
    assert evidence.focused_editable.password is True
    assert secret not in combined
    assert "raw_fields_redacted=true" in combined


def test_password_never_reaches_page_summary_or_persisted_step_digest():
    secret = "persisted-password-secret"
    ui = normalize_a11y_tree({
        "class": "Root",
        "children": [{
            "class": "android.widget.EditText",
            "text": secret,
            "contentDescription": f"description-{secret}",
            "hint": f"hint-{secret}",
            "editable": True,
            "focused": True,
            "password": True,
            "bounds": [10, 20, 410, 100],
        }],
    }, app_id="com.example")
    package = ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_ONLY,
        text_for_llm=render_semantic_tree(ui),
        image_for_llm=None,
        annotated_png=None,
        gap_reasons=[],
        interaction_state=build_interaction_state(ui),
        actionable=True,
        index_actionable=True,
        observation_id="password-observation",
    )
    step = ExecutorStep(
        action=Action(type="sleep"),
        summary="Observe without exposing a password",
    )
    report = Orchestrator._build_step_report(
        ui,
        ObservationMode.TREE_ONLY,
        step,
        ActionResult(success=True),
        {},
        package,
    )

    persisted = report.model_dump_json()
    assert secret not in ui.page_summary
    assert secret not in persisted
    assert report.observation_digest == ui.app_id


def test_explicit_false_editable_state_conflicts_with_editable_role():
    field = _field(text="", desc="搜索", hint="请输入")
    field.states["editable"] = False
    evidence = build_interaction_state(CanonicalUI(elements=[field]))
    envelope = interaction_envelope(evidence)

    assert evidence.focused_editable is None
    assert evidence.focused_element is not None
    assert evidence.focused_element.editability == "conflict"
    assert envelope["focused_element"]["editability"] == "conflict"
    assert envelope["focused_element"]["raw_text"] == ""
    rendered_tree = render_semantic_tree(CanonicalUI(
        app_id="com.example",
        semantic_tree=[field],
        elements=[field],
    ))
    assert "editability=conflict" in rendered_tree
    assert " editable " not in f" {rendered_tree} "
    decision_tree = semantic_tree_projection([field])
    assert decision_tree[0]["editability"] == "conflict"
    assert not (decision_tree[0].get("states") or {}).get("editable")


def test_password_conflict_remains_the_bound_redacted_text_target():
    field = _field(
        text="secret-value",
        desc="secret-description",
        hint="secret-hint",
        password=True,
    )
    field.states["editable"] = False
    package = _package(field)

    target = _focused_text_target(package)
    assert target is not None
    assert target.editability == "conflict"
    assert target.password is True
    snapshot = _target_snapshot(target).model_dump(mode="json")
    assert snapshot["editability"] == "conflict"
    assert snapshot["raw_fields_redacted"] is True
    assert "secret" not in json.dumps(snapshot)


@pytest.mark.parametrize("editability", ["editable", "conflict", "unknown"])
def test_password_focus_is_redacted_at_source_for_every_editability_state(editability: str):
    secret = f"source-secret-{editability}"
    field = _field(
        text=secret,
        desc=f"description-{secret}",
        hint=f"hint-{secret}",
        password=True,
    )
    if editability == "conflict":
        field.states["editable"] = False
    elif editability == "unknown":
        field.role = "android.view.View"
    interaction = build_interaction_state(CanonicalUI(
        app_id="com.example",
        semantic_tree=[field],
        elements=[field],
    ))
    serialized = interaction.model_dump_json()
    assert secret not in serialized
    assert f"description-{secret}" not in serialized
    assert f"hint-{secret}" not in serialized
    target = interaction.focused_editable or interaction.focused_element
    assert target is not None
    assert target.password is True
    assert target.editability == editability
    assert target.text == target.desc == target.hint == ""
    assert target.value_available is False
    assert secret not in target.identity


@pytest.mark.parametrize("editability", ["editable", "conflict", "unknown"])
def test_password_registry_persistence_projection_redacts_raw_nodes(editability: str):
    secret = f"registry-secret-{editability}"
    field = _field(
        text=secret,
        desc=f"description-{secret}",
        hint=f"hint-{secret}",
        password=True,
    )
    if editability == "conflict":
        field.states["editable"] = False
    elif editability == "unknown":
        field.role = "android.view.View"
    ui = CanonicalUI(app_id="com.example", semantic_tree=[field], elements=[field])
    entry = make_registry_entry(
        observation_id="obs-password",
        coordinate_space_id="space-password",
        ui=ui,
        actionable=True,
        index_actionable=True,
        captured_monotonic_ms=1,
        model_image_size=None,
        frame_geometry=(1080, 2400),
    )
    projection = entry.persistence_projection()
    rendered = json.dumps(projection, ensure_ascii=False)
    assert secret not in rendered
    assert projection["elements"][0]["raw_fields_redacted"] is True
    # Invocation-local grounding remains exact and is never replaced by the
    # redacted replay projection.
    assert entry.elements[0].text == secret


def test_empty_raw_text_and_password_redaction_cover_tree_and_target_history():
    empty = _field(text="", desc="搜索框", hint="输入关键词")
    empty_ui = CanonicalUI(app_id="com.example", semantic_tree=[empty], elements=[empty])
    empty_render = render_semantic_tree(empty_ui)
    empty_focus = interaction_envelope(build_interaction_state(empty_ui))
    assert 'raw_text=""' not in empty_render
    assert empty_focus["focused_editable"]["raw_text"] == ""
    assert empty_focus["focused_editable"]["raw_a11y_label"] == "搜索框"

    password = _field(
        text="secret-value",
        desc="secret-description",
        hint="secret-hint",
        password=True,
    )
    password_ui = CanonicalUI(
        app_id="com.example",
        semantic_tree=[password],
        elements=[password],
    )
    tree = render_semantic_tree(password_ui)
    decision_rows = json.dumps(
        semantic_tree_projection(password_ui.semantic_tree),
        ensure_ascii=False,
    )
    target = _target_snapshot(password).model_dump(mode="json")
    combined = tree + decision_rows + json.dumps(target, ensure_ascii=False)
    assert "secret-value" not in combined
    assert "secret-description" not in combined
    assert "secret-hint" not in combined
    assert "raw_fields_redacted" in combined
    assert target["raw_fields_redacted"] is True


def test_same_value_on_other_surface_does_not_mutate_envelope():
    base_ui = CanonicalUI(elements=[_field(text="", desc="", hint="搜索")])
    extra_ui = CanonicalUI(
        elements=[
            _field(text="", desc="", hint="搜索"),
            UIElement(index=82, role="View", text="Eiffel Tower", clickable=True),
        ]
    )
    assert interaction_envelope(build_interaction_state(base_ui)) == interaction_envelope(
        build_interaction_state(extra_ui)
    )


def _current_observation_payload(observation: str) -> dict:
    return json.loads(
        observation.split("CURRENT OBSERVATION:\n", 1)[1].splitlines()[0]
    )


def test_executor_prompt_reconciles_current_source_typed_focus_with_history():
    package = _package(_field(text="old query", desc="Search query", hint="Enter query"))
    system, task_anchor, history, observation = build_executor_prompt(
        "Replace the old query with Eiffel Tower", package
    )
    payload = _current_observation_payload(observation)
    normalized_system = " ".join(system.lower().split())

    assert "match the intended effect" in normalized_system
    assert "latest runtime state overrides older records" in normalized_system
    assert "repeated attempts without relevant progress" in normalized_system
    assert "supported change of target or method" in normalized_system
    assert "`type` inserts at the cursor" in normalized_system
    assert "`replace_text(index, text)` focuses a supported indexed editable" in normalized_system
    assert "Eiffel Tower" in task_anchor
    assert history == ""
    assert payload["foreground_package"] == "com.example"
    assert "focused_element" not in payload["focused_interaction"]
    focused = payload["focused_interaction"]["focused_editable"]
    assert focused["index"] == 3
    assert focused["raw_text"] == "old query"
    assert focused["raw_a11y_label"] == "Search query"
    assert focused["raw_hint"] == "Enter query"


@pytest.mark.parametrize(
    ("raw_text", "raw_label", "raw_hint"),
    [
        ("Eiffel Tower", "Search query", "Enter query"),
        ("old query", "Search query", "Eiffel Tower"),
        ("", "Suggested: Eiffel Tower", "Search places"),
    ],
)
def test_executor_observation_preserves_prefill_label_and_hint_without_semantic_fusion(
    raw_text: str,
    raw_label: str,
    raw_hint: str,
):
    package = _package(_field(text=raw_text, desc=raw_label, hint=raw_hint))
    _, _, _, observation = build_executor_prompt(
        "Retain Eiffel Tower only if it is the current field value", package
    )
    focused = _current_observation_payload(observation)["focused_interaction"][
        "focused_editable"
    ]

    assert focused["raw_text"] == raw_text
    assert focused["raw_a11y_label"] == raw_label
    assert focused["raw_hint"] == raw_hint
    assert "value_matches_task" not in focused
    assert "placeholder" not in focused
    assert "recommended_action" not in focused


def test_model_envelope_omits_internal_identity_bounds_confidence_age_and_sources():
    package = _package(_field(text="retained"))
    assert package.interaction_state is not None
    package.interaction_state.confidence = 0.97
    package.interaction_state.age_ms = 123
    package.interaction_state.sources = ["a11y", "adb_ime"]

    _, _, _, observation = build_executor_prompt("Keep the retained value", package)
    focused_interaction = _current_observation_payload(observation)["focused_interaction"]
    dumped = json.dumps(focused_interaction, ensure_ascii=False, sort_keys=True)

    for forbidden in ("identity", "bounds", "confidence", "age_ms", "sources"):
        assert forbidden not in dumped


def test_non_secret_source_fields_are_not_silently_truncated_by_a_presentation_cap():
    raw_text = "值" * 700
    raw_label = "标签" * 500
    raw_hint = "提示" * 500
    package = _package(_field(text=raw_text, desc=raw_label, hint=raw_hint))

    _, _, _, observation = build_executor_prompt("Inspect the current field", package)
    focused = _current_observation_payload(observation)["focused_interaction"][
        "focused_editable"
    ]

    assert focused["raw_text"] == raw_text
    assert focused["raw_a11y_label"] == raw_label
    assert focused["raw_hint"] == raw_hint
