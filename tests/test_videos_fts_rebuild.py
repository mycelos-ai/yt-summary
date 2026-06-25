"""
Regression test for the upgrade-path FTS corruption bug.

When a pre-feature DB (no channel_id column) has rowid gaps from deleted rows,
the INSERT/SELECT used to rebuild videos_new renumbers the rowids.  The
videos_fts external-content index still holds the OLD rowid mapping, causing:
  - wrong video returned for a search term
  - sqlite3.DatabaseError: fts5: missing row N from content table

The fix (INSERT INTO videos_fts(videos_fts) VALUES('rebuild')) must run inside
the rebuild block, after the RENAME, so the FTS index re-syncs with the new
rowid layout.
"""
import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRE_FEATURE_VIDEOS_DDL = """
CREATE TABLE videos (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'youtube'
        CHECK(kind IN ('youtube', 'web', 'email')),
    url TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
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
    related_links_json TEXT
);
"""

_PRE_FEATURE_FTS_DDL = """
CREATE VIRTUAL TABLE videos_fts USING fts5(
    id UNINDEXED,
    title,
    description,
    content='videos',
    content_rowid='rowid'
);
"""

# Mirror the trigger bodies from SCHEMA in app/db.py
_PRE_FEATURE_TRIGGERS_DDL = """
CREATE TRIGGER videos_ai AFTER INSERT ON videos BEGIN
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;

CREATE TRIGGER videos_ad AFTER DELETE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
END;

CREATE TRIGGER videos_au AFTER UPDATE ON videos BEGIN
    INSERT INTO videos_fts(videos_fts, rowid, id, title, description)
    VALUES ('delete', old.rowid, old.id, old.title, old.description);
    INSERT INTO videos_fts(rowid, id, title, description)
    VALUES (new.rowid, new.id, new.title, new.description);
END;
"""


async def _build_pre_feature_db(db_path):
    """
    Create a pre-feature DB that mimics a real user's DB:
    - OLD videos schema (no channel_id, no 'text' kind)
    - videos_fts virtual table + triggers
    - 5 rows inserted (rowids 1-5), then row 3 deleted → gap at rowid 3
    """
    conn = await aiosqlite.connect(db_path)
    await conn.executescript(_PRE_FEATURE_VIDEOS_DDL)
    await conn.executescript(_PRE_FEATURE_FTS_DDL)
    await conn.executescript(_PRE_FEATURE_TRIGGERS_DDL)

    rows = [
        ("v1", "keyword1 body"),
        ("v2", "keyword2 body"),
        ("v3", "keyword3 body"),  # will be deleted → creates rowid gap
        ("v4", "keyword4 body"),
        ("v5", "keyword5 body"),
    ]
    for vid_id, desc in rows:
        await conn.execute(
            "INSERT INTO videos (id, url, title, description) "
            "VALUES (?, '', ?, ?)",
            (vid_id, vid_id, desc),
        )
    await conn.commit()

    # Delete the middle row to produce a rowid gap (rowid 3 goes missing).
    await conn.execute("DELETE FROM videos WHERE id='v3'")
    await conn.commit()
    await conn.close()


# ---------------------------------------------------------------------------
# The actual test
# ---------------------------------------------------------------------------


def test_fts_rebuild_after_videos_table_rebuild(tmp_path):
    """
    After init_schema on an upgraded DB with rowid gaps, FTS search must:
    - return the CORRECT id for each surviving video's description keyword
    - not raise sqlite3.DatabaseError about missing rows
    """
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()

        # 1. Build the pre-feature DB (old schema + fts + triggers + gap).
        await _build_pre_feature_db(cfg.db_path)

        # 2. Run init_schema — triggers the rebuild + (with fix) fts rebuild.
        conn = await connect(cfg)
        await init_schema(conn)

        # 3. Check every surviving row.
        surviving = {"v1": "keyword1", "v2": "keyword2",
                     "v4": "keyword4", "v5": "keyword5"}
        errors = []
        for vid_id, keyword in surviving.items():
            try:
                cur = await conn.execute(
                    "SELECT f.id FROM videos_fts f "
                    "WHERE videos_fts MATCH ?",
                    (keyword,),
                )
                rows = await cur.fetchall()
                if not rows:
                    errors.append(
                        f"MATCH '{keyword}' returned no rows (expected '{vid_id}')"
                    )
                elif rows[0][0] != vid_id:
                    errors.append(
                        f"MATCH '{keyword}': got '{rows[0][0]}', expected '{vid_id}'"
                    )
            except Exception as exc:
                errors.append(f"MATCH '{keyword}' raised: {exc}")

        await conn.close()
        return errors

    errors = asyncio.get_event_loop().run_until_complete(scenario())
    assert errors == [], "FTS search corruption detected:\n" + "\n".join(errors)
