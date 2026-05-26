"""CRUD for the `feedback` table.

A feedback row is one user highlighting a span of text in a summary,
transcript, or digest and marking it interesting / not interesting,
optionally with a comment. Scoped strictly by `user_id` (Profile).
"""
from datetime import datetime

import aiosqlite

from app.models import Feedback, FeedbackSource, Sentiment


def _row_to_feedback(row: aiosqlite.Row) -> Feedback:
    return Feedback(
        id=row["id"],
        user_id=row["user_id"],
        video_id=row["video_id"],
        source=FeedbackSource(row["source"]),
        selected_text=row["selected_text"],
        text_offset_start=row["text_offset_start"],
        text_offset_end=row["text_offset_end"],
        sentiment=Sentiment(row["sentiment"]),
        comment=row["comment"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    video_id: str,
    source: FeedbackSource,
    selected_text: str,
    text_offset_start: int,
    text_offset_end: int,
    sentiment: Sentiment,
    comment: str | None,
) -> Feedback:
    cur = await db.execute(
        """
        INSERT INTO feedback (
            user_id, video_id, source, selected_text,
            text_offset_start, text_offset_end, sentiment, comment
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id, video_id, source.value, selected_text,
            text_offset_start, text_offset_end, sentiment.value, comment,
        ),
    )
    await db.commit()
    fb_id = cur.lastrowid
    assert fb_id is not None
    row = await (await db.execute(
        "SELECT * FROM feedback WHERE id=?", (fb_id,)
    )).fetchone()
    assert row is not None
    return _row_to_feedback(row)


async def list_for_video(
    db: aiosqlite.Connection, *, video_id: str, user_id: int,
) -> list[Feedback]:
    # Tiebreak ties on created_at by id; SQLite's datetime('now') resolves
    # to the second, so multiple feedbacks created in the same request
    # would otherwise come back in an undefined order.
    cur = await db.execute(
        "SELECT * FROM feedback WHERE video_id=? AND user_id=? "
        "ORDER BY created_at ASC, id ASC",
        (video_id, user_id),
    )
    return [_row_to_feedback(r) for r in await cur.fetchall()]


async def list_recent_for_user(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 50,
) -> list[Feedback]:
    cur = await db.execute(
        "SELECT * FROM feedback WHERE user_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [_row_to_feedback(r) for r in await cur.fetchall()]


async def delete(
    db: aiosqlite.Connection, *, feedback_id: int, user_id: int,
) -> bool:
    """Delete one feedback row. Returns True if a row was deleted, False
    if the row didn't exist or belonged to another Profile."""
    cur = await db.execute(
        "DELETE FROM feedback WHERE id=? AND user_id=?",
        (feedback_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0
