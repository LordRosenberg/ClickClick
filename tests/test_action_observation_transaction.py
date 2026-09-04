from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from types import SimpleNamespace
import time

import pytest
from PIL import Image

from agent.action_observation import (
    FOREGROUND_IDENTITY_ATTEMPT_TIMEOUT_MS,
    ActionObservationTransaction,
    degraded_post_observation,
    effect_class_for,
)
from driver.android import AndroidDriver
from driver.observation_deadline import ObservationDeadline, ObservationStageError
from driver.scrcpy_observation import FrameGeometry, FrameHandle
from perception.observation import ObservationBuilder
from shared.schemas import (
    Action,
    ActionReceipt,
    ActionResult,
    EffectClass,
    EffectOutcome,
    ObservationMode,
)


def _png(color: str = "white") -> bytes:
    out = BytesIO()
    Image.new("RGB", (32, 64), color).save(out, format="PNG")
    return out.getvalue()


def _tree(*, text: str = "", page: str = "search", package: str = "com.example") -> dict:
    return {
        "class": "Root",
        "package": package,
        "_capture": {"complete": True, "coordinate_compatible": True},
        "children": [
            {"class": "TextView", "text": page, "bounds": [0, 0, 32, 10]},
            {
                "class": "EditText", "text": text, "desc": "搜索",
                "editable": True, "focusable": True, "focused": True,
                "clickable": True, "bounds": [0, 10, 32, 24],
            },
        ],
    }


def _non_indexed_tree(*, page: str, package: str) -> dict:
    return {
        "class": "Root",
        "package": package,
        "_capture": {"complete": True, "coordinate_compatible": True},
        "children": [{
            "class": "TextView",
            "text": page,
            "clickable": False,
            "bounds": [0, 0, 32, 10],
        }],
    }


def _exhausted_image_only_tree(*, page: str, package: str) -> dict:
    tree = _non_indexed_tree(page=page, package=package)
    tree["_capture"].update({
        "complete": False,
        "coordinate_compatible": False,
        "tree_providers_exhausted": True,
    })
    return tree


class _SequenceDriver:
    serial = "fixture"

    def __init__(self, trees: list[dict]) -> None:
        self.trees = list(trees)
        self.last = trees[-1]
        self.capture_count = 0
        self.actions: list[Action] = []

    async def get_frame(self):
        self.capture_count += 1
        if self.trees:
            self.last = self.trees.pop(0)
        return self.last, _png()

    async def current_foreground_identity(self, *, timeout_s=None):
        package = str(self.last.get("package") or "com.example")
        return {
            "package": package,
            "activity": ".SearchActivity",
            "component": f"{package}/.SearchActivity",
            "sources": [],
            "conflict": False,
        }

    async def act(self, action: Action):
        self.actions.append(action)
        detail = {"resolved_package": action.app} if action.type == "launch" else {}
        return ActionResult(success=True, message="dispatched", detail=detail)

    def set_last_ui(self, _ui):
        return None


class _FailedDispatchDriver(_SequenceDriver):
    async def act(self, action: Action):
        self.actions.append(action)
        return ActionResult(success=False, message="dispatch failed")


class _FlakyCaptureDriver(_SequenceDriver):
    def __init__(self, trees: list[dict], *, failures: int = 1) -> None:
        super().__init__(trees)
        self.failures = failures

    async def capture_deadline_frame(self, _deadline):
        self.capture_count += 1
        if self.failures:
            self.failures -= 1
            raise ObservationStageError(
                "screencap", "adb command timed out", timed_out=True
            )
        if self.trees:
            self.last = self.trees.pop(0)
        return self.last, _png(), {"provider": "adb_fallback"}


class _AlwaysFailCaptureDriver(_SequenceDriver):
    async def capture_deadline_frame(self, _deadline):
        self.capture_count += 1
        raise ObservationStageError(
            "alignment", "capture_timeout", timed_out=True,
        )


class _CancelledThenSuccessfulCaptureDriver(_SequenceDriver):
    async def capture_deadline_frame(self, _deadline):
        self.capture_count += 1
        if self.capture_count == 1:
            raise ObservationStageError(
                "alignment",
                "tree_image_unaligned",
                provider_attempts=[{
                    "provider": "primary", "status": "error",
                }],
                cancelled_tasks=["pixel-task"],
            )
        self.last = self.trees.pop(0)
        return self.last, _png(), {
            "provider": "primary",
            "provider_attempts": [{
                "provider": "primary", "status": "ok",
            }],
        }


class _SelectedAdbCaptureDriver(_SequenceDriver):
    async def capture_deadline_frame(self, deadline):
        self.capture_count += 1
        self.last = _tree(page="adb selected")
        deadline.record_attempt({
            "provider": "adb_screencap",
            "attempt_id": "adb-1",
            "status": "ok",
            "lifecycle": "selected",
            "capture_ordinal": int(getattr(deadline, "capture_ordinal", 1)),
        })
        return self.last, _png(), {
            "provider": "adb_fallback",
            "pixel_provider": "adb_screencap",
            "coordinate_compatible": True,
            "complete": True,
        }


