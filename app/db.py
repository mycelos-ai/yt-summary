import logging

import aiosqlite
import sqlite_vec

from app.config import Config

log = logging.getLogger(__name__)

# Default vector dimension. Auto-detected on first successful embedding
# (the table is recreated if a different model is configured later).
DEFAULT_EMBEDDING_DIM = 768

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
    -- JSON array of {start: float (seconds), text: str} segments. Drives
    -- the timestamped detail-page rendering. Optional — older rows or
    -- web articles store NULL here and fall back to plain transcript.
    transcript_segments TEXT,
    transcript_source TEXT,
    summary TEXT,
    summary_model TEXT,
    summary_embedded_at TEXT,
    -- Bare YouTube id (the 11-char slug from the URL). Stored separately
    -- from `id` so we can dedupe imports across profiles: when user A
    -- already transcribed YouTube video X, user B can reuse that
    -- transcript instead of re-running Whisper. NULL for web articles —
    -- they dedupe by `url` instead.
    youtube_id TEXT,
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

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'admin',
    api_key_hash TEXT,
    api_key_prefix TEXT,
    api_key_created_at TEXT,
    -- Profile-specific cosmetic + behaviour fields. avatar_emoji is the
    -- header-dropdown / picker tile glyph; custom_summary_prompt
    -- (NULL = use the standard summarizer prompt) lets each profile
    -- tweak how summaries are written without affecting other profiles.
    avatar_emoji TEXT NOT NULL DEFAULT '👤',
    custom_summary_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_videos_youtube_user
    ON videos(youtube_id, user_id);

CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
    video_id TEXT PRIMARY KEY,
    summary_vec FLOAT[768]
);

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
        if "summary_embedded_at" not in video_cols:
            await conn.execute(
                "ALTER TABLE videos ADD COLUMN summary_embedded_at TEXT"
            )
        if "transcript_segments" not in video_cols:
            await conn.execute(
                "ALTER TABLE videos ADD COLUMN transcript_segments TEXT"
            )
        if "youtube_id" not in video_cols:
            # Multi-profile (V5) migration: split the bare YouTube id off
            # from `videos.id` so we can dedupe transcripts across
            # profiles. Old rows had id == youtube_id (single user only),
            # so backfill that. Web rows already have id like
            # 'web-abc...' which we leave NULL — web dedup uses URL.
            await conn.execute(
                "ALTER TABLE videos ADD COLUMN youtube_id TEXT"
            )
            await conn.execute(
                "UPDATE videos SET youtube_id = id "
                "WHERE youtube_id IS NULL AND kind = 'youtube'"
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

    if await _table_exists(conn, "users"):
        # V5: per-profile cosmetic + behaviour fields.
        user_cols = await _table_columns(conn, "users")
        if "avatar_emoji" not in user_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN avatar_emoji TEXT NOT NULL "
                "DEFAULT '👤'"
            )
        if "avatar_image" not in user_cols:
            # Path-suffix into app/static/avatars/ (e.g.
            # 'adult-techreviewer-m'). Empty string = "use the emoji
            # instead". Per-profile choice from the curated avatar
            # library; users without an image fall back to the emoji.
            await conn.execute(
                "ALTER TABLE users ADD COLUMN avatar_image TEXT NOT NULL "
                "DEFAULT ''"
            )
        if "custom_summary_prompt" not in user_cols:
            await conn.execute(
                "ALTER TABLE users ADD COLUMN custom_summary_prompt TEXT"
            )

        # Seed the standard summarizer prompt onto every existing
        # profile. After this migration runs, every user has a
        # concrete prompt stored — the runtime no longer falls back
        # to a hardcoded default. Idempotent: only NULL rows get
        # touched, so re-running this migration (e.g. after a code
        # update that tweaks the standard prompt) does NOT clobber
        # any user-edited prompts.
        from app.services.summarizer import build_system_prompt
        seed_prompt = build_system_prompt(language=None)
        await conn.execute(
            "UPDATE users SET custom_summary_prompt = ? "
            "WHERE custom_summary_prompt IS NULL",
            (seed_prompt,),
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
    # sqlite-vec ships its compiled extension as a loadable .dylib/.so;
    # we load it on every connection so vec0 / vec_* functions are
    # available everywhere queries run.
    await conn.enable_load_extension(True)
    try:
        await conn.load_extension(sqlite_vec.loadable_path())
    except Exception:
        log.exception("Failed to load sqlite-vec extension")
    finally:
        await conn.enable_load_extension(False)
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA journal_mode = WAL")
    return conn


async def init_schema(conn: aiosqlite.Connection) -> None:
    await _run_migrations(conn)
    await conn.executescript(SCHEMA)
    # Seed the single default user (id=1) if the table is empty. Every
    # existing user_id=1 reference now points at a real row.
    cursor = await conn.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    if row is not None and row[0] == 0:
        await conn.execute(
            "INSERT INTO users (id, name) VALUES (1, 'admin')"
        )
    await conn.commit()
