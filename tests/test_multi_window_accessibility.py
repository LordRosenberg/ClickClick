from __future__ import annotations

import asyncio
import io
import json

import pytest
from PIL import Image

from agent.decision_context import (
    render_executor_observation_v2,
    render_planner_observation_v2,
)
from driver.accessibility import (
    AccessibilityChannelBootstrap,
    AccessibilityCollectorClient,
    AccessibilitySnapshot,
    AccessibilitySnapshotChannel,
    AccessibilityWindow,
    decode_snapshot,
    mark_dump_fallback,
)
from driver.adb import AdbError
from driver.android import AndroidDriver
from driver.observation_deadline import ObservationDeadline, ObservationStageError
from perception.normalizer import (
    normalize_a11y_tree,
    render_semantic_tree,
    tree_ownership_candidates,
    tree_ownership_facts,
)
from perception.input_evidence import build_interaction_state
from perception.observation import (
    ObservationPackage,
    TREE_CAPTURE_INCOMPLETE,
    TREE_UNUSABLE,
    ObservationBuilder,
)
from shared.schemas import ObservationMode


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (200, 400), "white").save(out, format="PNG")
    return out.getvalue()


def _node(
    label: str,
    bounds: list[int],
    *,
    clickable: bool = True,
    package: str = "com.example",
) -> dict:
    return {
        "class": "android.widget.Button" if clickable else "android.widget.TextView",
        "text": label,
        "package": package,
        "bounds": bounds,
        "clickable": clickable,
        "children": [],
    }


def _snapshot(*windows: AccessibilityWindow, complete: bool = True) -> AccessibilitySnapshot:
    return AccessibilitySnapshot(
        generation=7,
        captured_monotonic_ms=1234.0,
        complete=complete,
        reasons=[] if complete else ["missing_root"],
        windows=list(windows),
    )


@pytest.mark.parametrize("value", [None, True, False])
def test_snapshot_cache_clear_evidence_is_optional_and_preserved(value):
    payload = {"schema_version": 1, "generation": 1, "captured_monotonic_ms": 1,
               "complete": True, "windows": [], "reasons": []}
    if value is not None:
        payload["cache_cleared"] = value
    snapshot = decode_snapshot(payload)
    assert snapshot.to_raw_tree()["_capture"]["collector_cache_cleared"] is value


def test_snapshot_cache_clear_evidence_does_not_coerce_text_to_true():
    with pytest.raises(AdbError, match="cache_cleared"):
        decode_snapshot({"schema_version": 1, "generation": 1, "captured_monotonic_ms": 1,
                         "complete": True, "windows": [], "cache_cleared": "false"})


def test_window_fence_metadata_is_optional_for_older_collectors():
    payload = {"schema_version": 1, "generation": 5, "captured_monotonic_ms": 1,
               "complete": True, "windows": [], "reasons": []}
    assert decode_snapshot(payload).window_generation is None
    snapshot = decode_snapshot({**payload, "window_generation": 2, "window_quiet_ms": 350,
                                "capture_attempts": 1, "snapshot_elapsed_ms": 2000,
                                "content_changed_during_capture": True})
    capture = snapshot.to_raw_tree()["_capture"]
    assert capture["generation"] == 5
    assert capture["window_generation"] == 2
    assert capture["window_quiet_ms"] == 350
    assert capture["collector_capture_attempts"] == 1
    assert capture["content_changed_during_capture"] is True


@pytest.mark.parametrize("field,value", [("window_generation", "2"),
                                        ("window_quiet_ms", "350"),
                                        ("content_changed_during_capture", "false")])
def test_window_fence_metadata_rejects_coerced_values(field, value):
    with pytest.raises(AdbError, match=field):
        decode_snapshot({"schema_version": 1, "generation": 1, "captured_monotonic_ms": 1,
                         "complete": True, "windows": [], field: value})


def test_window_snapshot_orders_layers_and_assigns_one_a11y_index_namespace():
    raw = _snapshot(
        AccessibilityWindow(1, 1, 2, [0, 0, 200, 400], False, False, 0,
                            _node("application", [0, 100, 200, 200])),
        AccessibilityWindow(2, 3, 9, [0, 250, 200, 400], True, True, 0,
                            _node("comment", [0, 300, 200, 390])),
    ).to_raw_tree()
    ui = normalize_a11y_tree(raw)

    wrappers = [item for item in ui.semantic_tree if item.window_wrapper and item.window_id is not None]
    assert [item.window_id for item in wrappers] == [2, 1]
    assert all(item.index == -1 and not item.interactable for item in wrappers)
    assert [item.index for item in ui.elements] == [0, 1]
    assert [item.text for item in ui.elements] == ["comment", "application"]
    assert [item.window_id for item in ui.elements] == [2, 1]
    assert ui.capture_provider == "accessibility_collector"
    assert ui.capture_generation == 7
    assert "Window id=2" in render_semantic_tree(ui)


