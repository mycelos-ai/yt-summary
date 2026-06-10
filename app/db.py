import logging

import aiosqlite
import sqlite_vec

from app.config import Config

log = logging.getLogger(__name__)

# Base schema for a fresh install. Existing databases are upgraded by
# _run_migrations() below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'youtube'
        CHECK(kind IN ('youtube','web','email')),
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
    source_language TEXT,
    summary_language TEXT,
    transcript_language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    highlights_json TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(id),
    state TEXT NOT NULL CHECK(state IN ('pending','running','done','failed')),
    step TEXT NOT NULL DEFAULT '',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    llm_model_id      INTEGER REFERENCES llm_models(id) ON DELETE SET NULL,
    additional_prompt TEXT
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
    -- Path-suffix into app/static/avatars/ (e.g. 'adult-techreviewer-m').
    -- Empty string = "use the emoji instead". Mirrored in the V5
    -- migration below so existing single-user installs upgrade in
    -- place; new installs get it directly from this CREATE TABLE.
    avatar_image TEXT NOT NULL DEFAULT '',
    custom_summary_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    interest_profile_md TEXT,
    interest_profile_version INTEGER NOT NULL DEFAULT 0,
    digest_enabled INTEGER NOT NULL DEFAULT 0,
    digest_hour_local INTEGER NOT NULL DEFAULT 7,
    -- Part B: per-profile podcast-feed capability token (nullable,
    -- plaintext). Mirrored in the migration below for existing installs.
    podcast_token TEXT
);

CREATE TABLE IF NOT EXISTS llm_models (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL,
    provider_id TEXT    NOT NULL,
    model       TEXT    NOT NULL,
    api_key     TEXT    NOT NULL DEFAULT '',
    base_url    TEXT    NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_models_default
    ON llm_models(is_default) WHERE is_default = 1;

CREATE INDEX IF NOT EXISTS idx_videos_youtube_user
    ON videos(youtube_id, user_id);

CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
    video_id TEXT PRIMARY KEY,
    summary_vec FLOAT[384]
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

CREATE TABLE IF NOT EXISTS tts_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('summary', 'transcript')),
    target_language TEXT NOT NULL,
    voice TEXT NOT NULL,
    quality TEXT NOT NULL CHECK (quality IN ('low','medium','high')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'translating', 'rendering', 'done', 'failed')),
    step TEXT,
    translated_text TEXT,
    audio_path TEXT,
    duration_seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (video_id, source, target_language, voice, quality),
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tts_jobs_status ON tts_jobs(status);
CREATE INDEX IF NOT EXISTS idx_tts_jobs_video  ON tts_jobs(video_id);

