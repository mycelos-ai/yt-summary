"""Related-item discovery (Part C.1).

Reuses the 384-d summary embeddings already stored for search. Given an
item, find its closest neighbours in the same Profile, excluding the item
itself and other profiles' copies of the same source.
"""

from __future__ import annotations

import math

import aiosqlite

from app.models import Video
from app.repos import embeddings as embeddings_repo
from app.repos import videos as videos_repo


async def related_video_ids(
    db: aiosqlite.Connection,
    video: Video,
    *,
    user_id: int,
    limit: int = 5,
    max_cosine_distance: float = 0.75,
) -> list[str]:
    """Ids of items related to `video`, closest first.

    Empty when the item has no embedding. Excludes: the item itself,
    items belonging to other profiles, and other-profile copies of the
    same source (same youtube_id or url). Keeps only neighbours within
    `max_cosine_distance` (cosine distance, [0, 2]); caps at `limit`.

    Embeddings are unit-length (see embeddings_local), so the vec0
    table's L2 distance is a monotonic function of cosine distance:
    L2² = 2·cosine_distance. We convert the cosine ceiling to an L2
    ceiling and compare against the L2 `distance` vec0 returns."""
    own = await embeddings_repo.get_summary_embedding(db, video.id)
    if own is None:
        return []
    l2_threshold = math.sqrt(2 * max_cosine_distance)
    # Over-fetch: the KNN index is global (all profiles), and we filter
    # down to this profile afterwards, so ask for more than `limit`.
    hits = await embeddings_repo.search_by_summary_vector(
        db, own, limit=max(limit * 5, 25),
    )
    candidate_ids = [
        vid for vid, dist in hits
        if vid != video.id and dist <= l2_threshold
    ]
    if not candidate_ids:
        return []
    placeholders = ",".join("?" for _ in candidate_ids)
    active_cur = await db.execute(
        f"SELECT id FROM videos WHERE id IN ({placeholders})"
        " AND archived_at IS NULL",
        candidate_ids,
    )
    active_ids = {r[0] for r in await active_cur.fetchall()}
    candidate_ids = [vid for vid in candidate_ids if vid in active_ids]
    if not candidate_ids:
        return []
    rows = await videos_repo.get_many(db, candidate_ids)

    out: list[str] = []
    for vid in candidate_ids:  # preserve KNN (closest-first) order
        cand = rows.get(vid)
        if cand is None or cand.user_id != user_id:
            continue
        # Drop a near-duplicate of the same underlying source (the same
        # video re-imported, or the same article URL).
        if video.youtube_id and cand.youtube_id == video.youtube_id:
            continue
        if cand.url == video.url:
            continue
        out.append(vid)
        if len(out) >= limit:
            break
    return out
