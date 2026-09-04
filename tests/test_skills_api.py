"""Skill API: filesystem list + mutation + pending + skill-links."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_skill_filesystem_list_and_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_ENABLE_SKILL_MINER", "false")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))

    skills = tmp_path / "skills" / "generic" / "seed"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: generic_seed\ndescription: seed\nversion: 0.1.0\nkind: generic\ntags: [t]\n---\n\nseed\n",
        encoding="utf-8",
    )

    from control_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/skills")
        assert listed.status_code == 200
        rows = listed.json()
        assert isinstance(rows, list)
        assert any(s.get("id") == "generic_seed" for s in rows)

        got = await client.get("/api/skills/generic_seed")
        assert got.status_code == 200
        body = got.json()
        assert body["id"] == "generic_seed"
        assert "seed" in body["body"]

        missing = await client.get("/api/skills/no-such-skill-xyz")
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_skill_crud_and_path_jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_ENABLE_SKILL_MINER", "false")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills").mkdir()

    from control_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/skills",
            json={
                "id": "generic_api",
                "description": "api",
                "tags": ["x"],
                "triggers": ["go"],
                "body": "plan\n\n## Executor notes\nhint\n",
            },
        )
        assert created.status_code == 200
        assert created.json()["id"] == "generic_api"

        conflict = await client.post(
            "/api/skills",
            json={"id": "generic_api", "body": "x"},
        )
        assert conflict.status_code == 409

        updated = await client.put(
            "/api/skills/generic_api",
            json={"body": "plan2"},
        )
        assert updated.status_code == 200
        assert "plan2" in updated.json()["body"]

        bad = await client.post(
            "/api/skills",
            json={"id": "../escape", "body": "x"},
        )
        assert bad.status_code == 400

        deleted = await client.delete("/api/skills/generic_api")
        assert deleted.status_code == 200
        assert (await client.get("/api/skills/generic_api")).status_code == 404


@pytest.mark.asyncio
async def test_pending_approve_and_learn_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_ENABLE_SKILL_MINER", "false")
    monkeypatch.setenv("CLICKCLICK_SKILLS_DIR", str(tmp_path / "skills"))
    (tmp_path / "skills" / "apps" / "com.demo").mkdir(parents=True)
    (tmp_path / "skills" / "apps" / "com.demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\nversion: 0.1.0\napp: com.demo\n---\n\nold\n",
        encoding="utf-8",
    )

    from agent.skills.pending import stage_pending_patch
    from control_api.main import create_app

    stage_pending_patch(
        target_rel="apps/com.demo/SKILL.md",
        new_text="---\nname: demo\ndescription: d\nversion: 0.1.1\napp: com.demo\n---\n\nnew\n",
        gist="test",
        root=tmp_path / "skills",
        old_text="old",
    )

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/skills/pending")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        pid = listed.json()[0]["id"]

        diff = await client.get(f"/api/skills/pending/{pid}")
        assert diff.status_code == 200
        assert "new" in diff.json()["new_text"]

        approved = await client.post(f"/api/skills/pending/{pid}/approve")
        assert approved.status_code == 200
        body = (tmp_path / "skills" / "apps" / "com.demo" / "SKILL.md").read_text(
            encoding="utf-8",
        )
        assert "new" in body

        # learn endpoint exists (may skip if model fails — force skip via monkeypatch)
        from agent.skills import learner as learner_mod

        async def fake_learn(task, **kw):
            return {"ok": True, "skipped": True, "reason": "test"}

        monkeypatch.setattr(learner_mod, "run_skill_learner", fake_learn)
        task = app.state.db.create_task("learn me")
        learned = await client.post(f"/api/tasks/{task.id}/learn")
        assert learned.status_code == 200
        assert learned.json().get("ok")
