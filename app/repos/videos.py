from datetime import UTC, datetime, timedelta
from pathlib import Path

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
    try:
        user_id = row["user_id"]
    except (IndexError, KeyError):
        user_id = 1
    try:
        youtube_id = row["youtube_id"]
    except (IndexError, KeyError):
        youtube_id = None
    # V6: language metadata columns.
    try:
        source_language = row["source_language"]
    except (IndexError, KeyError):
        source_language = None
    try:
        summary_language = row["summary_language"]
    except (IndexError, KeyError):
        summary_language = None
    try:
        transcript_language = row["transcript_language"]
    except (IndexError, KeyError):
        transcript_language = None
    try:
        archived_at = row["archived_at"]
    except (IndexError, KeyError):
        archived_at = None
    try:
        image_query = row["image_query"]
    except (IndexError, KeyError):
        image_query = None
    try:
        related_links_json = row["related_links_json"]
    except (IndexError, KeyError):
        related_links_json = None
    try:
        highlights_json = row["highlights_json"]
    except (IndexError, KeyError):
        highlights_json = None
    try:
        channel_id = row["channel_id"]
    except (IndexError, KeyError):
        channel_id = None
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
        user_id=user_id,
        transcript_segments=segments_raw,
        youtube_id=youtube_id,
        source_language=source_language,
        summary_language=summary_language,
        transcript_language=transcript_language,
        highlights_json=highlights_json,
        archived_at=archived_at,
        image_query=image_query,
        related_links_json=related_links_json,
        channel_id=channel_id,
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
    youtube_id: str | None = None,
    channel_id: str | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO videos (
            id, user_id, kind, url, title, description,
            thumbnail_path, duration_seconds, youtube_id, channel_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, videos.thumbnail_path),
            duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
            youtube_id=COALESCE(excluded.youtube_id, videos.youtube_id),
            channel_id=COALESCE(excluded.channel_id, videos.channel_id),
            updated_at=datetime('now')
        """,
        (
            video_id, user_id, kind.value, url, title, description,
            thumbnail_path, duration_seconds, youtube_id, channel_id,
        ),
    )
    await db.commit()


async def set_transcript(
    db: aiosqlite.Connection,
    video_id: str,
    transcript: str,
    source: TranscriptSource,
    segments_json: str | None = None,
    *,
    language: str | None = None,
) -> None:
    """Save the transcript text plus an optional structured-segments
    JSON for timestamped rendering.

    `segments_json` is a JSON-serialised list of {"start": float,
    "text": str} entries. None for web articles or transcribers that
    don't expose timing.

    `language`, when provided, stamps both `transcript_language` and
    `source_language` with the same value (they match in 100% of
    known cases — keep them separate at the schema level so later
    features can diverge them without another migration). When
    `language is None` the language columns are left untouched.
    """
    if language is None:
        await db.execute(
            """
            UPDATE videos SET transcript=?, transcript_segments=?,
            transcript_source=?, updated_at=datetime('now') WHERE id=?
            """,
            (transcript, segments_json, source.value, video_id),
        )
    else:
        await db.execute(
            """
            UPDATE videos SET transcript=?, transcript_segments=?,
            transcript_source=?, transcript_language=?,
            source_language=?, updated_at=datetime('now') WHERE id=?
            """,
            (
                transcript, segments_json, source.value,
                language, language, video_id,
            ),
        )
    await db.commit()


async def clear_transcript(
    db: aiosqlite.Connection,
    video_id: str,
) -> None:
    """Reset the transcript columns so the next pipeline pass fetches
    a fresh transcript. Used by the "Re-transcribe" button on the
    detail page when the stored data is stale (e.g. predates the
    rolling-window dedup fix).
    """
    await db.execute(
        """
        UPDATE videos SET transcript=NULL, transcript_segments=NULL,
        transcript_source=NULL, updated_at=datetime('now') WHERE id=?
        """,
        (video_id,),
    )
    await db.commit()


async def set_source_language(
    db: aiosqlite.Connection,
    video_id: str,
    language: str,
) -> None:
    """Stamp `source_language` for a video iff it's still NULL.

    Used by the pipeline's LLM-detect fallback so the detected code
    isn't lost when the user has an explicit `summary_language` set.
    `set_summary`'s COALESCE-on-summary-language trick handles the
    auto / matching case, but when the two diverge we need to write
    the source value through a dedicated path BEFORE `set_summary`
    runs (otherwise the COALESCE would backfill with the summary
    language, hiding the actual source language).

    No-op when `language` is empty.
    """
    if not language:
        return
    await db.execute(
        """
        UPDATE videos SET source_language=?, updated_at=datetime('now')
        WHERE id=? AND source_language IS NULL
        """,
        (language, video_id),
    )
    await db.commit()


async def set_summary(
    db: aiosqlite.Connection,
    video_id: str,
    summary: str,
    model: str,
    *,
    language: str | None = None,
) -> None:
    """Save the rendered summary and its source model.

    `language`, when provided, stamps `summary_language`. If
    `source_language` is currently NULL we also backfill it with
    `language` — this is the LLM-detected fallback path for videos
    that came in without either a Whisper or VTT language signal
    (e.g. web articles whose author the LLM identified after the
    summary was generated).
    """
    if language is None:
        await db.execute(
            "UPDATE videos SET summary=?, summary_model=?, "
            "updated_at=datetime('now') WHERE id=?",
            (summary, model, video_id),
        )
    else:
        await db.execute(
            """
            UPDATE videos SET summary=?, summary_model=?,
            summary_language=?,
            source_language=COALESCE(source_language, ?),
            updated_at=datetime('now') WHERE id=?
            """,
            (summary, model, language, language, video_id),
        )
    await db.commit()


async def get(db: aiosqlite.Connection, video_id: str) -> Video | None:
    cursor = await db.execute("SELECT * FROM videos WHERE id=?", (video_id,))
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


async def set_archived(
    db: aiosqlite.Connection, video_id: str, *, user_id: int, archived: bool,
) -> bool:
    """Archive or restore a video. Returns False when the video does
    not exist or belongs to another profile (caller answers 404)."""
    value = "datetime('now')" if archived else "NULL"
    cur = await db.execute(
        f"UPDATE videos SET archived_at={value}, updated_at=datetime('now') "
        "WHERE id=? AND user_id=?",
        (video_id, user_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def list_archived(
    db: aiosqlite.Connection, *, user_id: int, limit: int = 100, offset: int = 0,
) -> list[Video]:
    cur = await db.execute(
        "SELECT * FROM videos WHERE user_id=? AND archived_at IS NOT NULL "
        "ORDER BY datetime(archived_at) DESC, id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    return [_row_to_video(r) for r in await cur.fetchall()]


async def count_archived(db: aiosqlite.Connection, *, user_id: int) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM videos WHERE user_id=? AND archived_at IS NOT NULL",
        (user_id,),
    )
    row = await cur.fetchone()
    return row[0] if row else 0


async def delete(
    db: aiosqlite.Connection,
    video_id: str,
    *,
    data_dir: Path,
) -> None:
    """Delete a video and all per-video state, including TTS audio files.

    The FK `ON DELETE CASCADE` on tts_jobs.video_id already removes the
    DB rows. We pre-fetch the audio paths so they can be unlinked from
    disk after the cascade commits — a SQL trigger calling a UDF would
    work too, but pre-fetch + unlink keeps the cleanup logic in Python
    where it's testable and obvious.
    """
    cursor = await db.execute(
        "SELECT audio_path FROM tts_jobs "
        "WHERE video_id = ? AND audio_path IS NOT NULL",
        (video_id,),
    )
    paths = [row[0] for row in await cursor.fetchall()]
    await db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    await db.commit()
    for rel in paths:
        (data_dir / rel).unlink(missing_ok=True)
    video_dir = data_dir / "tts-audio" / video_id
    if video_dir.exists() and not any(video_dir.iterdir()):
        video_dir.rmdir()


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
    offset: int = 0,
    user_id: int = 1,
) -> list[Video]:
    if tag:
        cursor = await db.execute(
            "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL"
            + _TAG_FILTER_SQL
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, tag, limit, offset),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]


def _normalize_ts(value: str | None) -> str | None:
    """Coerce a caller-supplied timestamp to the stored string form.

    SQLite writes `datetime('now')` as '2026-08-13 17:37:05' — space
    separator, naive UTC. Callers send ISO-8601, often with a 'T' and a
    'Z'. These are compared as strings inside SQL, and ' ' < 'T', so an
    un-normalized 'T' bound silently skips every row on that second.
    Returns None for empty input, meaning "no lower bound".
    """
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1]
    return cleaned.replace("T", " ", 1)


def _parse_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Split "<updated_at>|<id>" into its parts.

    Returns None for anything unparseable, which the caller treats as
    "start from the beginning". A resync is cheap; a crashed sync loop
    is not.
    """
    if not cursor or "|" not in cursor:
        return None
    stamp, _, last_id = cursor.partition("|")
    stamp = _normalize_ts(stamp)
    if not stamp or not last_id:
        return None
    return stamp, last_id