class _ChangingPixelDriver(_SequenceDriver):
    def __init__(self, trees: list[dict], colors: list[str]) -> None:
        super().__init__(trees)
        self.colors = list(colors)

    async def get_frame(self):
        self.capture_count += 1
        if self.trees:
            self.last = self.trees.pop(0)
        color = self.colors.pop(0) if self.colors else "white"
        return self.last, _png(color)


class _IncompleteThenHangDriver(_SequenceDriver):
    async def get_frame(self):
        self.capture_count += 1
        if self.capture_count == 1:
            tree = _tree(page="incomplete")
            tree["_capture"].update({
                "complete": False,
                "tree_providers_exhausted": True,
            })
            return tree, _png()
        await asyncio.sleep(1.0)
        return self.last, _png()


def test_action_receipt_is_backward_compatible_and_serializable():
    legacy = ActionResult.model_validate({"success": True, "message": "tap"})
    assert legacy.receipt is None
    receipt = ActionReceipt(
        transaction_id="txn_1",
        effect_class=EffectClass.UI_TRANSITION,
        dispatch_succeeded=True,
        effect_outcome=EffectOutcome.CONFIRMED,
    )
    restored = ActionResult.model_validate(
        ActionResult(success=True, receipt=receipt).model_dump(mode="json")
    )
    assert restored.receipt == receipt


def test_effect_classes_are_small_and_app_independent():
    assert effect_class_for(Action(type="type", text="x")) == EffectClass.TEXT_INPUT
    assert effect_class_for(Action(type="replace_text", text="x")) == EffectClass.TEXT_INPUT


@pytest.mark.asyncio
async def test_selected_adb_attempt_is_bound_to_capture_ordinal():
    package = await ActionObservationTransaction(
        _SelectedAdbCaptureDriver([_tree()]),
        ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(attach_image=True)

    selected = next(
        row for row in package.capture_meta["provider_attempts"]
        if row.get("provider") == "adb_screencap"
    )
    assert selected["capture_ordinal"] == 1
    assert effect_class_for(Action(type="tap", index=1)) == EffectClass.UI_TRANSITION
    assert effect_class_for(Action(type="scroll", direction="up")) == EffectClass.GESTURE
    assert effect_class_for(Action(type="sleep")) == EffectClass.NONE


def test_launch_uses_a_bounded_evidence_budget_without_a_fixed_sleep():
    from agent.action_observation import effect_deadline_ms_for
    from driver.observation_deadline import CURRENT_DEADLINE_MS

    assert effect_deadline_ms_for(
        Action(type="launch", app="com.example")
    ) == CURRENT_DEADLINE_MS + 3_500
    assert effect_deadline_ms_for(
        Action(type="replace_text", text="x")
    ) == CURRENT_DEADLINE_MS
    assert effect_deadline_ms_for(
        Action(type="tap", index=1)
    ) == CURRENT_DEADLINE_MS


@pytest.mark.asyncio
async def test_current_observation_is_fresh_not_baseline_reuse():
    driver = _SequenceDriver([_tree(page="fresh")])
    txn = ActionObservationTransaction(driver, ObservationBuilder())
    package = await txn.observe_current()
    assert package.accepted is True
    assert package.observation_source == "fresh_current"
    assert driver.capture_count == 1


@pytest.mark.asyncio
async def test_empty_exact_foreground_identity_never_reaches_a_role() -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            return {
                "package": "", "activity": "", "component": "",
                "sources": [], "conflict": False,
            }

    driver = Driver([_tree()])
    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            driver, ObservationBuilder(),
        resample_settle_ms=0,
        ).observe_current()

    assert raised.value.stage == "foreground_identity"
    assert raised.value.reason == "unresolved_or_conflicting"
    assert raised.value.timed_out is False
    assert driver.identity_calls == 2


@pytest.mark.asyncio
async def test_identity_receives_independent_calibrated_budget() -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([_tree()])
            self.timeouts: list[float] = []

        async def current_foreground_identity(self, *, timeout_s=None):
            self.timeouts.append(timeout_s)
            return {
                "package": "com.example",
                "activity": ".SearchActivity",
                "component": "com.example/.SearchActivity",
                "sources": [],
                "conflict": False,
            }

    driver = Driver()
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=2_000)

    assert len(driver.timeouts) == 2
    assert driver.timeouts[0] == 1.2
    assert 0 < driver.timeouts[1] <= 1.2
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert [row["budget_ms"] for row in identity_rows] == [1200, 1200]


