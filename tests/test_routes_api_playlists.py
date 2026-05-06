from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.playlist import PlaylistMetadata


def _meta(plid="PLapi"):
    return PlaylistMetadata(
        id=plid, url=f"https://youtube.com/playlist?list={plid}",
        title="API Playlist", description="", thumbnail_url=None,
        entries=[],
    )


def test_list_playlists_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/playlists")
    assert resp.status_code == 200
    assert resp.json()["playlists"] == []


def test_create_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    fake = _meta()
    with (
        patch("app.routes.api.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        patch("app.routes.api.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/api/v1/playlists",
            json={"url": "https://www.youtube.com/playlist?list=PLapi"},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "PLapi"


def test_remove_playlist(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio
        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLrem", user_id=1,
                url="u", title="X", description="", thumbnail_path=None,
            )
        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.delete("/api/v1/playlists/PLrem")
    assert resp.status_code == 200
    assert resp.json() == {"removed": True}
