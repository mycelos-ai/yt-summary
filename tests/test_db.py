import aiosqlite

from app.db import init_schema


async def test_schema_creates_all_tables(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert {"videos", "jobs", "chat_messages", "settings"}.issubset(tables)


async def test_schema_creates_fts(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='videos_fts'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_init_schema_is_idempotent(db: aiosqlite.Connection):
    await init_schema(db)
    await init_schema(db)
    cursor = await db.execute("SELECT COUNT(*) FROM videos")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0