@pytest.mark.asyncio
async def test_identity_slow_tail_within_calibrated_attempt_succeeds() -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([_tree()])
            self.identity_calls = 0
            self.timeouts: list[float] = []

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            self.timeouts.append(timeout_s)
            if self.identity_calls == 1:
                await asyncio.sleep(0.8)
            return await super().current_foreground_identity(timeout_s=timeout_s)

    driver = Driver()
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=2_000)

    assert FOREGROUND_IDENTITY_ATTEMPT_TIMEOUT_MS == 1_200
    assert driver.identity_calls == 2
    assert driver.timeouts[0] == 1.2
    assert 0 < driver.timeouts[1] <= 1.2
    assert package.ui.app_id == "com.example"
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert [row["status"] for row in identity_rows] == ["ok", "ok"]
    assert identity_rows[0]["elapsed_ms"] >= 780


@pytest.mark.asyncio
async def test_cancelled_identity_is_named_in_request_ledger() -> None:
    entered = asyncio.Event()

    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            entered.set()
            await asyncio.Future()

    transaction = ActionObservationTransaction(
        Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
    )

    with pytest.raises(ObservationStageError) as raised:
        await transaction.observe_current(deadline_ms=80)

    assert entered.is_set()
    assert raised.value.stage == "observation_capture"
    assert raised.value.provider_attempts[-1]["provider"] == "foreground_identity"
    assert raised.value.provider_attempts[-1]["status"] == "cancelled"
    assert 0 < raised.value.provider_attempts[-1]["budget_ms"] <= 80
    assert raised.value.cancelled_tasks == ["foreground_identity:before_capture:1"]


@pytest.mark.asyncio
async def test_identity_attempt_timeout_is_distinct_from_empty_identity() -> None:
    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            await asyncio.sleep(2.0)
            return {}

    package = await ActionObservationTransaction(
        Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=3_000)

    assert package.accepted is True
    assert package.ui.app_id == "com.example"
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert len(identity_rows) == 2
    assert all(row["status"] == "error" for row in identity_rows)
    assert all(row["timed_out"] is True for row in identity_rows)
    assert all(row["error"] == "identity_request_timeout" for row in identity_rows)
    assert all(row["budget_ms"] == 1200 for row in identity_rows)
    assert any(
        edge.get("to") == "exact_tree_owner"
        for edge in package.capture_meta["fallback_edges"]
    )


@pytest.mark.asyncio
async def test_provider_typed_timeout_is_not_collapsed_into_empty_identity() -> None:
    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "",
                "activity": "",
                "component": "",
                "sources": [],
                "conflict": False,
                "timed_out": True,
                "error": "adb command timed out",
            }

    package = await ActionObservationTransaction(
        Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert package.accepted is True
    assert package.ui.app_id == "com.example"
    assert all(row["timed_out"] is True for row in identity_rows)
    assert all(row["error"] == "identity_request_timeout" for row in identity_rows)


@pytest.mark.asyncio
async def test_android_adb_timeout_reaches_transaction_as_typed_fact(
    monkeypatch,
) -> None:
    from driver import adb
    from driver.android import AndroidDriver

    async def run(_args, *, timeout):
        assert 0 < timeout <= 1.0
        raise adb.AdbTimeoutError("adb command timed out: dumpsys activity")

    monkeypatch.setattr(adb, "_run_async", run)
    android = AndroidDriver(serial="S1")

    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            return await android.current_foreground_identity(
                timeout_s=timeout_s
            )

    package = await ActionObservationTransaction(
        Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert len(identity_rows) == 2
    assert package.accepted is True
    assert package.ui.app_id == "com.example"
    assert all(row["timed_out"] is True for row in identity_rows)
    assert all(row["error"] == "identity_request_timeout" for row in identity_rows)


@pytest.mark.asyncio
async def test_pre_capture_identity_timeout_keeps_exact_posterior() -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls == 1:
                return {
                    "package": "",
                    "activity": "",
                    "component": "",
                    "sources": [],
                    "conflict": False,
                    "timed_out": True,
                    "error": "adb command timed out",
                }
            return {
                "package": "com.example",
                "activity": ".SearchActivity",
                "component": "com.example/.SearchActivity",
                "sources": [{"source": "fixture", "package": "com.example"}],
                "conflict": False,
                "timed_out": False,
            }

    driver = Driver([_tree(package="com.example")])
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000)

    assert driver.capture_count == 1
    assert package.accepted is True
    assert package.ui.app_id == "com.example"
    assert any(
        edge.get("to") == "posterior_identity"
        for edge in package.capture_meta["fallback_edges"]
    )


@pytest.mark.asyncio
async def test_pre_capture_timeout_without_tree_owner_still_fails() -> None:
    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "",
                "activity": "",
                "component": "",
                "sources": [],
                "conflict": False,
                "timed_out": True,
                "error": "adb command timed out",
            }

    tree = _tree(package="com.example")
    tree["_capture"]["complete"] = False
    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            Driver([tree, tree]), ObservationBuilder(), resample_settle_ms=0,
        ).observe_current(deadline_ms=1_000)

    assert raised.value.stage == "foreground_identity"
    assert raised.value.reason == "identity_request_timeout"


