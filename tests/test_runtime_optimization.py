"""Regression cases for role delivery, text reuse and bounded merged input."""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from agent.context_projection import reuse_observation_text
from agent.session import AgentSession, _delivered_text, executor_action_variants_schema
from agent.skills.library import SkillLibrary, parse_skill_markdown, serialize_skill_markdown
from agent.targeted_input import focused_same_target, input_method_visible, replace_target_text, supported_target
from shared.config import Settings
from shared.schemas import Action, ActionResult, CanonicalUI, UIElement


def field(index=1, *, focused=False, text="old", **kwargs):
    return UIElement(index=index, resource_id="name", role="android.widget.EditText",
                     window_id=7, bounds=[0, 80, 100, 120],
                     states={"editable": True, "enabled": True, "focused": focused}, text=text, **kwargs)


def package(*nodes, app="com.demo", complete=True, ime=False):
    semantic_tree = list(nodes)
    if ime:
        semantic_tree.append(UIElement(
            index=-1, role="Window", interactable=False,
            window_id=9, window_type=2, window_wrapper=True,
        ))
    return SimpleNamespace(accepted=True, index_actionable=True,
                           ui=CanonicalUI(app_id=app, elements=list(nodes),
                                          semantic_tree=semantic_tree,
                                          capture_complete=complete),
                           interaction_state=None)


def test_focus_identity_allows_changed_value_index_and_keyboard_geometry():
    target = field()
    before = package(target)
    moved = field(index=9, focused=True, text="formatted")
    moved.bounds = [0, 20, 100, 60]
    assert focused_same_target(before, target, package(moved)) == moved


@pytest.mark.parametrize("change", ["duplicate", "password", "incomplete", "disabled", "missing_id"])
def test_unsupported_input_never_guesses_target(change):
    target = field()
    current = package(target)
    if change == "duplicate":
        current.ui.elements.append(field(index=2))
    elif change == "password":
        target.states["password"] = True
    elif change == "incomplete":
        current.ui.capture_complete = False
    elif change == "disabled":
        target.states["enabled"] = False
    else:
        target.resource_id = ""
    assert supported_target(current, 1) is None


def test_same_index_is_not_identity_and_other_focus_cannot_authorize_input():
    target = field()
    wrong = field(focused=True)
    wrong.resource_id = "other"
    assert focused_same_target(package(target), target, package(wrong)) is None
    assert focused_same_target(package(target), target, package(field(focused=True), field(2, focused=True))) is None


@pytest.mark.parametrize("window_type,window_id,allowed", [
    (2, 9, True), (2, 7, False), (2, None, False),
    (1, 9, False), (None, 9, False), (3, 9, False),
])
def test_input_focus_distinguishes_ime_from_competing_windows(window_type, window_id, allowed):
    target = field()
    focused = field(focused=True)
    other = UIElement(index=5, role="android.widget.ImageView", window_id=window_id,
                      window_type=window_type, states={"focused": True, "editable": False})
    actual = focused_same_target(package(target), target, package(focused, other))
    assert (actual is focused) is allowed


@pytest.mark.parametrize("role,editable", [
    ("android.widget.EditText", True), ("android.widget.EditText", False),
    ("android.widget.ImageView", None),
])
def test_ime_editors_and_unknown_editability_cannot_authorize_replacement(role, editable):
    target = field()
    states = {"focused": True}
    if editable is not None:
        states["editable"] = editable
    ime_control = UIElement(index=4, role=role, window_id=9, window_type=2, states=states)
    assert focused_same_target(package(target), target, package(field(focused=True), ime_control)) is None


@pytest.mark.asyncio
async def test_ime_navigation_focus_does_not_split_one_targeted_replacement():
    target = field()
    calls = []
    ime_back = UIElement(index=0, role="android.widget.ImageView", window_id=9,
                         window_type=2, resource_id="input_method_nav_back",
                         states={"focused": True, "editable": False})

    async def act(action, current, **kwargs):
        calls.append(action.type)
        return ActionResult(success=True), package(
            field(index=8, focused=True, text="final" if action.type == "replace_text" else "old"),
            ime_back, ime=True,
        )

    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act),
        Action(type="replace_text", index=1, text="final"), package(target),
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1)
    assert calls == ["tap_xy", "replace_text"]
    assert result.success and result.detail["input_steps"]["readback"] == "exact_match"
    assert result.detail["device_action_units"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("already_focused", [False, True])
async def test_merged_input_single_submission_counts_one_androidworld_step(already_focused):
    before = package(field(focused=already_focused), ime=already_focused)
    calls = []

    async def act(action, current, **kwargs):
        calls.append(action)
        return ActionResult(success=True), package(field(focused=True, text="final" if action.type == "replace_text" else "changed on focus"))

    async def observe(**kwargs):
        return before

    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act, observe_current=observe),
        Action(type="replace_text", index=1, text="final"), before,
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=2)
    assert [call.type for call in calls] == (["replace_text"] if already_focused else ["tap_xy", "replace_text"])
    assert calls[-1].index is None
    assert result.detail["device_action_units"] == 1
    assert result.detail["input_steps"]["readback"] == "exact_match"


