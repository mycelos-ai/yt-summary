from datetime import datetime

import aiosqlite

from app.models import TranscriptSource, Video, VideoKind


def _row_to_video(row: aiosqlite.Row) -> Video:
    src = row["transcript_source"]
    # `kind` was added in V3; older rows / older fixtures may not have it.
    try:
        kind_raw = row["kind"]
    except (IndexError, KeyError):
        kind_raw = None
    # transcript_segments is V4 — same fallback pattern.
    try:
        segments_raw = row["transcript_segments"]
    except (IndexError, KeyError):
        segments_raw = None
    return Video(
        id=row["id"],
        url=row["url"],
        title=row["title"],
        description=row["description"],
        thumbnail_path=row["thumbnail_path"],
        duration_seconds=row["duration_seconds"],
        transcript=row["transcript"],
        transcript_source=TranscriptSource(src) if src else None,
        summary=row["summary"],
        summary_model=row["summary_model"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        kind=VideoKind(kind_raw) if kind_raw else VideoKind.YOUTUBE,
        transcript_segments=segments_raw,
    )


async def upsert_metadata(
    db: aiosqlite.Connection,
    *,
    video_id: str,
    url: str,
    title: str,
    description: str,
    thumbnail_path: str | None,
    duration_seconds: int | None,
    user_id: int = 1,
    kind: VideoKind = VideoKind.YOUTUBE,
) -> None:
    await db.execute(
        """
        INSERT INTO videos (
            id, user_id, kind, url, title, description,
            thumbnail_path, duration_seconds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, videos.thumbnail_path),
            duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
            updated_at=datetime('now')
        """,
        (
            video_id, user_id, kind.value, url, title, description,
            thumbnail_path, duration_seconds,
        ),
    )
    await db.commit()


async def set_transcript(
    db: aiosqlite.Connection,
    video_id: str,
    transcript: str,
    source: TranscriptSource,
    segments_json: str | None = None,
) -> None:
    """Save the transcript text plus an optional structured-segments
    JSON for timestamped rendering.

    `segments_json` is a JSON-serialised list of {"start": float,
    "text": str} entries. None for web articles or transcribers that
    don't expose timing.
    """
    await db.execute(
        """
        UPDATE videos SET transcript=?, transcript_segments=?,
        transcript_source=?, updated_at=datetime('now') WHERE id=?
        """,
        (transcript, segments_json, source.value, video_id),
    )
    await db.commit()


async def set_summary(
    db: aiosqlite.Connection,
    video_id: str,
    summary: str,
    model: str,
) -> None:
    await db.execute(
        "UPDATE videos SET summary=?, summary_model=?, updated_at=datetime('now') WHERE id=?",
        (summary, model, video_id),
    )
    await db.commit()


async def get(db: aiosqlite.Connection, video_id: str) -> Video | None:
    cursor = await db.execute("SELECT * FROM videos WHERE id=?", (video_id,))
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


_TAG_FILTER_SQL = (
    " AND EXISTS ("
    "SELECT 1 FROM video_tags vt JOIN tags t ON t.id = vt.tag_id "
    "WHERE vt.video_id = videos.id AND t.name = ? COLLATE NOCASE"
    ")"
)


async def list_recent(
    db: aiosqlite.Connection,
    limit: int = 50,
    *,
    tag: str | None = None,
) -> list[Video]:
    if tag:
        cursor = await db.execute(
            "SELECT * FROM videos WHERE 1=1"
            + _TAG_FILTER_SQL
            + " ORDER BY created_at DESC, id DESC LIMIT ?",
            (tag, limit),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM videos ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]


def _quote_fts_query(query: str) -> str:
    """Wrap user input as an FTS5 phrase query so reserved syntax doesn't crash."""
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


async def search_fts(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 50,
    *,
    tag: str | None = None,
) -> list[str]:
    """Return video ids ranked by FTS5 relevance to `query`."""
    if tag:
        cursor = await db.execute(
            """
            SELECT v.id FROM videos v
            JOIN videos_fts f ON v.rowid = f.rowid
            WHERE videos_fts MATCH ?
              AND EXISTS (
                SELECT 1 FROM video_tags vt
                JOIN tags t ON t.id = vt.tag_id
                WHERE vt.video_id = v.id AND t.name = ? COLLATE NOCASE
              )
            ORDER BY rank
            LIMIT ?
            """,
            (_quote_fts_query(query), tag, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT v.id FROM videos v
            JOIN videos_fts f ON v.rowid = f.rowid
            WHERE videos_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (_quote_fts_query(query), limit),
        )
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def get_many(
    db: aiosqlite.Connection, video_ids: list[str]
) -> dict[str, Video]:
    """Bulk-load videos by id, returning a dict for ranking-based lookup."""
    if not video_ids:
        return {}
    placeholders = ",".join("?" * len(video_ids))
    cursor = await db.execute(
        f"SELECT * FROM videos WHERE id IN ({placeholders})",
        tuple(video_ids),
    )
    rows = await cursor.fetchall()
    return {row["id"]: _row_to_video(row) for row in rows}


def reciprocal_rank_fuse(
    *ranked_id_lists: list[str], k: int = 60
) -> list[str]:
    """Reciprocal Rank Fusion over multiple ranked id lists.

    For each list, each id contributes 1/(k + rank). Sum across lists,
    sort descending. Ids absent from a list don't contribute from it
    (rank → infinity, term → 0). Returns the merged id list, best first.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_id_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.keys(), key=lambda i: -scores[i])


async def search(
    db: aiosqlite.Connection,
    query: str,
    limit: int = 50,
    *,
    tag: str | None = None,
    vector_ids: list[str] | None = None,
) -> list[Video]:
    """Hybrid search: FTS5 + optional pre-computed vector ranking.

    The route layer is responsible for embedding the query and passing
    `vector_ids` (id list ordered most-similar-first). When `vector_ids`
    is None or empty, the result is the FTS-only ranking.
    """
    fts_ids = await search_fts(db, query, limit=limit, tag=tag)

    if vector_ids:
        if tag:
            allowed = set(fts_ids)  # FTS already filtered by tag
            # vector path didn't see the tag; intersect by id existence.
            # Cheaper: query video_tags directly to filter.
            tag_cursor = await db.execute(
                """
                SELECT v.id FROM videos v
                WHERE v.id IN ({}) AND EXISTS (
                    SELECT 1 FROM video_tags vt
                    JOIN tags t ON t.id = vt.tag_id
                    WHERE vt.video_id = v.id AND t.name = ? COLLATE NOCASE
                )
                """.format(",".join("?" * len(vector_ids))),
                (*vector_ids, tag),
            )
            tag_ok = {r[0] for r in await tag_cursor.fetchall()}
            vec_filtered = [vid for vid in vector_ids if vid in tag_ok]
            # Still keep FTS hits even if not in vector results.
            fused = reciprocal_rank_fuse(fts_ids, vec_filtered)
            allowed = allowed | tag_ok
            fused = [i for i in fused if i in allowed]
        else:
            fused = reciprocal_rank_fuse(fts_ids, vector_ids)
    else:
        fused = fts_ids

    fused = fused[:limit]
    by_id = await get_many(db, fused)
    # Preserve the fused order (get_many returns unordered dict).
    return [by_id[vid] for vid in fused if vid in by_id]
