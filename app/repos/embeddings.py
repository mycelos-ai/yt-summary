"""Vector storage for video summaries using sqlite-vec's vec0 virtual
table.

The `video_embeddings` table is created by db.SCHEMA. Vectors are
stored as packed float32 BLOBs; sqlite-vec accepts both blobs and
JSON strings. We use BLOBs (`struct.pack`) to avoid the JSON parsing
overhead.

If the configured embedding model produces vectors of a different
dimension than the table was created with, the vec0 INSERT raises.
The pipeline catches that and logs a warning — recovery is to drop
and recreate `video_embeddings` (which init_schema does on fresh
installs).
"""

import logging
import struct

import aiosqlite

log = logging.getLogger(__name__)


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4  # 4 bytes per float32
    return list(struct.unpack(f"{n}f", blob))


async def get_summary_embedding(
    db: aiosqlite.Connection, video_id: str
) -> list[float] | None:
    """Return a video's own stored summary embedding, or None if it has
    none. Used to find related items (KNN with the item's own vector)."""
    cursor = await db.execute(
        "SELECT summary_vec FROM video_embeddings WHERE video_id = ?",
        (video_id,),
    )
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return _unpack_vector(row[0])


async def upsert_summary_embedding(
    db: aiosqlite.Connection, video_id: str, vector: list[float]
) -> None:
    """Insert or replace the summary embedding for a video.

    sqlite-vec's vec0 doesn't support ON CONFLICT directly, so we
    delete-then-insert. Both happen in a single autocommit transaction
    via the connection's default mode.
    """
    blob = _pack_vector(vector)
    await db.execute(
        "DELETE FROM video_embeddings WHERE video_id = ?", (video_id,)
    )
    await db.execute(
        "INSERT INTO video_embeddings (video_id, summary_vec) VALUES (?, ?)",
        (video_id, blob),
    )
    await db.execute(
        "UPDATE videos SET summary_embedded_at = datetime('now') WHERE id = ?",
        (video_id,),
    )
    await db.commit()


async def delete_summary_embedding(
    db: aiosqlite.Connection, video_id: str
) -> None:
    await db.execute(
        "DELETE FROM video_embeddings WHERE video_id = ?", (video_id,)
    )
    await db.execute(
        "UPDATE videos SET summary_embedded_at = NULL WHERE id = ?",
        (video_id,),
    )
    await db.commit()


async def search_by_summary_vector(
    db: aiosqlite.Connection, vector: list[float], limit: int = 50
) -> list[tuple[str, float]]:
    """Return videos sorted by ascending cosine distance to `vector`.

    Each tuple is (video_id, distance). Distance is in [0, 2]; smaller
    is more similar. sqlite-vec's MATCH operator does the KNN search
    using its built-in vec_distance_cosine.
    """
    blob = _pack_vector(vector)
    cursor = await db.execute(
        """
        SELECT video_id, distance
        FROM video_embeddings
        WHERE summary_vec MATCH ?
          AND k = ?
        ORDER BY distance
        """,
        (blob, limit),
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


async def videos_pending_reembed(
    db: aiosqlite.Connection, limit: int,
) -> list[str]:
    """Video IDs that have a summary but no current embedding.

    Used by the scheduler to drain the re-embed queue after the
    768d → 384d migration. Order is by `id` (deterministic, and
    matches insertion order on a typical install).
    """
    cursor = await db.execute(
        """
        SELECT id FROM videos
        WHERE summary IS NOT NULL AND summary_embedded_at IS NULL
        ORDER BY id
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def count_pending_reembed(db: aiosqlite.Connection) -> int:
    """COUNT(*) of the same predicate as videos_pending_reembed.

    Cheap; the diagnostics page polls it on each render.
    """
    cursor = await db.execute(
        """
        SELECT COUNT(*) FROM videos
        WHERE summary IS NOT NULL AND summary_embedded_at IS NULL
        """
    )
    row = await cursor.fetchone()
    return row[0] if row else 0
