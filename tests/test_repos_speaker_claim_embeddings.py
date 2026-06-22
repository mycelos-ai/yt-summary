import asyncio

from app.config import Config
from app.db import connect, init_schema


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


# ---------------------------------------------------------------------------
# Helpers shared by Task-2 tests
# ---------------------------------------------------------------------------
from app.repos import speaker_claim_embeddings as cve  # noqa: E402
from app.repos import speakers as speakers_repo  # noqa: E402
from app.services.embeddings_local import embed_text  # noqa: E402


async def _seed_claim(db, speaker_id, source_id, claim_text):
    cur = await db.execute(
        "INSERT INTO speaker_claims (user_id, speaker_id, source_id, claim, "
        "extraction_method) VALUES (1, ?, ?, ?, 'llm')",
        (speaker_id, source_id, claim_text),
    )
    await db.commit()
    return cur.lastrowid


async def _seed_video(db, vid):
    await db.execute(
        "INSERT INTO videos (id, user_id, kind, url, title) VALUES (?,1,'youtube','',?)",
        (vid, vid),
    )
    await db.commit()


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


# ---------------------------------------------------------------------------
# Task-2 tests: KNN search, speaker scoping, delete_for_source
# ---------------------------------------------------------------------------

def test_search_ranks_by_semantic_distance(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Test Person")
        await _seed_video(db, "vA")
        c_ai = await _seed_claim(db, sid, "vA", "AI regulation will slow innovation")
        c_food = await _seed_claim(db, sid, "vA", "Sourdough bread needs a long ferment")
        for cid, txt in ((c_ai, "AI regulation will slow innovation"),
                         (c_food, "Sourdough bread needs a long ferment")):
            await cve.upsert_claim_embedding(db, cid, await embed_text(txt))
        q = await embed_text("what do you think about regulating artificial intelligence")
        hits = await cve.search_claim_vectors(db, sid, q, limit=2)
        assert hits, "expected KNN hits"
        assert hits[0][0] == c_ai, "the AI claim must rank ahead of the bread claim"
    _run(go())


def test_search_scopes_to_one_speaker(db):
    async def go():
        a = await speakers_repo.resolve_speaker(db, name="Alice A")
        b = await speakers_repo.resolve_speaker(db, name="Bob B")
        await _seed_video(db, "vS")
        ca = await _seed_claim(db, a, "vS", "interest rates should stay high")
        cb = await _seed_claim(db, b, "vS", "interest rates should stay high")
        v = await embed_text("monetary policy and interest rates")
        await cve.upsert_claim_embedding(db, ca, v)
        await cve.upsert_claim_embedding(db, cb, v)
        hits = await cve.search_claim_vectors(db, a, v, limit=10)
        ids = {cid for cid, _ in hits}
        assert ca in ids and cb not in ids
    _run(go())


def test_delete_for_source_drops_only_that_source(db):
    async def go():
        sid = await speakers_repo.resolve_speaker(db, name="Carol C")
        await _seed_video(db, "v1"); await _seed_video(db, "v2")
        c1 = await _seed_claim(db, sid, "v1", "claim one")
        c2 = await _seed_claim(db, sid, "v2", "claim two")
        v = await embed_text("seed")
        await cve.upsert_claim_embedding(db, c1, v)
        await cve.upsert_claim_embedding(db, c2, v)
        await cve.delete_for_source(db, sid, "v1")
        hits = await cve.search_claim_vectors(db, sid, v, limit=10)
        ids = {cid for cid, _ in hits}
        assert c1 not in ids and c2 in ids
    _run(go())