CREATE TABLE IF NOT EXISTS mail_senders (
    user_id      INTEGER NOT NULL DEFAULT 1,
    sender_addr  TEXT    NOT NULL,
    sender_name  TEXT    NOT NULL DEFAULT '',
    -- Newsletters are strictly opt-in: only subscribed senders get
    -- ingested. New senders surface here as subscribed=0 until the user
    -- ticks them on the "Add a source" page.
    subscribed   INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    last_subject TEXT,
    PRIMARY KEY (user_id, sender_addr)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    -- Exactly one of (video_id, digest_id) is non-NULL. The XOR is
    -- enforced via the CHECK constraint below and at the route layer.
    -- video_id anchors feedback to a specific video's summary /
    -- transcript / digest-source entry. digest_id anchors feedback to
    -- a digest's TL;DR block (which is LLM-synthesised across many
    -- items and has no single owning video).
    video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
    digest_id INTEGER REFERENCES digests(id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK(source IN ('summary','transcript','digest','digest_tldr')),
    selected_text TEXT NOT NULL,
    text_offset_start INTEGER NOT NULL,
    text_offset_end INTEGER NOT NULL,
    sentiment TEXT NOT NULL CHECK(sentiment IN ('interesting','not_interesting')),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((video_id IS NOT NULL) <> (digest_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_feedback_user_created
    ON feedback(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_video ON feedback(video_id);
CREATE INDEX IF NOT EXISTS idx_feedback_digest ON feedback(digest_id);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    tldr TEXT,
    top_items_json TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('pending','rendering','ready','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_digests_user_created
    ON digests(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS syntheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    query TEXT NOT NULL,
    result_md TEXT,
    source_ids_json TEXT NOT NULL,   -- ordered video ids used
    status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_syntheses_user_created
    ON syntheses(user_id, created_at DESC);
"""


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _ensure_column(
    conn: aiosqlite.Connection, table: str, column: str, col_type: str
) -> None:
    """Add `column` to `table` if not present. Idempotent."""
    cols = await _table_columns(conn, table)
    if column not in cols:
        await conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
        )


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

        # TTS (V6): per-video language columns added so the audio-
        # render pipeline can decide whether translation is needed
        # before TTS. NULL on all pre-existing rows — they get
        # populated on the next re-process / new ingest.
        await _ensure_column(conn, "videos", "source_language",     "TEXT")
        await _ensure_column(conn, "videos", "summary_language",    "TEXT")
        await _ensure_column(conn, "videos", "transcript_language", "TEXT")
        await _ensure_column(conn, "videos", "highlights_json", "TEXT")

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
        await _ensure_column(conn, "users", "interest_profile_md", "TEXT")
        await _ensure_column(
            conn, "users", "interest_profile_version",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(
            conn, "users", "digest_enabled",
            "INTEGER NOT NULL DEFAULT 0",
        )
        await _ensure_column(
            conn, "users", "digest_hour_local",
            "INTEGER NOT NULL DEFAULT 7",
        )
        # Part B: per-profile podcast-feed capability token. Nullable;
        # stored in plaintext (deliberate — the settings page must be
        # able to re-display the feed URL, and the token gates a
        # read-only, audio-only surface).
        await _ensure_column(conn, "users", "podcast_token", "TEXT")

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

    # tts_jobs: created via SCHEMA's `CREATE TABLE IF NOT EXISTS`
    # for both fresh installs and upgrades. No ALTER needed.

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

    # feedback: TL;DR feedback (anchored to a digest_id instead of a
    # video_id) needs nullable video_id and a new digest_id column.
    # SQLite can't lower NOT NULL or add a CHECK via ALTER, so we
    # rebuild the table if the legacy shape is in place. Idempotent:
    # gated on `digest_id` already being a column.
    if await _table_exists(conn, "feedback"):
        feedback_cols = await _table_columns(conn, "feedback")
        if "digest_id" not in feedback_cols:
            await conn.executescript(
                """
                CREATE TABLE feedback_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
                    digest_id INTEGER REFERENCES digests(id) ON DELETE CASCADE,
                    source TEXT NOT NULL CHECK(source IN (
                        'summary','transcript','digest','digest_tldr'
                    )),
                    selected_text TEXT NOT NULL,
                    text_offset_start INTEGER NOT NULL,
                    text_offset_end INTEGER NOT NULL,
                    sentiment TEXT NOT NULL CHECK(sentiment IN ('interesting','not_interesting')),
                    comment TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    CHECK ((video_id IS NOT NULL) <> (digest_id IS NOT NULL))
                );
                INSERT INTO feedback_new (
                    id, user_id, video_id, digest_id, source,
                    selected_text, text_offset_start, text_offset_end,
                    sentiment, comment, created_at
                )
                SELECT
                    id, user_id, video_id, NULL, source,
                    selected_text, text_offset_start, text_offset_end,
                    sentiment, comment, created_at
                FROM feedback;
                DROP TABLE feedback;
                ALTER TABLE feedback_new RENAME TO feedback;
                """
            )

    # V7: 768d → 384d embedding dimension. Must run here (before
    # executescript(SCHEMA) creates the FTS triggers) so that the
    # UPDATE videos SET summary_embedded_at = NULL does not collide
    # with a freshly created-but-unpopulated videos_fts index.
    await _migrate_v7_embedding_dim(conn)

    # ── Multi-model migration ──────────────────────────────────
    #
    # Move the legacy single LLM config (settings.llm_model /
    # llm_api_key / llm_base_url) into a managed llm_models table
    # with a default row. Adds two override columns to `jobs` so
    # the Re-summarize endpoint can carry a per-run model + prompt
    # tweak through to the worker. Idempotent — each step is gated
    # on the relevant feature check.
    if not await _table_exists(conn, "llm_models"):
        await conn.execute(
            """
            CREATE TABLE llm_models (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT    NOT NULL,
                provider_id TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                api_key     TEXT    NOT NULL DEFAULT '',
                base_url    TEXT    NOT NULL DEFAULT '',
                is_default  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.execute(
            "CREATE UNIQUE INDEX idx_llm_models_default "
            "ON llm_models(is_default) WHERE is_default = 1"
        )
        await conn.commit()

    if await _table_exists(conn, "jobs"):
        await _ensure_column(conn, "jobs", "llm_model_id", "INTEGER")
        await _ensure_column(conn, "jobs", "additional_prompt", "TEXT")

    # Backfill from legacy settings keys, but only once: if any row
    # already exists in llm_models, the migration has already run
    # (or the user has added a model manually) — leave it alone.
    # Also skip on a blank DB where settings hasn't been created yet.
    # SELECT COUNT(*) always returns exactly one row — no None case to
    # guard against. The settings-table check is needed because on a
    # blank DB this block runs before SCHEMA has created `settings`
    # (the earlier _migrate_v7_embedding_dim path returns early when
    # tables don't exist yet).
    cursor = await conn.execute("SELECT COUNT(*) FROM llm_models")
    row = await cursor.fetchone()
    assert row is not None
    if row[0] == 0 and await _table_exists(conn, "settings"):
        cursor = await conn.execute(
            "SELECT key, value FROM settings WHERE user_id=1 AND key IN "
            "('llm_model','llm_api_key','llm_base_url')"
        )
        legacy = {r[0]: r[1] for r in await cursor.fetchall()}
        legacy_model = (legacy.get("llm_model") or "").strip()
        if legacy_model:
            from app.services.providers import PROVIDER_PRESETS

            # Match the legacy model's prefix against PROVIDER_PRESETS.
            # Exact match wins; otherwise require an underscore separator
            # so `ollama_chat/...` maps to the `ollama` preset without
            # accidentally matching a hypothetical `openai-compatible/`
            # against `openai`. The same `head.startswith(...)` shape
            # exists in routes/settings.py for the active-provider
            # detection; the underscore tightening is specific to the
            # migration where the input is user-supplied.
            head = legacy_model.split("/", 1)[0]
            provider_id = "custom"
            label = "Custom"
            for preset_id, preset in PROVIDER_PRESETS.items():
                lp = preset.litellm_provider
                if head == lp or head.startswith(lp + "_"):
                    provider_id = preset_id
                    label = preset.name
                    break
            await conn.execute(
                """
                INSERT INTO llm_models
                    (label, provider_id, model, api_key, base_url, is_default)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    label,
                    provider_id,
                    legacy_model,
                    legacy.get("llm_api_key", ""),
                    legacy.get("llm_base_url", ""),
                ),
            )
            await conn.execute(
                "DELETE FROM settings WHERE user_id=1 AND key IN "
                "('llm_model','llm_api_key','llm_base_url')"
            )
            await conn.commit()

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


async def _migrate_v7_embedding_dim(conn: aiosqlite.Connection) -> None:
    """One-shot 768d → 384d embedding-dimension migration.

    Idempotent: gated by the ``embedding_dim_migrated=384`` settings
    row. Called TWICE from init_schema:

    1. From ``_run_migrations`` (before ``executescript(SCHEMA)``) — this
       is the upgrade path. Both ``settings`` and ``videos`` already
       exist, and there are no FTS triggers yet (or they are already
       consistent), so the UPDATE is safe.

    2. After ``executescript(SCHEMA)`` — this handles fresh installs
       where the settings table was just created and the flag is absent.
       On a fresh DB there are no rows in ``videos``, so the UPDATE is a
       no-op and the DROP+CREATE of ``video_embeddings`` is a harmless
       round-trip on the newly created 384d table.

    The ``videos_au`` FTS trigger fires on any UPDATE to ``videos``.
    On a real upgrade the FTS table is already populated and consistent,
    so the trigger is safe. On a fresh install there are no rows, so it
    never fires.  The ``legacy_db`` test fixture deliberately omits the
    FTS table; that is safe here because this function is also called
    from ``_run_migrations`` (pass 1), before the FTS triggers exist.

    Uses raw SQL (not settings_repo.get/set) to avoid a circular
    import: settings_repo lives at app.repos.settings, which has no
    db.py dependency, but importing it here would pull repos into
    the db module's startup path.
    """
    # If settings table does not exist yet (earliest possible fresh install
    # state, before executescript(SCHEMA)), skip — the second call (after
    # SCHEMA) will handle it.
    if not await _table_exists(conn, "settings"):
        return

    cursor = await conn.execute(
        "SELECT value FROM settings WHERE user_id=1 AND key=?",
        ("embedding_dim_migrated",),
    )
    row = await cursor.fetchone()
    if row is not None and row[0] == "384":
        return  # already migrated

    # DROP the (possibly old) table and recreate at the new dimension.
    # On a fresh install the SCHEMA may have already created the 384d table;
    # the DROP+CREATE is harmless (one round-trip of a table with no rows).
    await conn.execute("DROP TABLE IF EXISTS video_embeddings")
    await conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS video_embeddings USING vec0(
            video_id TEXT PRIMARY KEY,
            summary_vec FLOAT[384]
        )
        """
    )
    # Clear summary_embedded_at so the scheduler re-embeds everything at the
    # new dimension. Only rows with a summary are eligible for embedding.
    # NOTE: this must happen before the FTS triggers are created (i.e., in
    # the _run_migrations pass) to avoid trigger-on-empty-FTS-index errors
    # that occur when executescript(SCHEMA) has just created videos_fts on
    # a non-empty videos table. On a real production DB the FTS index is
    # already consistent, so the UPDATE is always safe.
    if await _table_exists(conn, "videos"):
        await conn.execute(
            "UPDATE videos SET summary_embedded_at = NULL "
            "WHERE summary IS NOT NULL"
        )
    await conn.execute(
        """
        INSERT INTO settings (user_id, key, value) VALUES (1, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value
        """,
        ("embedding_dim_migrated", "384"),
    )
    await conn.commit()


async def init_schema(conn: aiosqlite.Connection) -> None:
    await _run_migrations(conn)
    await conn.executescript(SCHEMA)
    await _migrate_v7_embedding_dim(conn)
    # Seed the single default user (id=1) if the table is empty. Every
    # existing user_id=1 reference now points at a real row.
    cursor = await conn.execute("SELECT COUNT(*) FROM users")
    row = await cursor.fetchone()
    if row is not None and row[0] == 0:
        await conn.execute(
            "INSERT INTO users (id, name) VALUES (1, 'admin')"
        )
    await conn.commit()
