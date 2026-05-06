import aiosqlite

from app.models import TranscriptSource
from app.repos import videos as videos_repo


async def _insert_sample(db: aiosqlite.Connection, vid: str = "abc123") -> None:
    await videos_repo.upsert_metadata(
        db,
        video_id=vid,
        url=f"https://youtu.be/{vid}",
        title="Hello",
        description="A nice video",
        thumbnail_path=None,
        duration_seconds=600,
    )


async def test_upsert_metadata_creates_row(db: aiosqlite.Connection):
    await _insert_sample(db)
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.title == "Hello"
    assert v.summary is None


async def test_upsert_metadata_idempotent_keeps_transcript(db: aiosqlite.Connection):
    await _insert_sample(db)
    await videos_repo.set_transcript(db, "abc123", "the words", TranscriptSource.AUTO_SUBS)
    await _insert_sample(db)  # second upsert, simulating re-submit
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.transcript == "the words"
    assert v.transcript_source is TranscriptSource.AUTO_SUBS


async def test_set_summary(db: aiosqlite.Connection):
    await _insert_sample(db)
    await videos_repo.set_summary(db, "abc123", "TL;DR", "openai/gpt-4o")
    v = await videos_repo.get(db, "abc123")
    assert v is not None
    assert v.summary == "TL;DR"
    assert v.summary_model == "openai/gpt-4o"


async def test_list_recent_orders_by_created_desc(db: aiosqlite.Connection):
    await _insert_sample(db, "a")
    await _insert_sample(db, "b")
    await _insert_sample(db, "c")
    rows = await videos_repo.list_recent(db, limit=10)
    ids = [v.id for v in rows]
    assert ids == ["c", "b", "a"]


async def test_search_uses_fts(db: aiosqlite.Connection):
    await videos_repo.upsert_metadata(
        db,
        video_id="x1",
        url="u",
        title="Python tutorial",
        description="learn fastapi",
        thumbnail_path=None,
        duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db,
        video_id="x2",
        url="u",
        title="Cooking pasta",
        description="italian food",
        thumbnail_path=None,
        duration_seconds=None,
    )
    results = await videos_repo.search(db, "fastapi")
    assert [v.id for v in results] == ["x1"]


async def test_get_returns_none_for_missing(db: aiosqlite.Connection):
    assert await videos_repo.get(db, "nope") is None


async def test_search_handles_fts_special_chars(db):
    await videos_repo.upsert_metadata(
        db, video_id="s1", url="u",
        title="Python tutorial", description="learn fastapi",
        thumbnail_path=None, duration_seconds=None,
    )
    # These would all crash a naive FTS5 MATCH
    for query in ["foo:bar", '"unterminated', "a OR b", "(parens)", "x + y"]:
        results = await videos_repo.search(db, query)
        assert isinstance(results, list)


async def test_search_phrase_match_still_works(db):
    await videos_repo.upsert_metadata(
        db, video_id="p1", url="u",
        title="Hello world", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    results = await videos_repo.search(db, "Hello")
    assert [v.id for v in results] == ["p1"]


async def test_upsert_metadata_uses_default_user_when_not_passed(db):
    await videos_repo.upsert_metadata(
        db, video_id="u1", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
    )
    cursor = await db.execute("SELECT user_id FROM videos WHERE id='u1'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_upsert_metadata_accepts_explicit_user_id(db):
    await videos_repo.upsert_metadata(
        db, video_id="u2", url="u", title="t",
        description="", thumbnail_path=None, duration_seconds=None,
        user_id=42,
    )
    cursor = await db.execute("SELECT user_id FROM videos WHERE id='u2'")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 42


def test_reciprocal_rank_fuse_scores_first_higher():
    from app.repos.videos import reciprocal_rank_fuse
    fused = reciprocal_rank_fuse(["a", "b", "c"], ["c", "a", "b"])
    # 'a' is rank 1 in list1 + rank 2 in list2 → top
    assert fused[0] == "a"


def test_reciprocal_rank_fuse_handles_disjoint():
    from app.repos.videos import reciprocal_rank_fuse
    fused = reciprocal_rank_fuse(["a", "b"], ["c", "d"])
    # All four appear, ordered by their reciprocal-rank score.
    assert sorted(fused) == ["a", "b", "c", "d"]


def test_reciprocal_rank_fuse_empty_inputs():
    from app.repos.videos import reciprocal_rank_fuse
    assert reciprocal_rank_fuse([], []) == []


async def test_search_with_vector_ids_fuses_results(db: aiosqlite.Connection):
    """Vector-only hits also surface even when FTS has no match."""
    await videos_repo.upsert_metadata(
        db, video_id="vfts", url="u", title="Has Python in title",
        description="learn fastapi", thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.upsert_metadata(
        db, video_id="vvec", url="u", title="Different Title",
        description="not in fts hit", thumbnail_path=None, duration_seconds=None,
    )
    rows = await videos_repo.search(
        db, "Python", vector_ids=["vvec"]
    )
    ids = {r.id for r in rows}
    assert "vfts" in ids  # FTS hit
    assert "vvec" in ids  # vector hit, even though FTS missed it
