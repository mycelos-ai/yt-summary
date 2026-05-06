from datetime import datetime

import aiosqlite

from app.models import TranscriptSource, Video


def _row_to_video(row: aiosqlite.Row) -> Video:
    src = row["transcript_source"]
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
) -> None:
    await db.execute(
        """
        INSERT INTO videos (id, user_id, url, title, description, thumbnail_path, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            url=excluded.url,
            title=excluded.title,
            description=excluded.description,
            thumbnail_path=COALESCE(excluded.thumbnail_path, videos.thumbnail_path),
            duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
            updated_at=datetime('now')
        """,
        (video_id, user_id, url, title, description, thumbnail_path, duration_seconds),
    )
    await db.commit()


async def set_transcript(
    db: aiosqlite.Connection,
    video_id: str,
    transcript: str,
    source: TranscriptSource,
) -> None:
    await db.execute(
        """
        UPDATE videos SET transcript=?, transcript_source=?,
        updated_at=datetime('now') WHERE id=?
        """,
        (transcript, source.value, video_id),
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


async def list_recent(db: aiosqlite.Connection, limit: int = 50) -> list[Video]:
    cursor = await db.execute(
        "SELECT * FROM videos ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]


def _quote_fts_query(query: str) -> str:
    """Wrap user input as an FTS5 phrase query so reserved syntax doesn't crash."""
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


async def search(db: aiosqlite.Connection, query: str, limit: int = 50) -> list[Video]:
    cursor = await db.execute(
        """
        SELECT v.* FROM videos v
        JOIN videos_fts f ON v.rowid = f.rowid
        WHERE videos_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (_quote_fts_query(query), limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_video(r) for r in rows]
