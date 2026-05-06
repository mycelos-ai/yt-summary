from fastapi.testclient import TestClient

from app.main import create_app


def test_search_returns_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="searchhit", url="u",
                title="Python tutorial",
                description="learn fastapi",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/search?q=fastapi")
    assert resp.status_code == 200
    body = resp.json()
    assert "hits" in body
    assert any(h["id"] == "searchhit" for h in body["hits"])


def test_search_requires_query(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/search")
    assert resp.status_code in (400, 422)
