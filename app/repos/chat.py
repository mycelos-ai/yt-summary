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
    db: aiosqlite.Connection, video_id: str, role: ChatRole, content: str
) -> ChatMessage:
    cursor = await db.execute(
        "INSERT INTO chat_messages (video_id, role, content) VALUES (?, ?, ?)",
        (video_id, role, content),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    fetched = await db.execute(
        "SELECT * FROM chat_messages WHERE id=?", (cursor.lastrowid,)
    )
    row = await fetched.fetchone()
    assert row is not None
    return _row_to_msg(row)


async def history(db: aiosqlite.Connection, video_id: str) -> list[ChatMessage]:
    cursor = await db.execute(
        "SELECT * FROM chat_messages WHERE video_id=? ORDER BY created_at ASC, id ASC",
        (video_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_msg(r) for r in rows]
