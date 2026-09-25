import io
import json

import pytest
from PIL import Image

from agent.executor import Executor
from agent.screen_detail import detail_attachments, validate_region
from agent.session import AgentSession
from shared.config import Settings
from shared.llm_gateway import GatewayResponse, ToolCall
from test_active_observation_space import _MixedSizeDriver, _package, _executor_payload


def test_native_crops_cover_screen_without_upscaling_and_keep_source_identity():
    package = _package("fresh", 1080, 2400)
    package.clean_png = package.image_for_llm
    tiles = detail_attachments(package)
    assert len(tiles) == 5
    assert all(a.observation_id == "fresh" and not a.actionable_coordinate_reference
               and not a.indexed_targets_available for a in tiles)
    assert tiles[1].crop_box == (0, 0, 605, 1345)
    assert tiles[-1].crop_box == (475, 1056, 1080, 2400)
    for attachment in tiles[1:]:
        assert max(Image.open(io.BytesIO(attachment.content)).size) <= 1600
    region = detail_attachments(package, [0, 0, .1, .1])[-1]
    assert region.image_size == (108, 240)


@pytest.mark.parametrize("region", [[0, 0, 0, 1], [0, 0, 2, 1], [False, 0, 1, 1], [0, 0, float('nan'), 1]])
def test_invalid_region_is_rejected(region):
    with pytest.raises(ValueError):
        validate_region(region)


def test_experimental_tools_do_not_change_default_catalog():
    for enabled in [False, True]:
        session = AgentSession("executor", "test", settings=Settings(_env_file=None, screen_detail=enabled, double_tap=enabled))
        catalog = {s.name: s for s in session._build_registry({}).specs_for_role("executor")}
        assert ("detail" in catalog["observe_screen"].parameters["properties"]["mode"]["enum"]) == enabled
        assert ("double_tap" in catalog["submit_executor_step"].parameters["properties"]["action"]["properties"]["type"]["enum"]) == enabled


@pytest.mark.asyncio
async def test_detail_promotes_fresh_global_basis_and_delivers_readonly_native_tiles(monkeypatch):
    class Driver(_MixedSizeDriver):
        native_calls = 0
        async def capture_native_frame(self, deadline):
            self.native_calls += 1
            tree, image = await self.get_frame()
            return tree, image, tree["_capture"]

    driver = Driver()
    executor = Executor(driver, model="test", settings=Settings(_env_file=None, screen_detail=True, double_tap=True))
    calls = []

    async def complete(model, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return GatewayResponse(content="", model=model, stop_reason="tool_calls", tool_calls=[
                ToolCall(id="detail", name="observe_screen", arguments='{"mode":"detail"}')])
        wire = json.dumps(messages)
        assert "Reading tile 4" in wire
        assert wire.count('"type": "image_url"') == 6
        assert "observation_id: stale-initial" not in wire
        return GatewayResponse(content="", model=model, stop_reason="tool_calls", tool_calls=[
            ToolCall(id="act", name="submit_executor_step", arguments=json.dumps(
                _executor_payload({"type": "double_tap", "x": 240, "y": 400}, "zoom document")))])

    monkeypatch.setattr("agent.session.complete", complete)
    baseline = _package("stale-initial", 485, 1080)
    step, result, *_ = await executor.act_once("read small text", baseline)
    assert result.success
    assert 1 <= driver.native_calls <= 2  # Existing capture fences may resample once.
    assert len(driver.actions) == 1
    assert driver.actions[0].type == "double_tap"
    assert driver.actions[0].x == pytest.approx(240 * 1200 / 485)
    assert step.action_pipeline.target_geometry == [1200, 2670]


def test_chatgpt_history_override_is_model_scoped_and_explicit_policy_wins():
    settings = Settings(_env_file=None, chatgpt_history_tokens=24000,
                        models_json='{"chatgpt/custom":{"context":{"executor":{"history_tokens":12345}}}}')
    assert settings.context_policy("chatgpt/gpt-5.6-sol", "executor")["history_tokens"] == 24000
    assert settings.context_policy("other/model", "executor")["history_tokens"] == 16000
    assert settings.context_policy("chatgpt/custom", "executor")["history_tokens"] == 12345
    assert settings.context_policy("chatgpt/gpt-5.6-sol", "planner") == {}


def test_action_discriminator_is_required_at_union_root():
    from agent.revisable.session import executor_submission_schema
    action = executor_submission_schema()["$defs"]["Action"]
    assert action["required"] == ["type"]
    assert "replace_text" in action["properties"]["type"]["enum"]
    assert all("type" in branch["required"] for branch in action["anyOf"])


@pytest.mark.asyncio
async def test_unsupported_native_driver_leaves_action_basis_intact():
    from agent.read_tools import make_observe_screen_handler
    from agent.tool_registry import ToolExecutionContext, AgentRole, ToolStatus
    from perception.observation import ObservationBuilder
    baseline = _package("existing", 485, 1080)
    context = ToolExecutionContext(role=AgentRole.EXECUTOR, invocation_id="unsupported",
                                   state={"active_package": baseline, "active_observation_id": "existing"})
    handler = make_observe_screen_handler(driver=_MixedSizeDriver(), builder=ObservationBuilder(),
                                         artifacts=None, baseline_package=baseline, detail_enabled=True)
    result = await handler({"mode": "detail"}, context)
    assert result.status == ToolStatus.UNAVAILABLE
    assert context.state["active_observation_id"] == "existing"
    assert context.state["active_package"] is baseline
    assert result.attachments == []


@pytest.mark.asyncio
async def test_double_tap_dispatch_uses_fixed_numeric_device_program(monkeypatch):
    from driver import adb
    from driver.android import AndroidDriver
    from shared.schemas import Action
    calls = []
    async def shell(serial, args, **kwargs):
        calls.append((serial, args, kwargs))
        return b""
    monkeypatch.setattr(adb, "shell_async", shell)
    result = await AndroidDriver(serial="test").act(Action(type="double_tap", x=125.4, y=230.8))
    assert result.success
    assert calls == [("test", ["input tap 125 231; sleep 0.08; input tap 125 231"], {"timeout": 5.0})]


def test_double_tap_receipt_classifies_the_gesture():
    from agent.action_observation import effect_class_for
    from shared.schemas import Action, EffectClass
    assert effect_class_for(Action(type="double_tap", x=1, y=2)) == EffectClass.GESTURE
