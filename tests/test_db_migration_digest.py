import aiosqlite

from app.db import connect, init_schema


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_init_creates_feedback_table(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='feedback'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


async def test_init_creates_digests_table(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='digests'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


async def test_init_adds_highlights_column_to_videos(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        assert "highlights_json" in await _columns(conn, "videos")
    finally:
        await conn.close()


async def test_init_adds_profile_and_digest_columns_to_users(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cols = await _columns(conn, "users")
        assert "interest_profile_md" in cols
        assert "interest_profile_version" in cols
        assert "digest_enabled" in cols
        assert "digest_hour_local" in cols
    finally:
        await conn.close()


async def test_migration_is_idempotent(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        await init_schema(conn)  # second run must not raise
        assert "highlights_json" in await _columns(conn, "videos")
    finally:
        await conn.close()
