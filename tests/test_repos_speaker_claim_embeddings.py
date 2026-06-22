import asyncio

from app.config import Config
from app.db import connect, init_schema


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_speaker_claim_embeddings_table_exists(db):
    async def go():
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='speaker_claim_embeddings'"
        )
        assert await cur.fetchone() is not None
        # vec0 INSERT round-trip with a 384-d vector must not raise.
        import struct
        vec = [0.0] * 384
        blob = struct.pack("384f", *vec)
        await db.execute(
            "INSERT INTO speaker_claim_embeddings (claim_id, claim_vec) VALUES (?, ?)",
            (1, blob),
        )
        await db.commit()
    _run(go())


def test_init_schema_twice_keeps_vec_table(tmp_path):
    cfg = Config(data_dir=tmp_path); cfg.ensure_dirs()
    async def go():
        conn = await connect(cfg)
        await init_schema(conn)
        await init_schema(conn)   # second pass must be clean
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE name='speaker_claim_embeddings'"
        )
        assert await cur.fetchone() is not None
        await conn.close()
    asyncio.get_event_loop().run_until_complete(go())