@pytest.mark.asyncio
async def test_conflicting_identity_fails_closed_after_two_attempts() -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            return {
                "package": "com.one",
                "activity": ".Main",
                "component": "com.one/.Main",
                "sources": [
                    {"source": "mResumedActivity", "package": "com.one"},
                    {"source": "mResumedActivity", "package": "com.two"},
                ],
                "conflict": True,
                "timed_out": False,
            }

    driver = Driver([_tree()])
    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            driver, ObservationBuilder(),
        resample_settle_ms=0,
        ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in raised.value.provider_attempts
        if row["provider"] == "foreground_identity"
    ]
    assert raised.value.stage == "foreground_identity"
    assert raised.value.reason == "unresolved_or_conflicting"
    assert raised.value.timed_out is False
    assert driver.identity_calls == 2
    assert len(identity_rows) == 2
    assert all(row["conflict"] is True for row in identity_rows)
    assert all(row["timed_out"] is False for row in identity_rows)
    assert all(row["status"] == "error" for row in identity_rows)


@pytest.mark.asyncio
async def test_mixed_timeout_and_empty_identity_remains_non_timeout_failure() -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls == 1:
                return {
                    "package": "", "conflict": False,
                    "timed_out": True, "error": "adb command timed out",
                }
            return {"package": "", "conflict": False, "timed_out": False}

    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
        ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in raised.value.provider_attempts
        if row["provider"] == "foreground_identity"
    ]
    assert identity_rows[0]["timed_out"] is True
    assert any(row["timed_out"] is False for row in identity_rows)
    assert raised.value.reason == "unresolved_or_conflicting"
    assert raised.value.timed_out is False


@pytest.mark.asyncio
async def test_nonempty_identity_with_provider_error_is_not_admitted() -> None:
    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s=None):
            return {
                "package": "com.one",
                "activity": ".Main",
                "component": "com.one/.Main",
                "sources": [],
                "conflict": False,
                "timed_out": False,
                "error": "adb command failed",
            }

    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
        ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in raised.value.provider_attempts
        if row["provider"] == "foreground_identity"
    ]
    assert raised.value.reason == "unresolved_or_conflicting"
    assert raised.value.timed_out is False
    assert len(identity_rows) == 2
    assert all(row["status"] == "error" for row in identity_rows)
    assert all(row["error"] == "adb command failed" for row in identity_rows)


@pytest.mark.asyncio
async def test_foreground_identity_brackets_tree_and_pixels() -> None:
    order: list[str] = []

    class Driver(_SequenceDriver):
        async def current_foreground_identity(self, *, timeout_s: float):
            del timeout_s
            order.append("identity")
            return {
                "package": "com.example",
                "activity": ".Main",
                "component": "com.example/.Main",
                "conflict": False,
            }

        async def capture_deadline_frame(self, _deadline):
            assert order == ["identity"]
            order.append("capture")
            return self.last, _png(), {
                "provider": "scrcpy",
                "provider_attempts": [{
                    "provider": "scrcpy_frame",
                    "status": "ok",
                    "budget_ms": 20.0,
                    "elapsed_ms": 1.0,
                    "error": "",
                }],
            }

    package = await ActionObservationTransaction(
        Driver([_tree()]), ObservationBuilder(),
        resample_settle_ms=0,
    )._capture_once("txn", "concurrent-identity", capture_budget_ms=500)

    assert order == ["identity", "capture", "identity"]
    assert package.ui.app_id == "com.example"
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert identity_rows
    assert all(0 < row["budget_ms"] <= 500 for row in identity_rows)


@pytest.mark.asyncio
async def test_foreground_change_during_capture_rejects_mixed_package() -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([_tree(package="com.after")])
            self.identities = iter(("com.before", "com.after"))

        async def current_foreground_identity(self, *, timeout_s=None):
            package = next(self.identities)
            return {
                "package": package,
                "activity": ".Main",
                "component": f"{package}/.Main",
                "sources": [],
                "conflict": False,
            }

    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            Driver(), ObservationBuilder(),
        resample_settle_ms=0,
        )._capture_once("txn", "test")

    assert raised.value.stage == "grounding_barrier"
    assert raised.value.reason == "foreground_changed_during_capture"
    identity_rows = [
        attempt for attempt in raised.value.provider_attempts
        if attempt["provider"] == "foreground_identity"
    ]
    assert [attempt["phase"] for attempt in identity_rows] == [
        "before_capture", "after_capture",
    ]
    assert [attempt["package"] for attempt in identity_rows] == [
        "com.before", "com.after",
    ]
    barrier = raised.value.provider_attempts[-1]
    assert barrier["provider"] == "grounding_barrier"
    assert barrier["before_package"] == "com.before"
    assert barrier["after_package"] == "com.after"


