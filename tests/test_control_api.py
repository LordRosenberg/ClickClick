"""Control API: submit/status/failed/replay/trace/debug/skills."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient



@pytest.mark.asyncio
async def test_console_spa_deep_link_serves_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """React Router paths must fall back to index.html, not JSON 404."""
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLICKCLICK_USE_FIXTURE_DRIVER", "true")
    monkeypatch.setenv("CLICKCLICK_DRIVER_URL", "")

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>console</title><div id='root'></div>\n",
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("window.__console = 1;\n", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"ico")

    monkeypatch.setattr("control_api.main.DIST_DIR", dist)

    from control_api.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        assert root.status_code == 200
        assert "console" in root.text

        deep = await client.get(
            "/tasks/ea3a3a8c-5e53-4a9a-8301-3866970588c0"
        )
        assert deep.status_code == 200
        assert "console" in deep.text
        assert "text/html" in deep.headers["content-type"]

        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "window.__console" in asset.text

        favicon = await client.get("/favicon.ico")
        assert favicon.status_code == 200
        assert favicon.content == b"ico"

        missing_api = await client.get("/api/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.json()["detail"] == "Not Found"
