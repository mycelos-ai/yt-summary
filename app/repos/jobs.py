from datetime import datetime

import aiosqlite

from app.models import Job, JobState


def _row_to_job(row: aiosqlite.Row) -> Job:
    return Job(
        id=row["id"],
        video_id=row["video_id"],
        state=JobState(row["state"]),
        step=row["step"],
        error_message=row["error_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


async def enqueue(db: aiosqlite.Connection, video_id: str) -> int:
    cursor = await db.execute(
        "INSERT INTO jobs (video_id, state) VALUES (?, 'pending')",
        (video_id,),
    )
    await db.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


async def claim_next(db: aiosqlite.Connection) -> Job | None:
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        """
        SELECT * FROM jobs
        WHERE state = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """
    )
    row = await cursor.fetchone()
    if row is None:
        await db.commit()
        return None
    job_id = row["id"]
    await db.execute(
        "UPDATE jobs SET state='running', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()
    return await get(db, job_id)


async def get(db: aiosqlite.Connection, job_id: int) -> Job | None:
    cursor = await db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def latest_for_video(db: aiosqlite.Connection, video_id: str) -> Job | None:
    cursor = await db.execute(
        "SELECT * FROM jobs WHERE video_id=? ORDER BY id DESC LIMIT 1",
        (video_id,),
    )
    row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE jobs SET step=?, updated_at=datetime('now') WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def complete(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE jobs SET state='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        "UPDATE jobs SET state='failed', error_message=?, updated_at=datetime('now') WHERE id=?",
        (message, job_id),
    )
    await db.commit()


async def reset_orphaned_running(db: aiosqlite.Connection) -> None:
    """Called at startup. Jobs left running across a restart go back to pending."""
    await db.execute(
        "UPDATE jobs SET state='pending', updated_at=datetime('now') WHERE state='running'"
    )
    await db.commit()
