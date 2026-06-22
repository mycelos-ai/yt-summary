"""Vector storage for speaker claims using sqlite-vec's vec0 virtual table.

Analogous to app/repos/embeddings.py (summary vectors). Claim vectors
are packed float32 BLOBs keyed by speaker_claims.id. KNN search is
scoped to a single speaker by joining speaker_claims and filtering
speaker_id, so one speaker's question only ranks their own claims.
"""
import struct

import aiosqlite


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


async def upsert_claim_embedding(
    db: aiosqlite.Connection, claim_id: int, vector: list[float]
) -> None:
    # vec0 has no ON CONFLICT — delete-then-insert (same as embeddings.py).
    blob = _pack_vector(vector)
    await db.execute(
        "DELETE FROM speaker_claim_embeddings WHERE claim_id = ?", (claim_id,)
    )
    await db.execute(
        "INSERT INTO speaker_claim_embeddings (claim_id, claim_vec) VALUES (?, ?)",
        (claim_id, blob),
    )
    await db.commit()


async def delete_claim_embedding(db: aiosqlite.Connection, claim_id: int) -> None:
    await db.execute(
        "DELETE FROM speaker_claim_embeddings WHERE claim_id = ?", (claim_id,)
    )
    await db.commit()


async def delete_for_source(
    db: aiosqlite.Connection, speaker_id: int, source_id: str
) -> None:
    """Drop a (speaker, source) pair's claim vectors before reprocess.

    Mirrors the replace-on-reprocess contract of speaker_claims: the
    claim rows themselves are deleted by the extraction service; this
    keeps the vec table from accumulating orphans.
    """
    await db.execute(
        """
        DELETE FROM speaker_claim_embeddings
        WHERE claim_id IN (
            SELECT id FROM speaker_claims
            WHERE speaker_id = ? AND source_id = ?
        )
        """,
        (speaker_id, source_id),
    )
    await db.commit()


async def search_claim_vectors(
    db: aiosqlite.Connection,
    speaker_id: int,
    vector: list[float],
    *,
    limit: int = 12,
) -> list[tuple[int, float]]:
    """Return (claim_id, distance) for THIS speaker's claims, closest first.

    Uses the two-step over-fetch+filter shape proven in related.py:
    step 1 performs a pure vec0 KNN (no extra predicates), step 2 filters
    to the requested speaker via a separate query. This avoids sqlite-vec
    constraint errors that can occur when combining KNN MATCH with JOIN
    predicates in some sqlite-vec versions.
    """
    blob = _pack_vector(vector)
    # Step 1: pure vec0 KNN (no extra predicate) — over-fetch.
    # Use 20x (min 50) so a single speaker's nearest claims survive the GLOBAL KNN
    # cut even when many other speakers' claims are semantically close to the query.
    # "Claims per speaker in a shared corpus" is a much softer bound than the 5x
    # ratio used in related.py (videos per user), so we need a larger window here.
    cur = await db.execute(
        "SELECT claim_id, distance FROM speaker_claim_embeddings "
        "WHERE claim_vec MATCH ? AND k = ? ORDER BY distance",
        (blob, max(limit * 20, 50)),
    )
    candidates = [(r[0], r[1]) for r in await cur.fetchall()]
    if not candidates:
        return []
    # Step 2: keep only THIS speaker's claims (separate query), preserve KNN order
    ids = [cid for cid, _ in candidates]
    marks = ",".join("?" for _ in ids)
    cur2 = await db.execute(
        f"SELECT id FROM speaker_claims WHERE speaker_id = ? AND id IN ({marks})",
        (speaker_id, *ids),
    )
    owned = {r[0] for r in await cur2.fetchall()}
    return [(cid, dist) for cid, dist in candidates if cid in owned][:limit]