def test_focused_application_package_wins_over_higher_system_overlay():
    raw = _snapshot(
        AccessibilityWindow(
            9, 3, 10, [0, 0, 200, 80], False, False, 0,
            _node("status", [0, 0, 200, 80], package="com.android.systemui"),
        ),
        AccessibilityWindow(
            4, 1, 1, [0, 0, 200, 400], True, True, 0,
            _node("application", [0, 80, 200, 400], package="com.xingin.xhs"),
        ),
    ).to_raw_tree()

    ui = normalize_a11y_tree(raw)

    assert ui.app_id == "com.xingin.xhs"


def test_executor_and_planner_omit_inactive_status_bar_from_model_tree():
    """Inactive system chrome stays in the canonical tree but not model input."""
    raw = _snapshot(
        AccessibilityWindow(
            9, 3, 10, [0, 0, 200, 80], False, False, 0,
            _node(
                "battery", [0, 0, 200, 80],
                clickable=False, package="com.android.systemui",
            ),
        ),
        AccessibilityWindow(
            4, 1, 1, [0, 0, 200, 400], True, True, 0,
            _node("Open", [0, 80, 200, 160], package="com.demo"),
        ),
    ).to_raw_tree()
    ownership = tree_ownership_facts(raw, foreground_package="com.demo")
    ui = normalize_a11y_tree(raw)
    ui.app_id = "com.demo"
    interaction = build_interaction_state(ui)
    package = ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm=render_semantic_tree(ui),
        image_for_llm=_png(),
        clean_png=_png(),
        annotated_png=None,
        gap_reasons=[],
        interaction_state=interaction,
        actionable=True,
        index_actionable=True,
        frame_width=200,
        frame_height=400,
        model_image_width=200,
        model_image_height=400,
        capture_meta={
            "complete": True,
            "coordinate_compatible": True,
            **ownership,
        },
    )

    assert "battery" not in package.text_for_llm
    assert "Open" in package.text_for_llm
    assert any(
        el.window_wrapper and el.window_type == 3 for el in ui.semantic_tree
    )

    executor_tree = render_executor_observation_v2(package).split("\nTREE:\n", 1)[1]
    planner_tree = render_planner_observation_v2(package).split("\nTREE:\n", 1)[1]
    assert "battery" not in executor_tree
    assert "battery" not in planner_tree
    assert "Open" in executor_tree
    assert "Open" in planner_tree
    assert "[0]" in executor_tree
    assert "window_type=3" not in executor_tree
    assert "window_type=3" not in planner_tree


def test_focused_window_wrapper_does_not_mask_focused_editable_in_role_contexts():
    root = {
        "class": "android.widget.EditText",
        "package": "com.example",
        "text": "hello",
        "contentDescription": "搜索输入框",
        "hint": "搜索",
        "editable": True,
        "focusable": True,
        "focused": True,
        "bounds": [10, 20, 190, 80],
        "children": [],
    }
    raw = _snapshot(
        AccessibilityWindow(
            4, 1, 1, [0, 0, 200, 400], True, True, 0, root,
        ),
    ).to_raw_tree()
    ui = normalize_a11y_tree(raw)
    interaction = build_interaction_state(ui)
    package = ObservationPackage(
        ui=ui,
        mode=ObservationMode.TREE_PLUS_IMAGE,
        text_for_llm=render_semantic_tree(ui),
        image_for_llm=_png(),
        clean_png=_png(),
        annotated_png=None,
        gap_reasons=[],
        interaction_state=interaction,
        actionable=True,
        index_actionable=True,
        frame_width=200,
        frame_height=400,
        model_image_width=200,
        model_image_height=400,
        capture_meta={"complete": True, "coordinate_compatible": True},
    )

    assert interaction.focused_element is not None
    assert interaction.focused_element.role == "android.widget.EditText"
    planner = json.loads(
        render_planner_observation_v2(package)
        .split("CURRENT OBSERVATION:\n", 1)[1]
        .splitlines()[0]
    )["focused_interaction"]["focused_editable"]
    executor = json.loads(
        render_executor_observation_v2(package)
        .split("CURRENT OBSERVATION:\n", 1)[1]
        .splitlines()[0]
    )["focused_interaction"]["focused_editable"]
    assert planner["raw_text"] == executor["raw_text"] == "hello"
    assert planner["raw_a11y_label"] == executor["raw_a11y_label"] == "搜索输入框"
    assert planner["raw_hint"] == executor["raw_hint"] == "搜索"
    assert "index" not in planner
    assert executor["index"] == 0


