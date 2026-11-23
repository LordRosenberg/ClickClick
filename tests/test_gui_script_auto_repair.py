from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from agent.revisable.store import TaskStore
from agent.skills.script_runner import ScriptRunResult, ScriptStepReceipt, TakeoverDecision
from agent.skills.script_schema import GuiScript, script_fingerprint
from evaluation import gui_scripts
from evaluation import gui_script_auto_repair
from evaluation.gui_script_auto_repair import (ScriptRecoveryObservationBuilder, _dismiss_step, _same_control_step,
                                               is_low_impact_initial, is_low_impact_repair)
from evaluation.gui_script_release import register_script
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import digest
from shared.schemas import AgentState, CanonicalUI, ObservationMode, TaskStatus, UIElement


APP = "com.example.demo"
GOAL = "Open the result page."


def present(**fields):
    return {"type": "target_present", "target": fields}


def script(*, revised=False, step_type="tap"):
    target = {"resource_id": "play_button"}
    if not revised:
        target["desc"] = "Advance 12"
    action = {"type": step_type, **({"target": target} if step_type == "tap" else {})}
    return GuiScript.model_validate({
        "app_id": APP, "script_id": "auto-test", "revision": 2 if revised else 1,
        "initial": [present(text="Ready")], "final": [present(text="Done")],
        "steps": [{"id": "open", "before": [present(**target)], "after": [present(text="Done")],
                   "action": action, "timeout_seconds": 12.0}],
    })


def observation(elements, *, windows=(), tree=None):
    return ObservationPackage(ui=CanonicalUI(app_id=APP, capture_complete=True, elements=elements,
                                             semantic_tree=tree or elements),
                              mode=ObservationMode.TREE_ONLY, text_for_llm="", image_for_llm=None,
                              annotated_png=None, gap_reasons=[],
                              capture_meta={"tree_ownership_candidates": windows})


def node(desc, *, index=0, resource_id="play_button", window_id=None, role="Button", text=""):
    return UIElement(index=index, role=role, desc=desc, resource_id=resource_id, window_id=window_id,
                     clickable=index >= 0, interactable=index >= 0, text=text)


def takeover_scope(artifact):
    return SimpleNamespace(script=artifact, step=artifact.steps[0], parameters={}, failure=ScriptStepReceipt(
        step_id="open", attempt=1, succeeded=False, execution="not_dispatched", reason="before_condition_unsatisfied"))


def test_contracted_relocation_requires_a_unique_stable_control_and_same_description_prefix():
    base = script()
    context = takeover_scope(base)
    relocated = _same_control_step(context, observation([node("Advance 13")]))
    assert relocated.action.target.desc == "Advance 13"
    assert relocated.action.target.resource_id == "play_button"
    assert relocated.before[0].target == relocated.action.target
    assert relocated.after == base.steps[0].after
    assert _same_control_step(context, observation([node("Delete 13")])) is None
    assert _same_control_step(context, observation([node("Advance 13"), node("Advance 14", index=1)])) is None
    assert _same_control_step(context, observation([node("Advance 12")])) is None
    assert is_low_impact_repair(base, script(revised=True))
    assert not is_low_impact_repair(base, script(revised=True, step_type="back"))
    assert not is_low_impact_initial(base)
    assert is_low_impact_initial(script(step_type="back"))


def test_modal_requires_app_owned_active_upper_dialog_and_a_unique_cancel_button():
    context = takeover_scope(script())
    entry = {"source_kind": "window", "window_type": 1, "display_id": 0, "packages": [APP]}
    windows = [{**entry, "window_id": 8, "window_layer": 0, "active": False, "focused": False},
               {**entry, "window_id": 9, "window_layer": 1, "active": True, "focused": True}]
    cancel = node("Cancel", window_id=9, resource_id="cancel", index=1)
    dialog = node("", window_id=9, role="AlertDialog", resource_id="", index=-1)
    original = node("Advance 12", window_id=8)
    dismissal, window_id = _dismiss_step(context, observation([original, cancel], windows=windows,
                                                              tree=[original, dialog, cancel]))
    assert window_id == 9 and dismissal.action.target.resource_id == "cancel"
    assert dismissal.after[-1].type == "target_absent"
    assert _dismiss_step(context, observation([original, cancel], windows=windows)) is None
    foreign = [windows[0], {**windows[1], "packages": ["com.example.other"]}]
    assert _dismiss_step(context, observation([original, cancel], windows=foreign, tree=[original, dialog, cancel])) is None
    confirm = cancel.model_copy(update={"desc": "Confirm"})
    assert _dismiss_step(context, observation([original, confirm], windows=windows, tree=[original, dialog, confirm])) is None