@pytest.mark.asyncio
async def test_posterior_identity_timeout_keeps_exact_tree_image_package() -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls == 2:
                return {
                    "package": "",
                    "activity": "",
                    "component": "",
                    "sources": [],
                    "conflict": False,
                    "timed_out": True,
                    "error": "adb command timed out",
                }
            return {
                "package": "com.example",
                "activity": ".SearchActivity",
                "component": "com.example/.SearchActivity",
                "sources": [{"source": "mResumedActivity", "package": "com.example"}],
                "conflict": False,
                "timed_out": False,
            }

    driver = Driver([_tree(package="com.example")])
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000, source="posterior-timeout")

    assert driver.capture_count == 1
    assert package.accepted is True
    assert package.capture_meta["observation_capture_attempt_count"] == 1
    assert package.ui.app_id == "com.example"
    assert package.index_actionable is True
    assert package.clean_png
    assert package.capture_meta["foreground_identity_after_status"] == (
        "timeout_corroborated_by_exact_tree_owner"
    )
    assert package.capture_meta["grounding_barrier"] == "pre_identity_tree_exact"
    assert package.capture_meta["fallback_edges"][-1] == {
        "from": "foreground_identity:after_capture",
        "to": "pre_identity+exact_tree_owner",
        "reason": "identity_request_timeout",
        "capture_ordinal": "1",
        "pre_package": "com.example",
        "tree_package": "com.example",
        "tree_ownership_status": "exact",
    }
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert [row["status"] for row in identity_rows] == ["ok", "error"]
    assert identity_rows[-1]["error"] == "identity_request_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_package", ["", "com.other"])
async def test_posterior_identity_timeout_requires_exact_same_tree_owner(
    tree_package: str,
) -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls == 2:
                return {
                    "package": "",
                    "activity": "",
                    "component": "",
                    "sources": [],
                    "conflict": False,
                    "timed_out": True,
                    "error": "adb command timed out",
                }
            return {
                "package": "com.example",
                "activity": ".SearchActivity",
                "component": "com.example/.SearchActivity",
                "sources": [{"source": "fixture", "package": "com.example"}],
                "conflict": False,
                "timed_out": False,
            }

    tree = _tree(package=tree_package)
    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            Driver([tree]), ObservationBuilder(), resample_settle_ms=0,
        )._capture_once("txn", "posterior-timeout", capture_budget_ms=1_000)

    assert raised.value.stage == "foreground_identity"
    assert raised.value.reason == "identity_request_timeout"
    assert raised.value.timed_out is True
    assert raised.value.provider_attempts[-1]["phase"] == "after_capture"


