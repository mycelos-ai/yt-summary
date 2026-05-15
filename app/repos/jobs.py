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


async def counts(db: aiosqlite.Connection) -> dict[str, int]:
    """Aggregate job state counts for the diagnostics page.

    Returns ``{"pending": N, "running": N, "failed": N, "done_24h": N}``.
    ``done_24h`` is bounded to the last 24 hours against ``updated_at``
    so a long-lived install doesn't show a 5-digit number that scrolls
    off-screen.
    """
    cursor = await db.execute(
        """
        SELECT
          SUM(state='pending') AS pending,
          SUM(state='running') AS running,
          SUM(state='failed')  AS failed,
          SUM(state='done' AND updated_at >= datetime('now','-1 day')) AS done_24h
        FROM jobs
        """
    )
    row = await cursor.fetchone()
    # `SELECT SUM(...) FROM jobs` always returns exactly one row
    # (NULLs on an empty table). The `if row is None` branch is
    # unreachable in practice but guards against -O builds stripping
    # an `assert` and against future driver quirks.
    if row is None:
        return {"pending": 0, "running": 0, "failed": 0, "done_24h": 0}
    return {
        "pending": row["pending"] or 0,
        "running": row["running"] or 0,
        "failed": row["failed"] or 0,
        "done_24h": row["done_24h"] or 0,
    }


async def list_queue(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[Job, str]]:
    """Pending + running jobs in FIFO order, with the video title.

    ``LEFT JOIN`` so a deleted video still renders — the template
    falls back to the job's ``video_id``.
    """
    cursor = await db.execute(
        """
        SELECT j.*, v.title AS video_title
        FROM jobs j
        LEFT JOIN videos v ON v.id = j.video_id
        WHERE j.state IN ('pending','running')
        ORDER BY j.created_at ASC, j.id ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(_row_to_job(r), r["video_title"] or r["video_id"]) for r in rows]


async def list_recent_failed(
    db: aiosqlite.Connection, limit: int = 10,
) -> list[tuple[Job, str, bool]]:
    """Failed jobs, newest first, with video title and `video_done`.

    `video_done` is True when the video already has a summary — a
    common case where the failure is a stale leftover from an earlier
    attempt and a retry would just re-do work that already succeeded.
    The diagnostics page uses this to disable the Retry button.
    """
    cursor = await db.execute(
        """
        SELECT j.*,
               v.title AS video_title,
               (v.summary IS NOT NULL) AS video_done
        FROM jobs j
        LEFT JOIN videos v ON v.id = j.video_id
        WHERE j.state = 'failed'
        ORDER BY j.updated_at DESC, j.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        (
            _row_to_job(r),
            r["video_title"] or r["video_id"],
            bool(r["video_done"]),
        )
        for r in rows
    ]


async def retry(db: aiosqlite.Connection, job_id: int) -> int:
    """Reset a failed job back to ``pending`` so the worker picks it
    up. Returns the number of rows changed (0 => caller should 404).
    """
    cursor = await db.execute(
        """
        UPDATE jobs
        SET state='pending', error_message=NULL, updated_at=datetime('now')
        WHERE id=? AND state='failed'
        """,
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0


async def delete(db: aiosqlite.Connection, job_id: int) -> int:
    """Delete a failed job row. Returns the number of rows deleted
    (0 => caller should 404). The video row is untouched.
    """
    cursor = await db.execute(
        "DELETE FROM jobs WHERE id=? AND state='failed'",
        (job_id,),
    )
    await db.commit()
    return cursor.rowcount or 0