def test_capture_metadata_includes_traversal_normalization_and_node_counts():
    raw = _snapshot(
        AccessibilityWindow(
            1, 1, 2, [0, 0, 200, 400], True, True, 0,
            _node("application", [0, 100, 200, 200]),
            traversal_elapsed_ms=1.25,
        )
    ).to_raw_tree(elapsed_ms=2.5)
    package = ObservationBuilder().build((raw, None))

    assert package.capture_meta["collector_elapsed_ms"] == 2.5
    assert package.capture_meta["window_traversal_ms"] == [1.25]
    assert package.capture_meta["normalization_elapsed_ms"] >= 0
    assert package.capture_meta["semantic_node_count"] >= 2
    assert package.capture_meta["actionable_node_count"] == 1


def test_tree_ownership_schema_selects_exact_focused_application_source():
    raw = _snapshot(
        AccessibilityWindow(
            9, 3, 10, [0, 0, 200, 80], False, False, 0,
            _node("overlay", [0, 0, 200, 80], package="com.android.systemui"),
        ),
        AccessibilityWindow(
            4, 1, 5, [0, 0, 200, 400], True, True, 0,
            _node("target", [0, 80, 200, 200], package="com.target"),
        ),
        AccessibilityWindow(
            3, 1, 2, [0, 0, 200, 400], False, False, 0,
            _node("other", [0, 200, 200, 300], package="com.other"),
        ),
    ).to_raw_tree()

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "exact"
    assert facts["tree_application_package"] == "com.target"
    assert facts["tree_ownership_candidate_count"] == 2
    assert facts["tree_ownership_candidates_truncated"] is False
    assert facts["tree_ownership_candidates_complete"] is True
    target, other = facts["tree_ownership_candidates"]
    assert target["packages"] == ["com.target"]
    assert other["packages"] == ["com.other"]
    assert set(target) == {
        "source_kind", "packages", "window_id", "window_type",
        "window_layer", "display_id", "active", "focused",
        "semantic_node_count", "actionable_node_count",
        "model_visible_node_count", "retained_semantic_node_count",
        "retained_actionable_node_count",
        "retained_model_visible_node_count",
    }
    assert target["source_kind"] == "window"
    assert target["model_visible_node_count"] > 0
    assert target["retained_model_visible_node_count"] > 0


def test_tree_ownership_rejects_same_window_multiple_packages():
    root = {
        "class": "Root", "package": "com.target", "children": [
            _node("foreign", [0, 0, 100, 50], package="com.other"),
        ],
    }
    raw = _snapshot(AccessibilityWindow(
        4, 1, 5, [0, 0, 200, 400], True, True, 0, root,
    )).to_raw_tree()

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "ambiguous"
    assert facts["tree_application_package"] == ""
    assert facts["tree_ownership_candidates"][0]["packages"] == [
        "com.other", "com.target",
    ]


def test_tree_ownership_rejects_duplicate_window_ids_before_filter_projection():
    raw = _snapshot(
        AccessibilityWindow(
            4, 1, 5, [0, 0, 200, 400], True, True, 0,
            {"class": "Root", "package": "com.target", "children": []},
        ),
        AccessibilityWindow(
            4, 1, 2, [0, 0, 200, 400], False, False, 0,
            _node("foreign", [0, 0, 100, 50], package="com.foreign"),
        ),
    ).to_raw_tree()

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "missing"
    assert facts["tree_application_package"] == ""
    assert facts["tree_ownership_candidates_complete"] is False


def test_tree_ownership_rejects_unproven_window_list_completeness():
    raw = _snapshot(AccessibilityWindow(
        4, 1, 5, [0, 0, 200, 400], True, True, 0,
        _node("target", [0, 0, 200, 100], package="com.target"),
    )).to_raw_tree()
    raw["_capture"]["window_count"] = 2

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "missing"
    assert facts["tree_ownership_candidates_complete"] is False


