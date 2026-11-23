"""Persist explicitly selected, replay-verified Android Skill regression cases."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from tempfile import TemporaryDirectory
from time import sleep

from pydantic import BaseModel, ConfigDict, Field

from agent.skills.script_runner import ScriptRunResult
from agent.skills.script_schema import (
    TargetCondition, VisualCondition, VisualObjectCondition, script_fingerprint, visual_templates,
)
from agent.skills.script_visual import read_visual_asset
from evaluation.gui_script_case import CaseDefinition
from shared.file_lock import FileLock
from shared.recovery_store import digest


@dataclass(frozen=True)
class CaseIdentity:
    skill_target: str
    case_key: str
    app_id: str
    build: str

    def __post_init__(self):
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", self.app_id)
                or not re.fullmatch(r"[0-9]+", self.build)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", self.case_key)):
            raise ValueError("case identity needs an exact Android package, build and explicit key")
        path = PurePosixPath(self.skill_target)
        if (path.is_absolute() or path.as_posix() != self.skill_target or not path.parts
                or path.name != "SKILL.md" or "\\" in self.skill_target
                or any(part.startswith(("_", ".")) for part in path.parts)
                or not (path.parts[0] == "generic" and len(path.parts) >= 2
                        or path.parts[:2] == ("apps", self.app_id) and len(path.parts) >= 3)):
            raise ValueError("case Skill target must match the Android package or a generic Skill")


class CaseVerification(BaseModel):
    """Device-independent report produced by the trusted setup replay host."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    receipt: ScriptRunResult
    app_id: str
    build: str
    avd: str = Field(min_length=1)
    snapshot: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    case_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    conditions_met: bool
    goal_already_met: bool
    branch_ready: bool
    guards_passed: bool


@dataclass(frozen=True)
class RegisteredCase:
    case: CaseDefinition
    asset_root: Path
    path: Path
    verification: CaseVerification


def case_fingerprint(case: CaseDefinition) -> str:
    """Bind the setup script, task goal, parameters and independent conditions."""
    return digest(case.model_dump(mode="json"))


def _case_templates(case):
    yield from visual_templates(case.setup)
    for conditions in (case.result, case.guards, case.branch, case.branch_after):
        for condition in conditions:
            if isinstance(condition, VisualObjectCondition):
                yield condition.target.layout
            elif isinstance(condition, VisualCondition):
                yield condition.target
            elif isinstance(condition, TargetCondition) and condition.visual is not None:
                yield condition.visual


def _case_assets(case, root):
    assets = {}
    for template in _case_templates(case):
        if root is None or template.sha256 is None:
            raise ValueError("registered visual cases require hash-bound PNG assets")
        name = PureWindowsPath(template.png_path).as_posix()
        content = read_visual_asset(template, root)
        if assets.setdefault(name, content) != content:
            raise ValueError("conflicting visual assets for the registered case")
    return assets


def _publish_bundle(bundle: Path, folder: Path) -> None:
    # Windows can briefly deny renaming a directory containing newly written files.
    for delay in (0.05, 0.1, 0.2):
        try:
            bundle.rename(folder)
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or folder.exists():
                raise
            sleep(delay)
        else:
            return
    bundle.rename(folder)


def _verified_step(step, receipt):
    if not receipt.succeeded or not receipt.before_observation_id or not receipt.after_observation_id:
        return False
    if step.action.type == "wait":
        return receipt.execution == "not_dispatched" and receipt.receipt is None
    native = receipt.receipt
    return (receipt.execution == "known" and receipt.before_observation_id != receipt.after_observation_id
            and native is not None and bool(native.transaction_id) and native.dispatch_succeeded)


