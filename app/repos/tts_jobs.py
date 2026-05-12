from datetime import datetime

import aiosqlite

from app.models import TtsJob


def _row_to_tts_job(row: aiosqlite.Row) -> TtsJob:
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    return TtsJob(
        id=row["id"],
        video_id=row["video_id"],
        source=row["source"],
        target_language=row["target_language"],
        voice=row["voice"],
        quality=row["quality"],
        status=row["status"],
        step=row["step"],
        translated_text=row["translated_text"],
        audio_path=row["audio_path"],
        duration_seconds=row["duration_seconds"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(started_at) if started_at else None,
        finished_at=datetime.fromisoformat(finished_at) if finished_at else None,
    )


async def enqueue(
    db: aiosqlite.Connection,
    video_id: str,
    source: str,
    target_language: str,
    voice: str,
    quality: str,
) -> TtsJob:
    """Insert a new tts_job, or return the existing row if (video_id, source,
    target_language, voice, quality) already exists. The `DO UPDATE SET id=id`
    self-assign is a no-op that lets RETURNING fire on conflict (SQLite's
    DO NOTHING branch wouldn't return any row)."""
    cursor = await db.execute(
        """
        INSERT INTO tts_jobs (video_id, source, target_language, voice, quality)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(video_id, source, target_language, voice, quality)
        DO UPDATE SET id=id
        RETURNING *
        """,
        (video_id, source, target_language, voice, quality),
    )
    row = await cursor.fetchone()
    await db.commit()
    assert row is not None
    return _row_to_tts_job(row)


async def claim_next(db: aiosqlite.Connection) -> TtsJob | None:
    """Atomically transition the oldest queued job to 'translating' (the
    first active phase in the queued → translating → rendering → done
    state machine) and return it. The worker advances to 'rendering' via
    set_status once translation finishes, or immediately when no
    translation is needed (source_language == target_language)."""
    cursor = await db.execute(
        """
        UPDATE tts_jobs
        SET status='translating', started_at=datetime('now')
        WHERE id = (
            SELECT id FROM tts_jobs
            WHERE status='queued'
            ORDER BY id
            LIMIT 1
        )
        RETURNING *
        """
    )
    row = await cursor.fetchone()
    await db.commit()
    return _row_to_tts_job(row) if row else None


async def get(db: aiosqlite.Connection, job_id: int) -> TtsJob | None:
    cursor = await db.execute("SELECT * FROM tts_jobs WHERE id=?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_tts_job(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE tts_jobs SET step=? WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def set_status(db: aiosqlite.Connection, job_id: int, status: str) -> None:
    """Move a running job between transient states (translating/rendering)."""
    await db.execute(
        "UPDATE tts_jobs SET status=? WHERE id=?",
        (status, job_id),
    )
    await db.commit()


async def complete(
    db: aiosqlite.Connection,
    job_id: int,
    *,
    audio_path: str,
    duration_seconds: float,
    translated_text: str | None,
) -> None:
    await db.execute(
        """
        UPDATE tts_jobs
        SET status='done',
            audio_path=?,
            duration_seconds=?,
            translated_text=?,
            finished_at=datetime('now')
        WHERE id=?
        """,
        (audio_path, duration_seconds, translated_text, job_id),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        """
        UPDATE tts_jobs
        SET status='failed',
            error=?,
            finished_at=datetime('now')
        WHERE id=?
        """,
        (message, job_id),
    )
    await db.commit()


async def list_for_video(db: aiosqlite.Connection, video_id: str) -> list[TtsJob]:
    """Return all tts_jobs for a video. Done rows come first (boolean DESC
    treats true=1 / false=0), newest first within each group."""
    cursor = await db.execute(
        """
        SELECT * FROM tts_jobs
        WHERE video_id=?
        ORDER BY (status='done') DESC, id DESC
        """,
        (video_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_tts_job(r) for r in rows]