def test_overlay_content_cannot_make_empty_application_window_owned():
    empty_app = {
        "class": "android.widget.FrameLayout",
        "package": "com.target",
        "bounds": [0, 80, 200, 400],
        "children": [],
    }
    raw = _snapshot(
        AccessibilityWindow(
            9, 3, 10, [0, 0, 200, 80], True, True, 0,
            _node("overlay", [0, 0, 200, 80], package="com.android.systemui"),
        ),
        AccessibilityWindow(
            4, 1, 5, [0, 0, 200, 400], True, True, 0, empty_app,
        ),
    ).to_raw_tree()

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "missing"
    assert facts["tree_ownership_candidates"][0][
        "retained_model_visible_node_count"
    ] == 0


def test_empty_focused_target_is_missing_not_replaced_by_inactive_foreign():
    empty_app = {
        "class": "android.widget.FrameLayout",
        "package": "com.target",
        "bounds": [0, 0, 200, 400],
        "children": [],
    }
    raw = _snapshot(
        AccessibilityWindow(
            4, 1, 5, [0, 0, 200, 400], True, True, 0, empty_app,
        ),
        AccessibilityWindow(
            3, 1, 2, [0, 0, 200, 400], False, False, 0,
            _node("foreign", [0, 0, 200, 100], package="com.other"),
        ),
    ).to_raw_tree()

    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert facts["tree_ownership_status"] == "missing"
    assert facts["tree_application_package"] == ""
    assert facts["tree_ownership_candidates"][1]["packages"] == ["com.other"]


def test_flat_source_uses_null_window_metadata_and_requires_completeness():
    raw = _node("ready", [0, 0, 100, 50], package="com.target")
    raw["_capture"] = {"complete": True}
    candidate = tree_ownership_candidates(raw)[0]
    facts = tree_ownership_facts(raw, foreground_package="com.target")

    assert candidate["source_kind"] == "flat"
    assert candidate["packages"] == ["com.target"]
    assert all(candidate[key] is None for key in (
        "window_id", "window_type", "window_layer", "display_id",
    ))
    assert facts["tree_ownership_status"] == "exact"

    raw["_capture"]["complete"] = False
    incomplete = tree_ownership_facts(
        raw, foreground_package="com.target",
    )
    assert incomplete["tree_ownership_status"] == "missing"
    assert incomplete["tree_ownership_candidates_complete"] is False


@pytest.mark.asyncio
async def test_combined_collector_returns_overlay_and_application_in_one_request(
    monkeypatch,
):
    driver = AndroidDriver(collector_enabled=True)
    calls: list[str] = []

    async def collector_tree(*, timeout_s, provider="primary"):
        del timeout_s
        calls.append(provider)
        tree = _snapshot(
            AccessibilityWindow(
                9, 3, 10, [0, 0, 200, 80], True, True, 0,
                _node(
                    "overlay", [0, 0, 200, 80],
                    package="com.android.systemui",
                ),
            ),
            AccessibilityWindow(
                4, 1, 5, [0, 0, 200, 400], True, True, 0,
                _node("target", [0, 80, 200, 200], package="com.target"),
            ),
        ).to_raw_tree()
        tree["_capture"]["provider"] = provider
        return tree

    async def forbidden_dump(_deadline):
        raise AssertionError("a complete combined tree must skip dump fallback")

    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", forbidden_dump)
    tree = await driver.capture_deadline_tree(
        ObservationDeadline("current", 3500),
    )

    assert calls == ["primary"]
    assert tree["_capture"]["provider"] == "primary"
    assert [
        attempt["status"]
        for attempt in tree["_capture"]["tree_provider_attempts"]
    ] == ["ok"]
    assert tree_ownership_facts(
        tree, foreground_package="com.target",
    )["tree_ownership_status"] == "exact"


def test_each_snapshot_wholly_replaces_removed_windows():
    first = normalize_a11y_tree(_snapshot(
        AccessibilityWindow(3, 4, 10, [0, 0, 200, 200], True, True, 0,
                            _node("overlay", [0, 0, 100, 100]))
    ).to_raw_tree())
    second = normalize_a11y_tree(_snapshot(
        AccessibilityWindow(1, 1, 1, [0, 0, 200, 400], True, True, 0,
                            _node("home", [0, 0, 100, 100]))
    ).to_raw_tree())
    assert [item.text for item in first.elements] == ["overlay"]
    assert [item.text for item in second.elements] == ["home"]


