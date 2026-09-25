"""Skill API: filesystem list + mutation + pending + skill-links."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_system_scope_api_validation_preserves_valid_file(tmp_path, monkeypatch):
    monkeypatch.setenv('CLICKCLICK_DATA_DIR', str(tmp_path/'data'))
    monkeypatch.setenv('CLICKCLICK_SKILLS_DIR', str(tmp_path/'skills'))
    from control_api.main import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url='http://test') as client:
        payload = {'id': 'system-picker', 'kind': 'workflow', 'app': 'com.example.mail',
                   'description': 'Attach using system picker', 'capability': 'attach_file',
                   'interface_scope': 'system',
                   'body': '## Procedure\nInspect the picker.\n## Verification\nCheck the attachment.'}
        assert (await client.post('/api/skills', json=payload)).status_code == 400
        payload['device_profiles'] = ['mobileworld_api34']
        created = await client.post('/api/skills', json=payload)
        assert created.status_code == 200
        invalid = await client.put('/api/skills/system-picker', json={'device_profiles': []})
        assert invalid.status_code == 400
        updated = await client.put('/api/skills/system-picker', json={'description': 'Updated'})
        assert updated.status_code == 200
        assert updated.json()['frontmatter']['interface_scope'] == 'system'
        assert updated.json()['frontmatter']['device_profiles'] == ['mobileworld_api34']


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
