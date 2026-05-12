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
        # init_schema needs the sqlite-vec extension for vec0 tables.
        import sqlite_vec
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.enable_load_extension(False)
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
        import sqlite_vec
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.enable_load_extension(False)
        await conn.execute("PRAGMA foreign_keys = ON")
        from app.db import init_schema
        await init_schema(conn)
        cursor = await conn.execute("SELECT kind FROM videos WHERE id='legacy'")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "youtube"
        await conn.close()

    asyncio.new_event_loop().run_until_complete(run())


async def test_init_schema_creates_users_table(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    )
    assert await cursor.fetchone() is not None


async def test_init_schema_seeds_default_user(db: aiosqlite.Connection):
    cursor = await db.execute("SELECT id, name FROM users WHERE id = 1")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == "admin"


async def test_init_schema_default_user_has_no_key(db: aiosqlite.Connection):
    cursor = await db.execute(
        "SELECT api_key_hash, api_key_prefix FROM users WHERE id = 1"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] is None


def test_db_has_tts_jobs_table_and_language_columns(tmp_path, monkeypatch):
    """tts_jobs table exists; videos gains three nullable language
    columns (source_language, summary_language, transcript_language).
    Summaries and transcripts live as columns ON videos in this
    schema — there are no separate summaries/transcripts tables."""
    import asyncio
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))

    async def check():
        from app.config import Config
        from app.db import connect, init_schema
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        db = await connect(cfg)
        try:
            await init_schema(db)
            # tts_jobs columns
            async with db.execute("PRAGMA table_info(tts_jobs)") as cur:
                cols = {row[1] for row in await cur.fetchall()}
            for expected in (
                "id", "video_id", "source", "target_language", "voice",
                "quality", "status", "step", "translated_text",
                "audio_path", "duration_seconds", "error",
                "created_at", "started_at", "finished_at",
            ):
                assert expected in cols, f"missing column: {expected}"
            # Three new language columns on videos
            async with db.execute("PRAGMA table_info(videos)") as cur:
                v_cols = {row[1] for row in await cur.fetchall()}
            for c in ("source_language", "summary_language", "transcript_language"):
                assert c in v_cols, f"videos missing column: {c}"
        finally:
            await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_tts_jobs_check_constraint_rejects_invalid_source(tmp_path, monkeypatch):
    """The source CHECK should reject anything other than
    'summary' or 'transcript'."""
    import asyncio
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))

    async def check():
        import aiosqlite

        from app.config import Config
        from app.db import connect, init_schema
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        db = await connect(cfg)
        try:
            await init_schema(db)
            # Need a parent video row first so FK doesn't fire first
            await db.execute(
                "INSERT INTO videos (id, url, title) VALUES (?, ?, ?)",
                ("abc", "http://x", "T"),
            )
            await db.commit()
            try:
                await db.execute(
                    "INSERT INTO tts_jobs (video_id, source, target_language, "
                    "voice, quality) VALUES (?, ?, ?, ?, ?)",
                    ("abc", "BOGUS", "de", "thorsten", "medium"),
                )
                await db.commit()
                raise AssertionError("CHECK should have rejected 'BOGUS'")
            except aiosqlite.IntegrityError:
                pass
        finally:
            await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()


def test_tts_jobs_cascade_deletes_with_video(tmp_path, monkeypatch):
    """Deleting a video must remove its tts_jobs rows via FK cascade."""
    import asyncio
    monkeypatch.setenv("YTS_DATA_DIR", str(tmp_path))

    async def check():
        from app.config import Config
        from app.db import connect, init_schema
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        db = await connect(cfg)
        try:
            await init_schema(db)
            await db.execute(
                "INSERT INTO videos (id, url, title) VALUES (?, ?, ?)",
                ("abc", "http://x", "T"),
            )
            await db.execute(
                "INSERT INTO tts_jobs (video_id, source, target_language, "
                "voice, quality) VALUES (?, ?, ?, ?, ?)",
                ("abc", "summary", "de", "thorsten", "medium"),
            )
            await db.commit()
            await db.execute("DELETE FROM videos WHERE id = 'abc'")
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) FROM tts_jobs WHERE video_id = 'abc'"
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            assert row[0] == 0, "cascade-delete should have removed the tts_jobs row"
        finally:
            await db.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(check())
    finally:
        loop.close()
