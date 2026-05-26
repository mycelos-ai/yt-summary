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
    assert len(await feedback_repo.list_for_video(db, "v1", user_id=1)) == 1
    assert len(await feedback_repo.list_for_video(db, "v1", user_id=2)) == 1


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
    assert await feedback_repo.list_for_video(db, "v1", user_id=1) == []


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
    assert len(await feedback_repo.list_for_video(db, "v1", user_id=1)) == 1
