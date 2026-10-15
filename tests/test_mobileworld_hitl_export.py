import json
from copy import deepcopy

import pytest

from evaluation.mobileworld import export_hitl_submission as subject


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf8")


@pytest.fixture
def sample(tmp_path, monkeypatch):
    case = tmp_path / "case"
    (case / "artifacts").mkdir(parents=True)
    (case / "artifacts/screen.png").write_bytes(b"screenshot fixture")
    receipts = [{"interaction_id": str(i), "round": n, "status": "returned",
                 "provenance": "official_simulated_user", "call_id": str(i), "invocation_id": "invoke",
                 "action": {"action_type": "ask_user", "text": f"Question {i}?"}, "answer": f"Answer {i}"}
                for i, n in enumerate((1, 3))]
    persisted = {r["interaction_id"]: deepcopy(r) for r in receipts}
    monkeypatch.setattr(subject, "observation_images", lambda _: (
        {r["interaction_id"]: "screen.png" for r in receipts},
        persisted, "screen.png"))
    row = {"goal": "Original goal", "mobileworld_rounds": 5, "clickclick_status": "succeeded",
           "human_interaction": {"enabled": True, "query_count": 2, "dispatches": receipts},
           "steps": [{"seq": 0, "action": {"type": "tap", "index": 2}, "annotated_ref": "screen.png"},
                     {"seq": 1, "action": None, "executor_decision": "request_replan"},
                     {"seq": 2, "action": {"type": "type", "text": "hello"}, "annotated_ref": "screen.png"}],
           "answer_submission": {"official_action": "answer", "text": "Done"}}
    return row, case


def test_exact_round_order_answers_and_internal_handoff_exclusion(sample):
    row, case = sample
    result = subject.trajectory(row, case)
    assert [s["action"]["action_type"] for s in result] == ["ask_user", "tap", "ask_user", "type", "answer"]
    assert result[0]["ask_user_response"] == "Answer 0"
    assert result[2]["ask_user_response"] == "Answer 1"
    assert result[-1]["clickclick"]["submitted_to_environment"] is True


@pytest.mark.parametrize("fault", ["duplicate_round", "duplicate_id", "unknown_call", "unknown_invocation", "unreturned", "missing_round"])
def test_inconsistent_receipts_or_accounting_fail_closed(sample, fault):
    row, case = sample
    receipts = row["human_interaction"]["dispatches"]
    if fault == "duplicate_round": receipts[1]["round"] = 1
    if fault == "duplicate_id": receipts[1]["interaction_id"] = "0"
    if fault == "unknown_call": receipts[0]["call_id"] = "other"
    if fault == "unknown_invocation": receipts[0]["invocation_id"] = "other"
    if fault == "unreturned": receipts[0]["status"] = "dispatched"
    if fault == "missing_round": row["mobileworld_rounds"] = 7
    with pytest.raises(ValueError): subject.trajectory(row, case)


def test_failed_terminal_does_not_invent_a_submitted_answer(sample):
    row, case = sample
    row.update(answer_submission=None, clickclick_status="failed")
    result = subject.trajectory(row, case)
    assert result[-1]["action"] == {"action_type": "finished", "status": "failed"}
    assert result[-1]["clickclick"]["submitted_to_environment"] is False


def test_unsafe_screenshot_is_rejected(sample):
    row, case = sample
    row["steps"][0]["annotated_ref"] = "../outside.png"
    (case / "outside.png").write_bytes(b"outside")
    with pytest.raises(ValueError, match="unsafe"): subject.trajectory(row, case)


def test_viewer_frame_keeps_native_geometry_and_discloses_upscaling(tmp_path):
    from PIL import Image

    source, target = tmp_path / "source.png", tmp_path / "viewer.png"
    Image.new("RGB", (2, 4), "red").save(source)
    audit = subject.copy_frame(source, target, [4, 8])
    assert audit["source_dimensions"] == [2, 4] and audit["resized"]
    with Image.open(target) as image:
        assert image.size == (4, 8) and image.getpixel((0, 0)) == (255, 0, 0)
    with pytest.raises(ValueError, match="aspect ratio"):
        subject.copy_frame(source, target, [4, 4])


@pytest.fixture
def cohort(tmp_path):
    root = tmp_path / "interaction"
    names = [f"Task{i}" for i in range(44)]
    write(root.parent / "official-task-list.json", [{"name": n, "tags": ["agent-user-interaction"]} for n in names])
    write(root.parent / "official-goals.json", {n: "original" for n in names})
    write(root / "manifest.json", {"cases": [{"task": n} for n in names]})
    rows = [{"task": n, "goal": "original", "environment_status": "ready", "score": {"score": 0},
             "recording": {"mode": "continuous", "segments": [{"validation": "decoded_to_eof"}]}} for n in names]
    write(root / "results.json", rows)
    return root, rows


def test_full_category_retains_official_zeros(cohort):
    root, _ = cohort
    _, rows = subject.validate(root)
    assert len(rows) == 44 and all(r["score"]["score"] == 0 for r in rows)


@pytest.mark.parametrize("fault", ["missing_case", "duplicate_case", "unscored", "goal_variant", "bad_video", "diagnostic"])
def test_full_submission_rejects_incomplete_or_altered_evidence(cohort, fault):
    root, rows = cohort
    if fault == "missing_case": rows.pop()
    if fault == "duplicate_case": rows[-1] = rows[0]
    if fault == "unscored": rows[0]["score"] = None
    if fault == "goal_variant": rows[0]["goal"] = "clarified"
    if fault == "bad_video": rows[0]["recording"]["error"] = "truncated"
    if fault == "diagnostic": write(root.parent / "experiment.json", {"submission_eligible": False})
    write(root / "results.json", rows)
    with pytest.raises(ValueError): subject.validate(root)
