import asyncio
import sqlite3

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


def test_init_schema_migrates_legacy_database(tmp_path):
    """A pre-V2 DB (no user_id, single-column settings PK) must upgrade
    cleanly when init_schema runs against it."""
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE videos (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            thumbnail_path TEXT,
            duration_seconds INTEGER,
            transcript TEXT,
            transcript_source TEXT,
            summary TEXT,
            summary_model TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL REFERENCES videos(id),
            state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
            step TEXT NOT NULL DEFAULT '',
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL REFERENCES videos(id),
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content TEXT NOT NULL
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO videos (id, url, title) VALUES ('x', 'u', 't');
        INSERT INTO settings (key, value) VALUES ('llm_model', 'openai/gpt-4o');
        """
    )
    legacy.commit()
    legacy.close()

    async def run():
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await init_schema(conn)
        # videos.user_id default 1
        cursor = await conn.execute("SELECT user_id FROM videos WHERE id='x'")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        # settings now has user_id column and the row carried over
        cursor = await conn.execute(
            "SELECT user_id, value FROM settings WHERE key='llm_model'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1
        assert row[1] == "openai/gpt-4o"
        # New tables exist
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='table' AND name IN ('playlists','playlist_videos')"
        )
        names = {r[0] for r in await cursor.fetchall()}
        assert names == {"playlists", "playlist_videos"}
        await conn.close()

    # Use a dedicated loop so closing it doesn't affect the global event loop
    # used by the rest of the test suite.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


async def test_init_schema_creates_playlists_tables(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {r[0] for r in await cursor.fetchall()}
    assert "playlists" in tables
    assert "playlist_videos" in tables


async def test_init_schema_idempotent_on_v2(db):
    """Calling init_schema twice on an already-V2 DB must not error."""
    await init_schema(db)
    await init_schema(db)
    cursor = await db.execute("SELECT COUNT(*) FROM playlists")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_init_schema_adds_kind_column_to_existing_videos(db: aiosqlite.Connection):
    """Fresh V3 schema includes 'kind' on videos with default 'youtube'."""
    cursor = await db.execute("PRAGMA table_info(videos)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "kind" in cols


def test_init_schema_migrates_v2_database_to_v3(tmp_path):
    """A V2 DB (with user_id but no kind) gains the kind column with
    'youtube' as the default for existing rows."""
    import sqlite3

    db_path = tmp_path / "v2.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE videos (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL DEFAULT 1,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            thumbnail_path TEXT,
            duration_seconds INTEGER,
            transcript TEXT,
            transcript_source TEXT,
            summary TEXT,
            summary_model TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE settings (
            user_id INTEGER NOT NULL DEFAULT 1,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        INSERT INTO videos (id, url, title) VALUES ('legacy', 'u', 't');
        """
    )
    legacy.commit()
    legacy.close()

    import asyncio

    async def run():
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        from app.db import init_schema
        await init_schema(conn)
        cursor = await conn.execute("SELECT kind FROM videos WHERE id='legacy'")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "youtube"
        await conn.close()

    asyncio.new_event_loop().run_until_complete(run())
