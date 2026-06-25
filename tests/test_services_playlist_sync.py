from unittest.mock import AsyncMock, patch

from app.config import Config
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import settings as settings_repo
from app.repos import videos as videos_repo
from app.services import playlist_sync
from app.services.playlist import PlaylistEntry, PlaylistMetadata
from app.services.playlist_index import PlaylistApiError


def _meta(plid: str = "p1", entries: list[PlaylistEntry] | None = None) -> PlaylistMetadata:
    return PlaylistMetadata(
        id=plid,
        url=f"https://youtube.com/playlist?list={plid}",
        title="PL",
        description="",
        thumbnail_url=None,
        entries=entries or [],
    )


def _entry(vid: str, title: str = "x", position: int = 1) -> PlaylistEntry:
    return PlaylistEntry(
        id=vid,
        title=title,
        description="",
        thumbnail_url=None,
        duration_seconds=None,
        position=position,
    )


async def test_sync_creates_videos_and_links_them(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )

    meta = _meta(entries=[_entry("vid_aaaaaaaa1"), _entry("vid_bbbbbbbb2")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.total_in_playlist == 2
    assert result.newly_linked == 2
    assert result.newly_enqueued == 2
    # Both videos exist
    assert await videos_repo.get(db, "vid_aaaaaaaa1") is not None
    assert await videos_repo.get(db, "vid_bbbbbbbb2") is not None
    # Both videos linked
    linked = await playlists_repo.linked_video_ids(db, "p1")
    assert linked == {"vid_aaaaaaaa1", "vid_bbbbbbbb2"}
    # last_refreshed_at populated
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.last_refreshed_at is not None


async def test_sync_skips_already_linked_videos(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    # Pre-link an existing video
    await videos_repo.upsert_metadata(
        db, video_id="vid_old1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await playlists_repo.link_video(db, "p1", "vid_old1")

    meta = _meta(entries=[_entry("vid_old1"), _entry("vid_new1")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.newly_linked == 1
    assert result.newly_enqueued == 1


async def test_sync_does_not_enqueue_video_with_summary(db, tmp_path):
    """A video that already has a summary (e.g. imported earlier as a
    standalone video) only gets the playlist link, no new job."""
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="vid_done1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_summary(db, "vid_done1", "summary text", "model")

    meta = _meta(entries=[_entry("vid_done1")])
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1")

    assert result.newly_linked == 1
    assert result.newly_enqueued == 0
    # No job was created
    job = await jobs_repo.latest_for_video(db, "vid_done1")
    assert job is None


async def test_sync_respects_initial_limit(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    entries = [_entry(f"v_{i:08d}") for i in range(50)]
    meta = _meta(entries=entries)
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import sync_playlist
        result = await sync_playlist(db, config, "p1", initial_limit=20)

    assert result.total_in_playlist == 50
    assert result.newly_linked == 20
    assert result.newly_enqueued == 20


async def test_load_older_takes_unlinked_entries(db, tmp_path):
    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="PL",
        description="", thumbnail_path=None,
    )
    # Pre-link first 3 entries
    for i in range(3):
        await videos_repo.upsert_metadata(
            db, video_id=f"v_{i:08d}", url="u", title="t",
            description="", thumbnail_path=None, duration_seconds=None,
        )
        await playlists_repo.link_video(db, "p1", f"v_{i:08d}")

    entries = [_entry(f"v_{i:08d}") for i in range(10)]
    meta = _meta(entries=entries)
    with (
        patch("app.services.playlist_sync.fetch_playlist", AsyncMock(return_value=meta)),
        patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)),
    ):
        from app.services.playlist_sync import load_older_videos
        result = await load_older_videos(db, config, "p1", count=5)

    # 5 of the unlinked 7 get added
    assert result.newly_linked == 5
    assert result.newly_enqueued == 5


async def test_sync_raises_for_unknown_playlist(db, tmp_path):
    import pytest

    from app.services.playlist_sync import sync_playlist

    config = Config(data_dir=tmp_path)
    config.ensure_dirs()
    with pytest.raises(KeyError):
        await sync_playlist(db, config, "unknown_id")


async def test_process_entries_writes_positions(db, tmp_path):
    from unittest.mock import AsyncMock, patch

    from app.services.playlist_sync import _process_entries

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="T",
        description="", thumbnail_path=None,
    )
    entries = [_entry("a", position=1), _entry("b", position=2)]
    with patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)):
        result = await _process_entries(db, cfg, "p1", 1, entries)
    assert result.newly_linked == 2
    cur = await db.execute(
        "SELECT video_id, position FROM playlist_videos "
        "WHERE playlist_id='p1' ORDER BY position"
    )
    assert [tuple(r) async for r in cur] == [("a", 1), ("b", 2)]


async def test_process_entries_reprocess_updates_position_no_reenqueue(db, tmp_path):
    from unittest.mock import AsyncMock, patch

    from app.services.playlist_sync import _process_entries

    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="u", title="T",
        description="", thumbnail_path=None,
    )
    with patch("app.services.playlist_sync.download_thumbnail", AsyncMock(return_value=None)):
        await _process_entries(db, cfg, "p1", 1, [_entry("a", position=1)])
        # Re-process the same video at a new position: no new link, no re-enqueue.
        result = await _process_entries(db, cfg, "p1", 1, [_entry("a", position=5)])
    assert result.newly_linked == 0
    assert result.newly_enqueued == 0
    cur = await db.execute(
        "SELECT position FROM playlist_videos WHERE playlist_id='p1' AND video_id='a'"
    )
    assert (await cur.fetchone())[0] == 5


