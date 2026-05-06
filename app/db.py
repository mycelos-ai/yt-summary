import aiosqlite

from app.config import Config

# Base schema for a fresh install. Existing databases are upgraded by
# _run_migrations() below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'youtube'
        CHECK(kind IN ('youtube','web')),
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

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id),
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    step TEXT NOT NULL DEFAULT '',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    video_id TEXT NOT NULL REFERENCES videos(id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_video_created ON chat_messages(video_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER NOT NULL DEFAULT 1,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    last_refreshed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS playlist_videos (
    playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(id),
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (playlist_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_videos_video ON playlist_videos(video_id);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id TEXT NOT NULL REFERENCES videos(id),
    tag_id   INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (video_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    content='videos',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;
"""


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    """Upgrade an existing database to the current shape.

    Each migration is gated by a feature check, so running this on a fresh
    database (where SCHEMA already produced the V2 shape) is a no-op.

    IMPORTANT: This must be called *before* executescript(SCHEMA) so that
    columns referenced by CREATE INDEX statements already exist.
    """
    # Only migrate tables that actually exist (no-op on a blank database).
    if await _table_exists(conn, "videos"):
        video_cols = await _table_columns(conn, "videos")
        if "user_id" not in video_cols:
            await conn.execute(
                "ALTER TABLE videos ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
            )
        if "kind" not in video_cols:
            # ALTER ADD COLUMN with a CHECK constraint isn't allowed in
            # SQLite, so we add the column without CHECK; the SCHEMA's
            # CREATE TABLE has the CHECK for fresh installs.
            await conn.execute(
                "ALTER TABLE videos ADD COLUMN kind TEXT NOT NULL DEFAULT 'youtube'"
            )

    if await _table_exists(conn, "chat_messages"):
        # Legacy chat_messages may lack user_id and created_at, both
        # referenced by the SCHEMA index, so add them before SCHEMA runs.
        chat_cols = await _table_columns(conn, "chat_messages")
        if "user_id" not in chat_cols:
            await conn.execute(
                "ALTER TABLE chat_messages ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
            )
        if "created_at" not in chat_cols:
            await conn.execute(
                "ALTER TABLE chat_messages"
                " ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now'))"
            )

    if await _table_exists(conn, "settings"):
        # Settings: PK migration. SQLite cannot change a PK in place, so we
        # detect the old shape (single-column key PK with no user_id) and
        # rebuild the table.
        settings_cols = await _table_columns(conn, "settings")
        if "user_id" not in settings_cols:
            await conn.executescript(
                """
                CREATE TABLE settings_new (
                    user_id INTEGER NOT NULL DEFAULT 1,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );
                INSERT INTO settings_new (user_id, key, value)
                    SELECT 1, key, value FROM settings;
                DROP TABLE settings;
                ALTER TABLE settings_new RENAME TO settings;
                """
            )
    await conn.commit()


async def connect(config: Config) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    await _run_migrations(conn)
    await conn.executescript(SCHEMA)
    await conn.commit()
