"""CRUD for synthesis_messages — the turns of an ask-my-library thread.

Mirrors repos/chat.py plus a per-message status: user turns are inserted
'ready'; assistant turns start 'pending' (content NULL) and a background
job marks them 'ready' (with content) or 'failed'.
"""
from datetime import datetime

import aiosqlite

from app.models import ChatRole, SynthesisMessage, SynthesisStatus


def _row(r: aiosqlite.Row) -> SynthesisMessage:
    return SynthesisMessage(
        id=r["id"],
        synthesis_id=r["synthesis_id"],
        role=r["role"],
        content=r["content"],
        status=SynthesisStatus(r["status"]),
        error=r["error"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


async def append(
    db: aiosqlite.Connection, *, synthesis_id: int, role: ChatRole,
    content: str | None, status: SynthesisStatus,
) -> SynthesisMessage:
    cur = await db.execute(
        "INSERT INTO synthesis_messages "
        "(synthesis_id, role, content, status) VALUES (?, ?, ?, ?)",
        (synthesis_id, role, content, status.value),
    )
    await db.commit()
    assert cur.lastrowid is not None
    got = await get(db, cur.lastrowid)
    assert got is not None
    return got


async def get(
    db: aiosqlite.Connection, message_id: int,
) -> SynthesisMessage | None:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages WHERE id=?", (message_id,)
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def history(
    db: aiosqlite.Connection, *, synthesis_id: int,
) -> list[SynthesisMessage]:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages WHERE synthesis_id=? "
        "ORDER BY created_at ASC, id ASC",
        (synthesis_id,),
    )
    return [_row(r) for r in await cur.fetchall()]


async def mark_ready(
    db: aiosqlite.Connection, *, message_id: int, content: str,
) -> None:
    await db.execute(
        "UPDATE synthesis_messages SET status='ready', content=?, error=NULL "
        "WHERE id=?",
        (content, message_id),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection, *, message_id: int, error: str,
) -> None:
    await db.execute(
        "UPDATE synthesis_messages SET status='failed', error=? WHERE id=?",
        (error, message_id),
    )
    await db.commit()


async def first_pending(
    db: aiosqlite.Connection, *, synthesis_id: int,
) -> SynthesisMessage | None:
    cur = await db.execute(
        "SELECT * FROM synthesis_messages "
        "WHERE synthesis_id=? AND role='assistant' AND status='pending' "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (synthesis_id,),
    )
    row = await cur.fetchone()
    return _row(row) if row else None