# ── _index_playlist tests ────────────────────────────────────────────────────


def _meta_src(source: str) -> PlaylistMetadata:
    return PlaylistMetadata(
        id="PLx", url="u", title=source, description="",
        thumbnail_url=None, entries=[],
    )


async def _make_pl(db):
    await playlists_repo.create(
        db, playlist_id="p1", user_id=1, url="https://youtube.com/playlist?list=PLx",
        title="T", description="", thumbnail_path=None,
    )
    return await playlists_repo.get(db, "p1")


async def test_index_playlist_uses_api_when_key_set(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    pl = await _make_pl(db)
    await settings_repo.set_for_user(db, 1, "youtube_api_key", "KEY")
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(return_value=_meta_src("api")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta_src("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "api"


async def test_index_playlist_falls_back_when_no_key(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    pl = await _make_pl(db)
    # no youtube_api_key set
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(return_value=_meta_src("api")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta_src("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "ytdlp"


async def test_index_playlist_falls_back_on_api_error(db, tmp_path, monkeypatch):
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    pl = await _make_pl(db)
    await settings_repo.set_for_user(db, 1, "youtube_api_key", "KEY")
    monkeypatch.setattr(
        playlist_sync.playlist_index, "fetch_via_api",
        AsyncMock(side_effect=PlaylistApiError("boom")),
    )
    monkeypatch.setattr(
        playlist_sync, "fetch_playlist",
        AsyncMock(return_value=_meta_src("ytdlp")),
    )
    meta = await playlist_sync._index_playlist(db, cfg, pl)
    assert meta.title == "ytdlp"


async def test_index_playlist_uses_api_for_non_default_user(db, tmp_path, monkeypatch):
    """I1: youtube_api_key is saved household-global (user 1), but must be
    respected for playlists owned by other users too. Before the fix,
    get_for_user(db, user_id=2, ...) returned None and the feature was silently
    disabled for any profile other than user 1."""
    from app.repos import users as users_repo
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()

    # Create a second user and a playlist owned by that user.
    second_user = await users_repo.create(db, name="other")
    user2_id = second_user.id

    await playlists_repo.create(
        db, playlist_id="p2", user_id=user2_id,
        url="https://youtube.com/playlist?list=PLx",
        title="T", description="", thumbnail_path=None,
    )
    pl2 = await playlists_repo.get(db, "p2")
    assert pl2 is not None

    # Save the api key household-globally (as the settings route does).
    await settings_repo.set(db, "youtube_api_key", "KEY")

    mock_api = AsyncMock(return_value=_meta_src("api"))
    mock_ytdlp = AsyncMock(return_value=_meta_src("ytdlp"))
    monkeypatch.setattr(playlist_sync.playlist_index, "fetch_via_api", mock_api)
    monkeypatch.setattr(playlist_sync, "fetch_playlist", mock_ytdlp)

    meta = await playlist_sync._index_playlist(db, cfg, pl2)
    # With the fix: should use the API path, not yt-dlp.
    assert meta.title == "api", (
        "Expected API path to be taken for user_id != 1; "
        "got yt-dlp path — household-global key was not read correctly"
    )
