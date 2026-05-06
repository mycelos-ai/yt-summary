from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.playlist import PlaylistEntry, PlaylistMetadata


def _meta(plid: str = "PLtest", entries: list[PlaylistEntry] | None = None) -> PlaylistMetadata:
    return PlaylistMetadata(
        id=plid,
        url=f"https://youtube.com/playlist?list={plid}",
        title="Test playlist",
        description="",
        thumbnail_url=None,
        entries=entries or [],
    )


def test_post_playlists_imports_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta(
        entries=[
            PlaylistEntry(
                id=f"v{i:010d}", title=f"v{i}", description="",
                thumbnail_url=None, duration_seconds=None,
            )
            for i in range(3)
        ]
    )
    app = create_app()
    with (
        patch("app.routes.playlists.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        patch("app.routes.playlists.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        resp = client.post(
            "/playlists",
            data={"url": "https://www.youtube.com/playlist?list=PLtest"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLtest"


def test_post_playlists_invalid_url_returns_400(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/playlists", data={"url": "not-a-url"})
    assert resp.status_code == 400


def test_get_playlist_detail_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLshow", user_id=1,
                url="u", title="Show me", description="",
                thumbnail_path=None,
            )
            await videos_repo.upsert_metadata(
                app.state.db, video_id="v1show", url="u", title="Inner",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.link_video(app.state.db, "PLshow", "v1show")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/p/PLshow")
    assert resp.status_code == 200
    assert "Show me" in resp.text
    assert "Inner" in resp.text


def test_get_playlist_404_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/p/PLnope")
    assert resp.status_code == 404


def test_post_playlist_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    fake = _meta("PLref")
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLref", user_id=1,
                url="https://youtube.com/playlist?list=PLref",
                title="r", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLref/refresh", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLref"


def test_post_playlist_load_older(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    entries = [
        PlaylistEntry(
            id=f"v{i:010d}", title="t", description="",
            thumbnail_url=None, duration_seconds=None,
        )
        for i in range(10)
    ]
    fake = _meta("PLold", entries=entries)
    app = create_app()
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=fake)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
        TestClient(app) as client,
    ):
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLold", user_id=1,
                url="https://youtube.com/playlist?list=PLold",
                title="o", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLold/load-older", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/p/PLold"


def test_post_playlist_remove_deletes(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLgone", user_id=1, url="u",
                title="x", description="", thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.post("/p/PLgone/remove", follow_redirects=False)
        assert resp.status_code == 303

        async def check():
            from app.repos import playlists as playlists_repo
            assert await playlists_repo.get(app.state.db, "PLgone") is None

        asyncio.get_event_loop().run_until_complete(check())


def test_get_new_playlist_form(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/playlists/new")
    assert resp.status_code == 200
    assert 'name="url"' in resp.text
    assert 'action="/playlists"' in resp.text