@pytest.mark.asyncio
async def test_focused_editor_without_input_method_is_tapped_before_replace():
    before = package(field(focused=True))
    calls = []

    async def act(action, current, **kwargs):
        calls.append(action)
        return ActionResult(success=True), package(
            field(focused=True, text="final" if action.type == "replace_text" else "old"),
            ime=True,
        )

    result, _ = await replace_target_text(
        SimpleNamespace(act_and_observe=act),
        Action(type="replace_text", index=1, text="final"), before,
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1,
    )
    assert not input_method_visible(before)
    assert [action.type for action in calls] == ["tap_xy", "replace_text"]
    assert result.success
    assert result.detail["input_steps"]["readback"] == "exact_match"


@pytest.mark.asyncio
async def test_replace_readback_mismatch_is_not_reported_as_success():
    before = package(field(focused=True), ime=True)

    async def act(action, current, **kwargs):
        return ActionResult(success=True), package(field(focused=True, text="old"), ime=True)

    async def observe(**kwargs):
        return before

    result, _ = await replace_target_text(
        SimpleNamespace(act_and_observe=act, observe_current=observe),
        Action(type="replace_text", index=1, text="final"), before,
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1,
    )
    assert not result.success
    assert result.message == "replace_text: readback_mismatch"
    assert result.detail["input_steps"]["readback"] == "different"


@pytest.mark.asyncio
async def test_partial_focus_stops_before_clearing_wrong_field():
    calls = []

    async def act(action, current, **kwargs):
        calls.append(action)
        wrong = field(focused=True)
        wrong.resource_id = "other"
        return ActionResult(success=True), package(wrong)

    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act),
        Action(type="replace_text", index=1, text="final"), package(field()),
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=2)
    assert len(calls) == 1 and not result.success
    assert result.detail["input_steps"]["clear"] == "not_attempted"


@pytest.mark.asyncio
async def test_budget_is_checked_before_any_focus_or_clear():
    async def act(*args, **kwargs):
        pytest.fail("No write or tap may be dispatched")

    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act),
        Action(type="replace_text", index=1, text="final"), package(field()),
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=0)
    assert not result.success
    assert result.detail["input_steps"]["focus"] == "not_attempted"


@pytest.mark.asyncio
async def test_clear_success_input_failure_is_not_reported_as_unexecuted():
    async def act(*args, **kwargs):
        return ActionResult(success=False, detail={"stage": "input"}), package(field(focused=True, text=""))

    async def observe(**kwargs):
        return package(field(focused=True), ime=True)

    result, _ = await replace_target_text(SimpleNamespace(act_and_observe=act, observe_current=observe),
        Action(type="replace_text", index=1, text="final"), package(field(focused=True), ime=True),
        target_snapshot=None, cancel_requested=lambda: False, remaining_actions=1)
    assert result.detail["input_steps"] == {"focus": "confirmed", "clear": "dispatched", "input": "failed", "readback": "different"}