@pytest.mark.asyncio
async def test_uncorroborated_posterior_timeout_uses_one_fresh_resample() -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([
                _tree(package="com.other"),
                _tree(package="com.final"),
            ])
            self.identities = iter((
                {"package": "com.example", "activity": ".First"},
                {"timed_out": True, "error": "adb command timed out"},
                {"package": "com.final", "activity": ".Final"},
                {"package": "com.final", "activity": ".Final"},
            ))

        async def current_foreground_identity(self, *, timeout_s=None):
            identity = next(self.identities)
            package = str(identity.get("package") or "")
            return {
                "package": package,
                "activity": str(identity.get("activity") or ""),
                "component": f"{package}/.Main" if package else "",
                "sources": ([{"source": "fixture", "package": package}] if package else []),
                "conflict": False,
                "timed_out": bool(identity.get("timed_out")),
                "error": str(identity.get("error") or ""),
            }

    driver = Driver()
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000, source="posterior-timeout")

    assert driver.capture_count == 2
    assert package.ui.app_id == "com.final"
    assert package.capture_meta["observation_capture_attempt_count"] == 2
    assert package.capture_meta["grounding_barrier"] == "aligned"
    assert package.capture_meta["foreground_identity_after_status"] == "exact"
    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert [row["status"] for row in identity_rows] == [
        "ok", "error", "ok", "ok",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mixed_timeout",
    [
        {"conflict": True},
        {
            "package": "com.other",
            "activity": ".Other",
            "sources": [{"source": "partial", "package": "com.other"}],
        },
    ],
)
async def test_mixed_posterior_timeout_facts_cannot_use_fallback(
    mixed_timeout: dict,
) -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([
                _tree(package="com.example"),
                _tree(package="com.final"),
            ])
            self.identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls == 2:
                return {
                    "package": "",
                    "activity": "",
                    "component": "",
                    "sources": [],
                    "conflict": False,
                    "timed_out": True,
                    "error": "adb command timed out",
                    **mixed_timeout,
                }
            package = "com.example" if self.identity_calls == 1 else "com.final"
            return {
                "package": package,
                "activity": ".Main",
                "component": f"{package}/.Main",
                "sources": [{"source": "fixture", "package": package}],
                "conflict": False,
                "timed_out": False,
            }

    driver = Driver()
    package = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000, source="mixed-posterior-timeout")

    assert driver.capture_count == 2
    assert package.ui.app_id == "com.final"
    assert package.capture_meta["observation_capture_attempt_count"] == 2
    assert not any(
        edge.get("to") == "pre_identity+exact_tree_owner"
        for edge in package.capture_meta["fallback_edges"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tree_shape", ["incomplete", "ambiguous"])
async def test_nonexact_tree_shape_cannot_corroborate_posterior_timeout(
    tree_shape: str,
) -> None:
    class Driver(_SequenceDriver):
        identity_calls = 0

        async def current_foreground_identity(self, *, timeout_s=None):
            self.identity_calls += 1
            if self.identity_calls % 2 == 0:
                return {
                    "package": "",
                    "activity": "",
                    "component": "",
                    "sources": [],
                    "conflict": False,
                    "timed_out": True,
                    "error": "adb command timed out",
                }
            return {
                "package": "com.example",
                "activity": ".Main",
                "component": "com.example/.Main",
                "sources": [{"source": "fixture", "package": "com.example"}],
                "conflict": False,
                "timed_out": False,
            }

    tree = _tree(package="com.example")
    if tree_shape == "incomplete":
        tree["_capture"]["complete"] = False
    else:
        tree["children"].append({
            "class": "TextView",
            "package": "com.other",
            "text": "foreign",
            "bounds": [0, 24, 32, 32],
        })
    driver = Driver([tree, tree])

    with pytest.raises(ObservationStageError) as raised:
        await ActionObservationTransaction(
            driver, ObservationBuilder(), resample_settle_ms=0,
        ).observe_current(deadline_ms=1_000, source="nonexact-tree-timeout")

    assert driver.capture_count == 2
    assert raised.value.stage == "foreground_identity"
    assert raised.value.reason == "identity_request_timeout"
    assert raised.value.capture_attempt_count == 2


@pytest.mark.asyncio
async def test_foreground_change_retry_preserves_first_attempt_identity_provenance() -> None:
    class Driver(_SequenceDriver):
        def __init__(self):
            super().__init__([
                _tree(package="com.after"),
                _tree(package="com.final"),
            ])
            self.identities = iter((
                "com.before", "com.after", "com.final", "com.final",
            ))

        async def current_foreground_identity(self, *, timeout_s=None):
            package = next(self.identities)
            return {
                "package": package,
                "activity": ".Main",
                "component": f"{package}/.Main",
                "sources": [{"source": "fixture", "package": package}],
                "conflict": False,
            }

    package = await ActionObservationTransaction(
        Driver(), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current(deadline_ms=1_000)

    identity_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "foreground_identity"
    ]
    assert package.accepted is True
    assert package.ui.app_id == "com.final"
    assert package.capture_meta["observation_capture_attempt_count"] == 2
    assert [row["package"] for row in identity_rows] == [
        "com.before", "com.after", "com.final", "com.final",
    ]
    assert all(row["component"].endswith("/.Main") for row in identity_rows)
    assert all(row["sources"] for row in identity_rows)
    barrier_rows = [
        row for row in package.capture_meta["provider_attempts"]
        if row["provider"] == "grounding_barrier"
    ]
    assert barrier_rows == [{
        "provider": "grounding_barrier",
        "status": "error",
        "budget_ms": 0.0,
        "elapsed_ms": 0.0,
        "error": "foreground_changed_during_capture",
        "before_package": "com.before",
        "after_package": "com.after",
        "capture_ordinal": 1,
    }]


@pytest.mark.asyncio
async def test_generic_transition_captures_once_and_remains_unknown():
    driver = _SequenceDriver([
        _tree(page="transition"),
        _tree(page="results"),
        _tree(page="results"),
    ])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="search")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)
    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert after.accepted is True
    assert driver.capture_count == 1


