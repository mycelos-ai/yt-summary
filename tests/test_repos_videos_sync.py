"""Tests for the incremental-sync query (repos/videos.list_updated_since).

Ordering, cursor resumption, profile scoping and archive exclusion.
The cursor is a raw "<updated_at>|<id>" pair; see the plan's note on
the space-vs-T timestamp hazard.
"""

from app.repos import videos as videos_repo


async def _add(db, vid, *, ts, user_id=1, archived=False):
    """Insert one video and force its updated_at to a known value."""
    await videos_repo.upsert_metadata(
        db, video_id=vid, url=f"https://youtu.be/{vid}", title=vid,
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=user_id,
    )
    await db.execute(
        "UPDATE videos SET updated_at=?, archived_at=? WHERE id=?",
        (ts, "2026-01-01 00:00:00" if archived else None, vid),
    )
    await db.commit()


async def test_orders_by_updated_at_then_id(db):
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:a", "1:b", "1:c"]


async def test_since_is_inclusive_of_the_boundary(db):
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    rows = await videos_repo.list_updated_since(
        db, user_id=1, since="2026-01-02 10:00:00", limit=10,
    )
    assert [v.id for v in rows] == ["1:b"]


async def test_since_accepts_iso_t_separator(db):
    """A caller sending ISO-8601 with `T` must not silently lose rows.

    SQLite stores '2026-01-02 10:00:00' (space). Since ' ' < 'T',
    comparing a raw T-string in SQL would skip the boundary row.
    """
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    rows = await videos_repo.list_updated_since(
        db, user_id=1, since="2026-01-02T10:00:00Z", limit=10,
    )
    assert [v.id for v in rows] == ["1:b"]


BOUNDARY_INPUTS = [
    "2026-01-02 10:00:00",        # already normalized
    "2026-01-02T10:00:00Z",       # ISO-8601 with uppercase T/Z
    "2026-01-02t10:00:00z",       # lowercase t/z, legal per RFC 3339 5.6
    "2026-01-02T10:00:00.123456",  # datetime.now().isoformat()
    "2026-01-02T10:00:00+02:00",  # timezone-aware client
]


async def test_since_returns_boundary_row_for_every_iso_shape(db):
    """Every legal ISO-8601 spelling of the same second must behave alike.

    The bound is compared as a string in SQL, so an un-normalized
    variant silently drops the boundary row instead of erroring.
    Asserting on returned ids, not on the normalized string: the rows
    are the contract, the string is an implementation detail.
    """
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    for since in BOUNDARY_INPUTS:
        rows = await videos_repo.list_updated_since(
            db, user_id=1, since=since, limit=10,
        )
        assert [v.id for v in rows] == ["1:b", "1:c"], f"lost a row for {since!r}"


async def test_cursor_accepts_every_iso_shape(db):
    """A cursor stamp goes through the same normalization as `since`."""
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    for stamp in BOUNDARY_INPUTS:
        rows = await videos_repo.list_updated_since(
            db, user_id=1, cursor=f"{stamp}|1:b", limit=10,
        )
        assert [v.id for v in rows] == ["1:c"], f"wrong resume for {stamp!r}"


def test_normalize_ts_edge_cases():
    """Empty input means "no bound"; odd input passes through unchanged."""
    assert videos_repo._normalize_ts(None) is None
    assert videos_repo._normalize_ts("") is None
    assert videos_repo._normalize_ts("   ") is None
    # A bare date is a valid prefix bound and must survive its hyphens.
    assert videos_repo._normalize_ts("2026-01-02") == "2026-01-02"
    # Garbage is not an error: it yields no rows, not an exception.
    assert videos_repo._normalize_ts("garbage") == "garbage"
    assert videos_repo._normalize_ts("  2026-01-02T10:00:00Z  ") == (
        "2026-01-02 10:00:00"
    )
    # A negative offset must not be confused with the date's hyphens.
    assert videos_repo._normalize_ts("2026-01-02T10:00:00-05:00") == (
        "2026-01-02 10:00:00"
    )


async def test_cursor_resumes_exactly(db):
    await _add(db, "1:a", ts="2026-01-01 10:00:00")
    await _add(db, "1:b", ts="2026-01-02 10:00:00")
    await _add(db, "1:c", ts="2026-01-03 10:00:00")
    first = await videos_repo.list_updated_since(db, user_id=1, limit=2)
    assert [v.id for v in first] == ["1:a", "1:b"]
    cursor = videos_repo.make_cursor(first[-1])
    rest = await videos_repo.list_updated_since(
        db, user_id=1, cursor=cursor, limit=2,
    )
    assert [v.id for v in rest] == ["1:c"]


async def test_cursor_handles_shared_updated_at(db):
    """Two items on the same timestamp must not be skipped or repeated."""
    same = "2026-01-01 10:00:00"
    await _add(db, "1:a", ts=same)
    await _add(db, "1:b", ts=same)
    await _add(db, "1:c", ts=same)
    first = await videos_repo.list_updated_since(db, user_id=1, limit=2)
    assert [v.id for v in first] == ["1:a", "1:b"]
    cursor = videos_repo.make_cursor(first[-1])
    rest = await videos_repo.list_updated_since(
        db, user_id=1, cursor=cursor, limit=2,
    )
    assert [v.id for v in rest] == ["1:c"]


async def test_scopes_to_the_requesting_profile(db):
    await _add(db, "1:mine", ts="2026-01-01 10:00:00", user_id=1)
    await _add(db, "2:theirs", ts="2026-01-02 10:00:00", user_id=2)
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:mine"]


async def test_excludes_archived_items(db):
    await _add(db, "1:live", ts="2026-01-01 10:00:00")
    await _add(db, "1:gone", ts="2026-01-02 10:00:00", archived=True)
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=10)
    assert [v.id for v in rows] == ["1:live"]


async def test_limit_bounds_the_page(db):
    for i in range(5):
        await _add(db, f"1:v{i}", ts=f"2026-01-0{i + 1} 10:00:00")
    rows = await videos_repo.list_updated_since(db, user_id=1, limit=3)
    assert len(rows) == 3
