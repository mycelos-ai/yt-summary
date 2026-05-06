from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.youtube import VideoMetadata


def _meta(vid: str = "apivid12345") -> VideoMetadata:
    return VideoMetadata(
        id=vid,
        url=f"https://youtu.be/{vid}",
        title="API Test Video",
        description="d",
        duration_seconds=120,
        thumbnail_url=None,
    )


def test_post_videos_async_returns_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with (
        patch("app.services.api.fetch_metadata", AsyncMock(return_value=_meta())),
        patch("app.services.api.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/videos",
            json={"url": "https://youtu.be/apivid12345"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["video_id"] == "apivid12345"
    assert body["summary_ready"] is False
    assert body["kind"] == "youtube"


def test_post_videos_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/videos", json={"url": "ftp://x"})
    assert resp.status_code == 400


def test_get_video_returns_resource(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="getapi1", url="u", title="GotIt",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/getapi1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "getapi1"
    assert body["title"] == "GotIt"


def test_get_video_404(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/videos/nope")
    assert resp.status_code == 404


def test_list_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            for i in range(3):
                await videos_repo.upsert_metadata(
                    app.state.db, video_id=f"l{i}", url="u", title=f"T{i}",
                    description="", thumbnail_path=None, duration_seconds=None,
                )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body
    assert len(body["videos"]) == 2


def test_get_summary_404_when_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="nosum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/nosum/summary")
    assert resp.status_code == 404


def test_get_summary_returns_text_when_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="hassum", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await videos_repo.set_summary(
                app.state.db, "hassum", "## TL;DR\nyes", "model"
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/api/v1/videos/hassum/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert "TL;DR" in body["summary"]
    assert body["model"] == "model"


def test_reindex_returns_202(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="rev", url="u", title="X",
                description="", thumbnail_path=None, duration_seconds=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/api/v1/videos/rev/reindex")
    assert resp.status_code == 202