def _require_verified_case(identity, case, verification):
    setup, receipt = case.setup, verification.receipt
    if ((setup.app_id, case.build) != (identity.app_id, identity.build) or setup.platform != "android"
            or (verification.app_id, verification.build) != (identity.app_id, identity.build)
            or verification.case_hash != case_fingerprint(case)):
        raise ValueError("case and setup verification have different identities")
    setup.bind_parameters(case.parameters)
    if (not verification.conditions_met or verification.goal_already_met
            or verification.branch_ready != case.branch_expected or not verification.guards_passed):
        raise ValueError("setup replay did not independently prepare the intended initial state")
    if (receipt.status != "succeeded" or receipt.assisted or not receipt.final_asserted
            or receipt.script_hash != script_fingerprint(setup)
            or (receipt.script_id, receipt.revision) != (setup.script_id, setup.revision)
            or not receipt.initial_observation_id or not receipt.final_observation_id
            or receipt.initial_observation_id == receipt.final_observation_id
            or tuple(step.step_id for step in receipt.steps) != tuple(step.id for step in setup.steps)
            or any(not _verified_step(step, result) for step, result in zip(setup.steps, receipt.steps))):
        raise ValueError("registration requires an unassisted successful replay of the exact setup")


class CaseRegistry:
    """Resolve an explicitly named case only within its Skill, package and exact build."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _directory(self, identity: CaseIdentity) -> Path:
        folder = self.root / digest(asdict(identity))
        if folder.resolve() != folder:
            raise ValueError("case registry entry must be a local directory")
        return folder

    def lookup(self, identity: CaseIdentity) -> RegisteredCase | None:
        folder = self._directory(identity)
        if not folder.exists():
            return None
        record = folder / "case.json"
        if record.resolve() != record:
            raise ValueError("case manifest must be a local file")
        document = json.loads(record.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("identity") != asdict(identity):
            raise ValueError("case registry entry has an unexpected identity")
        case = CaseDefinition.model_validate_json(json.dumps(document["case"]))
        verification = CaseVerification.model_validate_json(json.dumps(document["verification"]))
        _require_verified_case(identity, case, verification)
        assets = folder / "assets"
        if assets.resolve() != assets:
            raise ValueError("case assets must stay in the registered bundle")
        _case_assets(case, assets)
        return RegisteredCase(case, assets, record, verification)

    def find_for_skill(self, *, skill_target: str, app_id: str, build: str, strict: bool = False):
        """Find intact cases in one exact scope, including older flat bundles."""
        scope = {"skill_target": skill_target, "app_id": app_id, "build": build}
        found = []
        for record in sorted(self.root.glob("*/case.json")):
            if record.resolve() != record:
                continue
            matched = False
            try:
                document = json.loads(record.read_text(encoding="utf-8"))
                fields = document.get("identity") if isinstance(document, dict) else None
                if not isinstance(fields, dict) or any(fields.get(key) != value for key, value in scope.items()):
                    continue
                matched = True
                identity = CaseIdentity(**scope, case_key=fields.get("case_key"))
                if self._directory(identity) != record.parent:
                    raise ValueError("case was saved under another identity")
                case = self.lookup(identity)
                if case is None:
                    raise ValueError("registered case disappeared")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if strict and matched:
                    raise ValueError(f"registered case could not be checked: {record.parent.name}") from exc
                # A damaged legacy record cannot authorize automatic reuse.
                continue
            found.append((identity, case))
        return found

    def register(self, identity: CaseIdentity, case: CaseDefinition, *, asset_root: Path | None,
                 verification: CaseVerification) -> RegisteredCase:
        _require_verified_case(identity, case, verification)
        folder = self._directory(identity)
        with FileLock(self.root / ".mutations.lock", timeout=5):
            existing = self.lookup(identity)
            if existing:
                if case_fingerprint(existing.case) != verification.case_hash:
                    raise ValueError("case identity is already registered with a different definition")
                return existing
            assets = _case_assets(case, asset_root)
            document = {"version": 1, "identity": asdict(identity), "case": case.model_dump(mode="json"),
                        "verification": verification.model_dump(mode="json")}
            with TemporaryDirectory(dir=self.root, prefix=".prepare-") as pending:
                bundle = Path(pending) / "bundle"
                destination = bundle / "assets"
                destination.mkdir(parents=True)
                for name, content in assets.items():
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as file:
                        file.write(content)
                (bundle / "case.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                _publish_bundle(bundle, folder)
            return RegisteredCase(case, folder / "assets", folder / "case.json", verification)
