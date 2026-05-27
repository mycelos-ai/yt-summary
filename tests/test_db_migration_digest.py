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


async def test_upgrade_path_adds_new_user_columns_to_existing_table(config):
    """Simulate an existing DB that predates the digest feature: the users
    table exists but lacks the four new columns. init_schema must add
    them via the _ensure_column path."""
    conn = await connect(config)
    try:
        # Manually create a minimal users table without the new columns
        # (mirroring a pre-digest install shape — pre-V5-ish).
        await conn.execute("DROP TABLE IF EXISTS users")
        await conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT 'admin',
                api_key_hash TEXT,
                api_key_prefix TEXT,
                api_key_created_at TEXT,
                avatar_emoji TEXT NOT NULL DEFAULT '👤',
                avatar_image TEXT NOT NULL DEFAULT '',
                custom_summary_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()

        # Now run init_schema — should upgrade the users table in place.
        await init_schema(conn)

        cols = await _columns(conn, "users")
        assert "interest_profile_md" in cols
        assert "interest_profile_version" in cols
        assert "digest_enabled" in cols
        assert "digest_hour_local" in cols
    finally:
        await conn.close()


async def test_upgrade_path_adds_highlights_column_to_existing_videos(config):
    """Same as above for the videos table — exercise the _ensure_column
    path for highlights_json on a pre-existing videos table."""
    conn = await connect(config)
    try:
        await conn.execute("DROP TABLE IF EXISTS videos")
        # Minimal pre-digest videos shape.
        await conn.execute(
            """
            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL DEFAULT 1,
                kind TEXT NOT NULL DEFAULT 'youtube',
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT,
                duration_seconds INTEGER,
                transcript TEXT,
                summary TEXT,
                summary_model TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()

        await init_schema(conn)

        cols = await _columns(conn, "videos")
        assert "highlights_json" in cols
    finally:
        await conn.close()
