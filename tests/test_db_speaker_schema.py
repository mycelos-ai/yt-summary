import asyncio
from pathlib import Path

import aiosqlite

from app.db import connect, init_schema, _run_migrations
from app.config import Config
from app.models import VideoKind


def test_videokind_has_text():
    assert VideoKind.TEXT == "text"
    assert VideoKind("text") is VideoKind.TEXT


def _fresh_db(tmp_path):
    cfg = Config(data_dir=Path(tmp_path)); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        return conn
    return asyncio.get_event_loop().run_until_complete(go())


def test_videos_accepts_text_kind(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        await conn.execute(
            "INSERT INTO videos (id, user_id, kind, url, title) "
            "VALUES ('text-1', 1, 'text', '', 'pasted')"
        )
        await conn.commit()
        cur = await conn.execute("SELECT kind FROM videos WHERE id='text-1'")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "text"
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_videos_has_channel_id(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "channel_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_migrations_run_twice_clean(tmp_path):
    cfg = Config(data_dir=Path(tmp_path)); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        await _run_migrations(conn)   # second pass
        await init_schema(conn)       # third pass
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "channel_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


# ── Task 3: Speaker tables + chat_threads + chat_messages rebuild ──────────────

EXPECTED_TABLES = {
    "known_shows", "known_speakers", "speakers", "source_speakers",
    "speaker_source_candidates", "speaker_claims", "chat_threads",
}


def test_speaker_tables_exist(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        cur = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r[0] for r in await cur.fetchall()}
        assert EXPECTED_TABLES <= names
        cur = await conn.execute("PRAGMA table_info(chat_messages)")
        cols = {r[1] for r in await cur.fetchall()}
        assert "thread_id" in cols
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_chat_threads_partial_unique_blocks_dupe_speaker_thread(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        await conn.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
        await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
        await conn.commit()
        import aiosqlite as _a
        raised = False
        try:
            await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
            await conn.commit()
        except _a.IntegrityError:
            raised = True
        assert raised, "partial unique index must block a duplicate speaker thread"
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


def test_chat_messages_video_id_nullable(tmp_path):
    conn = _fresh_db(tmp_path)
    async def go():
        # a thread-scoped row with NO video must be insertable
        await conn.execute("INSERT INTO speakers (user_id, name, name_key) VALUES (1,'X','x')")
        await conn.execute("INSERT INTO chat_threads (user_id, scope, speaker_id) VALUES (1,'speaker',1)")
        await conn.execute(
            "INSERT INTO chat_messages (user_id, video_id, role, content, thread_id) "
            "VALUES (1, NULL, 'user', 'hi', 1)"
        )
        await conn.commit()
        cur = await conn.execute("SELECT video_id, thread_id FROM chat_messages WHERE thread_id=1")
        row = await cur.fetchone()
        assert row is not None
        assert row[0] is None and row[1] == 1
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())


# ── P1 fix: shape CHECK added to existing chat_threads tables on upgrade ──────

# Legacy chat_threads DDL: has scope CHECK but NO shape CHECK.
_LEGACY_CHAT_THREADS_DDL = """
CREATE TABLE chat_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    scope TEXT NOT NULL CHECK(scope IN ('source','source_speaker','speaker')),
    source_id TEXT,
    speaker_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Pre-create videos in its CURRENT full shape so no videos migration fires
# (channel_id present → no rebuild triggered). speakers and users are left
# for SCHEMA to create so their DDL exactly matches the fresh-install shape.
_PREREQ_VIDEOS_DDL = """
CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'youtube'
        CHECK(kind IN ('youtube','web','email','text')),
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    thumbnail_path TEXT,
    duration_seconds INTEGER,
    transcript TEXT,
    transcript_segments TEXT,
    transcript_source TEXT,
    summary TEXT,
    summary_model TEXT,
    summary_embedded_at TEXT,
    youtube_id TEXT,
    source_language TEXT,
    summary_language TEXT,
    transcript_language TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT,
    highlights_json TEXT,
    image_query TEXT,
    related_links_json TEXT,
    channel_id TEXT
);
"""

_SHAPE_CHECK_SIGNATURE = "speaker_id IS NOT NULL AND source_id IS NULL"


async def _build_legacy_chat_threads_db(db_path):
    """Build a DB that mimics an upgraded install: legacy chat_threads (no shape CHECK)."""
    conn = await aiosqlite.connect(db_path)
    # Pre-create videos (full current shape — no migration fires) and the
    # legacy chat_threads. All other tables (users, speakers, settings, …) are
    # left for SCHEMA/init_schema to create, so they get the exact right DDL.
    await conn.executescript(_PREREQ_VIDEOS_DDL)
    await conn.executescript(_LEGACY_CHAT_THREADS_DDL)
    # Insert a valid row (scope='source', source_id set, speaker_id NULL).
    await conn.execute(
        "INSERT INTO videos (id, url, title) VALUES ('v1', 'http://x', 'Test Video')"
    )
    await conn.execute(
        "INSERT INTO chat_threads (user_id, scope, source_id, speaker_id) "
        "VALUES (1, 'source', 'v1', NULL)"
    )
    # Insert an invalid orphan row (scope='speaker', both NULL) — only possible
    # without the shape CHECK; must be quarantined (dropped) by the migration.
    await conn.execute(
        "INSERT INTO chat_threads (user_id, scope, source_id, speaker_id) "
        "VALUES (1, 'speaker', NULL, NULL)"
    )
    await conn.commit()
    await conn.close()


def test_chat_threads_check_added_on_upgrade(tmp_path):
    """
    On an upgraded DB (legacy chat_threads WITHOUT shape CHECK):
    - After init_schema(), the table has the shape CHECK.
    - A raw INSERT of an all-NULL orphan (scope='speaker', source_id=NULL, speaker_id=NULL)
      now raises IntegrityError.
    - The valid pre-existing row is preserved.
    - The invalid orphan row is dropped (quarantined during copy).
    """
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()

        # 1. Build legacy DB.
        await _build_legacy_chat_threads_db(cfg.db_path)

        # 2. Run init_schema (triggers _run_migrations then SCHEMA).
        conn = await connect(cfg)
        await init_schema(conn)

        # 3. Shape CHECK must be present in sqlite_master.
        cur = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_threads'"
        )
        row = await cur.fetchone()
        ddl = (row[0] if row else "") or ""
        assert _SHAPE_CHECK_SIGNATURE in ddl, (
            f"Shape CHECK signature not found in rebuilt DDL:\n{ddl}"
        )

        # 4. Invalid insert must now be rejected.
        raised = False
        try:
            await conn.execute(
                "INSERT INTO chat_threads (user_id, scope, source_id, speaker_id) "
                "VALUES (1, 'speaker', NULL, NULL)"
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            raised = True
        assert raised, "Shape CHECK must reject (scope='speaker', source_id=NULL, speaker_id=NULL)"

        # 5. Valid row (scope='source', source_id='v1') must be preserved.
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chat_threads WHERE scope='source' AND source_id='v1'"
        )
        count_row = await cur.fetchone()
        assert count_row is not None and count_row[0] == 1, (
            "Valid pre-existing row must be preserved after migration"
        )

        # 6. Orphan row must be gone (it had scope='speaker', both NULLs).
        cur = await conn.execute(
            "SELECT COUNT(*) FROM chat_threads WHERE scope='speaker' AND source_id IS NULL AND speaker_id IS NULL"
        )
        orphan_row = await cur.fetchone()
        assert orphan_row is not None and orphan_row[0] == 0, (
            "Invalid orphan row must be quarantined (dropped) by the migration copy"
        )

        await conn.close()

    asyncio.get_event_loop().run_until_complete(scenario())


def test_chat_threads_migration_idempotent(tmp_path):
    """
    Running init_schema twice on an upgraded DB must not error and must
    leave valid rows intact with a stable count.
    """
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()

        # Build legacy DB and run init_schema once.
        await _build_legacy_chat_threads_db(cfg.db_path)
        conn = await connect(cfg)
        await init_schema(conn)

        # Count valid rows after first migration.
        cur = await conn.execute("SELECT COUNT(*) FROM chat_threads WHERE scope='source'")
        r1 = await cur.fetchone()
        count_after_first = r1[0] if r1 else -1

        # Run init_schema a second time — must be a no-op.
        await init_schema(conn)

        cur = await conn.execute("SELECT COUNT(*) FROM chat_threads WHERE scope='source'")
        r2 = await cur.fetchone()
        count_after_second = r2[0] if r2 else -2

        assert count_after_first == count_after_second == 1, (
            f"Idempotency: counts must be stable (1, 1), got ({count_after_first}, {count_after_second})"
        )

        # Shape CHECK must still be present.
        cur = await conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_threads'"
        )
        row = await cur.fetchone()
        ddl = (row[0] if row else "") or ""
        assert _SHAPE_CHECK_SIGNATURE in ddl, "Shape CHECK must persist after second init_schema"

        await conn.close()

    asyncio.get_event_loop().run_until_complete(scenario())
