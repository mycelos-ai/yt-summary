"""CRUD for the `syntheses` table (Part C.2 — ask my library).

One row is one cross-library question + its answer. States transition
pending → ready | failed. Scoped by `user_id` (Profile) on reads.
Mirrors repos/digests.py.
"""
import json
from datetime import datetime

import aiosqlite

from app.models import Synthesis, SynthesisStatus


def _row_to_synthesis(row: aiosqlite.Row) -> Synthesis:
    return Synthesis(
        id=row["id"],
        user_id=row["user_id"],
        query=row["query"],
        result_md=row["result_md"],
        source_ids_json=row["source_ids_json"],
        status=SynthesisStatus(row["status"]),
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def create_pending(
    db: aiosqlite.Connection,
    *,
    user_id: int,
    query: str,
    source_ids: list[str],
) -> Synthesis:
    cur = await db.execute(
        """
        INSERT INTO syntheses (user_id, query, source_ids_json, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (user_id, query, json.dumps(source_ids)),
    )
    await db.commit()
    sid = cur.lastrowid
    assert sid is not None
    fetched = await get(db, sid)
    assert fetched is not None
    return fetched


async def get(db: aiosqlite.Connection, synthesis_id: int) -> Synthesis | None:
    cur = await db.execute(
        "SELECT * FROM syntheses WHERE id=?", (synthesis_id,)
    )
    row = await cur.fetchone()
    return _row_to_synthesis(row) if row else None


async def mark_ready(
    db: aiosqlite.Connection, *, synthesis_id: int, result_md: str,
) -> None:
    await db.execute(
        "UPDATE syntheses SET status='ready', result_md=?, error=NULL "
        "WHERE id=?",
        (result_md, synthesis_id),
    )
    await db.commit()


async def mark_failed(
    db: aiosqlite.Connection, *, synthesis_id: int, error: str,
) -> None:
    await db.execute(
        "UPDATE syntheses SET status='failed', error=? WHERE id=?",
        (error, synthesis_id),
    )
    await db.commit()


async def list_for_user(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 30,
) -> list[Synthesis]:
    cur = await db.execute(
        "SELECT * FROM syntheses WHERE user_id=? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [_row_to_synthesis(r) for r in await cur.fetchall()]