@pytest.mark.asyncio
async def test_effectful_dispatch_settles_before_first_post_action_capture(
    monkeypatch,
):
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("agent.action_observation.asyncio.sleep", record_sleep)
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="before")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([_tree(page="after")])

    _result, after = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=250,
    ).act_and_observe(Action(type="tap", index=1), before)

    assert sleeps == [0.25]
    assert driver.capture_count == 1
    assert after.capture_meta["post_action_settle_configured_ms"] == 250
    assert after.capture_meta["post_action_settle_elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_post_action_settle_consumes_the_shared_observation_deadline():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="before")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    transaction = ActionObservationTransaction(
        _SequenceDriver([_tree(page="after")]),
        ObservationBuilder(),
        effect_deadline_ms=200,
        resample_settle_ms=25,
    )
    observed_deadlines: list[int] = []
    original_observe = transaction.observe_current

    async def record_observe_deadline(**kwargs):
        observed_deadlines.append(int(kwargs["deadline_ms"]))
        return await original_observe(**kwargs)

    transaction.observe_current = record_observe_deadline
    await transaction.act_and_observe(Action(type="tap", index=1), before)

    assert len(observed_deadlines) == 1
    assert 0 < observed_deadlines[0] <= 175


@pytest.mark.asyncio
async def test_post_action_settle_exhaustion_starts_no_capture():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="before")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([_tree(page="must-not-be-captured")])

    result, after = await ActionObservationTransaction(
        driver,
        ObservationBuilder(),
        effect_deadline_ms=10,
        resample_settle_ms=50,
    ).act_and_observe(Action(type="tap", index=1), before)

    assert driver.capture_count == 0
    assert after.accepted is False
    assert after.acceptance_reason == (
        "observation_capture:outer_deadline_exhausted_during_post_action_settle"
    )
    assert after.capture_meta["observation_capture_attempt_count"] == 0
    assert result.receipt is not None
    assert result.receipt.observation_capture_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [Action(type="sleep"), Action(type="tap", index=1)])
async def test_sleep_or_failed_dispatch_skips_post_action_settle(
    monkeypatch,
    action,
):
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("agent.action_observation.asyncio.sleep", record_sleep)
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="before")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = (
        _SequenceDriver([_tree(page="after")])
        if action.type == "sleep"
        else _FailedDispatchDriver([_tree(page="after")])
    )

    result, after = await ActionObservationTransaction(
        driver, ObservationBuilder(), resample_settle_ms=250,
    ).act_and_observe(action, before)

    assert sleeps == []
    assert result.receipt is not None
    if action.type == "sleep":
        assert after is before
        assert driver.capture_count == 0
        assert result.receipt.observation_capture_count == 0
        assert result.receipt.observation_id == ""
    else:
        assert after.capture_meta["post_action_settle_elapsed_ms"] == 0


@pytest.mark.asyncio
async def test_generic_transition_does_not_interpret_dynamic_pixels():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="search")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _ChangingPixelDriver(
        [_tree(page="video"), _tree(page="video")],
        ["red", "blue"],
    )
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)

    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert after.accepted is True
    assert after.image_for_llm is not None
    assert driver.capture_count == 1


class _CoherenceDriver(_SequenceDriver):
    def __init__(self, samples: list[tuple[dict, dict]]) -> None:
        super().__init__([tree for tree, _ in samples])
        self.samples = list(samples)

    async def capture_deadline_frame(self, _deadline):
        self.capture_count += 1
        if self.samples:
            self.last, metadata = self.samples.pop(0)
        else:
            metadata = dict(self.last.get("_capture") or {})
        return self.last, _png(), metadata


def _coherence_meta(
    *, tree_ms: float, pixel_ms: float, status: str, tree_generation: int = 3,
) -> dict:
    aligned = status != "unaligned"
    return {
        "provider": "scrcpy",
        "pixel_provider": "scrcpy",
        "generation": 7,
        "tree_generation": tree_generation,
        "frame_geometry": [32, 64],
        "tree_ready_monotonic_ms": tree_ms,
        "pixel_monotonic_ms": pixel_ms,
        "pixel_validation_monotonic_ms": tree_ms + 50,
        "coherence_status": status,
        "complete": True,
        "coordinate_compatible": aligned,
    }


@pytest.mark.asyncio
async def test_launch_returns_exact_nonindexed_target_immediately():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="home", package="com.launcher")]),
        ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([
        _non_indexed_tree(page="transition", package="com.target"),
        _non_indexed_tree(page="static", package="com.target"),
        _tree(page="must-not-be-captured", package="com.target"),
    ])

    result, after = await ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=10_000,
    ).act_and_observe(Action(type="launch", app="com.target"), before)

    assert len(driver.actions) == 1
    assert driver.capture_count == 1
    assert after.index_actionable is False
    assert "transition" in after.text_for_llm
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.CONFIRMED
    assert result.receipt.effect_reason == "launch_target_foreground_observed"
    assert result.receipt.observation_capture_count == 1
    assert result.receipt.effect_observation_id == result.receipt.observation_id


