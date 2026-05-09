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


async def test_playlists_for_videos_returns_links_per_video(db: aiosqlite.Connection):
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await _make_video(db, "v3")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    await playlists_repo.link_video(db, "p2", "v2")

    result = await playlists_repo.playlists_for_videos(db, ["v1", "v2", "v3"])
    assert result["v1"] == [("p1", "My PL")]
    # v2 has both — order is by playlist title (both "My PL"), tiebreak undefined
    assert {p[0] for p in result["v2"]} == {"p1", "p2"}
    assert "v3" not in result


async def test_playlists_for_videos_returns_empty_for_empty_input(db):
    result = await playlists_repo.playlists_for_videos(db, [])
    assert result == {}


async def test_list_for_user_orders_recently_refreshed_first(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "p_old")
    await _make_playlist(db, "p_newer")
    # Force a future last_refreshed_at so the row sorts above the
    # other two (datetime('now') has 1-second resolution and the test
    # finishes in milliseconds).
    await db.execute(
        "UPDATE playlists SET last_refreshed_at='2099-01-01 00:00:00' "
        "WHERE id='p_newer'"
    )
    await db.commit()
    rows = await playlists_repo.list_for_user(db, 1)
    assert rows[0].id == "p_newer"


async def test_list_for_user_respects_limit(db: aiosqlite.Connection):
    for i in range(7):
        await _make_playlist(db, f"p{i}")
    rows = await playlists_repo.list_for_user(db, 1, limit=3)
    assert len(rows) == 3


async def test_list_with_stats_returns_video_counts(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "p1")
    await _make_playlist(db, "p2")
    await _make_video(db, "v1")
    await _make_video(db, "v2")
    await playlists_repo.link_video(db, "p1", "v1")
    await playlists_repo.link_video(db, "p1", "v2")
    rows = await playlists_repo.list_with_stats(db, 1)
    counts = {p.id: c for p, c in rows}
    assert counts == {"p1": 2, "p2": 0}


async def test_list_with_stats_orders_recently_refreshed_first(
    db: aiosqlite.Connection,
):
    await _make_playlist(db, "stats_old")
    await _make_playlist(db, "stats_new")
    await db.execute(
        "UPDATE playlists SET last_refreshed_at='2099-01-01 00:00:00' "
        "WHERE id='stats_new'"
    )
    await db.commit()
    rows = await playlists_repo.list_with_stats(db, 1)
    assert rows[0][0].id == "stats_new"


async def test_list_with_stats_empty_for_no_playlists(
    db: aiosqlite.Connection,
):
    assert await playlists_repo.list_with_stats(db, 1) == []