def test_decoder_rejects_malformed_window_bounds():
    with pytest.raises(AdbError, match="bounds"):
        decode_snapshot({
            "schema_version": 1,
            "generation": 1,
            "captured_monotonic_ms": 1,
            "complete": True,
            "reasons": [],
            "windows": [{
                "window_id": 1, "type": 1, "layer": 1, "bounds": [0, 1],
                "active": True, "focused": True, "display_id": 0, "root": None,
            }],
        })


def test_decoder_rejects_duplicate_window_ids():
    window = {
        "window_id": 1, "type": 1, "layer": 1,
        "bounds": [0, 0, 10, 10], "active": True,
        "focused": True, "display_id": 0, "root": None,
    }
    with pytest.raises(AdbError, match="duplicate window_id"):
        decode_snapshot({
            "schema_version": 1,
            "generation": 1,
            "captured_monotonic_ms": 1,
            "complete": True,
            "reasons": [],
            "windows": [window, dict(window)],
        })


@pytest.mark.asyncio
async def test_primary_collector_cancellation_closes_persistent_channel() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class Channel:
        async def snapshot(self, *, timeout):
            del timeout
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                closed.set()
                raise

    client = AccessibilityCollectorClient("S")
    client._channel = Channel()
    attempt = asyncio.create_task(client.fetch_primary(timeout=5.0))
    await started.wait()
    attempt.cancel()

    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert closed.is_set()


@pytest.mark.asyncio
async def test_channel_connect_cancellation_removes_uncommitted_forward(monkeypatch) -> None:
    channel = AccessibilitySnapshotChannel("S", "clickclick")
    entered = asyncio.Event()
    removed: list[int] = []

    async def bootstrap(*, timeout):
        assert 0 < timeout <= 2.5
        return AccessibilityChannelBootstrap("socket", "token", 1, 1)

    async def forward(_serial, _socket_name):
        return 43210

    async def connect(*_args, **_kwargs):
        entered.set()
        await asyncio.Future()

    async def remove(_serial, port):
        removed.append(port)

    monkeypatch.setattr(channel, "_load_bootstrap", bootstrap)
    monkeypatch.setattr("driver.accessibility.adb.forward_localabstract_async", forward)
    monkeypatch.setattr("driver.accessibility.asyncio.open_connection", connect)
    monkeypatch.setattr("driver.accessibility.adb.remove_forward_async", remove)
    attempt = asyncio.create_task(channel.snapshot(timeout=2.5))
    await entered.wait()
    attempt.cancel()

    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert removed == [43210]
    assert channel._port is None


@pytest.mark.asyncio
async def test_channel_forward_cancellation_never_removes_unowned_forward(monkeypatch) -> None:
    channel = AccessibilitySnapshotChannel("S", "clickclick")
    entered = asyncio.Event()
    reconciled = False

    async def bootstrap(*, timeout):
        assert timeout == 2.5
        return AccessibilityChannelBootstrap("exact_socket", "token", 1, 1)

    async def forward(_serial, _socket_name):
        entered.set()
        await asyncio.Future()

    async def reconcile(*_args, **_kwargs):
        nonlocal reconciled
        reconciled = True

    monkeypatch.setattr(channel, "_load_bootstrap", bootstrap)
    monkeypatch.setattr(
        "driver.accessibility.adb.forward_localabstract_async", forward
    )
    monkeypatch.setattr(
        "driver.accessibility.adb.remove_forwards_to_localabstract_async",
        reconcile,
    )

    attempt = asyncio.create_task(channel._ensure_connected(timeout=2.5))
    await entered.wait()
    attempt.cancel()

    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert reconciled is False
    assert channel._port is None


