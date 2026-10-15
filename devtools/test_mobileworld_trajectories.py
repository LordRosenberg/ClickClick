import json
import sqlite3

from PIL import Image

from devtools.export_mobileworld_trajectories import export_episode, public_text, public_exchange


def test_role_order_preserved_and_final_boundary_image_used(tmp_path):
    episode = tmp_path / "episode"
    artifacts = episode / "artifacts"
    artifacts.mkdir(parents=True)
    for name, color in [("before.png", "red"), ("after.png", "blue")]:
        Image.new("RGB", (80, 160), color).save(artifacts / name)
    (episode / "mobileworld-result.json").write_text(json.dumps({"steps": [
        {"seq": 0, "post_annotated_ref": "after.png"}]}))
    with sqlite3.connect(episode / "clickclick.db") as connection:
        connection.execute("create table agent_records (kind text, record_key text, payload_json text)")
        connection.execute("create table traces (id integer, kind text, payload_json text, step_seq integer)")
        entries = [
            ("planner_decision", {"decision": {"decision": "execute", "plan": {"current_stage": {"goal": "Open the item"}}}}, 0),
            ("executor_tick", {"decision": "act", "summary": "Enter the text", "action": {"type": "type", "text": "private-password"}, "som_ref": "before.png"}, 1),
            ("reviewer_decision", {"decision": {"decision": "complete", "reason": "Saved item confirmed"}}, 1),
        ]
        connection.executemany("insert into traces values (?,?,?,?)", [(i, kind, json.dumps(payload), seq) for i, (kind, payload, seq) in enumerate(entries)])
    out = tmp_path / "public"
    out.mkdir()
    url, counts = export_episode(episode, out, "Demo", "original")
    data = json.loads((out / "Demo-original.json").read_text())
    assert [event["role"] for event in data["events"]] == ["Planner", "Executor", "Reviewer", ""]
    assert data["events"][0]["screenshot"] == data["events"][1]["screenshot"]
    assert data["events"][2]["screenshot"] == data["events"][3]["screenshot"]
    assert data["events"][1]["screenshot"] != data["events"][2]["screenshot"]
    assert "private-password" not in json.dumps(data)
    assert counts == {"Planner": 1, "Executor": 1, "Reviewer": 1}


def test_host_paths_and_tokens_are_not_public():
    assert "private path omitted" in public_text("Open C:\\private\\notes.txt")
    assert "credential omitted" in public_text("sk-" + "a" * 40)
    assert "secret123" not in public_text("password: secret123")


def test_authentication_answers_are_omitted():
    assert public_exchange("Which recipient?", "Alice") == ("Which recipient?", "Alice")
    assert "123456" not in public_exchange("What is the verification code?", "123456")[1]
    assert "secret" not in public_exchange("请输入密码", "secret")[1]


def test_question_is_a_distinct_event_before_resumed_planning(tmp_path):
    episode = tmp_path / "episode"
    (episode / "artifacts").mkdir(parents=True)
    receipt = {"call_id": "ask", "round": 1, "action": {"text": "Which recipient?"}, "answer": "Alice"}
    (episode / "mobileworld-result.json").write_text(json.dumps({"steps": [], "human_interaction": {"dispatches": [receipt]}}))
    with sqlite3.connect(episode / "clickclick.db") as db:
        db.execute("create table agent_records (kind text, record_key text, payload_json text)")
        db.execute("create table traces (id integer, kind text, payload_json text, step_seq integer)")
        db.execute("insert into agent_records values ('human_input', 'q', ?)", (json.dumps({"call_id": "ask"}),))
        db.execute("insert into traces values (1, 'agent_tool_started', ?, 0)", (json.dumps({"call_id": "ask", "role": "planner", "name": "ask_user"}),))
        db.execute("insert into traces values (2, 'planner_decision', ?, 0)", (json.dumps({"decision": {"decision": "complete", "reason": "Answered"}}),))
    out = tmp_path / "public"
    out.mkdir()
    export_episode(episode, out, "Demo", "hitl")
    events = json.loads((out / "Demo-hitl.json").read_text())["events"]
    assert [e["kind"] for e in events] == ["Question", "Decision"]
    assert events[0]["question"] == "Which recipient?" and events[0]["answer"] == "Alice"
    assert events[0]["round"] == 1
