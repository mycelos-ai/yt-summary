from unittest.mock import AsyncMock, patch

from app.config import Config
from app.repos import jobs as jobs_repo
from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo
from app.services.playlist import PlaylistEntry, PlaylistMetadata


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
