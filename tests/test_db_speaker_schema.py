import asyncio

import aiosqlite
from pathlib import Path

from app.db import connect, init_schema
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


from app.db import _run_migrations  # noqa


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
