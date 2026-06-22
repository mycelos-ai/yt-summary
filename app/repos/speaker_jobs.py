from datetime import datetime

import aiosqlite

from app.models import SpeakerJob, SpeakerJobState


def _row(r: aiosqlite.Row) -> SpeakerJob:
    return SpeakerJob(
        id=r["id"], speaker_id=r["speaker_id"],
        state=SpeakerJobState(r["state"]), step=r["step"],
        error_message=r["error_message"],
        created_at=datetime.fromisoformat(r["created_at"]),
        updated_at=datetime.fromisoformat(r["updated_at"]),
    )


async def enqueue(db: aiosqlite.Connection, speaker_id: int) -> int:
    """Insert a new pending job unless one is already pending or running.

    Returns the job id (existing or newly created). Done/failed jobs do NOT
    block a fresh enqueue so a new activate after completion works correctly.
    """
    # Return the id of the existing unfinished job without inserting a duplicate.
    cur = await db.execute(
        "SELECT id FROM speaker_jobs WHERE speaker_id=? AND state IN ('pending','running') "
        "ORDER BY id ASC LIMIT 1",
        (speaker_id,),
    )
    existing = await cur.fetchone()
    if existing is not None:
        return existing["id"]

    cur = await db.execute(
        "INSERT INTO speaker_jobs (speaker_id, state) VALUES (?, 'pending')",
        (speaker_id,),
    )
    await db.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


async def claim_next(db: aiosqlite.Connection) -> SpeakerJob | None:
    # Single statement (no manual BEGIN/COMMIT) — same safety note as jobs.claim_next.
    cur = await db.execute(
        """
        UPDATE speaker_jobs
        SET state='running', updated_at=datetime('now')
        WHERE id = (
            SELECT id FROM speaker_jobs
            WHERE state='pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
        )
        RETURNING *
        """
    )
    row = await cur.fetchone()
    await db.commit()
    return _row(row) if row else None


async def get(db: aiosqlite.Connection, job_id: int) -> SpeakerJob | None:
    cur = await db.execute("SELECT * FROM speaker_jobs WHERE id=?", (job_id,))
    row = await cur.fetchone()
    return _row(row) if row else None


async def latest_for_speaker(
    db: aiosqlite.Connection, speaker_id: int
) -> SpeakerJob | None:
    cur = await db.execute(
        "SELECT * FROM speaker_jobs WHERE speaker_id=? ORDER BY id DESC LIMIT 1",
        (speaker_id,),
    )
    row = await cur.fetchone()
    return _row(row) if row else None


async def set_step(db: aiosqlite.Connection, job_id: int, step: str) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET step=?, updated_at=datetime('now') WHERE id=?",
        (step, job_id),
    )
    await db.commit()


async def complete(db: aiosqlite.Connection, job_id: int) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='done', updated_at=datetime('now') WHERE id=?",
        (job_id,),
    )
    await db.commit()


async def fail(db: aiosqlite.Connection, job_id: int, message: str) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='failed', error_message=?, "
        "updated_at=datetime('now') WHERE id=?",
        (message, job_id),
    )
    await db.commit()


async def reset_orphaned_running(db: aiosqlite.Connection) -> None:
    await db.execute(
        "UPDATE speaker_jobs SET state='pending', updated_at=datetime('now') "
        "WHERE state='running'"
    )
    await db.commit()
