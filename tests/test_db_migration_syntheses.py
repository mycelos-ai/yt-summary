"""Schema test for the `syntheses` table (Part C.2 — ask my library)."""

import aiosqlite

from app.db import connect, init_schema


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_init_creates_syntheses_table(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='syntheses'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


async def test_syntheses_has_expected_columns(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cols = await _columns(conn, "syntheses")
        assert {
            "id", "user_id", "query", "result_md", "source_ids_json",
            "status", "error", "created_at",
        } <= cols
    finally:
        await conn.close()


async def test_init_schema_is_idempotent_for_syntheses(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        await init_schema(conn)  # second run must not raise
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='syntheses'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()
