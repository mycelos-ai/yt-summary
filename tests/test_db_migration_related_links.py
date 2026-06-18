import asyncio

import aiosqlite

from app.config import Config
from app.db import connect, init_schema


def test_videos_gains_related_links_json_column(tmp_path):
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await aiosqlite.connect(cfg.db_path)
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
                transcript_source TEXT,
                summary TEXT,
                summary_model TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await conn.commit()
        await conn.close()
        conn = await connect(cfg)
        await init_schema(conn)
        cur = await conn.execute("PRAGMA table_info(videos)")
        cols = {row[1] for row in await cur.fetchall()}
        await conn.close()
        return cols

    cols = asyncio.get_event_loop().run_until_complete(scenario())
    assert "related_links_json" in cols
