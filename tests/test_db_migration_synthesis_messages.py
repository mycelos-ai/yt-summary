from app.db import connect, init_schema


async def _columns(conn, table):
    cur = await conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def test_creates_synthesis_messages_table(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        cols = await _columns(conn, "synthesis_messages")
        assert {
            "id", "synthesis_id", "role", "content",
            "status", "error", "created_at",
        } <= cols
    finally:
        await conn.close()


async def test_init_idempotent_for_synthesis_messages(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='synthesis_messages'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


async def test_old_syntheses_rows_cleared_once(config):
    conn = await connect(config)
    try:
        await init_schema(conn)
        # The one-time clear already ran (marker set). A row inserted now
        # must SURVIVE a second init_schema — the clear must not run again.
        await conn.execute(
            "INSERT INTO syntheses (user_id, query, source_ids_json, status) "
            "VALUES (1, 'kept', '[]', 'ready')"
        )
        await conn.commit()
        await init_schema(conn)
        cur = await conn.execute("SELECT COUNT(*) FROM syntheses")
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()