@pytest.mark.asyncio
async def test_generic_action_does_not_promote_strict_capture_change_to_confirmed():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="home", package="com.launcher")]),
        ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _CoherenceDriver([
        (
            _tree(page="content", package="com.target"),
            _coherence_meta(tree_ms=100, pixel_ms=105, status="post_action_frame"),
        ),
        (
            _tree(page="content", package="com.target"),
            _coherence_meta(
                tree_ms=120, pixel_ms=130, status="post_action_frame",
                tree_generation=4,
            ),
        ),
    ])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)

    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert after.capture_meta["coherence_status"] == "post_action_frame"
    assert driver.capture_count == 1


@pytest.mark.asyncio
async def test_generic_transition_does_not_infer_unchanged_from_immediate_frame():
    driver = _SequenceDriver([
        _tree(page="search"),
        _tree(page="search"),
        _tree(page="results"),
        _tree(page="results"),
    ])
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="search")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )
    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)
    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert "search" in after.text_for_llm
    assert driver.capture_count == 1


@pytest.mark.asyncio
async def test_sleep_delays_without_acquiring_observation_evidence():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="results")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([_tree(page="results")])

    result, after = await ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    ).act_and_observe(Action(type="sleep"), before)

    assert result.receipt is not None
    assert result.receipt.dispatch_succeeded is True
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "delay_completed"
    assert result.receipt.observation_capture_count == 0
    assert result.receipt.observation_id == ""
    assert after is before
    assert driver.capture_count == 0


@pytest.mark.asyncio
async def test_post_action_provider_failure_returns_degraded_without_redispatch():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="search")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _FlakyCaptureDriver([
        _tree(page="results"),
        _tree(page="results"),
    ])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)

    assert result.success is True
    assert len(driver.actions) == 1
    assert driver.capture_count == 2
    assert after.accepted is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == (
        "action_dispatched_observation_available"
    )


@pytest.mark.asyncio
async def test_post_action_capture_failure_returns_degraded_receipt_not_executor_error():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(page="search")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _AlwaysFailCaptureDriver([_tree(page="results")])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=100,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(Action(type="tap", index=1), before)

    assert result.success is True
    assert len(driver.actions) == 1
    assert result.receipt is not None
    assert result.receipt.dispatch_succeeded is True
    assert result.receipt.effect_outcome == EffectOutcome.TIMEOUT
    assert after.accepted is False
    assert after.actionable is False
    assert after.observation_id != before.observation_id
    assert after.acceptance_reason == "alignment:capture_timeout"
    assert after.ui.app_id == ""
    assert after.ui.semantic_tree == []
    assert after.clean_png is None
    assert after.image_for_llm is None
    assert driver.capture_count == 2
    assert result.receipt.observation_capture_count == 2
    assert after.capture_meta["observation_capture_attempt_count"] == 2


@pytest.mark.asyncio
async def test_type_records_dispatch_without_interpreting_exact_value():
    before_driver = _SequenceDriver([_tree(text="")])
    before = await ActionObservationTransaction(
        before_driver, ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([_tree(text="张凌赫")])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )
    result, after = await txn.act_and_observe(Action(type="type", text="张凌赫"), before)
    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.dispatch_succeeded is True
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert len(driver.actions) == 1
    assert driver.capture_count == 1
    # Current evidence is source-owned. The transaction must not synthesize the
    # typed value when the returned accessibility node does not expose it.
    assert "张凌赫" not in after.text_for_llm


@pytest.mark.asyncio
async def test_type_does_not_poll_or_classify_partial_text():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(text="")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([_tree(text="张"), _tree(text="张凌赫")])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(
        Action(type="type", text="张凌赫"), before,
    )

    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert len(driver.actions) == 1
    assert driver.capture_count == 1
    assert "张凌赫" not in after.text_for_llm


@pytest.mark.asyncio
async def test_type_does_not_interpret_composed_accessibility_label():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(text="label, ")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([
        _tree(text="label, target"),
        _tree(text="label, target"),
    ])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, _after = await txn.act_and_observe(
        Action(type="type", text="target"), before,
    )

    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert len(driver.actions) == 1
    assert driver.capture_count == 1


@pytest.mark.asyncio
async def test_type_does_not_classify_unchanged_accessibility_label():
    before = await ActionObservationTransaction(
        _SequenceDriver([_tree(text="label, ")]), ObservationBuilder(),
        resample_settle_ms=0,
    ).observe_current()
    driver = _SequenceDriver([
        _tree(text="label, "),
        _tree(text="label, "),
    ])
    txn = ActionObservationTransaction(
        driver, ObservationBuilder(), effect_deadline_ms=1000,
        resample_settle_ms=0,
    )

    result, after = await txn.act_and_observe(
        Action(type="type", text="target"), before,
    )

    assert result.success is True
    assert result.receipt is not None
    assert result.receipt.effect_outcome == EffectOutcome.UNKNOWN
    assert result.receipt.effect_reason == "action_dispatched_observation_available"
    assert len(driver.actions) == 1
    assert after.image_for_llm is not None
    assert driver.capture_count == 1
