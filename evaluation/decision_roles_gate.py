"""Capture a canonical observation, then run Reviewer scope→Planner without Executor.

The two commands are intentionally separate. ``capture`` is read-only device
I/O and creates immutable input files. ``run`` reads only those files, gives
the production Reviewer and Planner a frozen observation driver with no
``act`` method, and records a mechanical zero-dispatch receipt.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any

from agent.planner import Planner
from agent.reviewer import Reviewer
from perception.observation import ObservationBuilder
from shared.artifacts import ArtifactStore
from shared.config import get_settings
from shared.schemas import AgentState, ActiveTaskCompletionContract


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def _load_cell(freeze_path: Path, ordinal: int) -> tuple[dict[str, Any], dict[str, Any]]:
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    cells = [cell for cell in freeze["stage_m"]["cells"] if cell["ordinal"] == ordinal]
    if len(cells) != 1:
        raise ValueError(f"exactly one Stage-M cell required for ordinal {ordinal}")
    return freeze, cells[0]


def _run_root(freeze_path: Path, cell: dict[str, Any]) -> Path:
    return (
        freeze_path.parent / "runs" / "stage-m"
        / f"{int(cell['ordinal']):03d}-{cell['id']}"
    )


def _source_manifest(root: Path) -> str:
    paths: list[Path] = []
    for relative in (
        "agent", "shared", "perception", "driver", "skills",
        "android/accessibility-collector/app/src/main", "control_api",
    ):
        paths.extend(
            path for path in (root / relative).rglob("*")
            if path.is_file() and path.suffix in {".py", ".md", ".kt"}
        )
    paths.append(root / "evaluation" / "decision_roles_gate.py")
    lines = "".join(
        f"{_sha256(path.read_bytes())}  {path.relative_to(root)}\n"
        for path in sorted(set(paths))
    ).encode("utf-8")
    return _sha256(lines)


def _admission_checks(package: Any, setup: str) -> dict[str, bool]:
    elements = list(package.ui.elements)
    channels = [
        " ".join((item.text, item.desc, item.hint, item.resource_id)).strip()
        for item in elements
    ]
    checks = {
        "accepted": bool(package.accepted),
        "indexed_tree_image": bool(
            package.index_actionable and package.mode.value == "tree+image"
        ),
    }
    if setup == "calculator-reset":
        by_id = {item.resource_id: item for item in elements}
        required = {
            "expression", "btn_c_s", "op_mul", "op_sub", "btn_equal_s",
            *(f"digit_{value}" for value in range(10)),
        }
        checks.update({
            "expression_is_zero": bool(
                by_id.get("expression") and by_id["expression"].text == "0"
            ),
            "required_keys_visible": required.issubset(by_id),
        })
    elif setup == "settings-home":
        checks["wlan_row_visible"] = any("WLAN" in channel for channel in channels)
    elif setup == "bilibili-retained-query":
        checks.update({
            "exact_editable_value": any(
                "EditText" in item.role and item.text == "张凌赫" for item in elements
            ),
            "search_control_visible": any(
                "搜索" in " ".join((item.text, item.desc, item.hint))
                for item in elements
            ),
            "result_content_visible": len(package.ui.semantic_tree) > 10,
        })
    else:
        raise ValueError(f"unknown setup profile: {setup}")
    return checks


async def capture(serial: str, freeze_path: Path, ordinal: int) -> None:
    """Persist one accepted tree+image observation without taking an action."""
    from driver.factory import get_driver
    from driver.scrcpy_mirror import REGISTRY

    _freeze, cell = _load_cell(freeze_path, ordinal)
    output = _run_root(freeze_path, cell)
    expected_app = str(cell["app"])
    driver = get_driver(get_settings(), serial=serial)
    try:
        warmer = getattr(driver, "warm_observation_provider", None)
        if callable(warmer):
            await warmer()
        tree, image = await driver.get_frame()
        identity_reader = getattr(driver, "current_foreground_identity", None)
        identity = await identity_reader() if callable(identity_reader) else {}
        package = ObservationBuilder().build(
            (deepcopy(tree), image),
            app_id=str(identity.get("package") or ""),
            activity=str(identity.get("activity") or ""),
            will_send_image=True,
        )
        if not package.accepted or package.ui.app_id != expected_app:
            raise RuntimeError(
                f"observation admission failed: accepted={package.accepted} "
                f"app={package.ui.app_id!r} expected={expected_app!r}"
            )
        if not package.index_actionable or package.mode.value != "tree+image":
            raise RuntimeError(
                f"indexed tree+image required: mode={package.mode.value} "
                f"index_actionable={package.index_actionable}"
            )
        checks = _admission_checks(package, str(cell["setup"]))
        tree_bytes = json.dumps(
            tree, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        _write_new(output / "input" / "tree.json", tree_bytes)
        _write_new(output / "input" / "screen.png", image)
        _write_new(
            output / "preflight.json",
            json.dumps({
                "schema_version": 1,
                "serial": serial,
                "expected_app": expected_app,
                "foreground_app": package.ui.app_id,
                "activity": package.ui.activity,
                "accepted": package.accepted,
                "mode": package.mode.value,
                "index_actionable": package.index_actionable,
                "setup": cell["setup"],
                "admission_checks": checks,
                "admission_passed": all(checks.values()),
                "tree_sha256": _sha256(tree_bytes),
                "image_sha256": _sha256(image),
                "pixel_provider": package.capture_meta.get("pixel_provider"),
                "tree_provider": package.capture_meta.get("tree_provider"),
                "elements": [item.model_dump(mode="json") for item in package.ui.elements],
                "capture": package.capture_meta,
            }, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        if not all(checks.values()):
            raise RuntimeError(f"setup admission failed: {checks}")
    finally:
        closer = getattr(driver, "close_observation_provider", None)
        if callable(closer):
            await closer()
        await REGISTRY.shutdown()


class FrozenObservationDriver:
    """Read-only observation source; deliberately has no action dispatcher."""

    def __init__(self, tree: dict[str, Any], image: bytes) -> None:
        self._tree = tree
        self._image = image

    async def get_frame(self) -> tuple[dict[str, Any], bytes]:
        return deepcopy(self._tree), self._image


async def run_gate(
    freeze_path: Path,
    ordinal: int,
) -> None:
    """Run production Reviewer scope and Planner against the frozen input."""
    freeze, cell = _load_cell(freeze_path, ordinal)
    output = _run_root(freeze_path, cell)
    input_dir = output / "input"
    instruction = str(cell["instruction"])
    model = str(freeze["model"])
    root = Path(__file__).resolve().parents[1]
    expected_manifest = str(freeze["source_sha256"]["manifest_sha256"])
    actual_manifest = _source_manifest(root)
    if actual_manifest != expected_manifest:
        raise RuntimeError(
            f"source manifest drift: {actual_manifest} != {expected_manifest}"
        )
    preflight = json.loads((output / "preflight.json").read_text(encoding="utf-8"))
    if not preflight.get("admission_passed"):
        raise RuntimeError("preflight admission did not pass")
    tree = json.loads((input_dir / "tree.json").read_text(encoding="utf-8"))
    image = (input_dir / "screen.png").read_bytes()
    tree_digest = _sha256(json.dumps(
        tree, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8"))
    image_digest = _sha256(image)
    if tree_digest != preflight["tree_sha256"] or image_digest != preflight["image_sha256"]:
        raise RuntimeError("captured input hash does not match preflight")
    if preflight["foreground_app"] != cell["app"] or preflight["setup"] != cell["setup"]:
        raise RuntimeError("preflight cell binding mismatch")
    settings = get_settings()
    reasoning = settings.provider_for(model).get("reasoning") or {}
    if reasoning.get("effort") != freeze["reasoning_effort"]:
        raise RuntimeError("reasoning effort does not match freeze")
    _write_new(output / "claim.json", json.dumps({
        "schema_version": 1,
        "freeze_id": freeze["freeze_id"],
        "stage": "stage-m",
        "ordinal": ordinal,
        "cell_id": cell["id"],
        "instruction": instruction,
        "model": model,
        "reasoning_effort": freeze["reasoning_effort"],
        "source_manifest_sha256": actual_manifest,
        "tree_sha256": tree_digest,
        "image_sha256": image_digest,
    }, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    package = ObservationBuilder().build(
        (deepcopy(tree), image),
        app_id=str(preflight["foreground_app"]),
        activity=str(preflight["activity"]),
        will_send_image=True,
    )
    driver = FrozenObservationDriver(tree, image)
    artifacts = ArtifactStore(output / "artifacts")
    reviewer = Reviewer(driver, artifacts, model=model, settings=settings)
    planner = Planner(driver, artifacts, model=model, settings=settings)
    state = AgentState(instruction=instruction)

    scope, scope_refs = await reviewer.author_task_scope(state, task_id=output.name)
    state.task_completion_contract = ActiveTaskCompletionContract(
        contract_id=uuid.uuid4().hex,
        revision=1,
        created_step=0,
        body=scope,
    )
    decision, planner_refs, observation_refs = await planner.decide(
        state, package, task_id=output.name,
    )
    result = {
        "schema_version": 1,
        "instruction": instruction,
        "model": model,
        "input": {
            "tree_sha256": tree_digest,
            "image_sha256": image_digest,
            "foreground_app": package.ui.app_id,
            "mode": package.mode.value,
            "index_actionable": package.index_actionable,
        },
        "task_scope": scope.model_dump(mode="json"),
        "planner_decision": decision.model_dump(mode="json"),
        "scope_refs": scope_refs,
        "planner_refs": planner_refs,
        "observation_refs": observation_refs,
        "zero_dispatch_receipt": {
            "executor_constructed": False,
            "executor_calls": 0,
            "dispatcher_constructed": False,
            "device_actions": 0,
            "driver_type": type(driver).__name__,
            "driver_has_act": hasattr(driver, "act"),
        },
    }
    _write_new(
        output / "decision-roles-result.json",
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--serial", required=True)
    capture_parser.add_argument("--freeze", type=Path, required=True)
    capture_parser.add_argument("--stage-m-ordinal", type=int, required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--freeze", type=Path, required=True)
    run_parser.add_argument("--stage-m-ordinal", type=int, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        asyncio.run(capture(args.serial, args.freeze, args.stage_m_ordinal))
    else:
        asyncio.run(run_gate(args.freeze, args.stage_m_ordinal))


if __name__ == "__main__":
    main()
