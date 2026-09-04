"""Focused contracts for the bounded current-observation resample.

These tests intentionally describe the small public lifecycle: one fast-path
capture, or one settle followed by one wholly fresh capture.  Provider-local
scrcpy -> ADB selection is tested separately from that transaction boundary.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
import time

import pytest
from PIL import Image

from agent.action_observation import (
    DEFAULT_RESAMPLE_SETTLE_MS,
    RESAMPLE_SETTLE_ENV,
    ActionObservationTransaction,
    configured_resample_settle_ms,
)
from agent.decision_context import _observation_capabilities
from driver.android import AndroidDriver
from driver.observation_deadline import ObservationDeadline, ObservationStageError
from driver.scrcpy_observation import FrameGeometry, FrameHandle
from perception.observation import ObservationBuilder
from shared.schemas import Action, ActionResult, EffectOutcome, ObservationMode


APP = "com.example"
FOREIGN_APP = "com.foreign"


def _png(color: str = "white") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 64), color).save(output, format="PNG")
    return output.getvalue()


def _tree(
    marker: str,
    *,
    package: str | None = APP,
    complete: bool = True,
    coordinate_compatible: bool = True,
) -> dict:
    root = {
        "class": "Root",
        "_capture": {
            "complete": complete,
            "coordinate_compatible": coordinate_compatible,
            "tree_provider": "fixture_tree",
        },
        "children": [{
            "class": "Button",
            "text": marker,
            "clickable": True,
            "bounds": [0, 0, 32, 20],
        }],
    }
    if package is not None:
        root["package"] = package
        root["children"][0]["package"] = package
    if not complete:
        root["_capture"]["tree_providers_exhausted"] = True
    return root


class _CaptureSequenceDriver:
    serial = "fixture"

    def __init__(
        self,
        captures: list[tuple[dict, bytes | None]],
        *,
        foreground: str = APP,
    ) -> None:
        self.captures = list(captures)
        self.foreground = foreground
        self.capture_count = 0
        self.actions: list[Action] = []
        self.capture_ordinals: list[int] = []

    async def current_foreground_identity(self, *, timeout_s=None):
        del timeout_s
        return {
            "package": self.foreground,
            "activity": ".MainActivity",
            "component": f"{self.foreground}/.MainActivity",
            "sources": ["fixture"],
            "conflict": False,
        }

    async def capture_deadline_frame(self, deadline: ObservationDeadline):
        self.capture_count += 1
        ordinal = int(getattr(deadline, "capture_ordinal", 0))
        self.capture_ordinals.append(ordinal)
        tree, pixels = self.captures.pop(0)
        capture = tree.setdefault("_capture", {})
        capture.update({
            "active_capture_ordinal": ordinal,
            "pixel_provider": "adb_screencap" if pixels else "unavailable",
            "frame_geometry": [32, 64],
            "image_geometry": [32, 64] if pixels else [0, 0],
        })
        row = {
            "provider": "fixture_capture",
            "status": "ok",
            "capture_ordinal": ordinal,
        }
        deadline.record_attempt(row)
        return tree, pixels, {
            **capture,
            "provider_attempts": [row],
        }

    async def act(self, action: Action):
        self.actions.append(action)
        detail = (
            {"resolved_package": action.app}
            if action.type == "launch" else {}
        )
        return ActionResult(success=True, message="dispatched", detail=detail)

    def set_last_ui(self, _ui) -> None:
        return None


def _transaction(
    driver: _CaptureSequenceDriver,
    *,
    settle_ms: int = 0,
) -> ActionObservationTransaction:
    return ActionObservationTransaction(
        driver,
        ObservationBuilder(),
        resample_settle_ms=settle_ms,
    )


@pytest.mark.asyncio
async def test_valid_first_capture_is_fast_path_without_settle(monkeypatch):
    driver = _CaptureSequenceDriver([(_tree("first-valid"), _png())])
    sleep_calls: list[float] = []

    async def tracked_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("agent.action_observation.asyncio.sleep", tracked_sleep)

    package = await _transaction(driver, settle_ms=1_000).observe_current()

    assert package.accepted is True
    assert driver.capture_count == 1
    assert driver.capture_ordinals == [1]
    assert sleep_calls == []
    assert package.capture_meta["observation_capture_attempt_count"] == 1
    assert "first-valid" in package.text_for_llm


@pytest.mark.asyncio
async def test_mechanical_instability_settles_once_then_uses_fresh_second_capture(
    monkeypatch,
):
    driver = _CaptureSequenceDriver([
        (_tree("discarded-first"), None),
        (_tree("fresh-second"), _png("blue")),
    ])
    sleep_calls: list[float] = []

    async def tracked_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("agent.action_observation.asyncio.sleep", tracked_sleep)

    package = await _transaction(driver, settle_ms=250).observe_current()

    assert driver.capture_count == 2
    assert driver.capture_ordinals == [1, 2]
    assert sleep_calls == [0.25]
    assert package.capture_meta["resample_trigger"] == "pixels_unavailable"
    assert package.capture_meta["observation_capture_attempt_count"] == 2
    assert "fresh-second" in package.text_for_llm
    assert "discarded-first" not in package.text_for_llm


@pytest.mark.asyncio
async def test_repeated_mechanical_instability_never_starts_a_third_capture():
    driver = _CaptureSequenceDriver([
        (_tree("first-partial"), None),
        (_tree("second-partial"), None),
        (_tree("must-not-run"), _png()),
    ])

    package = await _transaction(driver).observe_current()

    assert package.accepted is True
    assert package.capture_meta["evidence_tier"] == "tree_only"
    assert driver.capture_count == 2
    assert driver.capture_ordinals == [1, 2]
    assert len(driver.captures) == 1
    assert "second-partial" in package.text_for_llm


@pytest.mark.asyncio
async def test_retryable_capture_error_has_only_one_replacement_attempt():
    class Driver(_CaptureSequenceDriver):
        async def capture_deadline_frame(self, deadline):
            self.capture_count += 1
            ordinal = int(getattr(deadline, "capture_ordinal", 0))
            self.capture_ordinals.append(ordinal)
            raise ObservationStageError(
                "alignment",
                "tree_image_unaligned",
                provider_attempts=[{
                    "provider": "fixture_capture",
                    "status": "error",
                    "capture_ordinal": ordinal,
                }],
            )

    driver = Driver([])

    with pytest.raises(ObservationStageError) as raised:
        await _transaction(driver).observe_current()

    assert driver.capture_count == 2
    assert driver.capture_ordinals == [1, 2]
    assert raised.value.capture_attempt_count == 2


@pytest.mark.asyncio
async def test_generation_break_is_preserved_as_the_resample_trigger():
    class Driver(_CaptureSequenceDriver):
        async def capture_deadline_frame(self, deadline):
            self.capture_count += 1
            ordinal = int(getattr(deadline, "capture_ordinal", 0))
            self.capture_ordinals.append(ordinal)
            if ordinal == 1:
                raise ObservationStageError(
                    "accessibility_tree",
                    "generation_changed",
                    provider_attempts=[{
                        "provider": "accessibility_collector_primary",
                        "status": "error",
                        "capture_ordinal": ordinal,
                    }],
                )
            tree, pixels = self.captures.pop(0)
            capture = tree.setdefault("_capture", {})
            capture.update({
                "active_capture_ordinal": ordinal,
                "pixel_provider": "adb_screencap",
                "frame_geometry": [32, 64],
            })
            return tree, pixels, capture

    driver = Driver([(_tree("fresh-second"), _png())])

    package = await _transaction(driver, settle_ms=0).observe_current()

    assert driver.capture_ordinals == [1, 2]
    assert package.accepted is True
    assert package.capture_meta["resample_trigger"] == (
        "accessibility_tree:generation_changed"
    )
    assert package.capture_meta["observation_capture_attempt_count"] == 2


@pytest.mark.asyncio
async def test_request_scoped_provider_ledger_is_not_duplicated_on_resample():
    class Driver(_CaptureSequenceDriver):
        async def capture_deadline_frame(self, deadline):
            self.capture_count += 1
            ordinal = int(getattr(deadline, "capture_ordinal", 0))
            self.capture_ordinals.append(ordinal)
            tree, pixels = self.captures.pop(0)
            row = {
                "provider": "fixture_capture",
                "status": "ok",
                "capture_ordinal": ordinal,
            }
            deadline.record_attempt(row)
            capture = tree.setdefault("_capture", {})
            capture.update({
                "active_capture_ordinal": ordinal,
                "pixel_provider": "adb_screencap" if pixels else "unavailable",
                "frame_geometry": [32, 64],
            })
            return tree, pixels, {
                **capture,
                "provider_attempts": list(deadline.provider_attempts),
            }

    driver = Driver([
        (_tree("first"), None),
        (_tree("second"), _png()),
    ])

    package = await _transaction(driver, settle_ms=0).observe_current()

    fixture_attempts = [
        row for row in package.capture_meta["provider_attempts"]
        if row.get("provider") == "fixture_capture"
    ]
    assert [row["capture_ordinal"] for row in fixture_attempts] == [1, 2]


@pytest.mark.asyncio
async def test_launch_dispatches_once_while_expected_foreground_mismatch_resamples():
    driver = _CaptureSequenceDriver(
        [
            (_tree("wrong-app-first", package=FOREIGN_APP), _png()),
            (_tree("wrong-app-second", package=FOREIGN_APP), _png("blue")),
        ],
        foreground=FOREIGN_APP,
    )
    transaction = _transaction(driver)
    before = ObservationBuilder().build(
        (_tree("before"), _png()), app_id=APP, activity=".MainActivity",
        will_send_image=True,
    )

    result, after = await transaction.act_and_observe(
        Action(type="launch", app=APP), before,
    )

    assert len(driver.actions) == 1
    assert driver.actions[0].type == "launch"
    assert driver.capture_count == 2
    assert driver.capture_ordinals == [1, 2]
    assert after.capture_meta["resample_trigger"] == (
        "expected_foreground_not_observed"
    )
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_tree", "second_pixels", "tier", "mode", "indexed"),
    [
        (_tree("tree-only"), None, "tree_only", ObservationMode.TREE_ONLY, True),
        (
            _tree(
                "discarded-incomplete-tree",
                complete=False,
                coordinate_compatible=False,
            ),
            _png("blue"),
            "image_only",
            ObservationMode.IMAGE_ONLY,
            False,
        ),
        (
            _tree("ownerless", package=None),
            _png("green"),
            "nonindexed_tree_image",
            ObservationMode.TREE_PLUS_IMAGE,
            False,
        ),
    ],
)
async def test_second_capture_returns_richest_safe_evidence_tier(
    second_tree,
    second_pixels,
    tier,
    mode,
    indexed,
):
    driver = _CaptureSequenceDriver([
        (_tree("first-image-missing"), None),
        (second_tree, second_pixels),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert package.accepted is True
    assert package.capture_meta["evidence_tier"] == tier
    assert package.mode == mode
    assert package.index_actionable is indexed
    assert driver.capture_count == 2


@pytest.mark.asyncio
async def test_first_image_never_mixes_into_second_tree_only_capture():
    driver = _CaptureSequenceDriver([
        (
            _tree("discarded-first", coordinate_compatible=False),
            _png("red"),
        ),
        (_tree("kept-second"), None),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.mode == ObservationMode.TREE_ONLY
    assert package.capture_meta["evidence_tier"] == "tree_only"
    assert package.clean_png is None
    assert package.image_for_llm is None
    assert package.annotated_png is None
    assert "kept-second" in package.text_for_llm
    assert "discarded-first" not in package.text_for_llm


@pytest.mark.asyncio
async def test_persistent_geometry_mismatch_ends_as_clean_image_only():
    driver = _CaptureSequenceDriver([
        (_tree("first", coordinate_compatible=False), _png("red")),
        (_tree("second", coordinate_compatible=False), _png("blue")),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert package.index_actionable is False
    assert package.ui.elements == []
    assert package.som_ref is None
    assert package.clean_png == _png("blue")
    assert package.annotated_png == package.clean_png
    assert "first" not in package.text_for_llm
    assert "second" not in package.text_for_llm


@pytest.mark.asyncio
async def test_foreign_second_tree_is_removed_while_current_pixels_survive():
    driver = _CaptureSequenceDriver([
        (_tree("first-image-missing"), None),
        (_tree("foreign-secret", package=FOREIGN_APP), _png("blue")),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert package.accepted is True
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert package.index_actionable is False
    assert "foreign-secret" not in package.text_for_llm
    assert package.clean_png == _png("blue")


@pytest.mark.asyncio
async def test_foreign_tree_without_coordinate_compatibility_is_never_exposed():
    first = _tree("foreign-first", package=FOREIGN_APP)
    second = _tree("foreign-second", package=FOREIGN_APP)
    first["_capture"].pop("coordinate_compatible")
    second["_capture"].pop("coordinate_compatible")
    driver = _CaptureSequenceDriver(
        [(first, _png()), (second, _png("blue"))],
        foreground=APP,
    )

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.accepted is True
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert "foreign-first" not in package.text_for_llm
    assert "foreign-second" not in package.text_for_llm


@pytest.mark.asyncio
async def test_second_capture_with_no_usable_component_is_unavailable():
    driver = _CaptureSequenceDriver([
        (_tree("first-image-missing"), None),
        (_tree("second-incomplete", complete=False), None),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert package.accepted is False
    assert package.capture_meta["evidence_tier"] == "unavailable"
    assert package.capture_meta["observation_capture_attempt_count"] == 2
    assert driver.capture_count == 2


@pytest.mark.asyncio
async def test_non_boolean_component_flags_fail_closed_consistently():
    first = _tree("first")
    second = _tree("second")
    first["_capture"]["complete"] = "true"
    second["_capture"]["coordinate_compatible"] = 1
    driver = _CaptureSequenceDriver([
        (first, _png()),
        (second, _png("blue")),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.accepted is True
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert "second" not in package.text_for_llm
    assert package.ui.semantic_tree == []
    assert package.ui.elements == []
    assert package.interaction_state.focused_element is None
    capabilities = _observation_capabilities(package)
    assert capabilities["evidence_tier"] == "image_only"
    assert "tree_semantics_available" not in capabilities


@pytest.mark.asyncio
async def test_missing_component_flags_cannot_enable_tree_grounding():
    first = _tree("first")
    second = _tree("second")
    first["_capture"].pop("complete")
    second["_capture"].pop("coordinate_compatible")
    driver = _CaptureSequenceDriver([
        (first, _png()),
        (second, _png("blue")),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.accepted is True
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert "second" not in package.text_for_llm
    assert package.ui.semantic_tree == []
    assert package.ui.elements == []
    assert package.interaction_state.focused_element is None
    capabilities = _observation_capabilities(package)
    assert capabilities["evidence_tier"] == "image_only"
    assert "tree_semantics_available" not in capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [True, "false", 0])
async def test_exhausted_or_malformed_provider_fact_cannot_restore_tree(exhausted):
    first = _tree("first")
    second = _tree("second")
    first["_capture"]["tree_providers_exhausted"] = exhausted
    second["_capture"]["tree_providers_exhausted"] = exhausted
    driver = _CaptureSequenceDriver([
        (first, _png()),
        (second, _png("blue")),
    ])

    package = await _transaction(driver).observe_current(attach_image=True)

    assert driver.capture_count == 2
    assert package.mode == ObservationMode.IMAGE_ONLY
    assert package.capture_meta["evidence_tier"] == "image_only"
    assert package.ui.semantic_tree == []
    assert package.index_actionable is False
    assert "second" not in package.text_for_llm


class _StreamProvider:
    def __init__(self, result, events: list[str]) -> None:
        self.result = result
        self.events = events

    async def start(self):
        self.events.append("scrcpy_start")
        return True

    def current(self):
        self.events.append("scrcpy_current")
        return self.result

    def diagnostics(self, *, selected_mode=None):
        del selected_mode
        return {"generation": 1, "healthy": True, "status": "healthy"}


def _healthy_stream_result() -> SimpleNamespace:
    geometry = FrameGeometry(
        stream_width=32,
        stream_height=64,
        device_width=32,
        device_height=64,
        generation=1,
    )
    return SimpleNamespace(
        status="healthy",
        frame=FrameHandle(
            data=_png("green"),
            timestamp=time.monotonic(),
            generation=1,
            geometry=geometry,
            frame_id=1,
        ),
        detail="",
    )


@pytest.mark.asyncio
async def test_healthy_scrcpy_is_selected_without_starting_adb(monkeypatch):
    events: list[str] = []
    provider = _StreamProvider(_healthy_stream_result(), events)
    driver = AndroidDriver(stream_provider=provider)

    async def tree(_deadline):
        return _tree("scrcpy-tree")

    async def adb(_deadline):
        events.append("adb")
        raise AssertionError("healthy scrcpy must not invoke ADB")

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", adb)

    _, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3_500),
    )

    assert pixels == _png("green")
    assert metadata["pixel_provider"] == "scrcpy"
    assert events == ["scrcpy_start", "scrcpy_current"]


@pytest.mark.asyncio
async def test_scrcpy_failure_invokes_one_sequential_adb_fallback(monkeypatch):
    events: list[str] = []
    provider = _StreamProvider(
        SimpleNamespace(status="unavailable", frame=None, detail="empty ring"),
        events,
    )
    driver = AndroidDriver(stream_provider=provider)

    async def tree(_deadline):
        return _tree("adb-tree")

    async def adb(_deadline):
        events.append("adb")
        return _png("blue")

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", adb)

    _, pixels, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 3_500),
    )

    assert pixels == _png("blue")
    assert metadata["pixel_provider"] == "adb_screencap"
    assert events == ["scrcpy_start", "scrcpy_current", "adb"]
    assert [
        row["provider"] for row in metadata["provider_attempts"]
        if row.get("provider") in {"scrcpy", "adb_screencap"}
    ] == ["scrcpy", "adb_screencap"]


@pytest.mark.asyncio
async def test_insufficient_deadline_does_not_sleep_away_remaining_budget(
    monkeypatch,
):
    driver = _CaptureSequenceDriver([(_tree("safe-tree-only"), None)])
    sleep_calls: list[float] = []

    async def tracked_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("agent.action_observation.asyncio.sleep", tracked_sleep)
    started = time.monotonic()

    package = await _transaction(driver, settle_ms=1_000).observe_current(
        deadline_ms=100,
    )

    assert time.monotonic() - started < 0.1
    assert sleep_calls == []
    assert driver.capture_count == 1
    assert package.accepted is True
    assert package.capture_meta["resample_not_attempted"] == (
        "insufficient_outer_deadline"
    )


@pytest.mark.asyncio
async def test_every_provider_attempt_row_carries_its_capture_ordinal():
    driver = _CaptureSequenceDriver([
        (_tree("first-tree-only"), None),
        (_tree("second-complete"), _png()),
    ])

    package = await _transaction(driver).observe_current()

    rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row.get("provider") in {"foreground_identity", "fixture_capture"}
    ]
    assert rows
    assert all(row.get("capture_ordinal") in {1, 2} for row in rows)
    assert {row["capture_ordinal"] for row in rows} == {1, 2}


@pytest.mark.parametrize(("configured", "expected"), [("0", 0), ("1000", 1_000)])
def test_resample_settle_environment_freezes_supported_candidates(
    monkeypatch,
    configured,
    expected,
):
    monkeypatch.setenv(RESAMPLE_SETTLE_ENV, configured)

    transaction = ActionObservationTransaction(
        _CaptureSequenceDriver([(_tree("unused"), _png())]),
        ObservationBuilder(),
    )

    assert configured_resample_settle_ms() == expected
    assert transaction.resample_settle_ms == expected


@pytest.mark.parametrize("configured", ["not-a-number", "-1", "5001", "1.5"])
def test_invalid_resample_settle_environment_uses_measured_default(
    monkeypatch,
    configured,
):
    monkeypatch.setenv(RESAMPLE_SETTLE_ENV, configured)

    transaction = ActionObservationTransaction(
        _CaptureSequenceDriver([(_tree("unused"), _png())]),
        ObservationBuilder(),
    )

    assert configured_resample_settle_ms() == DEFAULT_RESAMPLE_SETTLE_MS
    assert transaction.resample_settle_ms == DEFAULT_RESAMPLE_SETTLE_MS


@pytest.mark.asyncio
async def test_resample_metadata_separates_configured_and_elapsed_settle(
    monkeypatch,
):
    driver = _CaptureSequenceDriver([
        (_tree("first-tree-only"), None),
        (_tree("second-complete"), _png()),
    ])

    async def controlled_sleep(seconds: float) -> None:
        assert seconds == 0.25

    # Keep the test fast while preserving a real monotonic elapsed measurement;
    # only the scheduler-sized elapsed value should differ from the configured
    # policy value.
    monkeypatch.setattr("agent.action_observation.asyncio.sleep", controlled_sleep)

    package = await _transaction(driver, settle_ms=250).observe_current()

    assert package.capture_meta["settle_configured_ms"] == 250
    assert 0 <= package.capture_meta["settle_elapsed_ms"] < 50
    assert package.capture_meta["resample_trigger"] == "pixels_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "reason"),
    [
        ("cleanup", "provider_close_failed"),
        ("protocol", "malformed_provider_result"),
        ("observation_capture", "outer_deadline_exhausted"),
    ],
)
async def test_non_mechanical_failures_do_not_open_a_second_capture(stage, reason):
    class Driver(_CaptureSequenceDriver):
        async def capture_deadline_frame(self, deadline):
            self.capture_count += 1
            ordinal = int(getattr(deadline, "capture_ordinal", 0))
            self.capture_ordinals.append(ordinal)
            raise ObservationStageError(stage, reason, timed_out="deadline" in reason)

    driver = Driver([])

    with pytest.raises(ObservationStageError) as raised:
        await _transaction(driver).observe_current(deadline_ms=100)

    assert raised.value.stage == stage
    assert raised.value.reason == reason
    assert raised.value.capture_attempt_count == 1
    assert driver.capture_count == 1
    assert driver.capture_ordinals == [1]


@pytest.mark.asyncio
async def test_orchestrator_terminates_after_one_final_observation_failure(tmp_path):
    """The transaction owns recovery; Orchestrator does not restart providers."""
    from agent.orchestrator import Orchestrator
    from driver.fixture import FixtureDriver
    from shared.schemas import TaskStatus
    from tests.fake_agents import (
        FakeExecutor,
        FakePlanner,
        FakeReviewer,
        fake_task_scope,
    )
    from agent.traces import TraceWriter
    from shared.artifacts import ArtifactStore
    from shared.db import Database

    class Driver(FixtureDriver):
        def __init__(self):
            super().__init__()
            self.warm_calls = 0
            self.close_calls = 0

        async def warm_observation_provider(self):
            self.warm_calls += 1
            return True

        async def close_observation_provider(self):
            self.close_calls += 1

    driver = Driver()
    db = Database(tmp_path / "runtime.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    orchestrator = Orchestrator(
        db,
        TraceWriter(db, artifacts),
        lambda: FakePlanner([]),
        lambda: FakeReviewer([], task_scope=fake_task_scope()),
        lambda: FakeExecutor([]),
        driver=driver,
        role_call_retry_n=0,
        artifacts=artifacts,
    )
    observe_calls = 0

    async def final_failure(*, will_send_image=False):
        nonlocal observe_calls
        del will_send_image
        observe_calls += 1
        raise ObservationStageError(
            "observation_capture",
            "outer_deadline_exhausted",
            timed_out=True,
            capture_attempt_count=2,
        )

    orchestrator._observe = final_failure

    task_id = await orchestrator.start_task("bounded observation failure")

    assert db.get_task(task_id).status == TaskStatus.FAILED
    assert observe_calls == 1
    assert driver.warm_calls == 1
    assert driver.close_calls == 1
    assert not hasattr(Orchestrator, "_recover_observation_provider")
