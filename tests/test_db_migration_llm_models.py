"""Migration test: a pre-existing settings.llm_model row becomes a
default llm_models entry, and the old settings keys are deleted."""

import aiosqlite
import sqlite_vec

from app.db import _run_migrations
from app.repos import llm_models as llm_models_repo
from app.repos import settings as settings_repo


async def _make_legacy_db(path: str) -> aiosqlite.Connection:
    """Build a stripped-down DB containing only the legacy settings shape
    the migration cares about — enough to exercise the migration code
    without depending on the full SCHEMA's older shape."""
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    # Load the sqlite-vec extension so _migrate_v7_embedding_dim can
    # create the vec0 virtual table (same as the real connect() helper).
    await conn.enable_load_extension(True)
    await conn.load_extension(sqlite_vec.loadable_path())
    await conn.enable_load_extension(False)
    await conn.execute("""
        CREATE TABLE settings (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        )
    """)
    await conn.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            state TEXT NOT NULL,
            step TEXT NOT NULL DEFAULT '',
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    await conn.commit()
    return conn


async def test_migration_creates_default_row_from_settings(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "legacy.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'anthropic/claude-sonnet-4-6')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_api_key', 'sk-test')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_base_url', '')"
    )
    await conn.commit()

    await _run_migrations(conn)

    row = await llm_models_repo.get_default(conn)
    assert row is not None
    assert row.model == "anthropic/claude-sonnet-4-6"
    assert row.api_key == "sk-test"
    assert row.provider_id == "anthropic"
    assert row.label == "Anthropic"
    # Legacy keys are gone.
    assert await settings_repo.get(conn, "llm_model") is None
    assert await settings_repo.get(conn, "llm_api_key") is None
    assert await settings_repo.get(conn, "llm_base_url") is None
    await conn.close()


async def test_migration_skips_when_no_legacy_settings(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "fresh.db"))
    await _run_migrations(conn)
    assert await llm_models_repo.get_default(conn) is None
    rows = await llm_models_repo.list_all(conn)
    assert rows == []
    await conn.close()


async def test_migration_adds_jobs_override_columns(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "jobs.db"))
    await _run_migrations(conn)
    cursor = await conn.execute("PRAGMA table_info(jobs)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "llm_model_id" in cols
    assert "additional_prompt" in cols
    await conn.close()


async def test_migration_is_idempotent(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "idem.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'openai/gpt-5.5')"
    )
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_api_key', 'k')"
    )
    await conn.commit()
    await _run_migrations(conn)
    await _run_migrations(conn)  # second run must be a no-op
    rows = await llm_models_repo.list_all(conn)
    assert len(rows) == 1
    await conn.close()


async def test_migration_provider_id_falls_back_to_custom(tmp_path):
    conn = await _make_legacy_db(str(tmp_path / "custom.db"))
    await conn.execute(
        "INSERT INTO settings (user_id, key, value) VALUES (1, 'llm_model', 'mystery/foo-1')"
    )
    await conn.commit()
    await _run_migrations(conn)
    row = await llm_models_repo.get_default(conn)
    assert row is not None
    assert row.provider_id == "custom"
    assert row.label == "Custom"
    await conn.close()
