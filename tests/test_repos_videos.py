import aiosqlite

from app.config import Config
from app.models import TranscriptSource
from app.repos import tts_jobs as tts_jobs_repo
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


async def test_list_recent_offset_skips_first_rows(db: aiosqlite.Connection):
    await _insert_sample(db, "a")
    await _insert_sample(db, "b")
    await _insert_sample(db, "c")
    # Newest first → c, b, a. offset=1 drops "c".
    rows = await videos_repo.list_recent(db, limit=10, offset=1)
    assert [v.id for v in rows] == ["b", "a"]


async def test_list_recent_offset_with_tag(db: aiosqlite.Connection):
    from app.repos import tags as tags_repo
    for vid in ["v1", "v2", "v3"]:
        await _insert_sample(db, vid)
        await tags_repo.set_tags_for_video(db, vid, ["python"])
    rows = await videos_repo.list_recent(db, limit=10, tag="python", offset=1)
    # Newest first within the tag → v3, v2, v1; offset=1 drops v3.
    assert [v.id for v in rows] == ["v2", "v1"]


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


async def test_deleting_video_removes_tts_jobs_and_files(
    db: aiosqlite.Connection, config: Config
):
    """videos_repo.delete must drop tts_jobs rows (via FK cascade) and
    unlink their on-disk MP3s — including cleaning up an empty
    per-video directory under tts-audio/."""
    await videos_repo.upsert_metadata(
        db,
        video_id="abc",
        url="https://yt/abc",
        title="T",
        description="",
        thumbnail_path=None,
        duration_seconds=60,
    )
    j = await tts_jobs_repo.enqueue(
        db, "abc", "summary", "de", "thorsten", "medium"
    )
    mp3 = config.tts_audio_dir / "abc" / "summary-de-thorsten-medium.mp3"
    mp3.parent.mkdir(parents=True)
    mp3.write_bytes(b"x")
    await tts_jobs_repo.complete(
        db,
        j.id,
        audio_path=str(mp3.relative_to(config.data_dir)),
        duration_seconds=10.0,
        translated_text=None,
    )

    await videos_repo.delete(db, "abc", data_dir=config.data_dir)

    rows = await tts_jobs_repo.list_for_video(db, "abc")
    assert rows == []
    assert not mp3.exists()
    # Empty per-video directory should also be cleaned up
    assert not (config.tts_audio_dir / "abc").exists()


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


async def test_set_and_get_highlights(db: aiosqlite.Connection):
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_highlights(db, "v1", '[{"text":"x","rank":1,"reason":"y"}]')
    got = await videos_repo.get_highlights(db, "v1")
    assert got == '[{"text":"x","rank":1,"reason":"y"}]'


async def test_get_highlights_returns_none_when_unset(db: aiosqlite.Connection):
    await videos_repo.upsert_metadata(
        db, video_id="v2", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    assert await videos_repo.get_highlights(db, "v2") is None


async def test_set_highlights_to_empty_array(db: aiosqlite.Connection):
    """An empty array means 'LLM had nothing noteworthy' — distinct from NULL."""
    await videos_repo.upsert_metadata(
        db, video_id="v3", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_highlights(db, "v3", "[]")
    assert await videos_repo.get_highlights(db, "v3") == "[]"


async def _seed(db, vid, *, user_id=1):
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title=f"T {vid}", description="",
        thumbnail_path=None, duration_seconds=None, user_id=user_id,
    )


async def test_set_archived_roundtrip(db):
    await _seed(db, "v1")
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    assert ok is True
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is not None
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=False)
    assert ok is True
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is None


async def test_set_archived_foreign_profile_returns_false(db):
    await db.execute("INSERT INTO users (id, name) VALUES (2, 'o')")
    await db.commit()
    await _seed(db, "v1", user_id=2)
    ok = await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    assert ok is False
    v = await videos_repo.get(db, "v1")
    assert v.archived_at is None


async def test_list_recent_excludes_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    rows = await videos_repo.list_recent(db, user_id=1)
    assert [v.id for v in rows] == ["v1"]


async def test_list_archived_returns_only_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    rows = await videos_repo.list_archived(db, user_id=1)
    assert [v.id for v in rows] == ["v2"]


async def test_count_archived(db):
    await _seed(db, "v1")
    await _seed(db, "v2")
    await videos_repo.set_archived(db, "v2", user_id=1, archived=True)
    assert await videos_repo.count_archived(db, user_id=1) == 1


async def test_search_fts_excludes_archived(db):
    await _seed(db, "v1")
    await videos_repo.set_archived(db, "v1", user_id=1, archived=True)
    ids = await videos_repo.search_fts(db, "T", user_id=1)
    assert "v1" not in ids


async def test_set_image_query_roundtrip(db):
    await videos_repo.upsert_metadata(
        db, video_id="v1", url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )
    await videos_repo.set_image_query(db, "v1", "mountain sunrise")
    v = await videos_repo.get(db, "v1")
    assert v.image_query == "mountain sunrise"