def test_text_reuse_preserves_images_order_original_and_changed_values():
    text = "observed source value " * 100
    messages = [{"role": "user", "content": [{"type": "text", "text": text},
                 {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}"}}]} for i in range(3)]
    messages[2]["content"][0]["text"] += " corrected"
    original = deepcopy(messages)
    result = reuse_observation_text(messages)
    assert messages == original
    assert "repeats observation-text-" in result[1]["content"][0]["text"]
    assert result[0]["content"][0]["text"].endswith(text)
    assert result[2]["content"][0]["text"].endswith(" corrected")
    assert [row["content"][1] for row in result] == [row["content"][1] for row in original]
    assert reuse_observation_text([original[1]]) == [original[1]]


def test_role_sections_require_explicit_opt_in_and_keep_unknown_sections():
    body = "## Shared\nExact source fields.\n## Execution\nTap route.\n## Verification\nRead saved fields.\n## Unclassified\nPreserve duplicates."
    text = serialize_skill_markdown(name="demo", body=body, extra_frontmatter={"role_sections": True})
    pack = parse_skill_markdown(text)
    for role in ["planner", "reviewer", "executor"]:
        view = pack.section_for(role)
        assert "Exact source fields" in view and "Preserve duplicates" in view
        assert ("Tap route" in view) == (role == "executor")
        assert ("Read saved fields" in view) == (role != "planner")
    legacy = parse_skill_markdown(serialize_skill_markdown(name="legacy", body=body))
    assert "Tap route" in legacy.section_for("planner") and "Read saved fields" in legacy.section_for("planner")


def test_loaded_skill_version_refreshes_and_planner_candidates_receive_core(tmp_path):
    library = SkillLibrary(tmp_path)
    library.create_canonical(name="demo", app="com.demo", kind="app_core", body="## Hints\n- preserve all fields")
    session = AgentSession("planner", "test", library=library)
    session.set_foreground_app("other")
    session.set_workflow_catalog(["com.demo"], core_apps=["com.demo"])
    assert "preserve all fields" in _delivered_text(session._workflow_catalog)
    session.set_target_app("com.demo")
    library.update_skill("demo", version="2.0.0", body="## Hints\n- preserve corrected fields")
    session.set_target_app("com.demo")
    wire = str(session._k_wire())
    assert "@2.0.0" in wire and "corrected fields" in wire and "@0.1.0" not in wire


def test_skill_owned_aliases_route_core_without_guessing_ambiguous_names(tmp_path):
    from agent.skills.scope import scan_instruction_for_skill_apps

    library = SkillLibrary(tmp_path)
    library.create_canonical(name="expense", app="com.expense", kind="app_core", body="## Hints\n- fields")
    library.update_skill("expense", extra_frontmatter={"app_aliases": ["Expense", "Pro Expense"]})
    scan = lambda text, aliases={}: scan_instruction_for_skill_apps(text, library=library, alias_seed=aliases)
    assert scan("Log in Pro Expense") == ["com.expense"]
    assert scan("Use com.expense") == ["com.expense"]
    assert scan("the expenses") == []
    assert scan("Use Expense", {"Expense": "com.other"}) == []


def test_model_role_context_policy_is_explicit_and_default_unchanged():
    settings = Settings(_env_file=None, models_json=json.dumps({"chatgpt/test": {"context": {
        "max_input_tokens": 80000, "executor": {"history_tokens": 24000}}}}))
    assert settings.context_policy("unknown", "executor") == {"history_tokens": 16000}
    assert settings.context_policy("chatgpt/test", "executor") == {"history_tokens": 24000, "max_input_tokens": 80000}
    assert settings.context_policy("chatgpt/test", "planner") == {"max_input_tokens": 80000}


def test_replace_schema_keeps_old_path_and_exposes_optional_index():
    variant = next(v for v in executor_action_variants_schema()["anyOf"] if v["properties"]["type"]["enum"] == ["replace_text"])
    assert "index" in variant["properties"] and "index" not in variant["required"]
    assert "separate tap first" not in variant["description"]


@pytest.mark.asyncio
async def test_executor_dispatches_targeted_replacement_from_one_model_call(monkeypatch):
    from dataclasses import replace
    from test_executor import _make_executor, _submit_json
    from perception.observation import ObservationPackage
    from shared.schemas import ObservationMode

    executor, driver, calls = _make_executor(monkeypatch,
        first_content=_submit_json({"type": "replace_text", "index": 1, "text": "final"}),
        second_content="")
    before = ObservationPackage(ui=package(field()).ui, mode=ObservationMode.TREE_ONLY,
        text_for_llm="editable index 1", image_for_llm=None, annotated_png=None,
        gap_reasons=[], accepted=True, actionable=True, index_actionable=True,
        frame_width=900, frame_height=1600)

    async def transact(self, action, current, **kwargs):
        result = await driver.act(action)
        node = field(focused=True, text="final" if action.type == "replace_text" else "after focus")
        return result, replace(before, ui=package(node).ui)

    monkeypatch.setattr("agent.executor.ActionObservationTransaction.act_and_observe", transact)
    step, result, *_ = await executor.act_once("Enter final", before)
    assert calls["n"] == 1
    assert [action.type for action in driver.acts] == ["tap_xy", "replace_text"]
    assert step.submitted_action_snapshot.index == 1
    assert result.detail["device_action_units"] == 1
    assert result.detail["input_steps"]["readback"] == "exact_match"
