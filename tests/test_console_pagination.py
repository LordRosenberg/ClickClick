"""Page only task summaries, preserving global order and source ownership."""
import pytest
from httpx import ASGITransport, AsyncClient

from control_api.data_sources import ConsoleDataSources
from shared.artifacts import ArtifactStore
from shared.db import Database
from shared.schemas import AgentState, TaskStatus


def test_global_pages_read_only_selected_summaries(tmp_path, monkeypatch):
    primary = Database(tmp_path / "clickclick.db")
    external = Database(tmp_path / "run" / "clickclick.db")
    sources = ConsoleDataSources(primary, ArtifactStore(tmp_path / "artifacts"), temp_root=None)
    created = []
    for i in range(25):
        db = primary if i % 2 else external
        task = db.create_task(str(i), AgentState(instruction=str(i), step_number=i))
        created.append(task)
    external.close()
    # Discovery and pagination must never use the old full-task listing path.
    monkeypatch.setattr(Database, "list_tasks", lambda *a, **k: pytest.fail("read all task states"))
    calls = []
    original = Database.get_task_summary
    def counted(db, task_id):
        calls.append(task_id)
        return original(db, task_id)
    monkeypatch.setattr(Database, "get_task_summary", counted)
    try:
        rows, total, page = sources.task_page(page=2, page_size=10)
        expected = sorted(created, key=lambda t: (t.created_at, t.id), reverse=True)[10:20]
        assert [t.id for t, _ in rows] == [t.id for t in expected]
        assert (total, page, len(calls)) == (25, 2, 10)
        assert all(t.state is None for t, _ in rows)
        assert any(source.read_only for _, source in rows)
        calls.clear()
        rows, total, page = sources.task_page(page=100, page_size=20)
        assert (len(rows), total, page, len(calls)) == (5, 25, 2, 5)
    finally:
        sources.close()
        primary.close()


def test_primary_collision_wins_before_status_filter(tmp_path):
    primary = Database(tmp_path / "clickclick.db")
    task = primary.create_task("shared", AgentState(instruction="shared"))
    primary.update_task(task.id, status=TaskStatus.FAILED)
    # Use SQLite backup so WAL contents are included in the duplicate run.
    external = Database(tmp_path / "run" / "clickclick.db")
    primary._conn.backup(external._conn)
    external.close()
    primary.update_task(task.id, status=TaskStatus.SUCCEEDED)
    sources = ConsoleDataSources(primary, ArtifactStore(tmp_path / "artifacts"), temp_root=None)
    try:
        assert sources.task_page(page=1, page_size=10, status=TaskStatus.FAILED) == ([], 0, 1)
        rows, total, _ = sources.task_page(page=1, page_size=10)
        assert total == 1 and rows[0][1] is sources.primary
        primary.update_task(task.id, status=TaskStatus.FAILED)
        assert sources.task_page(page=1, page_size=10, status=TaskStatus.FAILED)[1] == 1
    finally:
        sources.close()
        primary.close()


@pytest.mark.asyncio
async def test_page_api_validation_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    db = app.state.data_sources.primary.db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = (await client.get("/api/tasks/page")).json()
            assert first["items"] == [] and first["total"] == 0
            assert first["page"] == 1 and first["page_size"] == 10
            assert isinstance(first["indexing"], bool)
            task = db.create_task("latest", AgentState(instruction="latest", step_number=3))
            response = await client.get("/api/tasks/page?page=2&page_size=20")
            assert response.status_code == 200
            payload = response.json()
            assert (payload["page"], payload["total"], payload["page_size"]) == (1, 1, 20)
            item = payload["items"][0]
            assert item["id"] == task.id and item["step_number"] == 3
            assert "state" not in item and "plan" not in item
            assert "execution_elapsed_ms" in item and item["data_source"] == "primary"
            assert (await client.get("/api/tasks/page?status=failed")).json()["total"] == 0
            for query in ("page=0", "page_size=15", "page_size=100", "status=invalid"):
                assert (await client.get("/api/tasks/page?" + query)).status_code == 422
    finally:
        app.state.data_sources.close()
        db.close()


@pytest.mark.asyncio
async def test_slow_index_does_not_block_http_and_details_remain_lazy(tmp_path, monkeypatch):
    import asyncio
    import threading
    monkeypatch.setenv("CLICKCLICK_DATA_DIR", str(tmp_path))
    from control_api.main import create_app
    app = create_app(include_temp_runs=False)
    sources = app.state.data_sources
    external = Database(tmp_path / "run" / "clickclick.db")
    task = external.create_task("external", AgentState(instruction="external"))
    external.close()
    started = threading.Event()
    release = threading.Event()
    def discover():
        started.set()
        release.wait(timeout=5)
        return {external.path.resolve()}
    monkeypatch.setattr(sources, "_discover_paths", discover)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            page = await asyncio.wait_for(client.get("/api/tasks/page"), timeout=0.5)
            assert page.json()["indexing"] is True and started.wait(timeout=0.1)
            # Check API responsiveness without requiring a built Console or a device.
            assert (await asyncio.wait_for(client.get("/api/models"), timeout=0.5)).status_code == 200
            # The blocked worker has not forced any external detail connections.
            assert sources._external == {}
            release.set()
            await asyncio.to_thread(sources._index_thread.join, 3)
            assert sources._index_ready and sources._external == {}
            page = (await client.get("/api/tasks/page")).json()
            assert page["indexing"] is False
            assert page["items"][0]["id"] == task.id
            assert len(sources._external) == 1
            monkeypatch.setattr(sources, "_discover_paths", lambda: pytest.fail("detail caused scan"))
            assert (await client.get(f"/api/tasks/{task.id}")).status_code == 200
    finally:
        release.set()
        sources.close()
        sources.primary.db.close()


def test_background_index_reuses_unchanged_headers_and_tracks_wal_updates(tmp_path, monkeypatch):
    primary = Database(tmp_path / "clickclick.db")
    external = Database(tmp_path / "run" / "clickclick.db")
    task = external.create_task("live", AgentState(instruction="live"))
    sources = ConsoleDataSources(primary, ArtifactStore(tmp_path / "artifacts"), temp_root=None)
    calls = []
    original = Database.task_headers
    def counted(db):
        calls.append(db.path)
        return original(db)
    monkeypatch.setattr(Database, "task_headers", counted)
    try:
        sources._update_index()
        assert len(calls) == 1
        sources._update_index()
        assert len(calls) == 1
        external.update_task(task.id, status=TaskStatus.SUCCEEDED)
        sources._update_index()
        assert len(calls) == 2
        assert sources._index_headers[external.path.resolve()][0]["status"] == "succeeded"
        sources.task_page(page=1, page_size=10, background=True)
        assert task.id in sources._task_sources
        monkeypatch.setattr(sources, "_discover_paths", lambda: set())
        sources._update_index()
        sources.task_page(page=1, page_size=10, background=True)
        assert task.id not in sources._task_sources
        assert sources.locate(task.id) is None
    finally:
        sources.close()
        external.close()
        primary.close()
