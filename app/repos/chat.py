from datetime import datetime

import aiosqlite

from app.models import ChatMessage, ChatRole


def _row_to_msg(row: aiosqlite.Row) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        video_id=row["video_id"],
        role=row["role"],
        content=row["content"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def append(
    db: aiosqlite.Connection,
    video_id: str | None = None,
    role: ChatRole = "user",
    content: str = "",
    *,
    user_id: int = 1,
    thread_id: int | None = None,
) -> ChatMessage:
    if thread_id is None and video_id is None:
        raise ValueError("append requires video_id or thread_id")
    cursor = await db.execute(
        "INSERT INTO chat_messages (user_id, video_id, role, content, thread_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, video_id, role, content, thread_id),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    fetched = await db.execute(
        "SELECT * FROM chat_messages WHERE id=?", (cursor.lastrowid,)
    )
    row = await fetched.fetchone()
    assert row is not None
    return _row_to_msg(row)


async def history(
    db: aiosqlite.Connection,
    video_id: str | None = None,
    *,
    thread_id: int | None = None,
) -> list[ChatMessage]:
    if thread_id is None and video_id is None:
        raise ValueError("history requires video_id or thread_id")
    if thread_id is not None:
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE thread_id=? ORDER BY created_at ASC, id ASC",
            (thread_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE video_id=? ORDER BY created_at ASC, id ASC",
            (video_id,),
        )
    rows = await cursor.fetchall()
    return [_row_to_msg(r) for r in rows]
