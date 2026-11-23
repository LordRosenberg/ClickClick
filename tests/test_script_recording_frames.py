import json
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from agent.revisable.store import TaskStore
from perception.observation import ObservationPackage
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.file_lock import FileLockBusy
from shared.recovery_store import RecoveryStore
from shared.schemas import AgentState, CanonicalUI, ObservationMode


def pixels(color, *, format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (12, 24), color).save(buffer, format=format)
    return buffer.getvalue()


def observation(key, raw):
    return ObservationPackage(
        ui=CanonicalUI(app_id="example.app"),
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm="Example observation",
        image_for_llm=pixels("blue"),
        annotated_png=None,
        gap_reasons=[],
        clean_png=raw,
        observation_id=key,
        frame_width=24,
        frame_height=48,
        crop_box=(0.0, 0.0, 1.0, 1.0),
        capture_meta={"pixel_provider": "scrcpy"},
    )


@pytest.fixture
def recording_store(tmp_path):
    db = Database(tmp_path / "task.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    db.artifacts = artifacts
    RecoveryStore(db, artifacts)
    yield db, artifacts
    db.close()


def test_raw_frames_are_opt_in_and_bound_to_existing_observations(recording_store):
    db, artifacts = recording_store
    ordinary = AgentState(instruction="Ordinary task")
    run = db.create_task(ordinary.instruction, ordinary)
    before_source = pixels("red", format="JPEG")
    after_source = pixels("green")
    before = observation("before", before_source)
    after = observation("after", after_source)
    default_store = TaskStore(db, artifacts, run.id)
    default_store.observe(before, ordinary)
    assert not default_store.records("script_recording_frame")
    assert not (artifacts.root / "script_recording_frames").exists()

    task = db.create_task("Record a task", AgentState(instruction="Record a task", generate_test_script=True))
    # Read the option from the persisted state, as a resumed worker does.
    state = db.get_task(task.id).state
    store = TaskStore(db, artifacts, task.id)
    store.observe(before, state)
    state.step_number += 1
    store.observe(after, state)
    store.observe(after, state)
    assert [row["key"] for row in store.records("script_recording_frame")] == ["before", "after"]
    for package, original, suffix in ((before, before_source, ".jpg"), (after, after_source, ".png")):
        record = store.get("script_recording_frame", package.observation_id)
        payload = record["payload"]
        ref = payload["image_ref"]
        assert record["version"] == 1
        assert ref.endswith(suffix)
        assert artifacts.read_bytes(ref) == original
        assert payload["status"] == "saved"
        assert payload["image_size"] == [12, 24] and payload["frame_size"] == [24, 48]
        assert payload["crop_box"] == [0.0, 0.0, 1.0, 1.0] and payload["pixel_provider"] == "scrcpy"
        history = store.resolve_observation(package.observation_id)["payload"]
        assert artifacts.read_bytes(history["image_ref"]) == package.image_for_llm
    assert not default_store.records("script_recording_frame")
    assert artifacts.prune("script_recording_frames", max_files=0) == 0


def test_raw_capture_never_substitutes_a_model_image_or_fails_observation(recording_store, monkeypatch):
    db, artifacts = recording_store
    state = AgentState(instruction="Record a task", generate_test_script=True)
    task = db.create_task(state.instruction, state)
    store = TaskStore(db, artifacts, task.id)
    missing = observation("no-original", None)
    store.observe(missing, state)
    info = store.get("script_recording_frame", missing.observation_id)["payload"]
    assert info["image_ref"] is None and info["reason"] == "original_pixels_unavailable"

    def busy(*args, **kwargs):
        raise FileLockBusy("test publication lock")

    monkeypatch.setattr(artifacts, "save_content_addressed_bytes", busy)
    failed = observation("busy", pixels("green"))
    store.observe(failed, state)
    info = store.get("script_recording_frame", failed.observation_id)["payload"]
    assert info["image_ref"] is None and info["reason"] == "original_image_write_failed"
    assert store.resolve_observation(failed.observation_id)["payload"]["image_ref"]


@pytest.mark.parametrize("enabled", [False, True])
async def test_task_receipt_links_to_retained_original_frames(recording_store, tmp_path, monkeypatch, enabled):
    from agent.runtime import create_orchestrator
    from agent.traces import TraceWriter
    from driver.fixture import FixtureDriver
    from shared.config import Settings
    from shared.llm_gateway import GatewayResponse, ToolCall
    from shared.schemas import TaskStatus

    db, artifacts = recording_store
    driver = FixtureDriver()
    before = await driver.screenshot()
    result_image = Image.open(BytesIO(before)).convert("RGB")
    ImageDraw.Draw(result_image).rectangle((0, 0, 500, 500), fill="red")
    buffer = BytesIO()
    result_image.save(buffer, format="PNG")
    after = buffer.getvalue()
    send_action = driver.act

    async def act(action):
        result = await send_action(action)
        driver._png = after
        return result

    monkeypatch.setattr(driver, "act", act)
    state = AgentState(instruction="Return home", generate_test_script=enabled)
    task = db.create_task(state.instruction, state, device_serial=driver.serial)
    settings = Settings(_env_file=None, data_dir=tmp_path, agent_architecture="plan_executor", default_model="test")
    runtime = create_orchestrator(db, TraceWriter(db, artifacts), settings=settings, artifacts=artifacts, driver=driver)
    calls = []

    async def model_reply(model, messages, *, tools, **kwargs):
        names = {tool["function"]["name"] for tool in tools}
        if "submit_planner_decision" in names:
            name = "submit_planner_decision"
            args = {"decision": "complete", "reason": "Home is visible"} if driver.actions else {
                "decision": "execute", "reason": "Go home", "plan": {"current_stage": {"goal": "Go home"}},
            }
        else:
            name = "submit_executor_step"
            context = next(json.loads(message["content"])["runtime_update"] for message in reversed(messages)
                           if isinstance(message.get("content"), str)
                           and message["content"].startswith('{"runtime_update"'))
            args = {"decision": "finish" if driver.actions else "act", "summary": "Go home",
                    "observation_id": context["current_observation_id"]}
            if not driver.actions:
                args["action"] = {"type": "home"}
        calls.append(name)
        return GatewayResponse(content="", model="test", stop_reason="tool_calls",
                               tool_calls=[ToolCall(id="decision", name=name, arguments=json.dumps(args))])

    monkeypatch.setattr("agent.session.complete", model_reply)
    assert await runtime.run_task(task.id) == TaskStatus.SUCCEEDED
    assert len(driver.actions) == 1
    assert calls == ["submit_planner_decision", "submit_executor_step",
                     "submit_executor_step", "submit_planner_decision"]
    store = TaskStore(db, artifacts, task.id)
    if not enabled:
        assert not store.records("script_recording_frame")
        return
    event = next(row["payload"] for row in store.records("event") if row["payload"]["decision"] == "act")
    before_id = event["observation_id"]
    receipt = event["action_result"]["receipt"]
    after_id = receipt["effect_observation_id"] or receipt["observation_id"]
    assert before_id != after_id
    for key, original in ((before_id, before), (after_id, after)):
        raw_ref = store.get("script_recording_frame", key)["payload"]["image_ref"]
        assert artifacts.read_bytes(raw_ref) == original
