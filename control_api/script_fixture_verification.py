"""Connect consented recording jobs to the isolated replay verifier."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from control_api.skill_loop_jobs import fixture_config, run_fixture_worker


class ScriptFixtureVerification:
    def __init__(self, settings, pool, fixture_slot):
        self.settings = settings
        self.pool = pool
        self.fixture_slot = fixture_slot
        self.root = settings.data_dir.resolve() / "recorded-script-verification"
        self.registry = settings.data_dir.resolve() / "scripts"

    async def verify(self, task, archive_path):
        fixture = fixture_config(self.settings)
        if fixture is None:
            return {"verification_status": "blocked", "verification_reason": "fixture_not_configured"}

        run_id = uuid4().hex
        run = self.root / run_id
        bundle = run / "bundle"
        bundle.mkdir(parents=True, exist_ok=False)
        with ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                destination = (bundle / entry.filename).resolve()
                if entry.is_dir() or not destination.is_relative_to(bundle.resolve()) or destination == bundle.resolve():
                    raise ValueError("invalid recorded script bundle member")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as file:
                    file.write(archive.read(entry))

        from evaluation.gui_scripts import _script

        path = bundle / "script.json"
        document = json.loads(path.read_bytes())
        script = _script(document)
        if script.parameters:
            return {"verification_status": "blocked", "verification_reason": "fixture_script_parameters_unavailable"}
        if 2 * sum(step.action.type != "wait" for step in script.steps) > self.settings.learning_default_actions:
            return {"verification_status": "blocked", "verification_reason": "action_budget"}
        seconds = self.settings.learning_default_seconds
        payload = {"operation": "verify_recorded_script", "fixture": fixture.model_dump(mode="json"),
                   "settings": self.settings.model_dump(mode="json", exclude={"jev_api_key"}),
                   "script": str(path), "goal": task.instruction, "seconds": seconds,
                   "output": str(run / "verification"), "registry": str(self.registry)}
        async with self.fixture_slot:
            inventory = await asyncio.wait_for(self.pool.inventory(), timeout=10)
            if any(item.get("serial") == fixture.serial for item in inventory):
                return {"verification_status": "blocked", "verification_reason": "fixture_visible_to_ordinary_tasks"}
            result = await run_fixture_worker(payload, seconds)
        if not result:
            return {"verification_status": "error", "verification_reason": "script_verification_worker_failed"}
        if result["status"] == "automatic_enrollment_deferred" and result.get("reason") == "revision_already_registered":
            candidate = document.get("candidate", document)
            if self._registered_exact(script, candidate.get("provenance") or {}):
                result = {"status": "automatically_registered"}
        return {"verification_status": result["status"], "verification_reason": result.get("reason"),
                "verification_id": run_id}

    def _registered_exact(self, script, provenance):
        from evaluation.gui_scripts import _load, _script

        path = self.registry / script.script_id / f"{script.revision}.json"
        try:
            saved = _load(path)
            return (saved.get("provenance") == provenance and saved.get("replay_required") is False
                    and _script(saved) == script)
        except (OSError, ValueError):
            return False

    def approve(self, verification_id, source_task_id, expected_script_hash=None):
        from evaluation.gui_scripts import _assets, _load, _script
        from evaluation.gui_script_release import register_script

        run = (self.root / verification_id).resolve(strict=True)
        if run.parent != self.root.resolve():
            raise ValueError("script verification escapes its data directory")
        raw = _load(run / "bundle" / "script.json")
        candidate = raw.get("candidate", raw)
        provenance = candidate.get("provenance") or {}
        if (provenance.get("task_id") != source_task_id
                or (expected_script_hash is not None and candidate.get("script_hash") != expected_script_hash)):
            raise ValueError("candidate was not recorded by this task")
        report = _load(run / "verification" / "report.json")
        script = _script(raw)
        try:
            return register_script(script, candidate, report, self.registry,
                                   asset_root=_assets(run / "bundle", candidate, None))
        except FileExistsError:
            if not self._registered_exact(script, provenance):
                raise
            return self.registry / script.script_id / f"{script.revision}.json"
