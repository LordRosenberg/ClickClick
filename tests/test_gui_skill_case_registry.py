"""Reusing registered setup cases requires the exact Skill, app, build and evidence."""

from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from agent.skills.script_runner import ScriptRunResult, ScriptStepReceipt
from agent.skills.script_schema import script_fingerprint
from evaluation.gui_script_case import CaseDefinition
from evaluation.gui_skill_case_registry import CaseIdentity, CaseRegistry, CaseVerification, case_fingerprint
from shared.schemas import ActionReceipt


IDENTITY = CaseIdentity("apps/com.test.notes/workflows/edit/SKILL.md", "source", "com.test.notes", "12")


def _present(text):
    return {"type": "target_present", "target": {"text": text}}


def _case(**updates):
    note = _present("Test note")
    return CaseDefinition.model_validate_json(json.dumps({
        "setup": {"script_id": "prepare-note", "app_id": IDENTITY.app_id,
                  "initial": [_present("Notes")], "final": [note],
                  "steps": [{"id": "open-note", "before": [_present("Notes")], "after": [note],
                             "action": {"type": "tap", "target": {"text": "Test note"}}}]},
        "goal": "Rename the opened note", "build": IDENTITY.build,
        "result": [_present("Expected title")], "guards": [_present("Private draft")],
        "branch": [note], "branch_action": "replace_text", "branch_after": [_present("Expected title")],
        "branch_expected": True, **updates,
    }))


def _verification(case):
    return CaseVerification(
        receipt=ScriptRunResult(script_hash=script_fingerprint(case.setup), script_id=case.setup.script_id,
                                revision=case.setup.revision, status="succeeded", reason="final_assertion_met",
                                initial_observation_id="initial", final_observation_id="opened", final_asserted=True,
                                steps=(ScriptStepReceipt(step_id="open-note", attempt=1, succeeded=True,
                                                         execution="known", reason="postcondition_met",
                                                         before_observation_id="initial", after_observation_id="opened",
                                                         receipt=ActionReceipt(transaction_id="native-1",
                                                                               dispatch_succeeded=True)),)),
        app_id=IDENTITY.app_id, build=IDENTITY.build, avd="PrivateTest", snapshot="clean",
        case_hash=case_fingerprint(case), initial_state_hash="a" * 64,
        conditions_met=True, goal_already_met=False, branch_ready=case.branch_expected, guards_passed=True,
    )


def test_verified_case_is_durable_and_only_reused_for_exact_identity(tmp_path):
    root = tmp_path / "registered"
    registry = CaseRegistry(root)
    case = _case()
    verification = _verification(case)
    assert registry.lookup(IDENTITY) is None

    first = registry.register(IDENTITY, case, asset_root=None, verification=verification)
    fresh = CaseRegistry(root)
    assert fresh.lookup(IDENTITY) == first
    assert fresh.register(IDENTITY, case, asset_root=None, verification=verification) == first
    assert fresh.lookup(replace(IDENTITY, case_key="related")) is None
    assert fresh.lookup(replace(IDENTITY, build="13")) is None
    assert fresh.lookup(replace(IDENTITY, skill_target="apps/com.test.notes/core/SKILL.md")) is None
    changed_case = _case(goal="Rename another note")
    with pytest.raises(ValueError, match="different definition"):
        fresh.register(IDENTITY, changed_case, asset_root=None, verification=_verification(changed_case))
    assert fresh.lookup(IDENTITY) == first


def test_scoped_search_excludes_other_skill_and_damaged_case(tmp_path):
    registry = CaseRegistry(tmp_path / "registered")
    case = _case()
    first = registry.register(IDENTITY, case, asset_root=None, verification=_verification(case))
    other = replace(IDENTITY, skill_target="apps/com.test.notes/core/SKILL.md")
    registry.register(other, case, asset_root=None, verification=_verification(case))
    invalid = registry.register(replace(IDENTITY, case_key="invalid"), case, asset_root=None,
                                verification=_verification(case))
    document = json.loads(invalid.path.read_text(encoding="utf-8"))
    document["case"]["goal"] = "Unverified edits"
    invalid.path.write_text(json.dumps(document), encoding="utf-8")
    scope = {"skill_target": IDENTITY.skill_target, "app_id": IDENTITY.app_id, "build": IDENTITY.build}
    assert registry.find_for_skill(**scope) == [(IDENTITY, first)]
    with pytest.raises(ValueError, match="registered case could not be checked"):
        registry.find_for_skill(**scope, strict=True)
    assert registry.find_for_skill(**{**scope, "build": "13"}) == []


@pytest.mark.parametrize("updates", [
    {"conditions_met": False}, {"goal_already_met": True}, {"branch_ready": False},
    {"guards_passed": False}, {"case_hash": "b" * 64},
])
def test_registration_requires_independent_initial_conditions(tmp_path, updates):
    registry = CaseRegistry(tmp_path / "registered")
    case = _case()
    with pytest.raises(ValueError):
        registry.register(IDENTITY, case, asset_root=None, verification=_verification(case).model_copy(update=updates))
    assert registry.lookup(IDENTITY) is None


def test_registered_near_miss_can_verify_branch_absence(tmp_path):
    identity = replace(IDENTITY, case_key="near-miss")
    case = _case(branch_expected=False)
    registry = CaseRegistry(tmp_path / "registered")
    result = registry.register(identity, case, asset_root=None, verification=_verification(case))
    assert result.verification.branch_ready is False
    assert registry.lookup(identity) == result


@pytest.mark.parametrize("updates", [{"assisted": True}, {"script_hash": "b" * 64}, {"steps": ()}])
def test_registration_rejects_unverified_replay(tmp_path, updates):
    registry = CaseRegistry(tmp_path / "registered")
    case = _case()
    verification = _verification(case)
    proof = verification.model_copy(update={"receipt": verification.receipt.model_copy(update=updates)})
    with pytest.raises(ValueError, match="exact setup"):
        registry.register(IDENTITY, case, asset_root=None, verification=proof)
    assert registry.lookup(IDENTITY) is None


def test_registry_owns_condition_only_pngs_and_rejects_tampering(tmp_path):
    original = tmp_path / "original" / "images" / "result.png"
    original.parent.mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (5, 5)).save(image, format="PNG")
    expected = image.getvalue()
    original.write_bytes(expected)
    case = _case(result=[{"type": "visual_present", "target": {
        "png_path": "images/result.png", "reference_width": 20, "reference_height": 20,
        "sha256": sha256(expected).hexdigest(),
    }}])
    registry = CaseRegistry(tmp_path / "registered")
    result = registry.register(IDENTITY, case, asset_root=original.parent.parent, verification=_verification(case))
    original.unlink()
    lookup = registry.lookup(IDENTITY)
    assert lookup is not None and lookup.path == result.path
    copied = lookup.asset_root / "images" / "result.png"
    assert copied.read_bytes() == expected
    copied.write_bytes(b"different")
    with pytest.raises(ValueError, match="hash_mismatch"):
        registry.lookup(IDENTITY)


def test_registry_retries_temporary_windows_rename_denial(tmp_path, monkeypatch):
    registry = CaseRegistry(tmp_path / "registered")
    case = _case()
    rename = Path.rename
    attempts = []

    def transient_rename(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("temporary Windows file hold")
            error.winerror = 5
            raise error
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", transient_rename)
    registered = registry.register(IDENTITY, case, asset_root=None, verification=_verification(case))

    assert len(attempts) == 2
    assert registry.lookup(IDENTITY) == registered
