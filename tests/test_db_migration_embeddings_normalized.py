import asyncio

from app.config import Config
from app.db import connect, init_schema


def test_existing_summaries_queued_for_reembed_once(tmp_path):
    async def scenario():
        cfg = Config(data_dir=tmp_path)
        cfg.ensure_dirs()
        conn = await connect(cfg)
        await init_schema(conn)
        # Seed two videos: one with a summary (already embedded), one without.
        await conn.execute(
            "INSERT INTO videos (id, url, title, summary, summary_embedded_at,"
            " created_at, updated_at) VALUES "
            "('v1','u1','t1','some summary','2026-01-01T00:00:00',"
            " datetime('now'), datetime('now'))"
        )
        await conn.execute(
            "INSERT INTO videos (id, url, title, summary, summary_embedded_at,"
            " created_at, updated_at) VALUES "
            "('v2','u2','t2',NULL,NULL, datetime('now'), datetime('now'))"
        )
        # Pretend the normalization migration hasn't run yet.
        await conn.execute(
            "DELETE FROM settings WHERE user_id=1 AND key='embeddings_normalized'"
        )
        await conn.commit()

        # Re-run init: should null summary_embedded_at for v1, set marker.
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT summary_embedded_at FROM videos WHERE id='v1'"
        )
        v1_embedded = (await cur.fetchone())[0]
        cur = await conn.execute(
            "SELECT value FROM settings WHERE user_id=1 AND key='embeddings_normalized'"
        )
        marker = (await cur.fetchone())[0]

        # Set a fake embedded timestamp on v1, run init again — must be a no-op
        # (marker present, so v1 stays embedded).
        await conn.execute(
            "UPDATE videos SET summary_embedded_at='2026-02-02T00:00:00' WHERE id='v1'"
        )
        await conn.commit()
        await init_schema(conn)
        cur = await conn.execute(
            "SELECT summary_embedded_at FROM videos WHERE id='v1'"
        )
        v1_after_second = (await cur.fetchone())[0]
        await conn.close()
        return v1_embedded, marker, v1_after_second

    v1_embedded, marker, v1_after_second = (
        asyncio.get_event_loop().run_until_complete(scenario())
    )
    assert v1_embedded is None          # queued for re-embed
    assert marker == "1"                # marker set
    assert v1_after_second == "2026-02-02T00:00:00"  # second run = no-op
