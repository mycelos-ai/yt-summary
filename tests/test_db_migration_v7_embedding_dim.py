"""Tests for the 768d → 384d embedding migration.

Note: pyproject sets `asyncio_mode = "auto"`, so async tests need no
decorator. The `db` fixture from conftest.py gives a clean in-memory
DB with init_schema already applied — but we need to *bypass* that
fixture in some tests so we can simulate an upgrade from the
old 768d shape.
"""
import struct

import aiosqlite
import pytest

from app.config import Config
from app.db import connect, init_schema
from app.repos import settings as settings_repo


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
async def legacy_db(tmp_path):
    """A DB initialised with the OLD 768d schema, mid-upgrade.

    This simulates a real user upgrading: their existing DB has the
    768d vec0 table, some videos with summaries, and no migration
    flag. We open a connection but DO NOT run init_schema yet —
    each test calls init_schema explicitly to exercise the migration.
    """
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = await connect(cfg)
    # Hand-roll the legacy schema: just the bits the migration cares about.
    await conn.executescript(
        """
        CREATE TABLE settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO users (id, name) VALUES (1, 'admin');
        CREATE TABLE videos (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            thumbnail_path TEXT,
            duration_seconds INTEGER,
            transcript TEXT,
            transcript_source TEXT,
            summary TEXT,
            summary_model TEXT,
            user_id INTEGER NOT NULL DEFAULT 1,
            youtube_id TEXT,
            kind TEXT NOT NULL DEFAULT 'youtube',
            transcript_segments TEXT,
            source_language TEXT,
            summary_language TEXT,
            transcript_language TEXT,
            summary_embedded_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE VIRTUAL TABLE video_embeddings USING vec0(
            video_id TEXT PRIMARY KEY,
            summary_vec FLOAT[768]
        );
        """
    )
    await conn.commit()
    try:
        yield conn
    finally:
        await conn.close()


async def test_migration_drops_old_table_on_upgrade(legacy_db):
    # Seed: video with summary + filled embedded_at, plus a 768d vector.
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("v1", "u", "t", "", "some summary"),
    )
    await legacy_db.execute(
        "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
        ("v1", _pack([0.1] * 768)),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    # The new table accepts a 384d insert (proves new dimension).
    await legacy_db.execute(
        "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
        ("v_new", _pack([0.0] * 384)),
    )
    await legacy_db.commit()

    # And rejects a 768d insert (proves the OLD dimension is gone).
    with pytest.raises(aiosqlite.Error):
        await legacy_db.execute(
            "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
            ("v_old", _pack([0.0] * 768)),
        )


async def test_migration_marks_videos_with_summary_as_pending(legacy_db):
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("withsum", "u", "t", "", "x"),
    )
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary) "
        "VALUES (?, ?, ?, ?, NULL)",
        ("nosum", "u", "t", ""),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    cursor = await legacy_db.execute(
        "SELECT id, summary_embedded_at FROM videos ORDER BY id"
    )
    rows = await cursor.fetchall()
    assert {(r[0], r[1]) for r in rows} == {
        ("nosum", None),       # no summary → untouched
        ("withsum", None),     # was set, now cleared by migration
    }


async def test_migration_sets_flag(legacy_db):
    await init_schema(legacy_db)
    flag = await settings_repo.get(legacy_db, "embedding_dim_migrated")
    assert flag == "384"


async def test_migration_is_idempotent(legacy_db):
    # First run does the work.
    await init_schema(legacy_db)
    # Now mark a video as embedded — to prove the second run does NOT
    # reset it.
    await legacy_db.execute(
        "INSERT INTO videos (id, url, title, description, summary, "
        "summary_embedded_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("v2", "u", "t", "", "x"),
    )
    await legacy_db.commit()

    await init_schema(legacy_db)

    cursor = await legacy_db.execute(
        "SELECT summary_embedded_at FROM videos WHERE id='v2'"
    )
    row = await cursor.fetchone()
    assert row[0] is not None  # untouched on second run


async def test_migration_on_fresh_install(tmp_path):
    """A fresh DB has no flag and no old table — migration must
    set the flag without crashing on the missing table."""
    cfg = Config(data_dir=tmp_path)
    cfg.ensure_dirs()
    conn = await connect(cfg)
    try:
        await init_schema(conn)
        flag = await settings_repo.get(conn, "embedding_dim_migrated")
        assert flag == "384"
        # And the new table is queryable.
        cursor = await conn.execute("SELECT COUNT(*) FROM video_embeddings")
        row = await cursor.fetchone()
        assert row[0] == 0
    finally:
        await conn.close()
