import asyncio

from fastapi.testclient import TestClient

from app.main import create_app
from app.repos import videos as videos_repo


def test_home_lists_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db,
                video_id="v1",
                url="https://youtu.be/v1",
                title="Test Video",
                description="d",
                thumbnail_path=None,
                duration_seconds=120,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Test Video" in resp.text


def test_home_search(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        async def setup():
            await videos_repo.upsert_metadata(
                app.state.db, video_id="a", url="u",
                title="Python tutorial", description="fastapi",
                thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="b", url="u",
                title="Cooking", description="pasta",
                thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/?q=fastapi")
    assert "Python tutorial" in resp.text
    assert "Cooking" not in resp.text
