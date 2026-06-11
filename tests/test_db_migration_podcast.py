"""Schema test for the users.podcast_token column (Part B)."""

import aiosqlite

from app.db import connect, init_schema


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_init_adds_podcast_token_column(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cols = await _columns(conn, "users")
        assert "podcast_token" in cols
    finally:
        await conn.close()


async def test_init_schema_idempotent_for_podcast_token(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        await init_schema(conn)  # must not raise (column already added)
        cols = await _columns(conn, "users")
        assert "podcast_token" in cols
    finally:
        await conn.close()