@pytest.mark.asyncio
async def test_collector_failure_does_not_start_nested_dump(monkeypatch):
    collector_calls = 0
    dump_calls = 0

    async def fail_collector(*, timeout_s):
        nonlocal collector_calls
        del timeout_s
        collector_calls += 1
        raise AdbError("collector unavailable")

    async def fresh_dump(_serial, *, max_attempts=1, timeout):
        nonlocal dump_calls
        dump_calls += 1
        assert max_attempts == 1
        assert timeout > 0
        return (
            '<hierarchy><node class="android.widget.Button" text="fresh" '
            'clickable="true" bounds="[0,0][10,10]"/></hierarchy>'
        )

    driver = AndroidDriver(serial="S", collector_enabled=True)
    monkeypatch.setattr(driver, "_collector_tree", fail_collector)
    monkeypatch.setattr("driver.android.adb.uiautomator_dump_async", fresh_dump)
    with pytest.raises(ObservationStageError) as raised:
        await driver.capture_deadline_tree(ObservationDeadline("current", 2000))

    assert collector_calls == 1
    assert dump_calls == 0
    assert raised.value.provider_attempts[0]["provider"] == (
        "accessibility_collector_primary"
    )


@pytest.mark.asyncio
async def test_incomplete_collector_snapshot_is_returned_to_outer_resample(monkeypatch):
    calls: list[str] = []

    async def collector_tree(*, timeout_s):
        del timeout_s
        calls.append("primary")
        return _snapshot(complete=False).to_raw_tree()

    async def dump(_deadline):
        calls.append("dump")
        tree = _node("search", [0, 0, 200, 80])
        tree["_capture"] = {
            "provider": "uiautomator_dump",
            "complete": True,
        }
        return tree

    driver = AndroidDriver(serial="S", collector_enabled=True)
    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)

    with pytest.raises(ObservationStageError) as raised:
        await driver.capture_deadline_tree(ObservationDeadline("current", 8500))

    assert calls == ["primary"]
    assert "incomplete snapshot" in raised.value.provider_attempts[0]["error"]


@pytest.mark.asyncio
async def test_model_empty_collector_snapshot_is_returned_to_outer_resample(monkeypatch):
    calls: list[str] = []

    async def collector_tree(*, timeout_s):
        del timeout_s
        calls.append("primary")
        return {
            "class": "android.view.accessibility.WindowSet",
            "role": "WindowSet",
            "window_wrapper": True,
            "children": [],
            "_capture": {"complete": True, "provider": "primary"},
        }

    async def dump(_deadline):
        calls.append("dump")
        tree = _node("search", [0, 0, 200, 80])
        tree["_capture"] = {
            "complete": True,
            "provider": "uiautomator_dump",
        }
        return tree

    driver = AndroidDriver(serial="S", collector_enabled=True)
    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)

    with pytest.raises(ObservationStageError) as raised:
        await driver.capture_deadline_tree(ObservationDeadline("current", 8500))

    assert calls == ["primary"]
    assert "model-empty snapshot" in raised.value.provider_attempts[0]["error"]


@pytest.mark.asyncio
async def test_model_empty_configured_tree_source_fails_explicitly(monkeypatch):
    empty = {
        "class": "android.view.accessibility.WindowSet",
        "role": "WindowSet",
        "window_wrapper": True,
        "children": [],
        "_capture": {"complete": True},
    }

    async def collector_tree(*, timeout_s):
        del timeout_s
        return dict(empty)

    async def dump(_deadline):
        return dict(empty)

    driver = AndroidDriver(serial="S", collector_enabled=True)
    monkeypatch.setattr(driver, "_collector_tree", collector_tree)
    monkeypatch.setattr(driver, "_deadline_dump", dump)

    with pytest.raises(ObservationStageError) as exc:
        await driver.capture_deadline_tree(ObservationDeadline("current", 8500))

    assert "model-empty snapshot" in exc.value.reason
    assert [attempt["status"] for attempt in exc.value.provider_attempts] == ["error"]
    assert all(
        "model-empty snapshot" in attempt["error"]
        for attempt in exc.value.provider_attempts
    )


@pytest.mark.asyncio
async def test_capture_cancellation_reaps_concurrent_tree_and_pixel_work(monkeypatch) -> None:
    driver = AndroidDriver(stream_provider=None)
    tree_started = asyncio.Event()
    pixel_started = asyncio.Event()
    tree_cancelled = asyncio.Event()
    pixel_cancelled = asyncio.Event()

    async def tree(_deadline):
        tree_started.set()
        try:
            await asyncio.Future()
        finally:
            tree_cancelled.set()

    async def pixels(_deadline):
        pixel_started.set()
        try:
            await asyncio.Future()
        finally:
            pixel_cancelled.set()

    monkeypatch.setattr(driver, "_deadline_tree", tree)
    monkeypatch.setattr(driver, "_deadline_screencap", pixels)
    capture = asyncio.create_task(
        driver.capture_deadline_frame(ObservationDeadline("current", 5_000))
    )
    await tree_started.wait()
    await pixel_started.wait()
    capture.cancel()

    with pytest.raises(asyncio.CancelledError):
        await capture
    assert tree_cancelled.is_set()
    assert pixel_cancelled.is_set()