def stock_alert(*, prefix="android", label="CANCEL", positive="CONTINUE", inside=True,
                handle="fresh-negative-handle", complete=True, provider="accessibility_collector_channel",
                owned=True, original_visible=False):
    def widget(role, resource, *children, **fields):
        return {"class": f"android.widget.{role}", "resource_id": resource, "package": APP,
                "bounds": [50, 80, 400, 540], "children": list(children), **fields}

    cancel = widget("Button", f"{prefix}:id/button2", node_handle=handle, text=label, enabled=True, clickable=True)
    continue_button = widget("Button", "android:id/button1", text=positive, enabled=True, clickable=True)
    title = widget("TextView", "android:id/alertTitle", text="Temporary alert")
    content = widget("FrameLayout", "android:id/contentPanel", text="Fixture notice")
    panel = widget("ScrollView", "android:id/buttonPanel", *((cancel, continue_button) if inside else (continue_button,)))
    layout = widget("LinearLayout", "android:id/parentPanel", title, content, panel, *((cancel,) if not inside else ()))
    roots = [layout]
    if original_visible:
        roots.append(widget("Button", f"{APP}:id/play_button", contentDescription="Advance 12",
                            enabled=True, clickable=True))
    window = {"source_kind": "window", "window_type": 1, "display_id": 0, "packages": [APP if owned else "other.app"],
              "window_id": 9, "window_layer": 0, "active": True, "focused": True}
    raw = {"class": "android.view.accessibility.WindowSet", "window_wrapper": True,
           "children": [{**window, "class": "android.view.accessibility.AccessibilityWindow", "window_wrapper": True,
                         "bounds": [50, 80, 400, 540], "children": roots}],
           "_capture": {"provider": "adb_fallback", "tree_provider": provider,
                        "complete": True, "coordinate_compatible": True,
                        "tree_ownership_candidates_complete": complete, "tree_ownership_candidates": [window]}}
    return ScriptRecoveryObservationBuilder().build((raw, None), app_id=APP, frame_geometry=(1080, 1920))


def test_stock_platform_dialog_uses_framework_negative_button_in_its_verified_panel():
    dismissal, window_id = _dismiss_step(takeover_scope(script()), stock_alert())
    assert window_id == 9 and dismissal.action.target.resource_id == "button2"
    assert dismissal.action.target.text == "CANCEL"
    assert dismissal.after[0] == script().steps[0].before[0]


