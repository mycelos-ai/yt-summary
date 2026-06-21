import aiosqlite

from app.models import Speaker
from app.repos.speakers import _row_to_speaker


async def link_speaker(
    db: aiosqlite.Connection,
    source_id: str,
    speaker_id: int,
    *,
    role: str | None = None,
    detection_source: str,
    sort_order: int = 0,
) -> int:
    """Link a speaker to a library item. Idempotent on
    UNIQUE(source_id, speaker_id): a second call returns the existing
    row id and updates role/sort_order/detection_source in place."""
    await db.execute(
        "INSERT INTO source_speakers "
        "(source_id, speaker_id, role, detection_source, sort_order) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, speaker_id) DO UPDATE SET "
        "role=excluded.role, detection_source=excluded.detection_source, "
        "sort_order=excluded.sort_order",
        (source_id, speaker_id, role, detection_source, sort_order),
    )
    await db.commit()
    cur = await db.execute(
        "SELECT id FROM source_speakers WHERE source_id=? AND speaker_id=?",
        (source_id, speaker_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return row["id"]


async def list_for_source(db: aiosqlite.Connection, source_id: str) -> list[Speaker]:
    cur = await db.execute(
        "SELECT s.* FROM speakers s "
        "JOIN source_speakers ss ON ss.speaker_id = s.id "
        "WHERE ss.source_id=? "
        "ORDER BY ss.sort_order, ss.id",
        (source_id,),
    )
    return [_row_to_speaker(r) for r in await cur.fetchall()]


async def unlink(db: aiosqlite.Connection, source_id: str, speaker_id: int) -> None:
    await db.execute(
        "DELETE FROM source_speakers WHERE source_id=? AND speaker_id=?",
        (source_id, speaker_id),
    )
    await db.commit()


async def list_sources_for_speaker(db: aiosqlite.Connection, speaker_id: int):
    """Confirmed library items (video rows) where the speaker appears.
    Returns raw rows ordered newest-first; the template reads row['id'],
    row['title'], row['kind'] directly."""
    cur = await db.execute(
        "SELECT v.* FROM videos v "
        "JOIN source_speakers ss ON ss.source_id = v.id "
        "WHERE ss.speaker_id=? AND v.archived_at IS NULL "
        "ORDER BY v.created_at DESC, v.id",
        (speaker_id,),
    )
    return await cur.fetchall()