@pytest.mark.asyncio
async def test_collector_and_dump_failure_never_reuses_previous_snapshot(monkeypatch):
    driver = AndroidDriver(serial="S", collector_enabled=True)
    collector_calls = 0
    dump_calls = 0

    async def fail_collector(*, timeout_s):
        nonlocal collector_calls
        del timeout_s
        collector_calls += 1
        raise AdbError("collector unavailable")

    async def fail_dump(_serial, *, max_attempts=1, timeout):
        nonlocal dump_calls
        dump_calls += 1
        assert max_attempts == 1
        assert timeout > 0
        raise AdbError("fresh dump missing")

    monkeypatch.setattr(driver, "_collector_tree", fail_collector)
    monkeypatch.setattr("driver.android.adb.uiautomator_dump_async", fail_dump)
    with pytest.raises(ObservationStageError, match="collector unavailable") as exc:
        await driver.capture_deadline_tree(ObservationDeadline("current", 2000))
    assert collector_calls == 1
    assert dump_calls == 0
    assert exc.value.provider_attempts[-1]["error"] == "collector unavailable"


@pytest.mark.asyncio
async def test_frame_capture_keeps_current_pixels_when_dump_times_out(monkeypatch):
    driver = AndroidDriver(serial="S", collector_enabled=False)

    async def fail_tree(_deadline):
        raise ObservationStageError(
            "ui_dump", "dump timed out", elapsed_ms=1400, timed_out=True
        )

    async def current_pixels(_deadline):
        return b"current-png"

    monkeypatch.setattr(driver, "_deadline_tree", fail_tree)
    monkeypatch.setattr(driver, "_deadline_screencap", current_pixels)
    tree, shot, metadata = await driver.capture_deadline_frame(
        ObservationDeadline("current", 2500), prefer_stream=False
    )

    assert shot == b"current-png"
    assert tree["_capture"]["complete"] is False
    assert tree["_frame_gate_degraded"] is True
    assert metadata["coordinate_compatible"] is False
    # Failed tree acquisition has no successful tree provider.
    assert metadata["tree_provider"] == "unavailable"
    assert "dump timed out" in tree["_capture"]["reasons"]
    assert metadata["pixel_provider"] == "adb_screencap"
    assert metadata["fallback_edges"] == [{
        "from": "direct_capture",
        "to": "adb_screencap",
        "reason": "provider_unavailable",
    }]


def test_successful_dump_fallback_remains_complete():
    tree = mark_dump_fallback(_node("open", [10, 10, 100, 80]), reason="not installed")
    tree["_capture"]["coordinate_compatible"] = True
    package = ObservationBuilder().build((tree, _png()))
    assert package.mode == ObservationMode.TREE_ONLY
    assert package.gap_reasons == []
    assert all(item.index >= 0 for item in package.ui.elements)


def test_empty_complete_tree_is_unusable_but_one_text_node_is_valid():
    empty = {"class": "Root", "children": [], "_capture": {
        "provider": "accessibility_collector", "complete": True,
    }}
    empty_package = ObservationBuilder().build((empty, _png()))
    assert empty_package.gap_reasons == [TREE_UNUSABLE]

    text = _node("Current page", [0, 0, 100, 30], clickable=False)
    text["_capture"] = {
        "provider": "accessibility_collector",
        "complete": True,
        "coordinate_compatible": True,
    }
    text_package = ObservationBuilder().build((text, _png()))
    assert text_package.mode == ObservationMode.TREE_ONLY
    assert text_package.gap_reasons == []


def test_explicit_image_never_creates_visual_indices():
    tree = _node("open", [10, 10, 100, 80])
    tree["_capture"] = {
        "provider": "accessibility_collector",
        "complete": True,
        "coordinate_compatible": True,
    }
    package = ObservationBuilder().build(
        (tree, _png()), will_send_image=True,
    )
    assert package.mode == ObservationMode.TREE_PLUS_IMAGE
    assert [item.text for item in package.ui.elements] == ["open"]
    assert package.gap_reasons == []