@pytest.mark.parametrize("change", [
    {"prefix": APP}, {"label": "Delete"}, {"label": "Dismiss"}, {"positive": "Close"},
    {"inside": False}, {"handle": ""}, {"complete": False}, {"provider": "uiautomator_dump"},
    {"owned": False}, {"original_visible": True},
])
def test_stock_dialog_guard_will_not_use_lookalike_ambiguous_or_incomplete_controls(change):
    assert _dismiss_step(takeover_scope(script()), stock_alert(**change)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_click", [True, False])
async def test_stock_dialog_only_hands_back_after_bound_native_click(monkeypatch, native_click):
    blocked = stock_alert()
    underlying = {**blocked.capture_meta["tree_ownership_candidates"][0], "window_id": 10}
    restored = observation([node("Advance 12", window_id=10)], windows=[underlying])
    frames = iter((blocked, restored))
    receipts = []

    async def observe():
        return next(frames)

    async def resume_status():
        return {TakeoverDecision.RETRY_CURRENT}

    async def dispatch(*args, before_dispatch, **kwargs):
        before_dispatch()
        receipts.append("negative_button")
        return SimpleNamespace(status="succeeded")

    monkeypatch.setenv("CLICKCLICK_NODE_CLICK_ENABLED", "1" if native_click else "0")
    monkeypatch.setattr(gui_script_auto_repair, "run_gui_script", dispatch)
    callback = gui_script_auto_repair.contracted_recovery(driver=SimpleNamespace(supports_native_node_click=True),
                                                          transaction=None, evidence=None)
    ctx = takeover_scope(script())
    ctx.observe, ctx.resume_status = observe, resume_status
    handoff = await callback(ctx)
    assert handoff == (TakeoverDecision.RETRY_CURRENT if native_click else TakeoverDecision.STOP)
    assert receipts == (["negative_button"] if native_click else [])


class PlaybackBuild:
    async def read_app_build(self, app):
        return {"package": app, "version_code": "1"}


def journal_replay(artifact, settings, fixture, *, assisted=False, passed=True):
    fixture = {**fixture, "initial_state_hash": "bound-initial", "goal_initially_met": False}
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    try:
        task = db.create_task(GOAL, AgentState(instruction=GOAL), device_serial="fixture")
        db.update_task(task.id, status=TaskStatus.SUCCEEDED if passed else TaskStatus.FAILED)
        reason = "postcondition_met" if passed and not assisted else "before_condition_unsatisfied"
        step = ScriptStepReceipt(step_id="open", attempt=1, succeeded=passed and not assisted,
                                 execution="not_dispatched" if assisted or not passed else "known", reason=reason)
        receipt = ScriptRunResult(script_hash=script_fingerprint(artifact), script_id=artifact.script_id,
                                  revision=artifact.revision, status="succeeded" if passed else "failed",
                                  assisted=assisted, steps=(step,), final_asserted=passed, reason=reason).model_dump(mode="json")
        TaskStore(db, artifacts, task.id).put("gui_script_run", "final", {"fixture": fixture, "receipt": receipt})
        return {"task_id": task.id, "status": receipt["status"], "fixture": fixture, "receipt": receipt}
    finally:
        db.close()


def reviewed(artifact, **provenance):
    sha = script_fingerprint(artifact)
    return {"script": artifact.model_dump(mode="json"), "script_hash": sha,
            "provenance": {"goal": GOAL, **provenance}, "review": {"decision": "accept", "script_hash": sha}}


def test_registry_directory_selects_latest_enrolled_revision_without_falling_back_from_corruption(tmp_path):
    base, revised = script(), script(revised=True)
    directory = tmp_path / base.script_id
    directory.mkdir()
    for artifact in (base, revised):
        enrollment = {**reviewed(artifact), "replay_required": False, "verification": {
            "update": "verified_pending_review", "second": {"status": "succeeded", "receipt": {
                "script_hash": script_fingerprint(artifact), "script_id": artifact.script_id,
                "revision": artifact.revision, "status": "succeeded", "assisted": False, "final_asserted": True}},
        }}
        (directory / f"{artifact.revision}.json").write_text(json.dumps(enrollment), encoding="utf-8")
    picked, _, parsed = gui_scripts._replay_artifact(directory)
    assert picked.name == "2.json" and parsed == revised
    assert gui_scripts._replay_artifact(directory / "1.json")[2] == base
    (directory / "3.json").write_text(json.dumps(reviewed(revised)), encoding="utf-8")
    with pytest.raises(ValueError, match="latest registered revision"):
        gui_scripts._replay_artifact(directory)


@pytest.mark.asyncio
@pytest.mark.parametrize("original_fails,high_impact", [(False, False), (True, False), (False, True), (True, True)])
async def test_automatic_release_replays_that_exact_registered_original_fixture(tmp_path, monkeypatch,
                                                                               original_fails, high_impact):
    base, revised = script(), script(revised=True)
    if high_impact:
        change = revised.steps[0].model_copy(update={"timeout_seconds": 3.0})
        revised = revised.model_copy(update={"steps": (change,)})
    registry = tmp_path / "registry"
    fixture = {"snapshot": "original", "app_build": "1", "avd": "fixture-avd", "parameters_hash": digest({})}
    replays = []
    for name in ("baseline-a", "baseline-b"):
        runtime = tmp_path / name
        replayed = journal_replay(base, gui_scripts.evaluation_settings(None, runtime), fixture)
        replays.append({**replayed, "runtime": str(runtime)})
    path = register_script(base, reviewed(base), {"update": "verified_pending_review",
                                                   "first": replays[0], "second": replays[1]}, registry)
    original_bytes = path.read_bytes()
    snapshots = []

    async def restore(_adb, _serial, snapshot):
        snapshots.append(snapshot)

    async def open_driver(_settings, _serial, operation, **_kwargs):
        return await operation(PlaybackBuild())

    async def replay_owned(artifact, *, settings, fixture, assist, **_kwargs):
        assert bool(assist) is (artifact == base)
        passed = not (original_fails and fixture["snapshot"] == "original")
        return journal_replay(artifact, settings, fixture, assisted=bool(assist), passed=passed)

    @contextmanager
    def exclusive(*_args):
        yield SimpleNamespace(quarantined=False)

    async def learn(_args, *, evidence, **_kwargs):
        assert evidence["receipt"]["assisted"] and evidence["receipt"]["final_asserted"]
        assert evidence["receipt"]["steps"][0]["execution"] == "not_dispatched"
        return {"status": "candidate", "candidate": reviewed(revised, base_hash=script_fingerprint(base),
                                                            assisted_run_hash=digest(evidence))}

    monkeypatch.setattr(gui_scripts, "restore_snapshot", restore)
    monkeypatch.setattr(gui_scripts, "with_android_driver", open_driver)
    monkeypatch.setattr(gui_scripts, "exclusive_emulator", exclusive)
    monkeypatch.setattr(gui_scripts, "_learn", learn)
    monkeypatch.setattr(gui_scripts, "replay_owned", replay_owned)
    report = await gui_scripts._replay(SimpleNamespace(script=path, env_file=None, goal=GOAL, parameters=None, assets=None,
                                                      ocr_executable=None, assist_policy=None, auto_repair_registry=registry,
                                                      repair=False, verify=False, snapshot="new", adb="unused", avd="fixture-avd",
                                                      serial="fixture", seconds=100.0, output=tmp_path / "output"))
    assert snapshots == ["new", "new", "original"]
    assert path.read_bytes() == original_bytes
    expected = "verified_pending_review" if high_impact else "automatically_registered"
    assert report["update"] == ("original_regression_incomplete" if original_fails else expected)
    published = path.with_name("2.json")
    assert published.exists() is (not original_fails and not high_impact)
    if published.exists():
        assert json.loads(published.read_text(encoding="utf-8"))["verification"]["original"]["fixture"] == replays[0]["fixture"]
