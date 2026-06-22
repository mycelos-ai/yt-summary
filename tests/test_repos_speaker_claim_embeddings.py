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


def test_search_recall_cliff_owned_claim_survives_30_nearer_foreign_claims(db):
    """Regression: owned claim must not be silently dropped when 30 foreign claims
    are globally closer to the query than the owned claim.

    Setup: the QUERIED speaker has exactly 1 claim — a loosely related statement
    about a different topic. A SECOND speaker has 30 claims that are near-duplicates
    of the query text and therefore globally outrank the owned claim in vec0 KNN.
    With the old over-fetch (max(limit*5, 25) = 25) those 30 foreign claims fill
    the entire candidate window, leaving zero owned claims. With the new over-fetch
    (max(limit*20, 50) = 50) the candidate window is wide enough to include the
    owned claim at global rank ~31.

    We use distinct-enough text to drive real embedding distances:
    - query / foreign claims: all variants of "neural networks and deep learning"
    - owned claim:            "trade tariffs affect import prices"
    The 30 near-duplicates should reliably dominate the global top-25 KNN while
    the owned claim lands in the 26-50 range.
    """
    async def go():
        queried = await speakers_repo.resolve_speaker(db, name="Recall Speaker")
        foreign = await speakers_repo.resolve_speaker(db, name="Foreign Speaker")
        await _seed_video(db, "vR")

        # Owned claim: semantically distant from the query
        owned_id = await _seed_claim(
            db, queried, "vR", "trade tariffs affect import prices"
        )
        owned_vec = await embed_text("trade tariffs affect import prices")
        await cve.upsert_claim_embedding(db, owned_id, owned_vec)

        # 30 foreign claims: near-duplicates of the query
        foreign_texts = [
            f"neural networks and deep learning architecture {i}"
            for i in range(30)
        ]
        for i, txt in enumerate(foreign_texts):
            cid = await _seed_claim(db, foreign, "vR", txt)
            vec = await embed_text(txt)
            await cve.upsert_claim_embedding(db, cid, vec)

        query_vec = await embed_text("neural networks and deep learning")

        # Verify that the foreign claims really do outrank the owned claim globally
        # (i.e. the recall cliff is real at the old 5x=25 window)
        import struct
        blob = struct.pack(f"{len(query_vec)}f", *query_vec)
        cur = await db.execute(
            "SELECT claim_id, distance FROM speaker_claim_embeddings "
            "WHERE claim_vec MATCH ? AND k = ? ORDER BY distance",
            (blob, 25),
        )
        top25_ids = [r[0] for r in await cur.fetchall()]
        assert owned_id not in top25_ids, (
            "Owned claim is in the global top-25 — the cliff scenario isn't triggered. "
            "Adjust text so foreign claims dominate more strongly."
        )

        # Now call search_claim_vectors and assert the owned claim is returned
        hits = await cve.search_claim_vectors(db, queried, query_vec, limit=5)
        ids = {cid for cid, _ in hits}
        assert owned_id in ids, (
            "search_claim_vectors returned empty/wrong results — "
            "the owned claim was dropped because the over-fetch window was too small."
        )
    _run(go())
