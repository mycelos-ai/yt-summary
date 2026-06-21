import asyncio

import aiosqlite
from pathlib import Path

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
        assert row[0] is None and row[1] == 1
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
