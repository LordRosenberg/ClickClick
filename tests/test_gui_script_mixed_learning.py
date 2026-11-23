"""One recorded path can contain both accessibility and screenshot bindings."""

import io
import json

import pytest
from PIL import Image

from agent.revisable.store import TaskStore
from agent.skills import script_mixed_learning
from agent.skills.script_schema import GuiScript, script_fingerprint
from agent.traces import TraceWriter
from shared.artifacts import ArtifactStore
from shared.config import Settings
from shared.db import Database
from shared.schemas import AgentState, TaskStatus
from test_gui_script_learning import FakeBudget, present, run_inline
from test_gui_visual_object_record import CLICK, capture, selection


APP = "example.alarms"


class MixedBudget(FakeBudget):
    review_content = None

    async def ask(self, model, system, payload, settings, *, user_content=None):
        if user_content is not None:
            self.review_content = user_content
        return await super().ask(model, system, payload, settings)


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(script_mixed_learning, "to_thread", inline_thread)
    settings = Settings(_env_file=None, data_dir=tmp_path, manager_model="author", skill_reviewer_model="reviewer")
    db = Database(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir)
    task = db.create_task("Disable alarm A and verify the recorded result.",
                          AgentState(instruction="Disable alarm A and verify the recorded result.",
                                     generate_test_script=True))
    db.update_task(task.id, status=TaskStatus.SUCCEEDED)
    store = TaskStore(db, artifacts, task.id)
    stream = io.BytesIO()
    Image.new("RGB", (184, 148), "#112027").save(stream, format="PNG")
    frames = [
        ("start", "tree-only", '[4] Button raw_text="Open" clickable id=open_controls', stream.getvalue()),
        ("visual", "image-only", "", capture(first_active=True)),
        ("verify", "tree+image", '[6] Button raw_text="Verify" clickable id=verify_result',
         capture(first_active=False)),
        ("done", "tree-only", 'TextView raw_text="Recording complete" id=result_complete', None),
    ]
    for key, mode, tree, png in frames:
        store.put("observation", key, {"app": APP, "text": f"foreground_package={APP}\n{tree}"})
        ref = artifacts.save_content_addressed_bytes("script_recording_frames", png, suffix=".png") if png else None
        store.put("script_recording_frame", key, {
            "accepted": True, "status": "saved" if png else "unavailable", "image_ref": ref,
            "mode": mode, "coordinate_space_id": f"space-{key}", "index_actionable": mode != "image-only",
            "image_size": [184, 148], "frame_size": [184, 148], "crop_box": None, "rotation_degrees": 0,
        })

    for index, (before, after, action) in enumerate((
        ("start", "visual", {"type": "tap", "index": 4}),
        ("visual", "verify", {"type": "tap_xy", "x": CLICK[0], "y": CLICK[1]}),
        ("verify", "done", {"type": "tap", "index": 6}),
    ), start=1):
        receipt = {"transaction_id": f"tx-{index}", "dispatch_succeeded": True, "observation_accepted": True,
                   "observation_id": after, "effect_outcome": "confirmed"}
        outcome = {"success": True, "receipt": receipt}
        store.put("event", str(index), {"step": index, "decision": "act", "observation_id": before,
                                      "submitted_action": action, "action_result": outcome})
        TraceWriter(db, artifacts).write(task.id, kind="executor_tick", step_seq=index, payload={
            "decision": "act", "basis_observation_id": before, "observation_id": before,
            "observation_accepted": True, "action": action, "action_result": outcome,
            "action_pipeline": {"basis_observation_id": before, "coordinate_space_id": f"space-{before}", "stages": [
                {"stage": "submitted", "action": action, "observation_id": before},
                {"stage": "dispatched", "action": action, "coordinate_space": "device"},
            ]},
        })
    yield db.get_task(task.id), settings, store
    db.close()


def proposal():
    return {"decision": "propose", "steps": [
        {"event": "event:1@1", "mode": "tree", "action": {"type": "tap", "target": {"resource_id": "open_controls"}},
         "before": [present("open_controls")], "after": []},
        {"event": "event:2@1", "mode": "visual"},
        {"event": "event:3@1", "mode": "tree", "action": {"type": "tap", "target": {"resource_id": "verify_result"}},
         "before": [present("verify_result")], "after": [present("result_complete")]},
    ]}


def author(mixed, destination, budget):
    task, settings, store = mixed
    return run_inline(script_mixed_learning.author_mixed_script(task, settings=settings, store=store,
                                                                destination=destination, budget=budget))


@pytest.mark.parametrize("review", [True, False])
def test_mixed_native_recording_compiles_continuous_cross_mode_checks(mixed, tmp_path, monkeypatch, review):
    requests = []

    async def select(**data):
        requests.append(data)
        return selection()

    monkeypatch.setattr(script_mixed_learning, "select_visual_recording", select)
    budget = MixedBudget(proposal(), review=review)
    output = tmp_path / "mixed"
    result = author(mixed, output, budget)
    assert requests[0]["click"] == CLICK
    assert requests[0]["before"] == capture(first_active=True)
    assert [part["type"] for part in budget.review_content] == ["text", "text", "image_url", "text", "image_url"]
    assert "event:2@1" in budget.review_content[1]["text"]
    if not review:
        assert result["status"] == "rejected"
        assert not output.exists()
        return
    assert result["status"] == "candidate", result
    assert result["modes"] == ["tree", "visual", "tree"]
    recorded = json.loads((output / "script.json").read_text(encoding="utf-8"))
    script = GuiScript.model_validate_json(json.dumps(recorded["script"]))
    assert recorded["script_hash"] == script_fingerprint(script)
    assert recorded["replay_required"] is True and result["budget"]["actions"] == 0
    assert [step.action.type for step in script.steps] == ["tap", "tap_visual", "tap"]
    assert script.steps[0].after[-1].target == script.steps[1].action.object
    assert script.steps[1].before.count(script.steps[0].after[-1]) == 1
    assert script.steps[1].after[-1].type == "visual_object_present"
    assert script.steps[1].after[-1] in script.steps[2].before
    assert script.final[-1].target.resource_id == "result_complete"
    assert (output / script.steps[1].action.object.layout.png_path).is_file()
    assert len(budget.requests) == 2


@pytest.mark.parametrize(
    "change,reason",
    (
        ({"status": "unavailable"}, "recording_original_pixels_unavailable"),
        ({"coordinate_space_id": "wrong"}, "recording_touch_coordinate_space_mismatch"),
        ({"crop_box": [1, 1, 180, 140]}, "recording_uses_unsupported_pixel_transform"),
    ),
)
def test_unverifiable_retained_visual_frame_blocks_whole_script(mixed, tmp_path, change, reason):
    store = mixed[2]
    raw = store.get("script_recording_frame", "visual")["payload"]
    store.put("script_recording_frame", "visual", {**raw, **change})
    budget = FakeBudget(proposal())
    output = tmp_path / "mixed"
    result = author(mixed, output, budget)
    assert result["status"] == "skipped" and result["reason"] == reason
    assert not output.exists() and not budget.requests


def test_learner_cannot_substitute_an_invented_tree_action(mixed, tmp_path):
    draft = proposal()
    draft["steps"][1]["mode"] = "tree"
    budget = MixedBudget(draft)
    output = tmp_path / "mixed"
    result = author(mixed, output, budget)
    assert result["status"] == "skipped"
    assert result["reason"] == "recording_action_mode_or_order_differs_from_trace"
    assert not output.exists() and len(budget.requests) == 1
