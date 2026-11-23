"""Explicit registration of an independently verified script revision."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from agent.revisable.store import TaskStore
from agent.skills.script_runner import ScriptRunResult
from agent.skills.script_schema import GuiScript, script_fingerprint, visual_templates
from agent.skills.script_visual import read_visual_asset
from evaluation.gui_script_auto_repair import is_low_impact_initial, is_low_impact_repair
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.recovery_store import digest
from shared.schemas import TaskStatus


def _verified_run(run, goal):
    runtime = Path(run["runtime"])
    journal = runtime / "clickclick.db"
    if not journal.is_file():
        raise FileNotFoundError(journal)
    db = Database(journal, read_only=True)
    try:
        task = db.get_task(run["task_id"])
        if task is None or task.status != TaskStatus.SUCCEEDED or task.instruction != goal:
            raise ValueError("replay did not complete the frozen task")
        store = TaskStore(db, ArtifactStore(runtime / "artifacts", create=False), task.id)
        saved = store.get("gui_script_run", "final")["payload"]
        if saved["receipt"] != run.get("receipt"):
            raise ValueError("replay report differs from the host record")
        fixture = saved.get("fixture") or {}
        if (not all(fixture.get(key) for key in ("app_build", "snapshot", "avd", "parameters_hash", "initial_state_hash"))
                or fixture.get("goal_initially_met") is not False):
            raise ValueError("registered replays require a verified initial state with an unmet goal")
        receipt = ScriptRunResult.model_validate(saved["receipt"])
        if receipt.status != "succeeded" or not receipt.final_asserted:
            raise ValueError("replay did not pass the final assertion")
        return receipt, fixture
    finally:
        db.close()


def _copy_templates(script, folder, asset_root):
    templates = tuple(visual_templates(script))
    if not templates:
        return {}
    if asset_root is None:
        raise ValueError("approval of a visual script requires its template bundle")
    directory = f"{script.revision}-{script_fingerprint(script)}.assets"
    destination = (folder / directory).resolve()
    if destination.parent != folder.resolve():
        raise ValueError("template destination escapes the script registry")
    files = {}
    for template in templates:
        path = (destination / template.png_path).resolve()
        path.relative_to(destination)
        data = read_visual_asset(template, asset_root)
        if path in files and files[path] != data:
            raise ValueError("two visual assets share a path with different contents")
        files[path] = data
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as output:
                output.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("registered visual asset was changed")
    return {"asset_directory": directory}


def _script_folder(registry, script_id):
    scope = Path(registry).resolve()
    official = Path(__file__).resolve().parents[1] / "skills"
    if scope.is_relative_to(official.resolve()):
        raise ValueError("script revisions belong in a separate registry")
    folder = scope / script_id
    if folder.resolve().parent != scope:
        raise ValueError("script registry path escapes its root")
    return folder


def registered_original_context(script: GuiScript, goal: str, registry: Path):
    """Read the prior original-state proof from the host journal, never a declared extra fixture."""
    folder = _script_folder(registry, script.script_id)
    path = folder / f"{script.revision}.json"
    if path.resolve().parent != folder.resolve():
        raise ValueError("registered script path escapes its folder")
    registered = json.loads(path.read_text(encoding="utf-8"))
    script_hash = script_fingerprint(script)
    review = registered.get("review") or {}
    if (script_fingerprint(GuiScript.model_validate_json(json.dumps(registered["script"]))) != script_hash
            or registered.get("script_hash") != script_hash or (registered.get("provenance") or {}).get("goal") != goal
            or review.get("decision") != "accept" or review.get("script_hash") != script_hash):
        raise ValueError("automatic recovery requires the exact reviewed and registered script")
    verification = registered["verification"]
    baseline = verification.get("original", verification["second"])
    receipt, fixture = _verified_run(baseline, goal)
    if receipt.script_hash != script_hash or receipt.assisted or receipt.revision != script.revision:
        raise ValueError("registered original replay did not independently exercise the exact script")
    return fixture, registered["provenance"]


def register_script(script: GuiScript, candidate, report, registry: Path, *, asset_root: Path | None = None,
                    automatic_base: GuiScript | None = None, automatic_initial: bool = False):
    """Publish an immutable revision and recheck the evidence appropriate to its route."""
    script_hash = script_fingerprint(script)
    review, provenance = candidate.get("review") or {}, candidate.get("provenance") or {}
    goal, base_hash = provenance.get("goal"), provenance.get("base_hash")
    if not goal or candidate.get("script_hash") != script_hash or review.get("decision") != "accept" or review.get("script_hash") != script_hash:
        raise ValueError("an independently reviewed exact candidate and frozen goal are required")
    if report.get("update") != "verified_pending_review":
        raise ValueError("the run did not verify the candidate")
    first, second = report["first"], report["second"]
    a, fixture_a = _verified_run(first, goal)
    b, fixture_b = _verified_run(second, goal)
    if first["task_id"] == second["task_id"] or fixture_a != fixture_b or b.script_hash != script_hash or b.assisted:
        raise ValueError("the candidate must pass independently from the same fixture in a distinct replay")
    folder = _script_folder(registry, script.script_id)
    registered_base = None
    if base_hash:
        if (script.revision < 2 or a.script_hash != base_hash or not a.assisted
                or provenance.get("assisted_run_hash") != digest(first)):
            raise ValueError("the repair must bind the original assisted run")
        current = json.loads((folder / f"{script.revision - 1}.json").read_text(encoding="utf-8"))
        registered_base = GuiScript.model_validate_json(json.dumps(current["script"]))
        if script_fingerprint(registered_base) != base_hash:
            raise ValueError("the registered base revision has changed")
    elif script.revision != 1 or a.script_hash != script_hash or a.assisted:
        raise ValueError("a first release requires two independent runs of the same script")

    if automatic_initial and (automatic_base is not None or base_hash or not is_low_impact_initial(script)):
        raise ValueError("only low-impact initial scripts can enroll automatically")
    if automatic_base is not None:
        if base_hash != script_fingerprint(automatic_base) or not is_low_impact_repair(automatic_base, script):
            raise ValueError("only low-impact repairs of the registered base can publish automatically")
    if automatic_base is not None or report.get("automatic_repair"):
        original = report.get("original")
        if not original or registered_base is None:
            raise ValueError("this repair requires an independent original-state regression")
        previous_fixture, _ = registered_original_context(registered_base, goal, registry)
        old_receipt, old_fixture = _verified_run(original, goal)
        if (old_receipt.script_hash != script_hash or old_receipt.assisted or old_fixture != previous_fixture
                or original["task_id"] in {first["task_id"], second["task_id"]}):
            raise ValueError("the repaired script did not independently pass the exact registered original state")

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{script.revision}.json"
    if target.exists():
        raise FileExistsError(target)
    bundle = _copy_templates(script, folder, asset_root)
    with NamedTemporaryFile(dir=folder, mode="w", encoding="utf-8", delete=False) as file:
        pending = Path(file.name)
        json.dump({**candidate, **bundle, "replay_required": False, "verification": report}, file,
                  ensure_ascii=False, indent=2)
    try:
        # Atomically publish only when the immutable revision name is still unused.
        os.link(pending, target)
    finally:
        pending.unlink()
    return target
