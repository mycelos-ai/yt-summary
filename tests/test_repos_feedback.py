import aiosqlite

from app.models import FeedbackSource, Sentiment
from app.repos import feedback as feedback_repo
from app.repos import videos as videos_repo


async def _video(db: aiosqlite.Connection, vid: str = "v1") -> None:
    await videos_repo.upsert_metadata(
        db, video_id=vid, url="u", title="t", description="",
        thumbnail_path=None, duration_seconds=None,
    )


async def test_create_and_list_for_video(db: aiosqlite.Connection):
    await _video(db)
    fb = await feedback_repo.create(
        db,
        user_id=1, video_id="v1",
        source=FeedbackSource.SUMMARY,
        selected_text="important point",
        text_offset_start=10, text_offset_end=25,
        sentiment=Sentiment.INTERESTING,
        comment=None,
    )
    assert fb.id > 0
    rows = await feedback_repo.list_for_video(db, video_id="v1", user_id=1)
    assert len(rows) == 1
    assert rows[0].selected_text == "important point"


async def test_list_for_video_scoped_per_user(db: aiosqlite.Connection):
    await _video(db)
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="a", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    await feedback_repo.create(
        db, user_id=2, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="b", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    assert len(await feedback_repo.list_for_video(db, video_id="v1", user_id=1)) == 1
    assert len(await feedback_repo.list_for_video(db, video_id="v1", user_id=2)) == 1


async def test_list_recent_for_user(db: aiosqlite.Connection):
    await _video(db)
    await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="x", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment="note",
    )
    rows = await feedback_repo.list_recent_for_user(db, user_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0].comment == "note"


async def test_delete(db: aiosqlite.Connection):
    await _video(db)
    fb = await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="x", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    deleted = await feedback_repo.delete(db, feedback_id=fb.id, user_id=1)
    assert deleted is True
    assert await feedback_repo.list_for_video(db, video_id="v1", user_id=1) == []


async def test_delete_rejects_cross_user(db: aiosqlite.Connection):
    await _video(db)
    fb = await feedback_repo.create(
        db, user_id=1, video_id="v1", source=FeedbackSource.SUMMARY,
        selected_text="x", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    deleted = await feedback_repo.delete(db, feedback_id=fb.id, user_id=2)
    assert deleted is False
    # Original feedback survives.
    assert len(await feedback_repo.list_for_video(db, video_id="v1", user_id=1)) == 1


async def _digest(db: aiosqlite.Connection, user_id: int = 1) -> int:
    from datetime import UTC, datetime, timedelta

    from app.repos import digests as digests_repo
    end = datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=24)
    d = await digests_repo.create_pending(
        db, user_id=user_id, period_start=start, period_end=end,
    )
    return d.id


async def test_create_for_digest_tldr_anchor(db: aiosqlite.Connection):
    """Feedback can anchor to a digest_id (TL;DR text) instead of a
    video_id, with source='digest_tldr'."""
    digest_id = await _digest(db)
    fb = await feedback_repo.create(
        db, user_id=1, digest_id=digest_id,
        source=FeedbackSource.DIGEST_TLDR,
        selected_text="the tldr line",
        text_offset_start=0, text_offset_end=13,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    assert fb.id > 0
    assert fb.video_id is None
    assert fb.digest_id == digest_id
    assert fb.source == FeedbackSource.DIGEST_TLDR


async def test_create_rejects_both_anchors(db: aiosqlite.Connection):
    import pytest
    digest_id = await _digest(db)
    await _video(db)
    with pytest.raises(ValueError, match="exactly one"):
        await feedback_repo.create(
            db, user_id=1, video_id="v1", digest_id=digest_id,
            source=FeedbackSource.DIGEST_TLDR,
            selected_text="x", text_offset_start=0, text_offset_end=1,
            sentiment=Sentiment.INTERESTING, comment=None,
        )


async def test_create_rejects_neither_anchor(db: aiosqlite.Connection):
    import pytest
    with pytest.raises(ValueError, match="exactly one"):
        await feedback_repo.create(
            db, user_id=1,
            source=FeedbackSource.DIGEST_TLDR,
            selected_text="x", text_offset_start=0, text_offset_end=1,
            sentiment=Sentiment.INTERESTING, comment=None,
        )


async def test_list_for_digest_returns_tldr_feedback(db: aiosqlite.Connection):
    digest_id = await _digest(db)
    await feedback_repo.create(
        db, user_id=1, digest_id=digest_id,
        source=FeedbackSource.DIGEST_TLDR,
        selected_text="A", text_offset_start=0, text_offset_end=1,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    rows = await feedback_repo.list_for_digest(
        db, digest_id=digest_id, user_id=1,
    )
    assert len(rows) == 1
    assert rows[0].digest_id == digest_id
    assert rows[0].video_id is None


async def test_list_for_digest_scoped_per_user(db: aiosqlite.Connection):
    # Two profiles, both feedback on the same digest_id (impossible in
    # practice since a digest belongs to one profile, but the repo
    # filter must still scope correctly).
    digest_id = await _digest(db, user_id=1)
    await feedback_repo.create(
        db, user_id=1, digest_id=digest_id,
        source=FeedbackSource.DIGEST_TLDR,
        selected_text="mine", text_offset_start=0, text_offset_end=4,
        sentiment=Sentiment.INTERESTING, comment=None,
    )
    rows1 = await feedback_repo.list_for_digest(
        db, digest_id=digest_id, user_id=1,
    )
    rows2 = await feedback_repo.list_for_digest(
        db, digest_id=digest_id, user_id=2,
    )
    assert len(rows1) == 1
    assert rows2 == []
