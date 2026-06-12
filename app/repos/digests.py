"""CRUD for the `digests` table.

One row represents one daily-digest job for one Profile over one window.
States transition pending → rendering → ready | failed. Scoped by
`user_id` (Profile) for all read queries.
"""
from datetime import datetime

import aiosqlite

from app.models import Digest, DigestStatus


def _row_to_digest(row: aiosqlite.Row) -> Digest:
    return Digest(
        id=row["id"],
        user_id=row["user_id"],
        period_start=datetime.fromisoformat(row["period_start"]),
        period_end=datetime.fromisoformat(row["period_end"]),
        tldr=row["tldr"],
        top_items_json=row["top_items_json"],
        item_count=row["item_count"],
        status=DigestStatus(row["status"]),
        error=row["error"],
        selected_video_ids_json=row["selected_video_ids_json"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create_pending(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    period_start: datetime,
    period_end: datetime,
    selected_video_ids_json: str | None = None,
) -> Digest:
    cur = await db.execute(
        """
        INSERT INTO digests (
            user_id, period_start, period_end,
            selected_video_ids_json, status
        ) VALUES (?, ?, ?, ?, 'pending')
        """,
        (
            user_id, period_start.isoformat(), period_end.isoformat(),
            selected_video_ids_json,
        ),
    )
    await db.commit()
    digest_id = cur.lastrowid
    assert digest_id is not None
    fetched = await get(db, digest_id)
    assert fetched is not None
    return fetched


async def get(db: aiosqlite.Connection, digest_id: int) -> Digest | None:
    cur = await db.execute("SELECT * FROM digests WHERE id=?", (digest_id,))
    row = await cur.fetchone()
    return _row_to_digest(row) if row else None


async def mark_rendering(db: aiosqlite.Connection, *, digest_id: int) -> None:
    await db.execute(
        "UPDATE digests SET status='rendering' WHERE id=?", (digest_id,)
    )
    await db.commit()


async def mark_ready(
    db: aiosqlite.Connection,
    *,
    digest_id: int,
    tldr: str,
    top_items_json: str,
    item_count: int,
) -> None:
    await db.execute(
        """
        UPDATE digests
        SET status='ready',
            tldr=?,
            top_items_json=?,
            item_count=?,
            error=NULL
        WHERE id=?
        """,
        (tldr, top_items_json, item_count, digest_id),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection, *, digest_id: int, error: str,
) -> None:
    await db.execute(
        "UPDATE digests SET status='failed', error=? WHERE id=?",
        (error, digest_id),
    )
    await db.commit()


async def list_for_user(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 30,
) -> list[Digest]:
    # Tiebreak ties on created_at by id DESC; SQLite's datetime('now')
    # has second resolution and two digests inserted in the same second
    # would otherwise come back in undefined order.
    cur = await db.execute(
        "SELECT * FROM digests WHERE user_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [_row_to_digest(r) for r in await cur.fetchall()]


async def exists_in_range(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    range_start: datetime,
    range_end: datetime,
    in_states: tuple[DigestStatus, ...],
) -> bool:
    placeholders = ",".join("?" for _ in in_states)
    # Normalise both sides through SQLite's datetime() so we don't depend
    # on whether the stored value uses a 'T' or space separator. The
    # column default datetime('now') produces 'YYYY-MM-DD HH:MM:SS' while
    # datetime.isoformat() produces 'YYYY-MM-DDTHH:MM:SS' — comparing as
    # raw strings would silently miss rows.
    cur = await db.execute(
        f"""
        SELECT 1 FROM digests
        WHERE user_id=?
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) <  datetime(?)
          AND status IN ({placeholders})
        LIMIT 1
        """,
        (
            user_id,
            range_start.isoformat(),
            range_end.isoformat(),
            *(s.value for s in in_states),
        ),
    )
    return await cur.fetchone() is not None
