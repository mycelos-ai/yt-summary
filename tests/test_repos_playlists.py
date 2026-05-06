import aiosqlite

from app.repos import playlists as playlists_repo
from app.repos import videos as videos_repo


async def _make_playlist(db: aiosqlite.Connection, pid: str = "p1") -> None:
    await playlists_repo.create(
        db,
        playlist_id=pid,
        user_id=1,
        url=f"https://youtube.com/playlist?list={pid}",
        title="My PL",
        description="",
        thumbnail_path=None,
    )


async def _make_video(db: aiosqlite.Connection, vid: str) -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
    )


async def test_create_and_get(db: aiosqlite.Connection):
    await _make_playlist(db)
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.id == "p1"
    assert p.user_id == 1
    assert p.title == "My PL"
    assert p.last_refreshed_at is None


async def test_create_is_idempotent(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_playlist(db)  # second call should not raise
    p = await playlists_repo.get(db, "p1")
    assert p is not None


async def test_list_for_user(db: aiosqlite.Connection):
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    rows = await playlists_repo.list_for_user(db, 1)
    ids = sorted(p.id for p in rows)
    assert ids == ["p1", "p2"]


async def test_list_for_user_returns_empty_for_other_user(db: aiosqlite.Connection):
    await _make_playlist(db, "p1")
    assert await playlists_repo.list_for_user(db, 99) == []


async def test_delete_cascades_to_links(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.delete(db, "p1")
    # link is gone via CASCADE
    cursor = await db.execute("SELECT COUNT(*) FROM playlist_videos")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_link_video_returns_true_when_new(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    assert await playlists_repo.link_video(db, "p1", "v1") is True


async def test_link_video_returns_false_when_already_linked(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await playlists_repo.link_video(db, "p1", "v1")
    assert await playlists_repo.link_video(db, "p1", "v1") is False


async def test_linked_video_ids(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    ids = await playlists_repo.linked_video_ids(db, "p1")
    assert ids == {"v1", "v2"}


async def test_videos_for_playlist_orders_recent_first(db: aiosqlite.Connection):
    await _make_playlist(db)
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    rows = await playlists_repo.videos_for_playlist(db, "p1")
    # v2 was linked second → most recent → comes first
    assert [v.id for v in rows] == ["v2", "v1"]


async def test_set_last_refreshed(db: aiosqlite.Connection):
    await _make_playlist(db)
    await playlists_repo.set_last_refreshed(db, "p1")
    p = await playlists_repo.get(db, "p1")
    assert p is not None
    assert p.last_refreshed_at is not None
