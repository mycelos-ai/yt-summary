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


def test_home_lists_playlists(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            await playlists_repo.create(
                app.state.db, playlist_id="PLhome", user_id=1, url="u",
                title="On home", description="",
                thumbnail_path=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "On home" in resp.text
    assert "/p/PLhome" in resp.text
    assert "/playlists/new" in resp.text


def test_home_no_playlists_strip_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    # Without playlists, the strip with the cards isn't shown,
    # but the fallback "Add a playlist" link still appears.
    assert "Add a playlist" in resp.text
    assert 'class="playlist-strip"' not in resp.text


def test_home_shows_playlist_tags_on_video_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import playlists as playlists_repo
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vplt", url="u", title="VidWithPlaylist",
                description="", thumbnail_path=None, duration_seconds=None,
            )
            await playlists_repo.create(
                app.state.db, playlist_id="PLshown", user_id=1, url="u",
                title="MyAwesomePlaylist", description="",
                thumbnail_path=None,
            )
            await playlists_repo.link_video(app.state.db, "PLshown", "vplt")

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "MyAwesomePlaylist" in resp.text
    assert "/p/PLshown" in resp.text


def test_home_no_playlist_tags_when_video_unlinked(tmp_path, monkeypatch):
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))
    app = create_app()
    with TestClient(app) as client:
        import asyncio

        async def setup():
            from app.repos import videos as videos_repo
            await videos_repo.upsert_metadata(
                app.state.db, video_id="vsolo", url="u", title="Standalone",
                description="", thumbnail_path=None, duration_seconds=None,
            )

        asyncio.get_event_loop().run_until_complete(setup())
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Standalone" in resp.text
    # No playlist-tags container rendered for this card
    # (the class is global, but for this video specifically there should
    # be no entry to render)
    assert 'class="playlist-tag"' not in resp.text