def make_cursor(video: Video) -> str:
    """The opaque resume token for `list_updated_since`.

    Deliberately built from the raw stored form rather than
    `datetime.isoformat()` — see `_normalize_ts`.
    """
    stamp = video.updated_at.strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp}|{video.id}"


async def list_updated_since(
    db: aiosqlite.Connection,
    *,
    user_id: int = 1,
    since: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> list[Video]:
    """Active items changed at or after `since`, oldest change first.

    For incremental sync: ordered by (updated_at ASC, id ASC) so a
    cursor can resume exactly, including across items that share an
    `updated_at`. `cursor` is the last seen "<updated_at>|<id>" pair
    and wins over `since` when both are given.

    Filters `archived_at IS NULL`, matching `list_recent`: archiving an
    item removes it from the sync feed. An item archived *after* it
    synced is not signalled to the consumer — deletion propagation is
    out of scope.

    Unlike `list_recent`, this orders by `updated_at` rather than
    `created_at`: summaries are updated in place (resummarize,
    highlights, language backfill) without a new row, and a
    created_at-ordered feed would never re-emit them.
    """
    where = ["user_id = ?", "archived_at IS NULL"]
    params: list = [user_id]

    resume = _parse_cursor(cursor)
    if resume is not None:
        stamp, last_id = resume
        where.append("(updated_at > ? OR (updated_at = ? AND id > ?))")
        params += [stamp, stamp, last_id]
    else:
        bound = _normalize_ts(since)
        if bound is not None:
            where.append("updated_at >= ?")
            params.append(bound)

    params.append(limit)
    cur = await db.execute(
        "SELECT * FROM videos WHERE " + " AND ".join(where)
        + " ORDER BY updated_at ASC, id ASC LIMIT ?",
        tuple(params),
    )
    rows = await cur.fetchall()
    return [_row_to_video(r) for r in rows]


async def get_most_recent(
    db: aiosqlite.Connection, *, user_id: int = 1,
) -> Video | None:
    """The most recently added active video (greatest created_at).

    Used by the diagnostics page to show "last added". None when the
    library is empty / all archived."""
    cursor = await db.execute(
        "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


async def get_most_recently_summarized(
    db: aiosqlite.Connection, *, user_id: int = 1,
) -> Video | None:
    """The most recently touched active video that has a summary
    (greatest updated_at among summarized videos).

    Used by the diagnostics page to show "last processed". None when no
    summarized videos exist."""
    cursor = await db.execute(
        "SELECT * FROM videos WHERE user_id = ? AND archived_at IS NULL "
        "AND summary IS NOT NULL ORDER BY updated_at DESC, id DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


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
    user_id: int = 1,
) -> list[str]:
    """Return video ids ranked by FTS5 relevance to `query`."""
    if tag:
        cursor = await db.execute(
            """
            SELECT v.id FROM videos v
            JOIN videos_fts f ON v.rowid = f.rowid
            WHERE videos_fts MATCH ?
              AND v.user_id = ?
              AND v.archived_at IS NULL
              AND EXISTS (
                SELECT 1 FROM video_tags vt
                JOIN tags t ON t.id = vt.tag_id
                WHERE vt.video_id = v.id AND t.name = ? COLLATE NOCASE
              )
            ORDER BY rank
            LIMIT ?
            """,
            (_quote_fts_query(query), user_id, tag, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT v.id FROM videos v
            JOIN videos_fts f ON v.rowid = f.rowid
            WHERE videos_fts MATCH ? AND v.user_id = ?
              AND v.archived_at IS NULL
            ORDER BY rank
            LIMIT ?
            """,
            (_quote_fts_query(query), user_id, limit),
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
    user_id: int = 1,
) -> list[Video]:
    """Hybrid search: FTS5 + optional pre-computed vector ranking.

    The route layer is responsible for embedding the query and passing
    `vector_ids` (id list ordered most-similar-first). When `vector_ids`
    is None or empty, the result is the FTS-only ranking. Results are
    always scoped to `user_id`.
    """
    fts_ids = await search_fts(
        db, query, limit=limit, tag=tag, user_id=user_id
    )

    if vector_ids:
        # The vector index is global (not partitioned by user), so we
        # have to intersect against the current profile's video set.
        if vector_ids:
            placeholders = ",".join("?" * len(vector_ids))
            user_cursor = await db.execute(
                f"SELECT id FROM videos WHERE user_id = ? "
                f"AND archived_at IS NULL "
                f"AND id IN ({placeholders})",
                (user_id, *vector_ids),
            )
            allowed_user = {r[0] for r in await user_cursor.fetchall()}
            vector_ids = [vid for vid in vector_ids if vid in allowed_user]

        if tag:
            allowed = set(fts_ids)  # FTS already filtered by tag
            # vector path didn't see the tag; intersect by id existence.
            # Cheaper: query video_tags directly to filter.
            if vector_ids:
                placeholders = ",".join("?" * len(vector_ids))
                tag_cursor = await db.execute(
                    f"""
                    SELECT v.id FROM videos v
                    WHERE v.id IN ({placeholders}) AND EXISTS (
                        SELECT 1 FROM video_tags vt
                        JOIN tags t ON t.id = vt.tag_id
                        WHERE vt.video_id = v.id AND t.name = ? COLLATE NOCASE
                    )
                    """,
                    (*vector_ids, tag),
                )
                tag_ok = {r[0] for r in await tag_cursor.fetchall()}
            else:
                tag_ok = set()
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


async def find_other_with_transcript(
    db: aiosqlite.Connection,
    *,
    youtube_id: str,
    exclude_user_id: int,
) -> Video | None:
    """Return any other profile's row for the same YouTube video that
    already has a transcript stored. Used by the pipeline to skip
    Whisper when a household member transcribed the same video first.

    Returns the most-recently-updated match so we get the freshest
    self-healing transcript (e.g. one that already has segments).
    """
    cursor = await db.execute(
        """
        SELECT * FROM videos
        WHERE youtube_id = ?
          AND user_id != ?
          AND transcript IS NOT NULL
          AND transcript != ''
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (youtube_id, exclude_user_id),
    )
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


async def find_other_with_transcript_by_url(
    db: aiosqlite.Connection,
    *,
    url: str,
    exclude_user_id: int,
) -> Video | None:
    """Same as find_other_with_transcript but matches by URL (web
    articles share by URL since they have no youtube_id)."""
    cursor = await db.execute(
        """
        SELECT * FROM videos
        WHERE url = ?
          AND user_id != ?
          AND transcript IS NOT NULL
          AND transcript != ''
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (url, exclude_user_id),
    )
    row = await cursor.fetchone()
    return _row_to_video(row) if row else None


async def set_highlights(
    db: aiosqlite.Connection, video_id: str, highlights_json: str,
) -> None:
    """Set the highlights JSON blob.

    Pass `"[]"` for "LLM explicitly returned no noteworthy highlights".
    Pass a NULL only by not calling this function at all (pre-feature
    backlog stays NULL).
    """
    await db.execute(
        "UPDATE videos SET highlights_json=? WHERE id=?",
        (highlights_json, video_id),
    )
    await db.commit()


async def set_image_query(
    db: aiosqlite.Connection, video_id: str, image_query: str | None,
) -> None:
    await db.execute(
        "UPDATE videos SET image_query=? WHERE id=?",
        (image_query, video_id),
    )
    await db.commit()


async def set_thumbnail_path(
    db: aiosqlite.Connection, video_id: str, thumbnail_path: str,
) -> None:
    await db.execute(
        "UPDATE videos SET thumbnail_path=?, updated_at=datetime('now') "
        "WHERE id=?",
        (thumbnail_path, video_id),
    )
    await db.commit()


async def list_for_thumbnail_backfill(
    db: aiosqlite.Connection,
    *,
    user_id: int | None = None,
    only_missing: bool = True,
    limit: int | None = None,
    since_days: int | None = None,
) -> list[Video]:
    """Email/web items eligible for a stock-photo backfill.

    only_missing=True restricts to rows without a thumbnail; False
    returns all (for --force re-runs).
    since_days, when set, restricts to items created within the last N days."""
    clauses = ["kind IN ('email','web')"]
    params: list = []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if only_missing:
        clauses.append("(thumbnail_path IS NULL OR thumbnail_path = '')")
    if since_days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
        clauses.append("datetime(created_at) >= datetime(?)")
        params.append(cutoff)
    sql = "SELECT * FROM videos WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = await db.execute(sql, tuple(params))
    return [_row_to_video(r) for r in await cur.fetchall()]


async def get_highlights(
    db: aiosqlite.Connection, video_id: str,
) -> str | None:
    cur = await db.execute(
        "SELECT highlights_json FROM videos WHERE id=?", (video_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row[0]


async def set_related_links(
    db: aiosqlite.Connection, video_id: str, related_links_json: str,
) -> None:
    """Set the curated related-links JSON blob.

    Pass `"[]"` for "nothing relevant found". A NULL means "not yet
    computed" — leave it by simply not calling this function.
    """
    await db.execute(
        "UPDATE videos SET related_links_json=? WHERE id=?",
        (related_links_json, video_id),
    )
    await db.commit()


async def get_related_links(
    db: aiosqlite.Connection, video_id: str,
) -> str | None:
    cur = await db.execute(
        "SELECT related_links_json FROM videos WHERE id=?", (video_id,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return row[0]
